#!/usr/bin/env python3
"""expolab apt-cache-admin : etat, efficacite, journal, mode et miroirs HTTPS du
cache apt (apt-cacher-ng, stack apt-cache).

Aucune base de donnees : tout vient de fichiers montes dans le conteneur.
  /config  (lecture-ecriture)  stacks/apt-cache/config : `mode` et
           `https-mirrors.conf`, relus par le cache a son demarrage - on les
           modifie puis on redemarre le conteneur (docker restart).
  /logs    (lecture seule)     journal des requetes : epoch|I|octets|client|chemin
           (I = telecharge depuis internet par le cache, O = servi a une machine)
  /cache   (lecture seule)     le cache lui-meme (taille, nombre de paquets)
  /leases  (lecture seule)     baux dnsmasq, pour afficher le NOM des machines
Le socket Docker ne sert qu'a redemarrer le conteneur du cache (meme precedent
que dnsmasq-admin).
"""
import datetime
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CONFIG_DIR = Path(os.environ.get("APT_CACHE_CONFIG_DIR", "/config"))
LOG_FILE = Path(os.environ.get("APT_CACHE_LOG_FILE", "/logs/apt-cacher.log"))
CACHE_DIR = Path(os.environ.get("APT_CACHE_DATA_DIR", "/cache"))
LEASES_FILE = Path(os.environ.get("APT_CACHE_LEASES_FILE", "/leases/dnsmasq.leases"))
CONTAINER = os.environ.get("APT_CACHE_CONTAINER", "expolab-apt-cache")

LOG_TAIL_BYTES = 4 * 1024 * 1024   # on n'analyse que la fin du journal
USAGE_CACHE_SECONDS = 60           # parcourir le cache coute : resultat memorise
MIRROR_RE = re.compile(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$")  # meme regle que entrypoint.sh
MAX_MIRRORS = 50
MODES = ("online", "offline")

MIRRORS_HEADER = """# Depots apt en HTTPS joignables par les machines de l'expo (qui n'ont pas
# internet). Une URL https:// par ligne. Le serveur telecharge le depot en
# HTTPS et le garde ; la machine le demande en HTTP sous la MEME adresse, par
# son proxy habituel (le cache). Seuls les depots listes ici sont joignables.
# Exemple :
#   https://download.docker.com/linux/debian
# puis, dans le role Ansible, une ligne de depot en http:// (et non https://) :
#   deb [signed-by=/etc/apt/keyrings/docker.asc] http://download.docker.com/linux/debian bookworm stable
# La cle GPG se recupere par le meme chemin : http://download.docker.com/linux/debian/gpg
# Modifiable depuis l'interface "Cache apt" du portail (bouton Appliquer) ou
# a la main, puis : sudo ./server/apt-cache-mode.sh restart
"""

_usage_cache: dict = {"at": 0.0, "value": None}
_lock = threading.Lock()  # un seul redemarrage / ecriture de config a la fois


# --- journal -----------------------------------------------------------------

def parse_line(line: str) -> dict | None:
    """`epoch|I|octets|client|chemin` -> dict, ou None si la ligne n'est pas valide."""
    parts = line.rstrip("\n").split("|", 4)
    if len(parts) != 5 or parts[1] not in ("I", "O"):
        return None
    try:
        return {"time": int(parts[0]), "dir": parts[1], "bytes": int(parts[2]), "client": parts[3], "path": parts[4]}
    except ValueError:
        return None


def is_package(path: str) -> bool:
    return path.endswith((".deb", ".udeb"))


def read_entries(path: Path = None, max_bytes: int = LOG_TAIL_BYTES) -> tuple[list[dict], int]:
    """Entrees de la FIN du journal (la plus ancienne peut etre tronquee, on la
    jette) et taille totale du fichier."""
    path = path or LOG_FILE
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # ligne probablement coupee
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return [], 0
    entries = [e for e in map(parse_line, text.splitlines()) if e]
    return entries, size


def load_names(path: Path = None) -> dict[str, str]:
    """ip -> nom de machine, d'apres les baux dnsmasq (vide si indisponible)."""
    path = path or LEASES_FILE
    names = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] != "*":
                names[parts[2]] = parts[3]
    except OSError:
        pass
    return names


def iso(epoch: int | None) -> str | None:
    return datetime.datetime.fromtimestamp(epoch).isoformat(timespec="seconds") if epoch else None


