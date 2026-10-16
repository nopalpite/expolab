#!/usr/bin/env python3
"""expolab portal : point d'entree unique vers tous les services du lab.

Trois roles dans un seul petit service :
  - connexion : une page de login, un cookie de session signe valable pour tout
    *.<domaine public> (donc pour chaque service affiche dans un cadre) ;
  - verification pour Caddy (`forward_auth` -> /auth/verify) : accepte le cookie
    OU un basic auth (curl -u, clients git...), refuse sinon ;
  - interface : tiroir de services, onglets, services affiches dans des cadres.

Les identifiants sont ceux de l'installation (stacks/caddy/auth.env, monte en
lecture seule) : un seul couple pour tout. Le hash bcrypt est celui que Caddy
utilisait pour son basic auth.

Second compte, facultatif : l'EXPLOITANT (stacks/dashboard/operator.env, gere depuis
caddy-admin). Il ne voit que l'interface d'exploitation - plans des salles en lecture
seule (plans.py) et preparation des cartes SD (page /exploitation du flasheur) ; tout le
reste lui est refuse, y compris par /auth/verify pour Caddy.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse

import bcrypt
import yaml
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file

import plans

app = Flask(__name__)

AUTH_ENV = Path(os.environ.get("PORTAL_AUTH_ENV", "/config/auth.env"))
OPERATOR_ENV = Path(os.environ.get("PORTAL_OPERATOR_ENV", "/config/operator.env"))
SERVICES_FILE = Path(os.environ.get("PORTAL_SERVICES_FILE", "/config/services.yaml"))
CA_FILE = Path(os.environ.get("PORTAL_CA_FILE", "/caddy-data/caddy/pki/authorities/local/root.crt"))
DOMAIN = os.environ.get("PORTAL_DOMAIN", "web.expolab.lan")
TLS_MODE = os.environ.get("PORTAL_TLS_MODE", "internal")
COOKIE_SECURE = os.environ.get("PORTAL_COOKIE_SECURE", "1") != "0"

COOKIE_NAME = "expolab_session"
SESSION_SECONDS = 8 * 3600
LOGIN_LIMIT = 5          # echecs...
LOGIN_WINDOW = 300       # ...par IP sur cette fenetre (secondes)
BASIC_CACHE_SECONDS = 300
STATUS_CACHE_SECONDS = 10
PORTAL_NAME = "dashboard"  # le portail lui-meme : jamais dans sa propre liste
ADMIN, OPERATOR = "admin", "exploitant"
# Ce que l'exploitant peut atteindre derriere Caddy : la page d'exploitation du flasheur.
OPERATOR_SERVICE = "sd-flasher"
OPERATOR_PATHS = ("/exploitation", "/api/exploitation/", "/static/")

_failures: dict[str, list[float]] = {}
_basic_cache: dict[str, float] = {}
_status_cache: dict = {"at": 0.0, "values": {}}
_creds_cache: dict = {"mtime": None, "value": None}
_operator_cache: dict = {"mtime": None, "value": None}


# --- identifiants ----------------------------------------------------------

def credentials() -> tuple[str, str] | None:
    """(utilisateur, hash bcrypt) lus dans auth.env (relu si le fichier change).
    Lu a la main : le hash contient des `$`."""
    try:
        mtime = AUTH_ENV.stat().st_mtime
    except OSError:
        return None
    if _creds_cache["mtime"] != mtime:
        values = {}
        for line in AUTH_ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        user, pw_hash = values.get("BASIC_AUTH_USER"), values.get("BASIC_AUTH_HASH")
        _creds_cache.update(mtime=mtime, value=(user, pw_hash) if user and pw_hash else None)
    return _creds_cache["value"]


def operator_credentials() -> tuple[str, str] | None:
    """(utilisateur, hash bcrypt) du compte exploitant, None s'il n'y en a pas."""
    try:
        mtime = OPERATOR_ENV.stat().st_mtime
    except OSError:
        return None
    if _operator_cache["mtime"] != mtime:
        values = {}
        for line in OPERATOR_ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        user, pw_hash = values.get("OPERATOR_USER"), values.get("OPERATOR_HASH")
        _operator_cache.update(mtime=mtime, value=(user, pw_hash) if user and pw_hash else None)
    return _operator_cache["value"]


def creds_for(role: str) -> tuple[str, str] | None:
    return credentials() if role == ADMIN else operator_credentials() if role == OPERATOR else None


def password_ok(user: str, password: str, creds: tuple[str, str]) -> bool:
    expected_user, pw_hash = creds
    # bcrypt est toujours evalue (meme si l'identifiant est faux) : meme duree
    # de reponse dans les deux cas.
    try:
        hash_ok = bcrypt.checkpw(password.encode(), pw_hash.encode())
    except ValueError:
        hash_ok = False
    return hmac.compare_digest(user.encode(), expected_user.encode()) and hash_ok


# --- cookie de session -----------------------------------------------------

def _secret() -> bytes:
    secret = os.environ.get("PORTAL_SECRET", "")
    if len(secret) < 16:
        raise RuntimeError("PORTAL_SECRET manquant ou trop court")
    return secret.encode()


def _signing_key(creds: tuple[str, str]) -> bytes:
    # Derivee aussi du hash du mot de passe : changer de mot de passe invalide
    # toutes les sessions en cours.
    return hmac.new(_secret(), creds[1].encode(), hashlib.sha256).digest()


def make_token(user: str, creds: tuple[str, str], now: float | None = None, role: str = ADMIN) -> str:
    """Le role est dans le jeton, signe avec le hash du mot de passe DE CE ROLE : changer le
    mot de passe de l'exploitant ne ferme que les sessions exploitant."""
    now = time.time() if now is None else now
    payload = json.dumps({"u": user, "r": role, "exp": int(now) + SESSION_SECONDS}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sig = hmac.new(_signing_key(creds), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str, creds: tuple[str, str], now: float | None = None) -> str | None:
    """Jeton ADMINISTRATEUR valide pour `creds` -> utilisateur (compatibilite : sans role = admin)."""
    session = read_session(token, now, admin=creds, operator=None)
    return session[0] if session and session[1] == ADMIN else None


def read_session(token: str, now: float | None = None, admin=None, operator=None) -> tuple[str, str] | None:
    """(utilisateur, role) si le jeton est valide, None sinon. Le role annonce choisit la cle de
    verification : un jeton exploitant ne peut pas se faire passer pour un jeton administrateur."""
    now = time.time() if now is None else now
    body, _, sig = token.partition(".")
    if not body:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    role = payload.get("r", ADMIN)
    creds = admin if role == ADMIN else operator if role == OPERATOR else None
    if creds is None:
        return None
    expected = hmac.new(_signing_key(creds), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected) or payload.get("exp", 0) < now or payload.get("u") != creds[0]:
        return None
    return payload["u"], role


def basic_ok(header: str, creds: tuple[str, str]) -> bool:
    """Basic auth (curl -u, clients git). bcrypt est lent (cout 14) : un succes
    est memorise quelques minutes, indexe par un hash de l'en-tete."""
    if not header.lower().startswith("basic "):
        return False
    key = hashlib.sha256((header + creds[1]).encode()).hexdigest()
    now = time.time()
    if _basic_cache.get(key, 0) > now:
        return True
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    if password_ok(user, password, creds):
        _basic_cache[key] = now + BASIC_CACHE_SECONDS
        return True
    return False


def authenticated_user(creds: tuple[str, str]) -> str | None:
    """Administrateur connecte (cookie ou basic auth) -> son identifiant."""
    session = current_session(creds)
    return session[0] if session and session[1] == ADMIN else None


def current_session(creds: tuple[str, str] | None = None) -> tuple[str, str] | None:
    """(utilisateur, role) de la requete : cookie (administrateur ou exploitant) ou basic
    auth (administrateur seulement : curl, clients git)."""
    creds = credentials() if creds is None else creds
    token = request.cookies.get(COOKIE_NAME)
    if token:
        session = read_session(token, admin=creds, operator=operator_credentials())
        if session:
            return session
    if creds and basic_ok(request.headers.get("Authorization", ""), creds):
        return creds[0], ADMIN
    return None


def operator_may_reach(host: str, uri: str) -> bool:
    """L'exploitant, derriere Caddy : uniquement la page d'exploitation du flasheur."""
    path = urlparse(uri or "/").path or "/"
    if "/../" in path or path.endswith("/.."):
        return False
    return host.lower() == f"{OPERATOR_SERVICE}.{DOMAIN}" and (path == "/exploitation" or path.startswith(OPERATOR_PATHS))


# --- limitation des tentatives --------------------------------------------

def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or request.remote_addr or "?"


def too_many_failures(ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    recent = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW]
    _failures[ip] = recent
    return len(recent) >= LOGIN_LIMIT


def record_failure(ip: str, now: float | None = None) -> None:
    _failures.setdefault(ip, []).append(time.time() if now is None else now)


# --- redirections ----------------------------------------------------------

def portal_host() -> str:
    return f"{PORTAL_NAME}.{DOMAIN}"


def safe_next(target: str | None) -> str:
    """Adresse de retour apres connexion : uniquement le portail ou l'un de ses
    sous-domaines (jamais un site tiers : pas de redirection ouverte)."""
    if not target:
        return "/"
    parsed = urlparse(target)
    if not parsed.scheme and not parsed.netloc:
        return target if target.startswith("/") and not target.startswith("//") else "/"
    host = parsed.hostname or ""
    if parsed.scheme in ("http", "https") and (host == DOMAIN or host.endswith("." + DOMAIN)):
        return target
    return "/"


def login_url(next_url: str | None = None) -> str:
    base = f"https://{portal_host()}/login"
    return f"{base}?next={quote(next_url, safe='')}" if next_url else base


def original_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("X-Forwarded-Host", portal_host())
    return f"{proto}://{host}{request.headers.get('X-Forwarded-Uri', '/')}"


def set_session_cookie(resp: Response, token: str | None) -> Response:
    kwargs = dict(path="/", secure=COOKIE_SECURE, httponly=True, samesite="Lax")
    if "." in DOMAIN:
        kwargs["domain"] = DOMAIN
    if token:
        resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, **kwargs)
    else:
        resp.set_cookie(COOKIE_NAME, "", max_age=0, expires=0, **kwargs)
    return resp


