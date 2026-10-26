#!/usr/bin/env python3
"""expolab git-mirror : mirroir git minimaliste (pas un forge).

Garde des clones bare (`git clone --mirror`) de depots distants (GitLab,
GitHub, etc.) a jour, avec sync manuel ou programme. Prefere a un outil
plus lourd type Gitea, volontairement laisse de cote pour ce besoin
(mirroring uniquement, pas de navigation de fichiers/PR/issues).

Contrairement a dnsmasq-admin/caddy-admin/vpn-admin, cette app ne pilote
pas un autre conteneur deja present - elle effectue elle-meme le travail
git (clone/fetch), en plus de servir l'UI.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file

import releases

app = Flask(__name__)

DATA_DIR = Path("/data")
REPOS_DIR = DATA_DIR / "repos"
CONFIG_PATH = DATA_DIR / "mirrors.yaml"
# Releases GitHub des mirroirs (programmes des machines, voir releases.py).
RELEASES_DIR = DATA_DIR / "releases"
_release_errors: dict = {}   # nom -> derniere erreur de recuperation des releases (le git reste a jour)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
GIT_TIMEOUT = 120  # clone/fetch peuvent prendre du temps sur un gros depot
LS_REMOTE_TIMEOUT = 15  # simple verification "a jour ?", doit rester rapide

INTERVAL_CHOICES = {0: None, 60: "1h", 360: "6h", 1440: "24h"}
# Verification reguliere "a jour ?" (ls-remote, sans fetch) de chaque mirroir, en arriere-plan :
# la page lit le dernier resultat au lieu de joindre chaque depot distant a chaque affichage.
CHECK_INTERVAL = int(os.environ.get("GIT_MIRROR_CHECK_INTERVAL", "300"))

_status: dict = {}            # nom -> resultat de la derniere verification (voir check_mirror)
_status_lock = threading.Lock()


def mask_url(url: str) -> str:
    """Masque un eventuel token/mot de passe integre dans l'URL pour l'affichage."""
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def load_mirrors() -> dict:
    if not CONFIG_PATH.exists():
        return {"mirrors": []}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {"mirrors": []}


def save_mirrors(data: dict) -> None:
    # Ecriture atomique (fichier temporaire + rename), meme precaution que
    # dnsmasq-admin/caddy-admin : evite un fichier tronque si le process
    # est interrompu en cours d'ecriture.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)


def find_mirror(data: dict, name: str) -> dict | None:
    return next((m for m in data.get("mirrors", []) if m["name"] == name), None)


def repo_path(name: str) -> Path:
    return REPOS_DIR / f"{name}.git"


def clone_url(name: str) -> str:
    # https:// code en dur (pas request.scheme) : ce conteneur repond en
    # HTTP simple, la terminaison TLS se fait chez Caddy en amont (meme
    # convention que tout le reste du lab) - request.scheme refleterait
    # "http" en interne, jamais ce que l'utilisateur tape reellement.
    return f"https://{request.host}/git/{name}.git"


def run_git(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def sync_mirror(name: str) -> tuple[bool, str]:
    """git fetch --prune sur le bare local, met a jour last_synced si ok."""
    data = load_mirrors()
    mirror = find_mirror(data, name)
    if mirror is None:
        return False, f"'{name}' introuvable"

    try:
        result = run_git(["-C", str(repo_path(name)), "fetch", "--prune"], GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "Timeout pendant le fetch"

    if result.returncode != 0:
        return False, result.stderr.strip() or "Echec du fetch"

    mirror["last_synced"] = datetime.now(timezone.utc).isoformat()
    save_mirrors(data)
    check_mirror(mirror)
    sync_mirror_releases(mirror)
    return True, ""


def sync_mirror_releases(mirror: dict) -> None:
    """Recupere les releases (depot GitHub) ; une erreur est retenue pour la page mais ne
    fait jamais echouer la synchronisation du git."""
    try:
        releases.sync_releases(mirror["name"], mirror["remote_url"], RELEASES_DIR)
        _release_errors.pop(mirror["name"], None)
    except Exception as exc:  # noqa: BLE001 - reseau, droits du jeton, disque plein...
        _release_errors[mirror["name"]] = f"Releases non recuperees : {exc.__class__.__name__}"


def local_commit(name: str) -> dict | None:
    """Commit actuel (HEAD) du clone local : empreinte, message, date. None si illisible."""
    try:
        result = run_git(["-C", str(repo_path(name)), "log", "-1", "--format=%H%x1f%s%x1f%cI", "HEAD"], 10)
    except subprocess.TimeoutExpired:
        return None
    parts = result.stdout.strip().split("\x1f")
    if result.returncode != 0 or len(parts) != 3:
        return None
    return {"sha": parts[0], "short": parts[0][:7], "subject": parts[1], "date": parts[2]}


def remote_head(url: str) -> str | None:
    """Empreinte du HEAD distant, sans fetch. None si injoignable."""
    try:
        result = run_git(["ls-remote", url, "HEAD"], LS_REMOTE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def check_mirror(mirror: dict) -> dict:
    """Compare le HEAD distant au HEAD local et retient le resultat. up_to_date : True, False,
    ou None si le distant (ou le clone local) est illisible."""
    commit = local_commit(mirror["name"])
    remote_sha = remote_head(mirror["remote_url"])
    status = {
        "commit": commit,
        "remote_sha": remote_sha,
        "up_to_date": None if commit is None or remote_sha is None else commit["sha"] == remote_sha,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    with _status_lock:
        _status[mirror["name"]] = status
    return status


def mirror_status(mirror: dict) -> dict:
    with _status_lock:
        cached = _status.get(mirror["name"])
    return cached or check_mirror(mirror)


def scheduler_loop() -> None:
    """Thread de fond : declenche un sync pour les mirroirs dont l'intervalle est ecoule, et
    verifie toutes les CHECK_INTERVAL s si chaque mirroir est a jour."""
    last_check = 0.0
    while True:
        try:
            data = load_mirrors()
            if time.monotonic() - last_check >= CHECK_INTERVAL:
                last_check = time.monotonic()
                for mirror in data.get("mirrors", []):
                    check_mirror(mirror)
            now = datetime.now(timezone.utc)
            for mirror in data.get("mirrors", []):
                interval = mirror.get("interval_minutes")
                if not interval:
                    continue
                last = mirror.get("last_synced")
                due = True
                if last:
                    last_dt = datetime.fromisoformat(last)
                    due = (now - last_dt) >= timedelta(minutes=interval)
                if due:
                    sync_mirror(mirror["name"])
        except Exception:
            # Ne jamais laisser une erreur ponctuelle (reseau, depot
            # supprime entre-temps...) tuer le thread de fond.
            pass
        time.sleep(60)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/mirrors", methods=["GET"])
def api_mirrors_list():
    data = load_mirrors()
    mirrors = []
    for m in data.get("mirrors", []):
        status = mirror_status(m)
        mirrors.append({
            "name": m["name"],
            "remote_url": mask_url(m["remote_url"]),
            "clone_url": clone_url(m["name"]),
            "interval_minutes": m.get("interval_minutes") or 0,
            "last_synced": m.get("last_synced"),
            "up_to_date": status["up_to_date"],
            "commit": status["commit"],
            "remote_short": (status["remote_sha"] or "")[:7] or None,
            "checked_at": status["checked_at"],
            "releases": [{"tag": r["tag"], "published_at": r["published_at"]}
                         for r in releases.read_index(RELEASES_DIR, m["name"])],
            "release_error": _release_errors.get(m["name"]),
        })
    return jsonify({"mirrors": mirrors, "check_interval": CHECK_INTERVAL})


@app.route("/api/mirrors", methods=["POST"])
def api_mirrors_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip().lower()
    remote_url = (body.get("remote_url") or "").strip()
    interval_minutes = body.get("interval_minutes") or 0

    if not NAME_RE.match(name):
        return jsonify({"error": "Nom invalide (minuscules/chiffres/tirets, 3-32 caracteres, doit commencer par une lettre)"}), 400
    if not remote_url:
        return jsonify({"error": "URL distante requise"}), 400
    if interval_minutes not in INTERVAL_CHOICES:
        return jsonify({"error": "Intervalle invalide"}), 400

    data = load_mirrors()
    if find_mirror(data, name) is not None:
        return jsonify({"error": f"'{name}' existe deja"}), 409

    dest = repo_path(name)
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = run_git(["clone", "--mirror", remote_url, str(dest)], GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return jsonify({"error": "Timeout pendant le clone"}), 502
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return jsonify({"error": result.stderr.strip() or "Echec du clone"}), 502

    data.setdefault("mirrors", []).append({
        "name": name,
        "remote_url": remote_url,
        "interval_minutes": interval_minutes or None,
        "last_synced": datetime.now(timezone.utc).isoformat(),
    })
    save_mirrors(data)
    check_mirror(data["mirrors"][-1])
    sync_mirror_releases(data["mirrors"][-1])
    return jsonify({"ok": True}), 201


@app.route("/api/mirrors/<name>", methods=["PUT"])
def api_mirrors_update(name: str):
    body = request.get_json(force=True, silent=True) or {}
    remote_url = (body.get("remote_url") or "").strip()
    interval_minutes = body.get("interval_minutes") or 0

    if not remote_url:
        return jsonify({"error": "URL distante requise"}), 400
    if interval_minutes not in INTERVAL_CHOICES:
        return jsonify({"error": "Intervalle invalide"}), 400

    data = load_mirrors()
    mirror = find_mirror(data, name)
    if mirror is None:
        return jsonify({"error": f"'{name}' introuvable"}), 404

    if remote_url != mirror["remote_url"]:
        result = run_git(["-C", str(repo_path(name)), "remote", "set-url", "origin", remote_url], 10)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip() or "Echec de la mise a jour de l'URL"}), 502
        mirror["remote_url"] = remote_url

    mirror["interval_minutes"] = interval_minutes or None
    save_mirrors(data)
    check_mirror(mirror)
    return jsonify({"ok": True})


@app.route("/api/mirrors/<name>", methods=["DELETE"])
def api_mirrors_delete(name: str):
    data = load_mirrors()
    mirror = find_mirror(data, name)
    if mirror is None:
        return jsonify({"error": f"'{name}' introuvable"}), 404

    shutil.rmtree(repo_path(name), ignore_errors=True)
    shutil.rmtree(RELEASES_DIR / name, ignore_errors=True)
    _release_errors.pop(name, None)
    data["mirrors"].remove(mirror)
    save_mirrors(data)
    with _status_lock:
        _status.pop(name, None)
    return jsonify({"ok": True})


@app.route("/api/mirrors/<name>/sync", methods=["POST"])
def api_mirrors_sync(name: str):
    ok, err = sync_mirror(name)
    if not ok:
        return jsonify({"error": err}), 502 if "introuvable" not in err else 404
    return jsonify({"ok": True, "release_error": _release_errors.get(name)})


@app.route("/api/mirrors/<name>/releases", methods=["GET"])
def api_mirror_releases(name: str):
    """Index des releases recuperees (les plus recentes d'abord) : ce que lit ansible-web."""
    if not NAME_RE.match(name) or find_mirror(load_mirrors(), name) is None:
        return jsonify({"error": f"'{name}' introuvable"}), 404
    return jsonify({"releases": releases.read_index(RELEASES_DIR, name)})


@app.route("/releases/<name>/<tag>/<asset>", methods=["GET"])
def release_asset(name: str, tag: str, asset: str):
    """Une archive de release, seulement si elle figure dans l'index du mirroir."""
    if not NAME_RE.match(name):
        return jsonify({"error": "Introuvable"}), 404
    path = releases.asset_path(RELEASES_DIR, name, tag, asset)
    if path is None:
        return jsonify({"error": "Introuvable"}), 404
    return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=asset)


