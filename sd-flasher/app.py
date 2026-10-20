#!/usr/bin/env python3
"""expolab sd-flasher : ecrit l'image master sur une carte SD (lecteur USB branche
sur le serveur) et la personnalise pour une machine : hostname, identite unique,
Wi-Fi et cles SSH. Voir flasher.py pour la logique et les garde-fous.

Aussi : capture d'une carte en image reduite (capture.py), import d'une image depuis
une URL (telechargee par le serveur) et telechargement d'une image stockee.

Dossiers montes dans le conteneur :
  /images             images master (.img, .img.gz, .img.xz, .img.zst) : envoyees depuis la page
                      (PUT /api/images/<nom>, flux ecrit directement sur disque) ou copiees a la main
  /data               settings.json (Wi-Fi, cle SSH - mode 600) et audit.log
  /dnsmasq-admin-config  reservations DHCP (lecture seule) : liste des noms de machines
  /keys/automation.pub  cle publique d'automatisation Ansible (optionnelle)
Le conteneur est privilegie (pid: host, /dev) : il ecrit sur des disques bruts.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

import capture
import flasher

app = Flask(__name__)

IMAGES_DIR = Path(os.environ.get("SD_FLASHER_IMAGES_DIR", "/images"))
DATA_DIR = Path(os.environ.get("SD_FLASHER_DATA_DIR", "/data"))
RESERVATIONS_FILE = Path(os.environ.get("SD_FLASHER_RESERVATIONS", "/dnsmasq-admin-config/reservations.conf"))
AUTOMATION_PUB = Path(os.environ.get("SD_FLASHER_AUTOMATION_PUB", "/keys/automation.pub"))
ACCESS_PUB = Path(os.environ.get("SD_FLASHER_ACCESS_PUB", "/keys/access.pub"))   # cle d'acces de Bastion (humains)
MAX_BYTES = int(os.environ.get("SD_FLASHER_MAX_GB", "256")) * 1000**3
# Mode simulation : aucun disque n'est touche, l'ecriture va dans des fichiers de ce dossier.
# SD_FLASHER_DEVICES_FILE = JSON {"eligible": [...], "ignored": [...]} qui remplace lsblk.
DRY_RUN = os.environ.get("SD_FLASHER_DRY_RUN") == "1"
DRY_RUN_DIR = Path(os.environ.get("SD_FLASHER_DRY_RUN_DIR", "/tmp/sd-flasher-dry-run"))
DEVICES_FILE = os.environ.get("SD_FLASHER_DEVICES_FILE")
# image/prepare-master.sh, monte depuis le depot (voir la stack) : la meme preparation master
# que sur une machine, appliquee hors ligne a la copie d'une carte capturee.
PREPARE_SCRIPT = Path(os.environ.get("SD_FLASHER_PREPARE_SCRIPT", "/app/prepare-master.sh"))
# Deploiement apres un flash exploitant : dossier partage avec ansible-web (voir ansible-web/deploy.py).
# Le flasheur y depose requests/<nom>.json, ansible-web y publie status/<nom>.json.
DEPLOY_DIR = Path(os.environ.get("SD_FLASHER_DEPLOY_DIR", "/deploy"))

UPLOAD_RESERVE = int(os.environ.get("SD_FLASHER_UPLOAD_RESERVE_MB", "1024")) * 1024 * 1024  # espace laisse libre au serveur
# + = @ , : presents dans les noms d'images officiels (ubuntu-...-arm64+raspi.img.xz) et sans risque dans un nom de fichier
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+=@,-]{0,120}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAGIC = {".gz": b"\x1f\x8b", ".xz": b"\xfd7zXZ\x00", ".zst": b"\x28\xb5\x2f\xfd"}  # debut des archives reconnues
UPLOAD_CHUNK = 4 * 1024 * 1024
DEFAULT_TIME_SERVER = "10.42.0.1"   # serveur de temps du lab (stack ntp) : sans danger ailleurs, chrony garde ses autres sources
# Cache apt du serveur : les machines de l'expo n'ont pas internet, c'est leur seul acces aux paquets
# (et l'installation de paquets au flash l'exige). A vider pour une carte destinee a un autre reseau.
DEFAULT_APT_PROXY = "http://10.42.0.1:3142"
DEFAULT_KEYBOARD = "fr"   # azerty
DEFAULT_LOCALE = "fr_FR.UTF-8"
DEFAULT_TIMEZONE = "Europe/Paris"

_job_lock = threading.Lock()
_job: dict = {"state": "idle"}  # idle | running | done | error
_upload_lock = threading.Lock()  # un seul envoi a la fois (depuis le navigateur OU depuis une URL)
_fetch: dict = {"state": "idle"}  # import depuis une URL : idle | running | done | error
_fetch_cancel = threading.Event()
FETCH_TIMEOUT = 30   # secondes sans donnees avant d'abandonner un telechargement


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
        "use_access_key": data.get("use_access_key", True),
        "user_name": data.get("user_name", ""),
        "user_password": data.get("user_password", ""),
        "user_sudo": data.get("user_sudo", True),
        "user_pwauth": data.get("user_pwauth", True),
        "time_server": data.get("time_server", DEFAULT_TIME_SERVER),
        "apt_proxy": data.get("apt_proxy", DEFAULT_APT_PROXY),
        "keyboard_layout": data.get("keyboard_layout", DEFAULT_KEYBOARD),
        "locale": data.get("locale", DEFAULT_LOCALE),
        "timezone": data.get("timezone", DEFAULT_TIMEZONE),
        "packages": [p for p in data.get("packages", []) if isinstance(p, str)],
        "operator_image": data.get("operator_image", "") if isinstance(data.get("operator_image", ""), str) else "",
    }


def parse_packages(value) -> list[str]:
    """Liste de paquets depuis la page (texte, un par ligne ou separes par des espaces/virgules)
    ou depuis l'API (liste) : sans doublon, dans l'ordre saisi."""
    items = value.replace(",", " ").split() if isinstance(value, str) else [str(v).strip() for v in value or []]
    return list(dict.fromkeys(p.strip().lower() for p in items if p.strip()))


def _public_key(path: Path) -> str:
    try:
        return path.read_text().strip().splitlines()[0]
    except (OSError, IndexError):
        return ""


def automation_key() -> str:
    return _public_key(AUTOMATION_PUB)


def access_key() -> str:
    return _public_key(ACCESS_PUB)


def effective_settings() -> dict:
    """Ce qui est reellement ecrit sur la carte : reglages + cles du serveur (automatisation,
    puis acces Bastion, puis les cles saisies)."""
    s = load_settings()
    server_keys = [key for key, wanted in ((automation_key(), s["use_automation_key"]),
                                           (access_key(), s["use_access_key"])) if wanted and key]
    keys = server_keys + list(s["extra_keys"])
    return {"wifi_ssid": s["wifi_ssid"], "wifi_psk": s["wifi_psk"], "wifi_country": s["wifi_country"], "authorized_keys": keys,
            "user_name": s["user_name"], "user_password": s["user_password"], "user_sudo": s["user_sudo"], "user_pwauth": s["user_pwauth"],
            "time_server": s["time_server"], "apt_proxy": s["apt_proxy"],
            "keyboard_layout": s["keyboard_layout"], "locale": s["locale"], "timezone": s["timezone"],
            "packages": s["packages"]}


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
        "user_name": s["user_name"],
        "has_user_password": bool(s["user_password"]),  # idem
        "user_sudo": s["user_sudo"],
        "user_pwauth": s["user_pwauth"],
        "time_server": s["time_server"],
        "apt_proxy": s["apt_proxy"],
        "keyboard_layout": s["keyboard_layout"],
        "locale": s["locale"],
        "timezone": s["timezone"],
        "packages": s["packages"],
        "operator_image": s["operator_image"],
        "extra_keys": s["extra_keys"],
        "use_automation_key": s["use_automation_key"],
        "automation_key_available": bool(automation_key()),
        "use_access_key": s["use_access_key"],
        "access_key_available": bool(access_key()),
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
            if len(parts) == 3 and re.fullmatch(r"\d+[smhdw]?|infinite", parts[2]):
                parts = parts[:2]   # duree de bail ecrite par dnsmasq-admin (nom,ip,12h)
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


# --- images : liste, envoi, suppression ------------------------------------------------------

def sha_path(name: str) -> Path:
    return IMAGES_DIR / (name + ".sha256")


def list_images() -> list[dict]:
    images = flasher.list_images(IMAGES_DIR)
    for image in images:
        try:
            image["sha256"] = sha_path(image["name"]).read_text().strip()
        except OSError:
            image["sha256"] = ""  # image copiee a la main : pas d'empreinte connue
    return images


def image_name_error(name: str) -> str | None:
    if not IMAGE_NAME_RE.match(name) or ".." in name:
        return "Nom de fichier invalide (lettres, chiffres et . _ - + = @ , uniquement ; sans espace ni accent ; 120 caracteres maximum)."
    if not name.endswith(flasher.IMAGE_SUFFIXES):
        return "Type de fichier non reconnu : .img, .img.gz, .img.xz ou .img.zst."
    return None


def magic_error(name: str, head: bytes) -> str | None:
    """Le debut du fichier correspond-il a son extension ? (evite d'envoyer un mauvais fichier pendant des minutes)"""
    for suffix, magic in MAGIC.items():
        if name.endswith(suffix):
            return None if head.startswith(magic) else f"Ce fichier n'est pas une archive {suffix} valide."
    # image brute : table de partitions MBR (signature 55 AA a l'offset 510), y compris derriere un GPT
    return None if len(head) >= 512 and head[510:512] == b"\x55\xaa" else "Ce fichier ne ressemble pas a une image de carte SD (pas de table de partitions)."


def remove_stale_parts() -> None:
    """Envois ou captures interrompus par un arret du service : les restes ne servent a rien."""
    if IMAGES_DIR.is_dir():
        for part in IMAGES_DIR.glob("*.part"):
            try:
                part.unlink()
            except OSError:
                pass
        for work in IMAGES_DIR.glob(".capture-*"):
            shutil.rmtree(work, ignore_errors=True)


def free_for_images() -> int:
    """Place utilisable pour une image : le serveur garde toujours UPLOAD_RESERVE."""
    return max(0, shutil.disk_usage(IMAGES_DIR).free - UPLOAD_RESERVE) if IMAGES_DIR.is_dir() else 0


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
            "images": list_images(),
            "images_free": shutil.disk_usage(IMAGES_DIR).free if IMAGES_DIR.is_dir() else 0,
            "machines": machine_names(),
            "job": _job,
            "fetch": _fetch,
            "dry_run": DRY_RUN,
            "max_gb": MAX_BYTES // 10**9,
        }
    )