@app.after_request
def security_headers(resp: Response) -> Response:
    # Le portail ne doit jamais etre affiche dans le cadre d'un autre site.
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.path != "/ca.crt" and not request.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# --- verification pour Caddy ----------------------------------------------

@app.route("/auth/verify")
def auth_verify():
    creds = credentials()
    if creds is None:
        return Response("Identifiants d'administration introuvables", 503)  # ferme par defaut
    session = current_session(creds)
    if session and session[1] == ADMIN:
        return Response("", 204)
    wants_page_ = "text/html" in request.headers.get("Accept", "") and request.headers.get("X-Forwarded-Method", "GET") == "GET"
    if session:   # exploitant : seulement l'interface d'exploitation
        if operator_may_reach(request.headers.get("X-Forwarded-Host", ""), request.headers.get("X-Forwarded-Uri", "/")):
            return Response("", 204)
        if wants_page_:
            return redirect(f"https://{portal_host()}/", 302)
        return Response("Acces reserve a l'administrateur\n", 403)
    wants_page = "text/html" in request.headers.get("Accept", "") and request.headers.get("X-Forwarded-Method", "GET") == "GET"
    if wants_page:
        return redirect(login_url(original_url()), 302)
    resp = Response("Authentification requise\n", 401)
    resp.headers["WWW-Authenticate"] = 'Basic realm="expolab"'
    return resp


