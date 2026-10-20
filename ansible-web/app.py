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
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

import deploy

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

# Deploiement apres un flash exploitant (voir deploy.py) : dossier partage avec sd-flasher.
DEPLOY_DIR = Path("/deploy")
DEPLOY_INTERVAL = 10   # secondes entre deux passages sur les demandes
SSH_PROBE_TIMEOUT = 20


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


def ordered_roles() -> set[str] | None:
    """Roles de order.yaml : les seuls que site.yml (genere a partir de lui)
    execute. Un role absent n'a aucun play : un run sur son tag ne fait rien.
    None si order.yaml est illisible (rien a signaler alors)."""
    try:
        data = yaml.safe_load((REPO_DIR / "order.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    order = data.get("order") if isinstance(data, dict) else None
    return {str(r) for r in order} if isinstance(order, list) else None


RECAP_HOST_RE = re.compile(r"^\S+\s+:\s+ok=\d+", re.MULTILINE)


def run_status(returncode: int, log: str) -> str:
    """ok / failed / empty : `empty` = ansible-playbook a reussi sans traiter
    aucune machine (aucun play pour cette cible, ou aucune machine dedans)."""
    if returncode != 0:
        return "failed"
    recap = log.rsplit("PLAY RECAP", 1)
    if len(recap) < 2 or not RECAP_HOST_RE.search(recap[1]):
        return "empty"
    return "ok"


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
            status = run_status(result.returncode, log_path.read_text(errors="replace"))
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


def build_targets(machines: list[dict], group_name, roles: set[str], ordered: set[str] | None = None) -> dict:
    """Cibles valides pour `ansible-playbook --limit` : un groupe par tag
    Bastion (nom normalise, comme bastion_inventory.build_inventory) et un
    hote par machine (son `name` Bastion). Fonction pure : testable sans
    reseau ni clone. `in_order` : role present dans order.yaml (sinon site.yml
    n'a aucun play pour lui) ; toujours vrai si order.yaml est inconnu."""
    tag_names = sorted({t for m in machines for t in (m.get("tags") or [])})
    tags = [
        {"name": t, "role": group_name(t), "has_role": group_name(t) in roles,
         "in_order": ordered is None or group_name(t) in ordered}
        for t in tag_names
    ]
    hosts = [
        {
            "name": m["name"],
            "host": m.get("host", ""),
            "tags": sorted({group_name(t) for t in (m.get("tags") or [])}),
        }
        for m in sorted(machines, key=lambda m: m["name"])
    ]
    return {"tags": tags, "machines": hosts}


def load_targets() -> tuple[dict | None, str | None, int]:
    """Synchronise le clone puis interroge Bastion. (cibles, erreur, code HTTP)."""
    ok, err = sync_repo()
    if not ok:
        return None, err, 502

    if not (REPO_DIR / "inventory" / "bastion_inventory.py").exists():
        return None, "inventory/bastion_inventory.py introuvable dans le depot clone - est-ce bien le bon mirroir ?", 502

    inventory = load_inventory_module()
    try:
        machines = inventory.fetch_machines()
    except SystemExit:
        return None, "Impossible de joindre Bastion (verifier BASTION_URL/BASTION_API_TOKEN).", 502

    return build_targets(machines, inventory.group_name, local_roles(), ordered_roles()), None, 200


def allowed_limits(targets: dict) -> set[str]:
    return {t["role"] for t in targets["tags"]} | {m["name"] for m in targets["machines"]}


@app.route("/api/tags", methods=["GET"])
def api_tags_get():
    targets, err, code = load_targets()
    if targets is None:
        return jsonify({"error": err}), code
    return jsonify({**targets, "mirror_name": load_settings()["mirror_name"]})


@app.route("/api/runs", methods=["GET"])
def api_runs_list():
    return jsonify({"runs": sorted(load_runs(), key=lambda r: r["started_at"], reverse=True)})


@app.route("/api/runs", methods=["POST"])
def api_runs_create():
    body = request.get_json(force=True, silent=True) or {}
    limit = (body.get("limit") or "").strip() or None

    # Cible validee contre ce que Bastion connait reellement (groupes et
    # machines) : jamais une chaine libre transmise telle quelle a
    # `ansible-playbook --limit` (qui accepterait aussi des motifs).
    if limit is not None:
        targets, err, code = load_targets()
        if targets is None:
            return jsonify({"error": err}), code
        if limit not in allowed_limits(targets):
            return jsonify({"error": f"Cible inconnue : '{limit}' (ni tag, ni machine de Bastion)"}), 400

    run_id = start_run(limit)
    if run_id is None:
        return jsonify({"error": "Un run est deja en cours"}), 409
    return jsonify({"ok": True, "id": run_id}), 201


def start_run(limit: str | None, actor: str = "") -> str | None:
    """Lance un run en arriere-plan ; None si un run tourne deja (un seul a la fois)."""
    with RUN_LOCK:
        if RUN_STATE["running"]:
            return None
        RUN_STATE["running"] = True

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs = load_runs()
    entry = {
        "id": run_id,
        "limit": limit,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    if actor:
        entry["actor"] = actor
    runs.append(entry)
    save_runs(runs)

    threading.Thread(target=execute_run, args=(run_id, limit), daemon=True).start()
    return run_id


# --- deploiement apres un flash exploitant (voir deploy.py) -------------------------------

def planned_tasks(limit: str) -> int:
    """Nombre de taches que site.yml executerait sur cette machine (0 : rien a deployer)."""
    try:
        result = subprocess.run(["ansible-playbook", str(REPO_DIR / "site.yml"), "--list-hosts", "--list-tasks", "--limit", limit],
                                capture_output=True, text=True, timeout=120, cwd=REPO_DIR)
    except (subprocess.TimeoutExpired, OSError):
        return 0
    return deploy.count_planned_tasks(result.stdout) if result.returncode == 0 else 0


def probe_card(ip: str, instance_id: str) -> bool:
    """La carte attendue repond-elle en SSH, premier demarrage termine ? (cle d'automatisation)"""
    cmd = ["ssh", "-i", os.environ.get("ANSIBLE_PRIVATE_KEY_FILE", "/keys/automation_ed25519"),
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR",
           f"{os.environ.get('ANSIBLE_REMOTE_USER', 'pi')}@{ip}",
           "cat /var/lib/cloud/data/instance-id; cloud-init status"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SSH_PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return deploy.card_ready(result.stdout, instance_id)


def run_entry(run_id: str) -> dict | None:
    return next((r for r in load_runs() if r["id"] == run_id), None)


def deploy_step(req: dict, status: dict | None, now: float) -> dict:
    """Fait avancer une demande d'un cran et renvoie son nouvel etat."""
    if status is None or status.get("requested_at") != req["requested_at"]:
        status = {"hostname": req["hostname"], "requested_at": req["requested_at"], "state": "pending"}
    state = status["state"]
    if state in deploy.FINAL:
        return status

    if state == "pending":
        ok, _err = sync_repo()
        if not ok:
            return status   # mirroir injoignable : on reessaiera au prochain passage
        try:
            machines = load_inventory_module().fetch_machines()
        except SystemExit:
            return status   # Bastion injoignable : idem
        limit = deploy.inventory_name(machines, req["hostname"])
        planned = planned_tasks(limit) if limit else 0
        if not planned:
            return {**status, "state": "none"}
        return {**status, "state": "waiting", "limit": limit, "planned": planned,
                "deadline": req["requested_at"] + deploy.WAIT_SECONDS}

    if state == "waiting":
        if now > status["deadline"]:
            return {**status, "state": "timeout"}
        if not req.get("ip") or not probe_card(req["ip"], req["instance_id"]):
            return status
        run_id = start_run(status["limit"], actor="exploitant")
        if run_id is None:
            return status   # un autre run tourne : la machine attend son tour
        return {**status, "state": "running", "run_id": run_id, "percent": 1}

    if state == "running":
        run = run_entry(status["run_id"])
        if run is None:
            return {**status, "state": "failed"}
        if run["status"] == "running":
            try:
                log = (RUNS_DIR / f"{status['run_id']}.log").read_text(errors="replace")
            except OSError:
                log = ""
            return {**status, "percent": deploy.progress(deploy.count_done_tasks(log), status["planned"])}
        return {**status, "state": "done" if run["status"] == "ok" else "failed", "percent": 100}
    return status


def deploy_once(now: float | None = None) -> None:
    now = time.time() if now is None else now
    requests_dir, status_dir = DEPLOY_DIR / "requests", DEPLOY_DIR / "status"
    names = set()
    for path in sorted(requests_dir.glob("*.json")) if requests_dir.is_dir() else []:
        req = deploy.read_json(path)
        if not deploy.valid_request(req) or path.stem != req["hostname"]:
            continue
        names.add(req["hostname"])
        status_path = status_dir / f"{req['hostname']}.json"
        before = deploy.read_json(status_path)
        after = deploy_step(req, before, now)
        if after != before:
            deploy.write_json(status_path, after)
    # Demande retiree (abandon, ou l'exploitant a ferme l'ecran) : son etat disparait aussi.
    for path in status_dir.glob("*.json") if status_dir.is_dir() else []:
        if path.stem not in names:
            path.unlink(missing_ok=True)


def deploy_loop() -> None:
    while True:
        try:
            deploy_once()
        except Exception as exc:  # noqa: BLE001 - ne jamais tuer la boucle
            print(f"deploiement : {exc}", flush=True)
        time.sleep(DEPLOY_INTERVAL)


@app.route("/api/runs/<run_id>/log", methods=["GET"])
def api_runs_log(run_id: str):
    log_path = RUNS_DIR / f"{run_id}.log"
    if not log_path.exists():
        return jsonify({"error": "Log introuvable"}), 404
    return jsonify({"log": log_path.read_text()})


if __name__ == "__main__":
    threading.Thread(target=deploy_loop, daemon=True).start()
    app.run(host="127.0.0.1", port=5055)
