#!/usr/bin/env python3
"""expolab prises-admin : prises connectees Matter des modules de l'expo.

Deux onglets :
  - Approvisionnement : ajouter une prise (code ou scan du QR code), l'associer a un module,
    l'identifier, la retirer ;
  - Tableau de bord : marche/arret, puissance, energie, historique 24 h, actions groupees.

Le controleur est python-matter-server (stack matter, API WebSocket sur 127.0.0.1:5580) :
cette page ne fait que lui parler (matter_client.py). L'etat des prises est lu en arriere-plan
toutes les quelques secondes (la page ne declenche aucun appel au controleur pour s'afficher).

"Eteindre proprement" : la machine du module est d'abord arretee par SSH (cle d'automatisation,
la meme qu'ansible-web), puis la prise est coupee - couper un Raspberry Pi en marche peut abimer
sa carte SD.
"""
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import matter_client
import plugs as P

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("PRISES_DATA_DIR", "/data"))
RESERVATIONS = Path(os.environ.get("PRISES_RESERVATIONS", "/dnsmasq-admin-config/reservations.conf"))
BASTION_CONFIG = Path(os.environ.get("PRISES_BASTION_CONFIG", "/bastion-config/machines.yaml"))
FLASHER_SETTINGS = Path(os.environ.get("PRISES_FLASHER_SETTINGS", "/flasher-data/settings.json"))
SSH_KEY = Path(os.environ.get("PRISES_SSH_KEY", "/keys/automation_ed25519"))
SSH_USER = os.environ.get("PRISES_SSH_USER", "pi")
POLL_SECONDS = int(os.environ.get("PRISES_POLL_SECONDS", "5"))
HISTORY_SECONDS = 60
SHUTDOWN_WAIT = 90        # secondes au plus pour que la machine s'eteigne avant la coupure
SHUTDOWN_GRACE = 10       # marge apres l'arret du SSH (fin de l'ecriture sur la carte SD)

_lock = threading.Lock()
_state: dict = {"controller": None, "nodes": [], "error": None, "updated": 0}
_job: dict = {"state": "idle"}          # ajout d'une prise : idle | running | done | error
_busy: dict = {}                        # node_id -> message (extinction propre en cours)
_notices: dict = {}                     # node_id -> message (echec d'une extinction, jusqu'a la prochaine action)
_history: P.History | None = None


# --- association prise -> module (data/plugs.json) ------------------------------------------

def plugs_path() -> Path:
    return DATA_DIR / "plugs.json"