# --- connexion / deconnexion ----------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    creds = credentials()
    next_url = safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        ip = client_ip()
        if creds is None:
            error = "Identifiants d'administration introuvables sur le serveur."
        elif too_many_failures(ip):
            error = "Trop de tentatives. Réessaie dans quelques minutes."
            return render_template("login.html", error=error, next_url=next_url, tls_mode=TLS_MODE), 429
        elif password_ok(request.form.get("username", ""), request.form.get("password", ""), creds):
            _failures.pop(ip, None)
            resp = redirect(next_url, 303)
            return set_session_cookie(resp, make_token(creds[0], creds))
        elif operator_credentials() and password_ok(request.form.get("username", ""), request.form.get("password", ""), operator_credentials()):
            _failures.pop(ip, None)
            op = operator_credentials()
            # Toujours vers l'accueil d'exploitation : jamais vers une page d'administration demandee avant.
            return set_session_cookie(redirect("/", 303), make_token(op[0], op, role=OPERATOR))
        else:
            record_failure(ip)
            error = "Identifiant ou mot de passe incorrect."
        return render_template("login.html", error=error, next_url=next_url, tls_mode=TLS_MODE), 401
    session = current_session(creds) if creds is not None else None
    if session:
        return redirect(next_url if session[1] == ADMIN else "/", 302)
    return render_template("login.html", error=None, next_url=next_url, tls_mode=TLS_MODE)