@app.route("/api/images/<name>", methods=["PUT"])
def api_image_put(name):
    """Recoit une image : corps brut de la requete, ecrit par morceaux dans <nom>.part puis
    renomme (jamais d'image a moitie ecrite dans la liste). Rien n'est charge en memoire."""
    error = image_name_error(name)
    if error:
        return jsonify({"error": error}), 400
    expected = request.args.get("sha256", "").strip().lower()
    if expected and not SHA256_RE.match(expected):
        return jsonify({"error": "Empreinte SHA-256 invalide (64 caracteres hexadecimaux)."}), 400
    length = request.content_length
    if not length:
        return jsonify({"error": "Taille de l'envoi inconnue."}), 411
    if not IMAGES_DIR.is_dir():
        return jsonify({"error": "Dossier des images introuvable."}), 500
    target, part = IMAGES_DIR / name, IMAGES_DIR / (name + ".part")
    if target.exists():
        return jsonify({"error": "Une image de ce nom existe deja : supprime-la d'abord."}), 409
    free = shutil.disk_usage(IMAGES_DIR).free
    if length + UPLOAD_RESERVE > free:
        return jsonify({"error": f"Espace insuffisant : {length / 1e9:.1f} Go a envoyer, {max(0, free - UPLOAD_RESERVE) / 1e9:.1f} Go disponibles."}), 507
    if not _upload_lock.acquire(blocking=False):
        return jsonify({"error": "Un autre envoi est en cours."}), 409
    try:
        digest, received = hashlib.sha256(), 0
        with open(part, "wb") as f:
            while True:
                chunk = request.stream.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                if received == 0:
                    bad = magic_error(name, chunk[:512])
                    if bad:
                        return jsonify({"error": bad}), 400
                f.write(chunk)
                digest.update(chunk)
                received += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if received != length:
            return jsonify({"error": "Envoi interrompu : fichier incomplet."}), 400
        if expected and digest.hexdigest() != expected:
            return jsonify({"error": "L'empreinte SHA-256 ne correspond pas : fichier corrompu pendant l'envoi."}), 400
        os.replace(part, target)
        sha_path(name).write_text(digest.hexdigest() + "\n")
        audit("image-upload", image=name, bytes=received, sha256=digest.hexdigest())
        return jsonify({"name": name, "size": received, "sha256": digest.hexdigest()}), 201
    except OSError as exc:  # disque plein, etc.
        return jsonify({"error": f"Ecriture impossible : {exc}"}), 507
    finally:
        _upload_lock.release()
        try:
            part.unlink()
        except OSError:
            pass


