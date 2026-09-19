#!/usr/bin/env python3
"""expolab webui : creer/supprimer des faux Raspberry Pi depuis un navigateur.

Pilote Incus directement (le socket hote est monte jusqu'ici via
expo-apps) et reutilise fleet/deploy-fleet.sh (monte en volume) comme
seule source de verite pour la logique de provisioning - la webui ne
fait qu'editer fleet/inventory.yaml et declencher ce script en tache de
fond, exactement comme le ferait un humain en ligne de commande.
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

FLEET_DIR = Path("/expolab/fleet")
INVENTORY_PATH = FLEET_DIR / "inventory.yaml"
DEPLOY_SCRIPT = FLEET_DIR / "deploy-fleet.sh"

MAC_PREFIX = "dc:a6:32:00:00"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
ROLES = ["sans-ecran", "avec-ecran"]

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def load_inventory() -> dict:
    if not INVENTORY_PATH.exists():
        return {"fleet": []}
    with open(INVENTORY_PATH) as f:
        return yaml.safe_load(f) or {"fleet": []}


def save_inventory(data: dict) -> None:
    with open(INVENTORY_PATH, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)


def next_mac(fleet: list[dict]) -> str:
    used = set()
    for pi in fleet:
        parts = str(pi.get("mac", "")).split(":")
        if len(parts) == 6:
            try:
                used.add(int(parts[-1], 16))
            except ValueError:
                pass
    for i in range(1, 255):
        if i not in used:
            return f"{MAC_PREFIX}:{i:02x}"
    raise RuntimeError("plus d'adresse MAC disponible dans la plage")


def incus_status_by_name() -> dict:
    """Etat live (status + IPv4) de chaque conteneur, via `incus list`."""
    try:
        out = subprocess.run(
            ["incus", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        items = json.loads(out.stdout)
    except Exception:
        return {}

    statuses = {}
    for item in items:
        name = item.get("name")
        state = item.get("state") or {}
        ips = []
        for net in (state.get("network") or {}).values():
            for addr in net.get("addresses", []):
                address = addr.get("address")
                if addr.get("family") == "inet" and addr.get("scope") != "local":
                    ips.append(address)
        statuses[name] = {"status": item.get("status", "Inconnu"), "ips": ips}
    return statuses


def start_job(job_id: str, cmd: list[str]) -> None:
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "log": ""}

    def run() -> None:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(FLEET_DIR),
                capture_output=True,
                text=True,
                timeout=900,
                stdin=subprocess.DEVNULL,
            )
            log = proc.stdout + proc.stderr
            status = "done" if proc.returncode == 0 else "error"
        except Exception as exc:  # noqa: BLE001 - surface to the UI
            log = str(exc)
            status = "error"
        with _jobs_lock:
            _jobs[job_id] = {"status": status, "log": log}

    threading.Thread(target=run, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html", roles=ROLES)


@app.route("/api/fleet", methods=["GET"])
def api_fleet_list():
    data = load_inventory()
    live = incus_status_by_name()
    fleet = []
    for pi in data.get("fleet", []):
        entry = dict(pi)
        entry["live"] = live.get(pi["name"], {"status": "Absent", "ips": []})
        fleet.append(entry)
    return jsonify({"fleet": fleet})


@app.route("/api/fleet", methods=["POST"])
def api_fleet_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip().lower()
    role = body.get("role") if body.get("role") in ROLES else ROLES[0]
    username = (body.get("username") or "pi").strip()
    password = body.get("password") or "raspberry"

    if not NAME_RE.match(name):
        return jsonify({"error": "Nom invalide (minuscules/chiffres/tirets, 3-32 caracteres, doit commencer par une lettre)"}), 400
    if not username:
        return jsonify({"error": "Nom d'utilisateur requis"}), 400

    data = load_inventory()
    fleet = data.setdefault("fleet", [])
    if any(pi["name"] == name for pi in fleet):
        return jsonify({"error": f"'{name}' existe deja dans l'inventaire"}), 409

    mac = next_mac(fleet)
    fleet.append(
        {
            "name": name,
            "mac": mac,
            "role": role,
            "username": username,
            "password": password,
        }
    )
    save_inventory(data)

    job_id = f"create-{name}-{int(time.time())}"
    start_job(job_id, ["bash", str(DEPLOY_SCRIPT)])
    return jsonify({"job_id": job_id, "mac": mac}), 202


@app.route("/api/fleet/<name>", methods=["DELETE"])
def api_fleet_delete(name: str):
    data = load_inventory()
    fleet = data.get("fleet", [])
    new_fleet = [pi for pi in fleet if pi["name"] != name]
    if len(new_fleet) == len(fleet):
        return jsonify({"error": f"'{name}' introuvable dans l'inventaire"}), 404
    data["fleet"] = new_fleet
    save_inventory(data)

    job_id = f"delete-{name}-{int(time.time())}"
    start_job(job_id, ["incus", "delete", name, "--force"])
    return jsonify({"job_id": job_id}), 202


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job inconnu"}), 404
    return jsonify(job)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