@app.route("/logout", methods=["POST"])
def logout():
    return set_session_cookie(redirect("/login", 303), None)


# --- services --------------------------------------------------------------

def load_services() -> list[dict]:
    try:
        data = yaml.safe_load(SERVICES_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    services = []
    for svc in data.get("services") or []:
        name = svc.get("name")
        if not name or name == PORTAL_NAME or svc.get("portal") is False:
            continue
        services.append(
            {
                "name": name,
                "title": svc.get("title") or name,
                "group": svc.get("group") or "Autres",
                "icon": svc.get("icon") or "box",
                "description": svc.get("description") or "",
                "framed": svc.get("framed") is not False,
                "url": f"https://{name}.{DOMAIN}/",
                "_backend": (svc.get("backend_host") or "localhost", svc.get("backend_port")),
            }
        )
    return services


def tcp_up(host: str, port: int | None, timeout: float = 0.4) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def service_status(services: list[dict], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if now - _status_cache["at"] > STATUS_CACHE_SECONDS:
        # En parallele : un service injoignable (filtre, pas de reponse) ne doit
        # pas allonger d'autant le chargement de la page.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda s: tcp_up(*s["_backend"]), services))
        _status_cache["values"] = {s["name"]: up for s, up in zip(services, results)}
        _status_cache["at"] = now
    return _status_cache["values"]


def public_service(svc: dict) -> dict:
    return {k: v for k, v in svc.items() if not k.startswith("_")}


def require_login():
    """Administrateur connecte -> ses identifiants ; None sinon (exploitant compris)."""
    creds = credentials()
    if creds is None or not authenticated_user(creds):
        return None
    return creds


@app.route("/")
def index():
    creds = credentials()
    session = current_session(creds) if creds is not None else None
    if session is None:
        return redirect("/login", 302)
    if session[1] == OPERATOR:
        return exploitant_page(session)
    return render_template("index.html", user=creds[0], domain=DOMAIN, has_ca=TLS_MODE == "internal")


def exploitant_page(session: tuple[str, str]):
    return render_template("exploitant.html", user=session[0], domain=DOMAIN, is_admin=session[1] == ADMIN,
                           flasher_url=f"https://{OPERATOR_SERVICE}.{DOMAIN}/exploitation")


@app.route("/exploitation")
def exploitation():
    """La vue de l'exploitant, telle qu'il la voit - aussi pour l'administrateur (sans changer de
    compte), avec un lien de retour vers l'administration."""
    session = current_session()
    if session is None:
        return redirect(f"/login?next={quote('/exploitation', safe='')}", 302)
    return exploitant_page(session)


# --- plans des salles (administrateur et exploitant) ----------------------------

@app.route("/api/plans")
def api_plans():
    if current_session() is None:
        return jsonify({"error": "Authentification requise"}), 401
    rooms, machines = plans.load_bastion()
    return jsonify(plans.build_plans(rooms, machines, plans.fetch_presence()))


@app.route("/plans/image/<name>")
def plan_image(name):
    if current_session() is None:
        return jsonify({"error": "Authentification requise"}), 401
    rooms, _machines = plans.load_bastion()
    if not plans.map_image_allowed(name, rooms) or not (plans.BASTION_MAPS / name).is_file():
        return Response("Plan introuvable\n", 404)
    resp = send_file(plans.BASTION_MAPS / name, max_age=300)
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@app.route("/api/services")
def api_services():
    if require_login() is None:
        return jsonify({"error": "Authentification requise"}), 401
    services = load_services()
    status = service_status(services)
    return jsonify({"services": [dict(public_service(s), up=status.get(s["name"], False)) for s in services]})


@app.route("/api/status")
def api_status():
    if require_login() is None:
        return jsonify({"error": "Authentification requise"}), 401
    return jsonify(service_status(load_services()))


@app.route("/ca.crt")
def ca_certificate():
    """Certificat racine de Caddy (mode internal) : public par nature, et
    necessaire AVANT de pouvoir se connecter sans avertissement."""
    if TLS_MODE != "internal" or not CA_FILE.exists():
        return Response("Aucun certificat racine a installer.\n", 404)
    return Response(
        CA_FILE.read_bytes(),
        mimetype="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="expolab-ca.crt"'},
    )


if __name__ == "__main__":
    _secret()  # echoue des le demarrage plutot qu'a la premiere connexion
    app.run(host="127.0.0.1", port=3001, threaded=True)
