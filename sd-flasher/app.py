#!/usr/bin/env python3
"""expolab sd-flasher : ecrit l'image master sur une carte SD (lecteur USB branche
sur le serveur) et la personnalise pour une machine : hostname, identite unique,
Wi-Fi et cles SSH. Voir flasher.py pour la logique et les garde-fous.

Dossiers montes dans le conteneur :
  /images             images master (.img, .img.gz, .img.xz, .img.zst), lecture seule
  /data               settings.json (Wi-Fi, cle SSH - mode 600) et audit.log
  /dnsmasq-admin-config  reservations DHCP (lecture seule) : liste des noms de machines
  /keys/automation.pub  cle publique d'automatisation Ansible (optionnelle)
Le conteneur est privilegie (pid: host, /dev) : il ecrit sur des disques bruts.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import flasher

app = Flask(__name__)

IMAGES_DIR = Path(os.environ.get("SD_FLASHER_IMAGES_DIR", "/images"))
DATA_DIR = Path(os.environ.get("SD_FLASHER_DATA_DIR", "/data"))
RESERVATIONS_FILE = Path(os.environ.get("SD_FLASHER_RESERVATIONS", "/dnsmasq-admin-config/reservations.conf"))
AUTOMATION_PUB = Path(os.environ.get("SD_FLASHER_AUTOMATION_PUB", "/keys/automation.pub"))
MAX_BYTES = int(os.environ.get("SD_FLASHER_MAX_GB", "256")) * 1000**3
# Mode simulation : aucun disque n'est touche, l'ecriture va dans des fichiers de ce dossier.
# SD_FLASHER_DEVICES_FILE = JSON {"eligible": [...], "ignored": [...]} qui remplace lsblk.
DRY_RUN = os.environ.get("SD_FLASHER_DRY_RUN") == "1"
DRY_RUN_DIR = Path(os.environ.get("SD_FLASHER_DRY_RUN_DIR", "/tmp/sd-flasher-dry-run"))
DEVICES_FILE = os.environ.get("SD_FLASHER_DEVICES_FILE")

_job_lock = threading.Lock()
_job: dict = {"state": "idle"}  # idle | running | done | error


def settings_path() -> Path:
    return DATA_DIR / "settings.json"


def audit(event: str, **fields) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {"time": int(time.time()), "event": event, **fields}
    with open(DATA_DIR / "audit.log", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- reglages --------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {
        "wifi_ssid": data.get("wifi_ssid", ""),
        "wifi_psk": data.get("wifi_psk", ""),
        "wifi_country": data.get("wifi_country", "FR"),
        "extra_keys": [k for k in data.get("extra_keys", []) if isinstance(k, str)],
        "use_automation_key": data.get("use_automation_key", True),
    }


def automation_key() -> str:
    try:
        return AUTOMATION_PUB.read_text().strip().splitlines()[0]
    except (OSError, IndexError):
        return ""


def effective_settings() -> dict:
    """Ce qui est reellement ecrit sur la carte : reglages + cle d'automatisation."""
    s = load_settings()
    keys = list(s["extra_keys"])
    key = automation_key()
    if s["use_automation_key"] and key:
        keys.insert(0, key)
    return {"wifi_ssid": s["wifi_ssid"], "wifi_psk": s["wifi_psk"], "wifi_country": s["wifi_country"], "authorized_keys": keys}


def save_settings(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = settings_path().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # contient le mot de passe Wi-Fi
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, settings_path())


def public_settings() -> dict:
    s = load_settings()
    return {
        "wifi_ssid": s["wifi_ssid"],
        "wifi_country": s["wifi_country"],
        "has_wifi_psk": bool(s["wifi_psk"]),  # jamais renvoye : le mot de passe ne quitte pas le serveur
        "extra_keys": s["extra_keys"],
        "use_automation_key": s["use_automation_key"],
        "automation_key_available": bool(automation_key()),
    }


# --- machines et peripheriques -----------------------------------------------------------

def machine_names() -> list[dict]:
    """Reservations DHCP par NOM (sans MAC) : {hostname, ip}. C'est la liste des
    machines que l'expo sait remplacer sans toucher a la reservation."""
    machines = []
    try:
        for line in RESERVATIONS_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2 and not is_mac(parts[0]):
                machines.append({"hostname": parts[0], "ip": parts[1]})
    except OSError:
        pass
    machines.sort(key=lambda m: m["hostname"])
    return machines


def is_mac(value: str) -> bool:
    return bool(re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value))


def read_devices() -> dict:
    if DEVICES_FILE:
        return json.loads(Path(DEVICES_FILE).read_text())
    try:
        return flasher.read_devices(MAX_BYTES)
    except Exception as exc:  # lsblk absent, timeout...
        return {"eligible": [], "ignored": [], "error": f"Detection impossible : {exc}"}


def pick_image(name: str | None) -> Path | None:
    images = flasher.list_images(IMAGES_DIR)
    if not images:
        return None
    if name:
        chosen = next((i for i in images if i["name"] == name), None)  # nom pris dans la liste, jamais un chemin
    else:
        chosen = max(images, key=lambda i: i["mtime"])
    return IMAGES_DIR / chosen["name"] if chosen else None


# --- routes ----------------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    devices = read_devices()
    return jsonify(
        {
            "devices": devices,
            "images": flasher.list_images(IMAGES_DIR),
            "machines": machine_names(),
            "job": _job,
            "dry_run": DRY_RUN,
            "max_gb": MAX_BYTES // 10**9,
        }
    )


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(public_settings())