def compute_stats(entries: list[dict], names: dict[str, str]) -> dict:
    """Efficacite du cache. Une requete servie a une machine produit une ligne O ;
    si le cache a du telecharger le paquet, une ligne I la precede. Donc :
    paquets servis = lignes O (.deb), telecharges = lignes I (.deb), le reste
    est servi directement par le cache."""
    served = downloaded = served_bytes = downloaded_bytes = 0
    clients: dict[str, dict] = {}
    for e in entries:
        if not is_package(e["path"]):
            continue
        if e["dir"] == "I":
            downloaded += 1
            downloaded_bytes += e["bytes"]
            continue
        served += 1
        served_bytes += e["bytes"]
        c = clients.setdefault(e["client"], {"client": e["client"], "name": names.get(e["client"], ""), "requests": 0, "bytes": 0, "last": 0})
        c["requests"] += 1
        c["bytes"] += e["bytes"]
        c["last"] = max(c["last"], e["time"])
    hits = max(0, served - downloaded)
    times = [e["time"] for e in entries]
    return {
        "requests": served,
        "downloads": downloaded,
        "hits": hits,
        "hit_ratio": round(hits / served, 3) if served else None,
        "served_bytes": served_bytes,
        "downloaded_bytes": downloaded_bytes,
        "saved_bytes": max(0, served_bytes - downloaded_bytes),
        "since": iso(min(times)) if times else None,
        "until": iso(max(times)) if times else None,
        "clients": [
            dict(c, last=iso(c["last"]))
            for c in sorted(clients.values(), key=lambda c: c["last"], reverse=True)
        ],
    }


PAIR_WINDOW = 15  # secondes : une ligne I suivie d'une ligne O pour le meme fichier = un seul acces


def group_events(entries: list[dict]) -> list[dict]:
    """Regroupe le journal en acces lisibles. Une machine ne parle QU'AU cache :
    quand il n'a pas le fichier, il le telecharge d'abord (ligne I, c'est LUI qui
    sort sur internet) puis le sert (ligne O). Donc :
      miss  = I puis O pour le meme fichier : le cache est alle le chercher pour la machine
      hit   = O seul : servi directement depuis le cache
      fetch = I sans O : le cache a recupere un fichier sans qu'une machine l'attende (index...)"""
    pending: dict[tuple[str, str], list[dict]] = {}
    events: list[dict] = []
    for e in entries:
        key = (e["client"], e["path"])
        if e["dir"] == "I":
            fetch = {"time": e["time"], "kind": "fetch", "bytes": e["bytes"], "client": e["client"], "path": e["path"]}
            pending.setdefault(key, []).append(fetch)
            events.append(fetch)
            continue
        waiting = [f for f in pending.get(key, []) if e["time"] - f["time"] <= PAIR_WINDOW]
        if waiting:
            fetch = waiting[-1]
            pending[key].remove(fetch)
            fetch.update(kind="miss", bytes=e["bytes"])  # taille servie a la machine
        else:
            events.append({"time": e["time"], "kind": "hit", "bytes": e["bytes"], "client": e["client"], "path": e["path"]})
    return events


def recent_entries(entries: list[dict], names: dict[str, str], limit: int, client: str, query: str, only_packages: bool) -> list[dict]:
    query = query.lower()
    out = []
    for e in reversed(group_events(entries)):
        if only_packages and not is_package(e["path"]):
            continue
        name = names.get(e["client"], "")
        if client and client.lower() not in e["client"].lower() and client.lower() not in name.lower():
            continue
        if query and query not in e["path"].lower():
            continue
        out.append({"time": iso(e["time"]), "kind": e["kind"], "bytes": e["bytes"], "client": e["client"], "name": name, "path": e["path"]})
        if len(out) >= limit:
            break
    return out


# --- cache, disque, conteneur ---------------------------------------------------