# Protocole HTTP intelligent de git (smart HTTP), pour que les mirroirs
# soient directement clonables (`git clone https://.../git/<nom>.git`) -
# sans ca, ils n'etaient accessibles qu'en passant par SSH sur le fichier
# bare directement. `git http-backend` est le binaire officiel de git
# pour ce protocole (meme chose qu'Apache/nginx+fcgi utiliseraient) - on
# l'invoque ici comme un CGI classique (variables d'environnement +
# stdin/stdout) plutot que de reimplementer le protocole. Lecture seule
# par defaut (git-receive-pack/push reste desactive tant que
# http.receivepack n'est pas active sur un repo - jamais fait ici, ces
# mirroirs ne sont pas censes recevoir de push).
GIT_HTTP_BACKEND_TIMEOUT = 300  # clone initial d'un gros depot peut prendre du temps


@app.route("/git/<path:git_path>", methods=["GET", "POST"])
def git_http_backend(git_path: str):
    env = {
        **os.environ,
        "GIT_PROJECT_ROOT": str(REPOS_DIR),
        "GIT_HTTP_EXPORT_ALL": "1",
        "REQUEST_METHOD": request.method,
        "PATH_INFO": "/" + git_path,
        "QUERY_STRING": request.query_string.decode(),
        "CONTENT_TYPE": request.content_type or "",
        "CONTENT_LENGTH": str(request.content_length or 0),
        "REMOTE_ADDR": request.remote_addr or "",
        "GATEWAY_INTERFACE": "CGI/1.1",
        "SERVER_PROTOCOL": "HTTP/1.1",
    }
    try:
        result = subprocess.run(
            ["git", "http-backend"],
            input=request.get_data(),
            capture_output=True,
            env=env,
            timeout=GIT_HTTP_BACKEND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "Timeout", 504

    raw = result.stdout
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in raw:
            header_block, body = raw.split(sep, 1)
            break
    else:
        return result.stderr.decode(errors="replace") or "Erreur git http-backend", 500

    status_code = 200
    headers = []
    for line in header_block.replace(b"\r\n", b"\n").split(b"\n"):
        key, _, value = line.decode(errors="replace").partition(":")
        key, value = key.strip(), value.strip()
        if key.lower() == "status":
            status_code = int(value.split()[0])
        elif key.lower() not in ("content-length", "transfer-encoding"):
            headers.append((key, value))

    return Response(body, status=status_code, headers=headers)


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5054)