@app.route("/api/settings", methods=["PUT"])
def api_settings_put():
    body = request.get_json(silent=True) or {}
    current = load_settings()
    new = {
        "wifi_ssid": str(body.get("wifi_ssid", current["wifi_ssid"])).strip(),
        "wifi_country": str(body.get("wifi_country", current["wifi_country"])).strip().upper(),
        "extra_keys": [k.strip() for k in body.get("extra_keys", current["extra_keys"]) if isinstance(k, str) and k.strip()],
        "use_automation_key": bool(body.get("use_automation_key", current["use_automation_key"])),
        "wifi_psk": current["wifi_psk"],
    }
    if body.get("wifi_psk"):  # vide = on garde l'actuel
        new["wifi_psk"] = str(body["wifi_psk"])
    if body.get("clear_wifi"):
        new["wifi_ssid"] = new["wifi_psk"] = ""
    try:
        flasher.check_settings({"wifi_ssid": new["wifi_ssid"], "wifi_psk": new["wifi_psk"], "wifi_country": new["wifi_country"],
                                "authorized_keys": new["extra_keys"]})
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400
    save_settings(new)
    audit("settings", wifi_ssid=new["wifi_ssid"], keys=len(new["extra_keys"]))
    return jsonify(public_settings())


@app.route("/api/preview")
def api_preview():
    """Ce qui serait ecrit pour ce hostname, secrets masques (recapitulatif avant confirmation)."""
    hostname = request.args.get("hostname", "")
    try:
        flasher.check_hostname(hostname)
        eff = effective_settings()
        flasher.check_settings(eff)
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "hostname": hostname,
            "wifi": eff["wifi_ssid"] or None,
            "keys": len(eff["authorized_keys"]),
            "actions": ["hostname", "machine-id vide", "cles SSH d'hote supprimees", "ejection"],
        }
    )


@app.route("/api/flash", methods=["POST"])
def api_flash():
    global _job
    body = request.get_json(silent=True) or {}
    device, hostname = body.get("device", ""), body.get("hostname", "")
    try:
        flasher.check_hostname(hostname)
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400
    if body.get("confirm") != hostname:
        return jsonify({"error": "La confirmation ne correspond pas au hostname."}), 400

    # Le peripherique doit etre dans la liste ACTUELLE des disques eligibles : jamais un chemin pris tel quel.
    eligible = {d["path"]: d for d in read_devices().get("eligible", [])}
    if device not in eligible:
        return jsonify({"error": "Carte introuvable ou non eligible (retiree ? deja utilisee par le serveur ?)."}), 409
    image = pick_image(body.get("image"))
    if image is None:
        return jsonify({"error": "Aucune image master disponible."}), 409
    settings = effective_settings()
    try:
        flasher.check_settings(settings)
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400

    with _job_lock:
        if _job.get("state") == "running":
            return jsonify({"error": "Un flash est deja en cours."}), 409
        _job = {"state": "running", "step": "write", "percent": 0, "message": "Demarrage", "hostname": hostname,
                "device": device, "model": eligible[device]["model"], "image": image.name, "started": int(time.time()),
                "steps": [{"id": s, "label": flasher.STEP_LABELS[s]} for s in flasher.STEPS]}
    audit("flash-start", device=device, model=eligible[device]["model"], serial=eligible[device]["serial"],
          hostname=hostname, image=image.name, dry_run=DRY_RUN)
    threading.Thread(target=_run, args=(image, device, hostname, settings, eligible[device]), daemon=True).start()
    return jsonify(_job), 202


def _update(**fields) -> None:
    _job.update(fields)


def _recheck(device: str, expected: dict, image: Path) -> None:
    """Juste avant d'ecrire : c'est TOUJOURS la meme carte ? Entre la verification et
    l'ecriture, le lecteur a pu etre debranche et un autre disque USB prendre le meme
    nom (/dev/sda) - on n'ecrit alors jamais dessus."""
    now = {d["path"]: d for d in read_devices().get("eligible", [])}.get(device)
    if not now or now["serial"] != expected["serial"] or now["size"] != expected["size"]:
        raise flasher.FlashError("La carte a change ou a ete retiree depuis la verification : rien n'a ete ecrit.")
    flasher.check_fits(image, now["size"])


def _run(image: Path, device: str, hostname: str, settings: dict, expected: dict) -> None:
    target = device
    if DRY_RUN:
        DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
        target = str(DRY_RUN_DIR / os.path.basename(device))
    try:
        _recheck(device, expected, image)
        result = flasher.run_flash(image_path=image, target=target, hostname=hostname, settings=settings,
                                   dry_run=DRY_RUN, dry_run_dir=DRY_RUN_DIR, update=_update)
        ip = next((m["ip"] for m in machine_names() if m["hostname"] == hostname), None)
        _update(state="done", step="eject", percent=100, message="Carte prete", result=result, ip=ip)
        audit("flash-done", device=device, hostname=hostname, sha256=result["sha256"], bytes=result["bytes"])
    except flasher.FlashError as exc:
        _update(state="error", error=str(exc))
        audit("flash-error", device=device, hostname=hostname, error=str(exc))
    except Exception as exc:  # jamais de thread silencieusement morte : l'interface doit voir l'echec
        _update(state="error", error=f"Erreur inattendue : {exc}")
        audit("flash-error", device=device, hostname=hostname, error=repr(exc))


@app.route("/api/job/dismiss", methods=["POST"])
def api_job_dismiss():
    global _job
    with _job_lock:
        if _job.get("state") == "running":
            return jsonify({"error": "Un flash est en cours."}), 409
        _job = {"state": "idle"}
    return jsonify(_job)


@app.route("/api/audit")
def api_audit():
    try:
        lines = (DATA_DIR / "audit.log").read_text().splitlines()[-50:]
    except OSError:
        lines = []
    return jsonify([json.loads(line) for line in reversed(lines) if line.startswith("{")])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5057)
