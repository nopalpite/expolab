"""Supervision des machines de l'expo (supervision-admin).

Une seule ecoute, sur 127.0.0.1 : derriere Caddy et le portail (vhost supervision.<domaine>). Toutes les 30 s, le
serveur interroge node_exporter (port 9100) des machines taguees `monitoring` dans Bastion et juge leur etat
(checks.py) ; l'etat et l'historique sont dans data/supervision.db (supervisor.py, history.py).

Point d'entree pour la future gestion d'allumage de l'expo : POST /api/expo {"open": true|false, "source": "..."}
depuis le serveur lui-meme, avec l'en-tete X-Requested-With: expolab (comme la page).
"""
import logging
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import checks as C
import notify as N
import scrape
from fleet import BastionMachines, short_name
from history import History
from supervisor import Supervisor

DATA_DIR = Path(os.environ.get("SUPERVISION_DATA_DIR", "/data"))
BIND = os.environ.get("SUPERVISION_BIND", "127.0.0.1")
PORT = int(os.environ.get("SUPERVISION_PORT", "5064"))
BASTION_CONFIG = os.environ.get("SUPERVISION_BASTION_CONFIG", "/bastion-config/machines.yaml")
POLL_SECONDS = 30

log = logging.getLogger("supervision")
app = Flask(__name__, root_path=str(Path(__file__).resolve().parent))
supervisor = notifier = None


def create(data_dir=DATA_DIR, machines_source=None, fetch=scrape.fetch, send=N.apprise_send, clock=time.time,
           background=True):
    global supervisor, notifier
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    history = History(Path(data_dir) / "supervision.db")
    supervisor = Supervisor(history, machines_source or BastionMachines(BASTION_CONFIG), fetch, clock=clock)
    notifier = N.Notifier(history, send=send, clock=clock, background=background)
    return supervisor, notifier


def loop():
    while True:
        started = time.monotonic()
        try:
            events = supervisor.tick()
            for event in events:
                log.info("%s %s %s : %s (%s)", event.kind, event.machine, event.label, event.level, event.detail)
            notifier.after_tick(events, supervisor.expo()["open"])
        except Exception:                      # un passage rate ne doit jamais arreter la supervision
            log.exception("passage en echec")
        time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))


@app.before_request
def forbid_forged_posts():
    """Un autre site ne peut pas envoyer cet en-tete sans autorisation : requetes forgees refusees."""
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Requested-With") != "expolab":
        return jsonify({"error": "Requete refusee."}), 403
    return None


@app.after_request
def no_store(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(supervisor.view())


@app.route("/api/history")
def api_history():
    try:
        days = max(1, min(int(request.args.get("days", 7)), 3650))
    except ValueError:
        days = 7
    return jsonify({"incidents": supervisor.history.incidents(supervisor.clock() - 86400 * days)})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"settings": supervisor.settings(),
                        "fields": {k: {"default": d, "min": lo, "max": hi, "label": label}
                                   for k, (d, lo, hi, label) in C.SETTINGS.items()}})
    try:
        return jsonify({"settings": supervisor.update_settings(body())})
    except C.SettingsError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/expo", methods=["POST"])
def api_expo():
    data = body()
    if not isinstance(data.get("open"), bool):
        return jsonify({"error": "« open » doit valoir true ou false."}), 400
    supervisor.set_expo(data["open"], data.get("source") or "manuel")
    return jsonify({"expo": supervisor.expo()})


@app.route("/api/maintenance", methods=["POST"])
def api_maintenance():
    data = body()
    name = short_name(data.get("machine"))
    if name not in {m.name for m in supervisor.machines_source()}:
        return jsonify({"error": "Machine inconnue (tag monitoring dans Bastion ?)."}), 400
    supervisor.set_maintenance(name, bool(data.get("on")), data.get("note", ""))
    return jsonify({"ok": True})


@app.route("/api/channels")
def api_channels():
    return jsonify({"channels": notifier.channels_view(), "night": notifier.night(),
                    # une liste : jsonify trierait les cles d'un dictionnaire (Telegram doit rester en tete)
                    "kinds": [{"id": k, "label": label, "fields": [{"name": n, "label": fl, "secret": s} for n, fl, s in fields]}
                              for k, (label, fields) in N.KINDS.items()]})


def channel_call(fn):
    try:
        return jsonify(fn() or {"ok": True})
    except N.ChannelError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/channels", methods=["POST"])
def api_channel_save():
    return channel_call(lambda: {"id": notifier.save_channel(body())["id"]})


@app.route("/api/channels/<channel_id>", methods=["DELETE"])
def api_channel_delete(channel_id):
    return channel_call(lambda: notifier.delete_channel(channel_id))


@app.route("/api/channels/<channel_id>/test", methods=["POST"])
def api_channel_test(channel_id):
    return channel_call(lambda: {"last": notifier.test_channel(channel_id)})


@app.route("/api/night", methods=["POST"])
def api_night():
    return channel_call(lambda: {"night": notifier.set_night(body())})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    create()
    threading.Thread(target=loop, daemon=True).start()
    app.run(host=BIND, port=PORT, threaded=True)