@app.route("/api/images/<name>", methods=["DELETE"])
def api_image_delete(name):
    images = {i["name"] for i in flasher.list_images(IMAGES_DIR)}
    if name not in images:  # nom pris dans la liste, jamais un chemin
        return jsonify({"error": "Image introuvable."}), 404
    if _job.get("state") == "running" and _job.get("image") == name:
        return jsonify({"error": "Cette image est en cours d'ecriture."}), 409
    try:
        (IMAGES_DIR / name).unlink()
        sha_path(name).unlink(missing_ok=True)
    except OSError as exc:
        return jsonify({"error": f"Suppression impossible : {exc}"}), 500
    audit("image-delete", image=name)
    return jsonify({"deleted": name})


# --- telechargement d'une image stockee ----------------------------------------------------

@app.route("/api/images/<name>/file")
def api_image_file(name):
    """Fichier de l'image, en piece jointe ; reprise possible (requetes partielles)."""
    if name not in {i["name"] for i in flasher.list_images(IMAGES_DIR)}:   # nom pris dans la liste, jamais un chemin
        return jsonify({"error": "Image introuvable."}), 404
    audit("image-download", image=name)
    return send_file(IMAGES_DIR / name, as_attachment=True, download_name=name, conditional=True,
                     mimetype="application/octet-stream")