def cache_usage(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if _usage_cache["value"] is not None and now - _usage_cache["at"] < USAGE_CACHE_SECONDS:
        return _usage_cache["value"]
    packages = total = 0
    for root, _dirs, files in os.walk(CACHE_DIR, followlinks=False):
        for name in files:
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            total += size
            if is_package(name):
                packages += 1
    _usage_cache.update(at=now, value={"packages": packages, "bytes": total})
    return _usage_cache["value"]


def disk_usage() -> dict:
    try:
        usage = shutil.disk_usage(CACHE_DIR)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def read_mode() -> str:
    try:
        mode = (CONFIG_DIR / "mode").read_text().strip()
    except OSError:
        return "online"
    return mode if mode in MODES else "online"


def container_running() -> bool | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "true"


def restart_cache() -> tuple[bool, str]:
    try:
        result = subprocess.run(["docker", "restart", CONTAINER], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, result.stderr.strip() or "docker restart a echoue"
    return True, ""


# --- miroirs HTTPS ---------------------------------------------------------------

def read_mirrors() -> list[str]:
    try:
        lines = (CONFIG_DIR / "https-mirrors.conf").read_text().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def check_mirrors(mirrors) -> tuple[list[str] | None, str | None]:
    if not isinstance(mirrors, list):
        return None, "Liste de miroirs attendue."
    cleaned = []
    for raw in mirrors:
        url = str(raw).strip().rstrip("/")
        if not url:
            continue
        if not MIRROR_RE.match(url):
            return None, f"URL invalide : '{raw}' (https://hote/chemin attendu, caracteres simples)."
        if url in cleaned:
            return None, f"Miroir en double : {url}"
        cleaned.append(url)
    if len(cleaned) > MAX_MIRRORS:
        return None, f"Trop de miroirs ({MAX_MIRRORS} maximum)."
    return cleaned, None


def write_mirrors(mirrors: list[str]) -> None:
    # Ecriture dans le fichier lui-meme (le conteneur du cache monte le dossier).
    (CONFIG_DIR / "https-mirrors.conf").write_text(MIRRORS_HEADER + "".join(f"{url}\n" for url in mirrors))


# --- API ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    entries, log_size = read_entries()
    stats = compute_stats(entries, load_names())
    return jsonify(
        {
            "mode": read_mode(),
            "container": {"running": container_running()},
            "disk": disk_usage(),
            "cache": cache_usage(),
            "efficiency": {k: v for k, v in stats.items() if k != "clients"} | {"log_bytes": log_size},
            "clients": stats["clients"],
        }
    )


@app.route("/api/log")
def api_log():
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except ValueError:
        limit = 100
    entries, _ = read_entries()
    only_packages = request.args.get("all") != "1"
    return jsonify(
        {
            "entries": recent_entries(
                entries, load_names(), limit, request.args.get("client", "").strip(), request.args.get("q", "").strip(), only_packages
            )
        }
    )


def _apply(change_message: str, action):
    """Execute `action` (ecriture de config) puis redemarre le cache ; un seul a la fois."""
    if not _lock.acquire(blocking=False):
        return jsonify({"error": "Une operation est deja en cours."}), 409
    try:
        action()
        ok, err = restart_cache()
    finally:
        _lock.release()
    if not ok:
        return jsonify({"error": f"{change_message} enregistre, mais le redemarrage du cache a echoue : {err}"}), 502
    return jsonify({"ok": True, "running": container_running()})


@app.route("/api/mode", methods=["PUT"])
def api_mode():
    mode = (request.get_json(force=True, silent=True) or {}).get("mode")
    if mode not in MODES:
        return jsonify({"error": "mode doit valoir online ou offline"}), 400
    return _apply("Mode", lambda: (CONFIG_DIR / "mode").write_text(mode + "\n"))


@app.route("/api/restart", methods=["POST"])
def api_restart():
    return _apply("Redemarrage", lambda: None)


@app.route("/api/mirrors", methods=["GET"])
def api_mirrors_get():
    return jsonify({"mirrors": read_mirrors()})


@app.route("/api/mirrors", methods=["PUT"])
def api_mirrors_put():
    body = request.get_json(force=True, silent=True) or {}
    mirrors, error = check_mirrors(body.get("mirrors"))
    if error:
        return jsonify({"error": error}), 400
    if not body.get("apply"):
        if not _lock.acquire(blocking=False):
            return jsonify({"error": "Une operation est deja en cours."}), 409
        try:
            write_mirrors(mirrors)
        finally:
            _lock.release()
        return jsonify({"ok": True, "applied": False, "mirrors": mirrors})
    response = _apply("Miroirs", lambda: write_mirrors(mirrors))
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5056, threaded=True)
