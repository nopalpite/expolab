"""Console audio de l'expo (audio-admin) : volume de l'exposition, des salles et de chaque machine.

DEUX surfaces reseau, volontairement separees (meme processus, deux ecoutes) :
  - l'interface (pages + /api/...) sur 127.0.0.1 : derriere Caddy et le portail (administrateur ;
    l'exploitant n'atteint que /exploitation et /api/exploitation/) ;
  - l'API des agents (/agent/...) sur l'adresse de l'expo (10.42.0.1) : les machines ne passent pas
    par le portail, elles s'authentifient par un JETON partage (data/agent.token).
La logique (niveaux, presets, machines) est dans hub.py.
"""
import hmac
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

import hub as H
from bastion import BastionRooms

DATA_DIR = Path(os.environ.get("AUDIO_DATA_DIR", "/data"))
UI_BIND = os.environ.get("AUDIO_UI_BIND", "127.0.0.1")
UI_PORT = int(os.environ.get("AUDIO_UI_PORT", "5062"))
AGENT_BIND = os.environ.get("AUDIO_AGENT_BIND", "10.42.0.1")
AGENT_PORT = int(os.environ.get("AUDIO_AGENT_PORT", "5063"))
BASTION_CONFIG = os.environ.get("AUDIO_BASTION_CONFIG", "/bastion-config/machines.yaml")

# Les salles et l'appartenance des machines viennent de Bastion (lecture seule) : une machine qui n'est dans
# aucune salle n'existe pas pour la console.
hub = H.Hub(str(DATA_DIR / "state.json"), rooms_source=BastionRooms(BASTION_CONFIG))
ui = Flask(__name__)
agent_api = Flask("audio-agent-api")
_token_cache = {"mtime": None, "value": ""}


# --- jeton des agents ----------------------------------------------------------------------------

def agent_token() -> str:
    path = DATA_DIR / "agent.token"
    try:
        mtime = path.stat().st_mtime
        if _token_cache["mtime"] != mtime:
            _token_cache.update(mtime=mtime, value=path.read_text().strip())
    except OSError:
        _token_cache.update(mtime=None, value="")
    return _token_cache["value"]


def token_ok() -> bool:
    expected = agent_token()
    return bool(expected) and hmac.compare_digest(request.headers.get("X-Agent-Token", ""), expected)


@agent_api.before_request
def check_agent_token():
    if not agent_token():
        return jsonify({"error": "Jeton des agents absent sur le serveur."}), 503      # ferme par defaut
    if not token_ok():
        return jsonify({"error": "Jeton invalide."}), 401
    return None


@agent_api.route("/agent/poll", methods=["POST"])
def agent_poll():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(hub.agent_poll(str(body.get("name", "")), body, int(body.get("rev", 0))))
    except (H.HubError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@agent_api.route("/agent/report", methods=["POST"])
def agent_report():
    try:
        hub.agent_report(request.get_json(silent=True) or {})
    except H.HubError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


# --- interface -----------------------------------------------------------------------------------

@ui.before_request
def forbid_forged_posts():
    """Un autre site ne peut pas envoyer cet en-tete sans autorisation : requetes forgees refusees."""
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Requested-With") != "expolab":
        return jsonify({"error": "Requete refusee."}), 403
    return None


@ui.after_request
def no_store(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def run(fn):
    """Execute une commande du hub et rend une reponse JSON (erreurs de saisie = 400)."""
    try:
        fn()
    except H.HubError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@ui.route("/")
def index():
    return render_template("index.html")


@ui.route("/exploitation")
def exploitation():
    return render_template("exploitation.html")


@ui.route("/api/state")
def api_state():
    return jsonify(hub.state_view())


@ui.route("/api/expo", methods=["POST"])
def api_expo():
    b = request.get_json(silent=True) or {}
    return run(lambda: hub.set_expo(b.get("db"), b.get("mute")))


@ui.route("/api/kill", methods=["POST"])
def api_kill():
    return run(hub.kill)


@ui.route("/api/rooms/<path:room_id>", methods=["POST"])
def api_room(room_id):
    b = request.get_json(silent=True) or {}
    return run(lambda: hub.set_room(room_id, b.get("db"), b.get("mute")))


@ui.route("/api/devices/<name>", methods=["POST"])
def api_device(name):
    b = request.get_json(silent=True) or {}
    return run(lambda: hub.set_device(name, b.get("db"), b.get("mute"), b.get("free"), b.get("label")))


@ui.route("/api/devices/<name>/validate", methods=["POST"])
def api_validate(name):
    return run(lambda: hub.validate(name))


@ui.route("/api/devices/<name>", methods=["DELETE"])
def api_forget(name):
    return run(lambda: hub.forget(name))


@ui.route("/api/presets", methods=["POST"])
def api_preset_save():
    return run(lambda: hub.save_preset((request.get_json(silent=True) or {}).get("name", "")))


@ui.route("/api/presets/<path:name>/recall", methods=["POST"])
def api_preset_recall(name):
    return run(lambda: hub.recall_preset(name))


@ui.route("/api/presets/<path:name>", methods=["DELETE"])
def api_preset_delete(name):
    return run(lambda: hub.delete_preset(name))


# --- exploitant : pourcentages, aucun detail de machine ---------------------------------------------

@ui.route("/api/exploitation/state")
def op_state():
    return jsonify(hub.operator_view())


@ui.route("/api/exploitation/expo", methods=["POST"])
def op_expo():
    b = request.get_json(silent=True) or {}
    return run(lambda: hub.set_expo(H.pct_to_db(b["pct"]) if "pct" in b else None, b.get("mute")))


@ui.route("/api/exploitation/rooms/<path:room_id>", methods=["POST"])
def op_room(room_id):
    b = request.get_json(silent=True) or {}
    return run(lambda: hub.set_room(room_id, H.pct_to_db(b["pct"]) if "pct" in b else None, b.get("mute")))


@ui.route("/api/exploitation/presets/<path:name>/recall", methods=["POST"])
def op_preset(name):
    return run(lambda: hub.recall_preset(name))


# --- demarrage ----------------------------------------------------------------------------------------

def serve(app, bind, port):
    server = make_server(bind, port, app, threaded=True)
    server.daemon_threads = True
    server.serve_forever()


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=serve, args=(agent_api, AGENT_BIND, AGENT_PORT), daemon=True).start()
    print(f"console audio : interface {UI_BIND}:{UI_PORT}, agents {AGENT_BIND}:{AGENT_PORT}", flush=True)
    serve(ui, UI_BIND, UI_PORT)


if __name__ == "__main__":
    main()