# --- import d'une image depuis une URL -----------------------------------------------------------

def _opener() -> urllib.request.OpenerDirector:
    """HTTP(S) uniquement, redirections comprises : jamais file:// ni ftp:// (pas de
    FileHandler/FTPHandler, une redirection vers eux echoue)."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPHandler, urllib.request.HTTPSHandler, urllib.request.HTTPRedirectHandler,
                    urllib.request.HTTPErrorProcessor, urllib.request.HTTPDefaultErrorHandler):
        opener.add_handler(handler())
    return opener


def url_error(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Lien invalide : une adresse http:// ou https:// est attendue."
    return None


def _fetch_update(**fields) -> None:
    _fetch.update(fields)


def _run_fetch(url: str, name: str, expected: str) -> None:
    target, part = IMAGES_DIR / name, IMAGES_DIR / (name + ".part")
    # L'etat final n'est publie QU'APRES le nettoyage et la liberation du verrou : sinon l'interface (ou un
    # test) voit "termine/erreur" alors que le .part existe encore et qu'un nouvel envoi serait refuse.
    outcome = {"state": "error", "error": "Telechargement interrompu de facon inattendue."}
    try:
        request_ = urllib.request.Request(url, headers={"User-Agent": "expolab-sd-flasher"})
        with _opener().open(request_, timeout=FETCH_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            _fetch_update(total=total)
            if total and total > free_for_images():
                raise flasher.FlashError(f"Espace insuffisant : {total / 1e9:.1f} Go a telecharger, {free_for_images() / 1e9:.1f} Go disponibles.")
            digest, received = hashlib.sha256(), 0
            with open(part, "wb") as f:
                while True:
                    if _fetch_cancel.is_set():
                        raise flasher.FlashError("Telechargement annule.")
                    chunk = resp.read(UPLOAD_CHUNK)
                    if not chunk:
                        break
                    if received == 0:
                        bad = magic_error(name, chunk[:512])
                        if bad:
                            raise flasher.FlashError(bad)
                    if free_for_images() < len(chunk):   # taille inconnue d'avance : on surveille en cours de route
                        raise flasher.FlashError("Espace insuffisant sur le serveur : telechargement arrete.")
                    f.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    _fetch_update(received=received)
                f.flush()
                os.fsync(f.fileno())
        if total and received != total:
            raise flasher.FlashError("Telechargement interrompu : fichier incomplet.")
        if received == 0:
            raise flasher.FlashError("Le lien n'a renvoye aucune donnee.")
        if expected and digest.hexdigest() != expected:
            raise flasher.FlashError("L'empreinte SHA-256 ne correspond pas : fichier corrompu ou different de celui attendu.")
        os.replace(part, target)
        sha_path(name).write_text(digest.hexdigest() + "\n")
        audit("image-fetch", image=name, url=url, bytes=received, sha256=digest.hexdigest())
        outcome = {"state": "done", "sha256": digest.hexdigest()}
    except flasher.FlashError as exc:
        outcome = {"state": "error", "error": str(exc)}
    except urllib.error.HTTPError as exc:
        outcome = {"state": "error", "error": f"Le serveur distant a repondu {exc.code} ({exc.reason})."}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        outcome = {"state": "error", "error": f"Telechargement impossible : {reason} (le serveur a-t-il acces a internet ?)"}
    finally:
        part.unlink(missing_ok=True)
        _upload_lock.release()
        _fetch_update(**outcome)


@app.route("/api/images/fetch", methods=["POST"])
def api_image_fetch():
    global _fetch
    body = request.get_json(silent=True) or {}
    url, name = str(body.get("url", "")).strip(), str(body.get("name", "")).strip()
    expected = str(body.get("sha256", "")).strip().lower()
    error = url_error(url) or image_name_error(name)
    if error:
        return jsonify({"error": error}), 400
    if expected and not SHA256_RE.match(expected):
        return jsonify({"error": "Empreinte SHA-256 invalide (64 caracteres hexadecimaux)."}), 400
    if not IMAGES_DIR.is_dir():
        return jsonify({"error": "Dossier des images introuvable."}), 500
    if (IMAGES_DIR / name).exists():
        return jsonify({"error": "Une image de ce nom existe deja : supprime-la d'abord."}), 409
    if not _upload_lock.acquire(blocking=False):
        return jsonify({"error": "Un autre envoi est en cours."}), 409
    _fetch_cancel.clear()
    _fetch = {"state": "running", "url": url, "name": name, "received": 0, "total": 0, "started": int(time.time())}
    threading.Thread(target=_run_fetch, args=(url, name, expected), daemon=True).start()
    return jsonify(_fetch), 202


@app.route("/api/images/fetch", methods=["DELETE"])
def api_image_fetch_cancel():
    """Annule un telechargement en cours, ou efface le resultat du dernier."""
    global _fetch
    if _fetch.get("state") == "running":
        _fetch_cancel.set()
        return jsonify({"cancelling": True})
    _fetch = {"state": "idle"}
    return jsonify(_fetch)


# --- capture d'une carte (image reduite) -------------------------------------------------------

def capture_name(base: str, compression: str) -> tuple[str | None, str | None]:
    """(nom final, erreur) : le nom saisi + l'extension de la compression choisie."""
    if compression not in capture.COMPRESSIONS:
        return None, "Compression inconnue (zst, xz ou none)."
    base = base.strip()
    for suffix in flasher.IMAGE_SUFFIXES:   # "rpi-test.img.zst" saisi en entier : on ne double pas l'extension
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    name = base + capture.COMPRESSIONS[compression]
    return name, image_name_error(name)


