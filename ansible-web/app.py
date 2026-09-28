#!/usr/bin/env python3
"""expolab ansible-web : declenche des runs Ansible contre le parc Bastion.

Ne contient AUCUN contenu Ansible lui-meme (pas de roles/, pas de
site.yml commits ici) - clone/pull un mirroir git-mirror en lecture
seule avant chaque action (voir sync_repo()), configurable depuis cette
meme UI (nom du mirroir). Le mecanisme tag -> role (un role = un tag,
empilement multi-tags via order.yaml/site.yml) vit entierement dans ce
depot externe (bastion-ansible) et n'est jamais modifie d'ici - editer
un role ou en scaffolder un nouveau reste une operation locale sur ce
depot (git push), jamais depuis cette UI : son propre clone vient d'un
mirroir qui n'accepte aucun push, un changement fait ici n'aurait nulle
part ou etre sauvegarde durablement.

Pas d'authentification, comme le reste des apps admin de ce lab - cette
UI peut executer des taches (become: true) contre tout le parc reference
dans Bastion, protegee uniquement par l'isolation reseau de son
environnement de deploiement.
"""
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

REPO_DIR = Path("/repo")
RUNS_DIR = Path("/runs")
RUNS_INDEX = RUNS_DIR / "index.yaml"
CONFIG_PATH = Path("/data/config.yaml")
SITE_YML = REPO_DIR / "site.yml"

GIT_TIMEOUT = 120
DEFAULT_MIRROR_NAME = "ansible"
MIRROR_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

RUN_STATE = {"running": False}
RUN_LOCK = threading.Lock()


def load_settings() -> dict:
    if not CONFIG_PATH.exists():
        return {"mirror_name": DEFAULT_MIRROR_NAME}
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {"mirror_name": data.get("mirror_name") or DEFAULT_MIRROR_NAME}


def save_settings(settings: dict) -> None:
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    tmp_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    os.replace(tmp_path, CONFIG_PATH)


def run_git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def sync_repo() -> tuple[bool, str]:
    """Clone (premiere fois) ou pull le mirroir configure - toujours
    appele avant de lister les tags ou de lancer un run, pour ne jamais
    executer un contenu perime."""
    mirror_name = load_settings()["mirror_name"]
    url = f"http://127.0.0.1:5054/git/{mirror_name}.git"

    if not (REPO_DIR / ".git").exists():
        REPO_DIR.mkdir(parents=True, exist_ok=True)
        if any(REPO_DIR.iterdir()):
            # Reste d'un clone precedent pointant sur un autre mirroir
            # (nom change depuis les reglages) - jamais un `git clone`
            # dans un dossier non vide.
            shutil.rmtree(REPO_DIR)
            REPO_DIR.mkdir(parents=True, exist_ok=True)
        result = run_git(["clone", url, str(REPO_DIR)])
        if result.returncode != 0:
            return False, (
                f"Impossible de cloner le mirroir '{mirror_name}' - existe-t-il "
                f"dans git-mirror ? ({result.stderr.strip() or 'erreur inconnue'})"
            )
        return True, ""

    result = run_git(["-C", str(REPO_DIR), "pull", "--ff-only"])
    if result.returncode != 0:
        return False, result.stderr.strip() or "Echec du git pull"
    return True, ""