def load_meta() -> dict:
    try:
        return {int(k): v for k, v in json.loads(plugs_path().read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_meta(meta: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = plugs_path().with_suffix(".tmp")
    tmp.write_text(json.dumps({str(k): v for k, v in meta.items()}, indent=2, ensure_ascii=False))
    os.replace(tmp, plugs_path())


def modules() -> dict:
    return P.load_modules(RESERVATIONS, BASTION_CONFIG)


# --- lecture de l'etat en arriere-plan -------------------------------------------------------

def refresh() -> None:
    try:
        info = matter_client.call(None, timeout=10)
        nodes = matter_client.call("get_nodes", timeout=20) or []
        with _lock:
            _state.update(controller={"bluetooth": info.get("bluetooth_enabled"), "wifi": info.get("wifi_credentials_set"),
                                      "sdk": info.get("sdk_version")},
                          nodes=[P.read_node(n) for n in nodes], error=None, updated=time.time())
    except matter_client.MatterError as exc:
        with _lock:
            _state.update(error=str(exc), controller=None)


def poll_loop() -> None:
    last_history = 0.0
    while True:
        refresh()
        if _history and time.time() - last_history >= HISTORY_SECONDS:
            last_history = time.time()
            with _lock:
                nodes = list(_state["nodes"])
            try:
                _history.add(nodes)
            except Exception as exc:  # noqa: BLE001 - l'historique ne doit jamais arreter la lecture
                print(f"historique : {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def view() -> dict:
    """Etat complet pour la page."""
    meta, mods = load_meta(), modules()
    with _lock:
        nodes, controller, error = list(_state["nodes"]), _state["controller"], _state["error"]
        busy, notices = dict(_busy), dict(_notices)
    plugs_out = []
    for n in nodes:
        m = meta.get(n["node_id"], {})
        series = _history.series(n["node_id"]) if _history and n["measures"] else []
        plugs_out.append({**n, "name": m.get("name") or f"prise-{n['node_id']}", "module": m.get("module") or "",
                          "module_ip": mods.get(m.get("module") or "", ""), "busy": busy.get(n["node_id"]),
                          "notice": notices.get(n["node_id"]),
                          "history": series,
                          "energy_today_wh": _history.energy_today(n["node_id"], n["energy_wh"]) if _history else None})
    plugs_out.sort(key=lambda p: (p["module"] or "~", p["name"]))
    on = [p for p in plugs_out if p["available"] and p["on"]]
    return {
        "controller": controller, "error": error, "job": dict(_job), "modules": list(mods),
        "plugs": plugs_out,
        "summary": {
            "power_w": round(sum(p["power_w"] or 0 for p in plugs_out if p["available"]), 1),
            "energy_today_wh": round(sum(p["energy_today_wh"] or 0 for p in plugs_out), 1),
            "on": len(on), "total": len(plugs_out),
            "unreachable": sum(1 for p in plugs_out if not p["available"]),
        },
    }


def plug(node_id: int) -> dict | None:
    with _lock:
        return next((n for n in _state["nodes"] if n["node_id"] == node_id), None)


# --- ajout d'une prise -----------------------------------------------------------------------

def wifi_credentials() -> dict | None:
    """Wi-Fi de l'expo : celui des reglages du flasheur (les cartes et les prises sur le meme reseau)."""
    try:
        s = json.loads(FLASHER_SETTINGS.read_text())
    except (OSError, ValueError):
        return None
    return {"ssid": s["wifi_ssid"], "credentials": s["wifi_psk"]} if s.get("wifi_ssid") and s.get("wifi_psk") else None


def _commission(code: str, name: str, module: str) -> None:
    started = time.time()
    try:
        _job.update(step="wifi", message="Transmission du Wi-Fi de l'expo au controleur")
        creds = wifi_credentials()
        if creds is None:
            raise matter_client.MatterError("Aucun Wi-Fi dans les reglages du flasheur (Cartes SD > Reglages) : la prise ne saurait pas a quel reseau se connecter.")
        matter_client.call("set_wifi_credentials", creds)
        _job.update(step="commission", message="Bluetooth, verification de la prise, connexion au Wi-Fi (jusqu'a quelques minutes)")
        node = matter_client.call("commission_with_code", {"code": code, "network_only": False}, timeout=600)
        node_id = int(node["node_id"])
        meta = load_meta()
        meta[node_id] = {"name": name or f"prise-{module or node_id}", "module": module}
        save_meta(meta)
        refresh()
        _job.update(state="done", step="done", node_id=node_id, seconds=round(time.time() - started),
                    message=f"Prise ajoutee ({meta[node_id]['name']}).")
    except (matter_client.MatterError, KeyError, TypeError, ValueError) as exc:
        _job.update(state="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - l'interface doit toujours voir la fin
        _job.update(state="error", error=f"Erreur inattendue : {exc}")


@app.route("/api/plugs", methods=["POST"])
def api_add():
    global _job
    body = request.get_json(silent=True) or {}
    code = P.normalize_code(str(body.get("code", "")))
    if code is None:
        return jsonify({"error": "Code d'appairage invalide : 11 chiffres, ou le texte du QR code (MT:...)."}), 400
    module = str(body.get("module", "")).strip()
    if module and module not in modules():
        return jsonify({"error": "Module inconnu."}), 400
    name = str(body.get("name", "")).strip()[:40]
    if _job.get("state") == "running":
        return jsonify({"error": "Un ajout est deja en cours."}), 409
    _job = {"state": "running", "step": "start", "message": "Demarrage", "started": int(time.time()),
            "module": module, "name": name}
    threading.Thread(target=_commission, args=(code, name, module), daemon=True).start()
    return jsonify(_job), 202


@app.route("/api/job/dismiss", methods=["POST"])
def api_job_dismiss():
    global _job
    if _job.get("state") != "running":
        _job = {"state": "idle"}
    return jsonify(_job)


# --- reglages, identification, retrait -------------------------------------------------------

def known_or_404(node_id: int):
    if plug(node_id) is None and node_id not in load_meta():
        return jsonify({"error": "Prise inconnue."}), 404
    return None


@app.route("/api/plugs/<int:node_id>", methods=["PUT"])
def api_update(node_id):
    if (err := known_or_404(node_id)):
        return err
    body = request.get_json(silent=True) or {}
    module = str(body.get("module", "")).strip()
    if module and module not in modules():
        return jsonify({"error": "Module inconnu."}), 400
    meta = load_meta()
    meta[node_id] = {"name": str(body.get("name", "")).strip()[:40] or f"prise-{node_id}", "module": module}
    save_meta(meta)
    return jsonify(meta[node_id])


@app.route("/api/plugs/<int:node_id>/identify", methods=["POST"])
def api_identify(node_id):
    p = plug(node_id)
    if p is None:
        return jsonify({"error": "Prise inconnue ou injoignable."}), 404
    try:
        matter_client.call("device_command", {"node_id": node_id, "endpoint_id": p["endpoint"], "cluster_id": P.IDENTIFY,
                                              "command_name": "Identify", "payload": {"identifyTime": 10}})
    except matter_client.MatterError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@app.route("/api/plugs/<int:node_id>", methods=["DELETE"])
def api_remove(node_id):
    if (err := known_or_404(node_id)):
        return err
    try:
        if plug(node_id) is not None:
            matter_client.call("remove_node", {"node_id": node_id}, timeout=60)
    except matter_client.MatterError as exc:
        return jsonify({"error": str(exc)}), 502
    meta = load_meta()
    meta.pop(node_id, None)
    save_meta(meta)
    refresh()
    return jsonify({"ok": True})


# --- marche / arret ---------------------------------------------------------------------------

def switch(node_id: int, on: bool) -> None:
    p = plug(node_id)
    if p is None or not p["can_switch"]:
        raise matter_client.MatterError("Prise inconnue ou sans commande marche/arret.")
    matter_client.call("device_command", {"node_id": node_id, "endpoint_id": p["endpoint"], "cluster_id": P.ONOFF,
                                          "command_name": "On" if on else "Off", "payload": {}})


def ssh_up(ip: str) -> bool:
    try:
        with socket.create_connection((ip, 22), timeout=2):
            return True
    except OSError:
        return False


def clean_off(node_id: int, ip: str) -> None:
    """Arret propre de la machine par SSH, attente de son extinction, puis coupure de la prise."""
    try:
        _busy[node_id] = "Arret propre de la machine en cours..."
        if ip and ssh_up(ip):
            subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8", f"{SSH_USER}@{ip}",
                            "sudo -n systemctl poweroff"], capture_output=True, timeout=30)
            deadline = time.time() + SHUTDOWN_WAIT
            while time.time() < deadline and ssh_up(ip):
                time.sleep(3)
            if ssh_up(ip):
                # Jamais de coupure d'une machine qui tourne encore : on le signale, on ne force pas.
                _notices[node_id] = (f"La machine ({ip}) ne s'est pas eteinte en {SHUTDOWN_WAIT} s : prise laissee allumee. "
                                     "Utiliser \"Couper tout de suite\" si elle est bloquee.")
                return
            _busy[node_id] = "Machine arretee, coupure de la prise dans quelques secondes..."
            time.sleep(SHUTDOWN_GRACE)
        switch(node_id, False)
        refresh()
    except (matter_client.MatterError, subprocess.SubprocessError, OSError) as exc:
        _notices[node_id] = f"Extinction interrompue : {exc}"
    finally:
        _busy.pop(node_id, None)


@app.route("/api/plugs/<int:node_id>/power", methods=["POST"])
def api_power(node_id):
    body = request.get_json(silent=True) or {}
    if node_id in _busy:
        return jsonify({"error": "Une extinction est deja en cours pour cette prise."}), 409
    _notices.pop(node_id, None)
    try:
        if body.get("on"):
            switch(node_id, True)
        elif body.get("clean", True):
            module = load_meta().get(node_id, {}).get("module", "")
            _busy[node_id] = "Arret propre de la machine en cours..."   # avant le thread : visible tout de suite
            threading.Thread(target=clean_off, args=(node_id, modules().get(module, "")), daemon=True).start()
            return jsonify({"ok": True, "clean": True}), 202
        else:
            switch(node_id, False)
    except matter_client.MatterError as exc:
        return jsonify({"error": str(exc)}), 502
    refresh()
    return jsonify({"ok": True})


@app.route("/api/all", methods=["POST"])
def api_all():
    """Tout allumer, ou tout eteindre proprement (prises joignables seulement)."""
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in ("on", "off"):
        return jsonify({"error": "Action inconnue."}), 400
    meta, mods = load_meta(), modules()
    with _lock:
        targets = [n for n in _state["nodes"] if n["available"] and n["can_switch"]]
    done = []
    for n in targets:
        if action == "on" and not n["on"]:
            try:
                switch(n["node_id"], True)
                done.append(n["node_id"])
            except matter_client.MatterError:
                pass
        elif action == "off" and n["on"] and n["node_id"] not in _busy:
            ip = mods.get(meta.get(n["node_id"], {}).get("module", ""), "")
            _busy[n["node_id"]] = "Arret propre de la machine en cours..."
            threading.Thread(target=clean_off, args=(n["node_id"], ip), daemon=True).start()
            done.append(n["node_id"])
    refresh()
    return jsonify({"ok": True, "nodes": done})


# --- pages ----------------------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(view())


def start() -> None:
    global _history
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _history = P.History(DATA_DIR / "history.db")
    threading.Thread(target=poll_loop, daemon=True).start()


if __name__ == "__main__":
    start()
    app.run(host="127.0.0.1", port=5058, threaded=True)