@app.route("/api/capture", methods=["POST"])
def api_capture():
    global _job
    body = request.get_json(silent=True) or {}
    device, compression = body.get("device", ""), body.get("compression", "zst")
    prepare = body.get("prepare", True) is not False   # preparation master par defaut
    name, error = capture_name(str(body.get("name", "")), compression)
    if error:
        return jsonify({"error": error}), 400
    if (IMAGES_DIR / name).exists():
        return jsonify({"error": f"Une image {name} existe deja : choisis un autre nom."}), 409
    eligible = {d["path"]: d for d in read_devices().get("eligible", [])}
    if device not in eligible:
        return jsonify({"error": "Carte introuvable ou non eligible (retiree ? deja utilisee par le serveur ?)."}), 409
    if eligible[device].get("mountpoints"):
        return jsonify({"error": "Une partition de la carte est montee : demonte-la avant la capture (copie incoherente sinon)."}), 409
    with _job_lock:
        if _job.get("state") == "running":
            return jsonify({"error": "Une operation sur une carte est deja en cours."}), 409
        _job = {"kind": "capture", "state": "running", "step": "read", "percent": 0, "message": "Demarrage",
                "device": device, "model": eligible[device]["model"], "image": name, "prepare": prepare, "started": int(time.time()),
                "steps": [{"id": s, "label": capture.STEP_LABELS[s]} for s in capture.STEPS]}
    audit("capture-start", device=device, model=eligible[device]["model"], serial=eligible[device]["serial"],
          image=name, compression=compression, prepare=prepare, dry_run=DRY_RUN)
    threading.Thread(target=_run_capture, args=(device, name, compression, eligible[device], prepare), daemon=True).start()
    return jsonify(_job), 202