def load_inventory_module():
    """Charge inventory/bastion_inventory.py DEPUIS LE CLONE ACTUEL, sans
    jamais passer par le cache d'import Python (sys.modules) - le
    contenu peut changer a chaque sync_repo(), un import classique
    garderait la premiere version chargee en memoire indefiniment."""
    spec = importlib.util.spec_from_file_location(
        "bastion_inventory_live", REPO_DIR / "inventory" / "bastion_inventory.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local_roles() -> set[str]:
    roles_dir = REPO_DIR / "roles"
    if not roles_dir.exists():
        return set()
    return {p.name for p in roles_dir.iterdir() if p.is_dir()}


def load_runs() -> list[dict]:
    if not RUNS_INDEX.exists():
        return []
    return yaml.safe_load(RUNS_INDEX.read_text()) or []


def save_runs(runs: list[dict]) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = RUNS_INDEX.with_suffix(".tmp")
    tmp_path.write_text(yaml.safe_dump(runs, sort_keys=False))
    os.replace(tmp_path, RUNS_INDEX)


def execute_run(run_id: str, limit: str | None) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNS_DIR / f"{run_id}.log"

    ok, err = sync_repo()
    if not ok:
        log_path.write_text(f"Sync du mirroir echouee, run annule :\n{err}\n")
        status = "failed"
    else:
        cmd = ["ansible-playbook", str(SITE_YML)]
        if limit:
            cmd += ["--limit", limit]
        # Args supplementaires fixes pour tout run declenche par l'UI (ex:
        # --extra-vars ansible_become_pass=... pour un environnement de
        # demo ou le mot de passe sudo est connu/partage) - jamais une
        # valeur par defaut de ce depot, uniquement ce qu'un deploiement
        # fournit via son propre environnement.
        extra_args = os.environ.get("ANSIBLE_WEB_EXTRA_ARGS", "")
        if extra_args:
            cmd += shlex.split(extra_args)
        try:
            with open(log_path, "w") as logf:
                result = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=REPO_DIR)
            status = "ok" if result.returncode == 0 else "failed"
        except Exception as exc:
            with open(log_path, "a") as logf:
                logf.write(f"\nErreur d'execution : {exc}\n")
            status = "failed"

    runs = load_runs()
    for r in runs:
        if r["id"] == run_id:
            r["status"] = status
            r["finished_at"] = datetime.now(timezone.utc).isoformat()
            break
    save_runs(runs)
    with RUN_LOCK:
        RUN_STATE["running"] = False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["PUT"])
def api_settings_put():
    body = request.get_json(force=True, silent=True) or {}
    mirror_name = (body.get("mirror_name") or "").strip().lower()
    if not MIRROR_NAME_RE.match(mirror_name):
        return jsonify({"error": "Nom de mirroir invalide (minuscules/chiffres/tirets, 3-32 caracteres, doit commencer par une lettre)"}), 400

    save_settings({"mirror_name": mirror_name})

    # Force un clone frais au prochain sync_repo() : le clone actuel (si
    # present) pointe sur l'ancien mirroir, jamais reutilisable tel quel.
    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR, ignore_errors=True)

    return jsonify({"ok": True})


@app.route("/api/tags", methods=["GET"])
def api_tags_get():
    ok, err = sync_repo()
    if not ok:
        return jsonify({"error": err}), 502

    if not (REPO_DIR / "inventory" / "bastion_inventory.py").exists():
        return jsonify({"error": "inventory/bastion_inventory.py introuvable dans le depot clone - est-ce bien le bon mirroir ?"}), 502

    inventory = load_inventory_module()
    try:
        machines = inventory.fetch_machines()
    except SystemExit:
        return jsonify({"error": "Impossible de joindre Bastion (verifier BASTION_URL/BASTION_API_TOKEN)."}), 502

    raw_tags = sorted({t for m in machines for t in (m.get("tags") or [])})
    roles = local_roles()
    tags = [{"name": t, "role": inventory.group_name(t), "has_role": inventory.group_name(t) in roles} for t in raw_tags]
    return jsonify({"tags": tags, "mirror_name": load_settings()["mirror_name"]})


@app.route("/api/runs", methods=["GET"])
def api_runs_list():
    return jsonify({"runs": sorted(load_runs(), key=lambda r: r["started_at"], reverse=True)})


@app.route("/api/runs", methods=["POST"])
def api_runs_create():
    with RUN_LOCK:
        if RUN_STATE["running"]:
            return jsonify({"error": "Un run est deja en cours"}), 409
        RUN_STATE["running"] = True

    body = request.get_json(force=True, silent=True) or {}
    limit = (body.get("limit") or "").strip() or None

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs = load_runs()
    runs.append({
        "id": run_id,
        "limit": limit,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    })
    save_runs(runs)

    threading.Thread(target=execute_run, args=(run_id, limit), daemon=True).start()
    return jsonify({"ok": True, "id": run_id}), 201


@app.route("/api/runs/<run_id>/log", methods=["GET"])
def api_runs_log(run_id: str):
    log_path = RUNS_DIR / f"{run_id}.log"
    if not log_path.exists():
        return jsonify({"error": "Log introuvable"}), 404
    return jsonify({"log": log_path.read_text()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5055)