def _run_capture(device: str, name: str, compression: str, expected: dict, prepare: bool = True) -> None:
    source = device
    try:
        if DRY_RUN:
            # Simulation : la "carte" est le fichier ecrit par un flash simule sur ce peripherique.
            source = str(DRY_RUN_DIR / os.path.basename(device))
            if not os.path.isfile(source):
                raise capture.CaptureError("Simulation : aucune carte simulee pour ce peripherique (flashe-la d'abord en simulation).")
        else:
            now = {d["path"]: d for d in read_devices().get("eligible", [])}.get(device)
            if not now or now["serial"] != expected["serial"] or now["size"] != expected["size"]:
                raise capture.CaptureError("La carte a change ou a ete retiree depuis la verification : rien n'a ete lu.")
        result = capture.run_capture(source=source, work_dir=IMAGES_DIR / f".capture-{name}", output=IMAGES_DIR / name,
                                     compression=compression, update=_update, free_bytes=free_for_images(),
                                     prepare_script=PREPARE_SCRIPT if prepare else None)
        sha_path(name).write_text(result["sha256"] + "\n")
        if not DRY_RUN:
            flasher.eject(device)
        _update(state="done", step="checksum", percent=100, message="Image prete", result=result)
        audit("capture-done", device=device, image=name, bytes=result["bytes"], sha256=result["sha256"])
    except (capture.CaptureError, flasher.FlashError) as exc:
        _update(state="error", error=str(exc))
        audit("capture-error", device=device, image=name, error=str(exc))
    except Exception as exc:  # jamais de thread silencieusement morte : l'interface doit voir l'echec
        _update(state="error", error=f"Erreur inattendue : {exc}")
        audit("capture-error", device=device, image=name, error=repr(exc))


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
        "use_access_key": bool(body.get("use_access_key", current["use_access_key"])),
        "wifi_psk": current["wifi_psk"],
        "user_name": str(body.get("user_name", current["user_name"])).strip(),
        "user_sudo": bool(body.get("user_sudo", current["user_sudo"])),
        "user_pwauth": bool(body.get("user_pwauth", current["user_pwauth"])),
        "user_password": current["user_password"],
        "time_server": str(body.get("time_server", current["time_server"])).strip(),
        "apt_proxy": str(body.get("apt_proxy", current["apt_proxy"])).strip(),
        "keyboard_layout": str(body.get("keyboard_layout", current["keyboard_layout"])).strip().lower(),
        "locale": str(body.get("locale", current["locale"])).strip(),
        "timezone": str(body.get("timezone", current["timezone"])).strip(),
        "packages": parse_packages(body.get("packages", current["packages"])),
        "operator_image": str(body.get("operator_image", current["operator_image"])).strip(),
    }
    if body.get("wifi_psk"):  # vide = on garde l'actuel
        new["wifi_psk"] = str(body["wifi_psk"])
    if body.get("clear_wifi"):
        new["wifi_ssid"] = new["wifi_psk"] = ""
    if body.get("user_password"):
        new["user_password"] = str(body["user_password"])
    if body.get("clear_user_password") or not new["user_name"]:
        new["user_password"] = ""  # pas de compte, pas de mot de passe
    try:
        flasher.check_settings({"wifi_ssid": new["wifi_ssid"], "wifi_psk": new["wifi_psk"], "wifi_country": new["wifi_country"],
                                "authorized_keys": new["extra_keys"], "user_name": new["user_name"],
                                "user_password": new["user_password"], "time_server": new["time_server"],
                                "apt_proxy": new["apt_proxy"], "keyboard_layout": new["keyboard_layout"],
                                "locale": new["locale"], "timezone": new["timezone"], "packages": new["packages"]})
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400
    if new["operator_image"] and new["operator_image"] not in {i["name"] for i in flasher.list_images(IMAGES_DIR)}:
        return jsonify({"error": "Image de l'exploitant introuvable dans la liste des images."}), 400
    save_settings(new)
    audit("settings", wifi_ssid=new["wifi_ssid"], keys=len(new["extra_keys"]), user=new["user_name"])
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
    personal_keys = len(load_settings()["extra_keys"])
    warnings = []
    if not eff["user_name"]:
        warnings.append("Aucun compte utilisateur defini : le compte par defaut de l'image sera cree sans mot de passe.")
    elif not eff["user_password"] and not personal_keys:
        warnings.append("Ni mot de passe ni cle SSH personnelle : seule la cle d'automatisation pourra se connecter.")
    return jsonify(
        {
            "hostname": hostname,
            "wifi": eff["wifi_ssid"] or None,
            "user": eff["user_name"] or None,
            "password": bool(eff["user_password"]),
            "sudo": bool(eff["user_name"] and eff["user_sudo"]),
            "time_server": eff["time_server"] or None,
            "apt_proxy": eff["apt_proxy"] or None,
            "keyboard_layout": eff["keyboard_layout"] or None,
            "locale": eff["locale"] or None,
            "timezone": eff["timezone"] or None,
            "packages": eff["packages"],
            "reboot": bool(eff["wifi_ssid"]),  # redemarrage unique au premier demarrage (active le Wi-Fi)
            "warnings": warnings,
            "keys": len(eff["authorized_keys"]),
            "actions": ["hostname", "machine-id vide", "cles SSH d'hote supprimees", "ejection"],
        }
    )


@app.route("/api/flash", methods=["POST"])
def api_flash():
    body = request.get_json(silent=True) or {}
    device, hostname = body.get("device", ""), body.get("hostname", "")
    try:
        flasher.check_hostname(hostname)
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400
    if body.get("confirm") != hostname:
        return jsonify({"error": "La confirmation ne correspond pas au hostname."}), 400

    image = pick_image(body.get("image"))
    if image is None:
        return jsonify({"error": "Aucune image master disponible."}), 409
    return _start_flash(device, hostname, image, actor="administrateur")


def _start_flash(device: str, hostname: str, image: Path, actor: str):
    """Lance un flash (administrateur ou exploitant) apres les verifications communes."""
    global _job
    # Le peripherique doit etre dans la liste ACTUELLE des disques eligibles : jamais un chemin pris tel quel.
    eligible = {d["path"]: d for d in read_devices().get("eligible", [])}
    if device not in eligible:
        return jsonify({"error": "Carte introuvable ou non eligible (retiree ? deja utilisee par le serveur ?)."}), 409
    settings = effective_settings()
    try:
        flasher.check_settings(settings)
    except flasher.FlashError as exc:
        return jsonify({"error": str(exc)}), 400

    with _job_lock:
        if _job.get("state") == "running":
            return jsonify({"error": "Une operation sur une carte est deja en cours."}), 409
        _job = {"kind": "flash", "state": "running", "step": "write", "percent": 0, "message": "Demarrage", "hostname": hostname,
                "device": device, "model": eligible[device]["model"], "image": image.name, "started": int(time.time()),
                "steps": [{"id": s, "label": flasher.STEP_LABELS[s]} for s in flasher.STEPS]}
    audit("flash-start", device=device, model=eligible[device]["model"], serial=eligible[device]["serial"],
          hostname=hostname, image=image.name, actor=actor, dry_run=DRY_RUN)
    threading.Thread(target=_run, args=(image, device, hostname, settings, eligible[device], actor), daemon=True).start()
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


def _run(image: Path, device: str, hostname: str, settings: dict, expected: dict, actor: str = "") -> None:
    target = device
    if DRY_RUN:
        DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
        target = str(DRY_RUN_DIR / os.path.basename(device))
    try:
        _recheck(device, expected, image)
        result = flasher.run_flash(image_path=image, target=target, hostname=hostname, settings=settings,
                                   dry_run=DRY_RUN, dry_run_dir=DRY_RUN_DIR, update=_update)
        ip = next((m["ip"] for m in machine_names() if m["hostname"] == hostname), None)
        deploy = actor == "exploitant" and request_deploy(hostname, ip, result.get("instance_id", ""))
        _update(state="done", step="eject", percent=100, message="Carte prete", result=result, ip=ip, deploy=deploy)
        audit("flash-done", device=device, hostname=hostname, sha256=result["sha256"], bytes=result["bytes"])
    except flasher.FlashError as exc:
        _update(state="error", error=str(exc))
        audit("flash-error", device=device, hostname=hostname, error=str(exc))
    except Exception as exc:  # jamais de thread silencieusement morte : l'interface doit voir l'echec
        _update(state="error", error=f"Erreur inattendue : {exc}")
        audit("flash-error", device=device, hostname=hostname, error=repr(exc))


# --- interface d'exploitation (compte exploitant, voir le portail) ----------------------------
# Seules ces routes sont ouvertes a l'exploitant (le portail le verifie pour Caddy) : il choisit
# une machine parmi les reservations, l'image et TOUS les reglages sont ceux fixes ici par
# l'administrateur ; rien n'est lu du navigateur d'autre que la carte et le nom de la machine.

def deploy_paths(hostname: str) -> tuple[Path, Path]:
    return DEPLOY_DIR / "requests" / f"{hostname}.json", DEPLOY_DIR / "status" / f"{hostname}.json"


def request_deploy(hostname: str, ip: str | None, instance_id: str) -> bool:
    """Demande a ansible-web d'installer la machine des que CETTE carte aura demarre."""
    if not instance_id or not ip:
        return False
    req_path, _ = deploy_paths(hostname)
    try:
        req_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = req_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hostname": hostname, "ip": ip, "instance_id": instance_id,
                                   "requested_at": int(time.time())}))
        os.replace(tmp, req_path)
    except OSError:
        return False
    audit("deploy-request", hostname=hostname, ip=ip)
    return True


def deploy_state(hostname: str) -> dict:
    """Ce que l'exploitant voit du deploiement : un etat et un pourcentage, rien d'autre."""
    _, status_path = deploy_paths(hostname)
    try:
        status = json.loads(status_path.read_text())
    except (OSError, ValueError):
        status = {}
    state = status.get("state") if status.get("state") in ("waiting", "running", "done", "failed", "timeout", "none") else "waiting"
    return {"state": state, "percent": int(status.get("percent") or 0) if state == "running" else 0}


def forget_deploy(hostname: str) -> None:
    req_path, _ = deploy_paths(hostname)
    req_path.unlink(missing_ok=True)


def operator_image() -> Path | None:
    name = load_settings()["operator_image"]
    return pick_image(name) if name else None


def operator_job() -> dict:
    """Ce que l'exploitant voit de l'operation en cours (jamais les details de l'administrateur)."""
    job = _job
    if job.get("state", "idle") == "idle":
        return {"state": "idle"}
    if job.get("kind") != "flash":
        return {"state": "busy"} if job.get("state") == "running" else {"state": "idle"}
    keep = ("kind", "state", "step", "percent", "hostname", "model", "steps", "ip")
    out = {k: job[k] for k in keep if k in job}
    if job.get("state") == "done" and job.get("deploy"):
        out["deploy"] = deploy_state(job["hostname"])
    if job.get("state") == "error":
        out["error"] = job.get("error", "")
    return out


@app.route("/exploitation")
def exploitation():
    return render_template("exploitation.html")


@app.route("/api/exploitation/status")
def api_exploitation_status():
    devices = read_devices()
    return jsonify({
        "devices": [{"path": d["path"], "model": d["model"], "size": d["size"]} for d in devices.get("eligible", [])],
        "machines": machine_names(),
        "job": operator_job(),
        "image_ready": operator_image() is not None,
        "dry_run": DRY_RUN,
    })


@app.route("/api/exploitation/flash", methods=["POST"])
def api_exploitation_flash():
    body = request.get_json(silent=True) or {}
    hostname, device = str(body.get("hostname", "")), str(body.get("device", ""))
    if hostname not in {m["hostname"] for m in machine_names()}:
        return jsonify({"error": "Machine inconnue : choisis-la dans la liste."}), 400
    image = operator_image()
    if image is None:
        return jsonify({"error": "Aucune image n'a ete choisie par l'administrateur pour l'exploitation."}), 409
    res = _start_flash(device, hostname, image, actor="exploitant")
    if isinstance(res, tuple) and res[1] == 202:
        return jsonify(operator_job()), 202
    return res


@app.route("/api/exploitation/dismiss", methods=["POST"])
def api_exploitation_dismiss():
    """Fermer l'ecran de fin (reussi, echec, ou « Abandonner » quand la machine n'a pas
    demarre). Jamais pendant que la machine est attendue ou s'installe."""
    global _job
    with _job_lock:
        if _job.get("kind") == "flash" and _job.get("state") in ("done", "error"):
            if _job.get("deploy"):
                if deploy_state(_job["hostname"])["state"] in ("waiting", "running"):
                    return jsonify({"error": "L'installation de la machine est en cours."}), 409
                forget_deploy(_job["hostname"])
            _job = {"state": "idle"}
    return jsonify(operator_job())


@app.route("/api/exploitation/deploy/wait", methods=["POST"])
def api_exploitation_deploy_wait():
    """« J'ai verifie, attendre encore » : la machine n'a pas demarre a temps, on relance l'attente."""
    with _job_lock:
        job = _job
        if job.get("kind") != "flash" or job.get("state") != "done" or not job.get("deploy"):
            return jsonify({"error": "Aucune installation en attente."}), 409
        if deploy_state(job["hostname"])["state"] != "timeout":
            return jsonify(operator_job())
        ok = request_deploy(job["hostname"], job.get("ip"), (job.get("result") or {}).get("instance_id", ""))
    if not ok:
        return jsonify({"error": "Impossible de relancer l'attente."}), 500
    return jsonify(operator_job())


@app.route("/api/job/dismiss", methods=["POST"])
def api_job_dismiss():
    global _job
    with _job_lock:
        if _job.get("state") == "running":
            return jsonify({"error": "Une operation sur une carte est en cours."}), 409
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
    remove_stale_parts()
    app.run(host="0.0.0.0", port=5057)
