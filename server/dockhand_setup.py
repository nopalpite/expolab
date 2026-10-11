#!/usr/bin/env python3
"""Configuration initiale de Dockhand par son API REST (aucune etape dans l'UI).

  ensure-env   cree l'environnement "local" (socket Docker de l'hote) s'il n'y
               en a aucun - c'etait la derniere etape manuelle de l'installation.
  ensure-auth  cree le premier utilisateur (admin) avec les identifiants
               d'administration du lab, active l'authentification de Dockhand,
               puis genere un jeton d'API depose dans server/dockhand.env
               (DOCKHAND_TOKEN, lu par dockhand-api.sh, dockhand_stack.py et
               caddy-admin). Le mot de passe est lu dans la variable
               d'environnement DOCKHAND_SETUP_PASSWORD (jamais en argument).

  set-credentials  change l'identifiant et le mot de passe du compte admin
               (server/set-credentials.sh) ; le jeton d'API reste valable.

Idempotent : un jeton deja valide dans dockhand.env => rien a faire.
Environnement : DOCKHAND_URL (defaut http://127.0.0.1:3000).
Endpoints (spec OpenAPI de Dockhand, FEAT_API_DOCS=true) : /api/environments,
/api/users (creation sans authentification tant qu'aucun admin n'existe),
/api/auth/settings, /api/auth/login, /api/auth/tokens (exige le mot de passe).
"""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:3000"
ENV_FILE = Path(__file__).resolve().parent / "dockhand.env"
LOCAL_SOCKET = "/var/run/docker.sock"
TOKEN_NAME = "expolab-deploy"


class SetupError(Exception):
    pass


class Client:
    """Client HTTP minimal : cookie de session conserve, jeton Bearer optionnel."""

    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, body: dict | None = None):
        """Retourne (code HTTP, corps JSON). Ne leve que si Dockhand est injoignable."""
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            code, raw = exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise SetupError(f"Impossible de joindre Dockhand : {exc.reason}") from exc
        try:
            return code, (json.loads(raw) if raw else {})
        except ValueError:
            return code, {"raw": raw.decode(errors="replace")[:200]}


def _error_text(body) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or body)
    return str(body)


def read_token(env_file: Path) -> str | None:
    token = os.environ.get("DOCKHAND_TOKEN")
    if token:
        return token
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DOCKHAND_TOKEN="):
                return line.partition("=")[2].strip() or None
    return None


def write_token(env_file: Path, token: str) -> None:
    lines = []
    if env_file.exists():
        lines = [line for line in env_file.read_text().splitlines() if not line.startswith("DOCKHAND_TOKEN=")]
    lines.append(f"DOCKHAND_TOKEN={token}")
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)


def ensure_environment(client: Client) -> int:
    code, envs = client.request("GET", "/api/environments")
    if code == 401:
        raise SetupError("Dockhand demande une authentification (jeton absent ou invalide dans server/dockhand.env).")
    if code != 200:
        raise SetupError(f"Lecture des environnements impossible ({code}) : {_error_text(envs)}")
    if isinstance(envs, dict):
        envs = envs.get("environments") or envs.get("data") or []
    if envs:
        return envs[0]["id"]
    code, created = client.request(
        "POST", "/api/environments",
        {"name": "local", "connectionType": "socket", "socketPath": LOCAL_SOCKET},
    )
    if code not in (200, 201):
        raise SetupError(f"Creation de l'environnement local refusee ({code}) : {_error_text(created)}")
    return created["id"]


def ensure_auth(base_url: str, username: str, password: str, env_file: Path = ENV_FILE) -> str:
    """Retourne "deja" si l'authentification etait deja en place avec un jeton
    valide, "configure" sinon."""
    token = read_token(env_file)
    # Un 200 sur /api/environments ne prouve rien tant que l'authentification
    # est desactivee (tout passe) : on regarde d'abord l'etat des reglages.
    code, settings = Client(base_url, token).request("GET", "/api/auth/settings")
    auth_enabled = code == 401 or (code == 200 and bool(settings.get("authEnabled")))
    if auth_enabled and token:
        code, _ = Client(base_url, token).request("GET", "/api/environments")
        if code == 200:
            return "deja"

    client = Client(base_url)
    if not auth_enabled:
        code, created = client.request("POST", "/api/users", {"username": username, "password": password})
        if code not in (200, 201):
            # Un utilisateur existe peut-etre deja (execution interrompue) :
            # la connexion ci-dessous tranche, avec son propre message.
            print(f"[!] Creation de l'utilisateur Dockhand : {_error_text(created)}", file=sys.stderr)
        code, enabled = client.request("PUT", "/api/auth/settings", {"authEnabled": True})
        if code != 200:
            raise SetupError(f"Activation de l'authentification refusee ({code}) : {_error_text(enabled)}")

    code, login = client.request("POST", "/api/auth/login", {"username": username, "password": password})
    if code != 200 or not login.get("success", True):
        raise SetupError(
            f"Connexion a Dockhand refusee pour '{username}' ({code}) : {_error_text(login)}. "
            "Un compte Dockhand existe deja avec d'autres identifiants ?"
        )

    code, created = client.request("POST", "/api/auth/tokens", {"name": TOKEN_NAME, "password": password})
    if code == 409 or (code == 400 and "name" in _error_text(created).lower()):
        code, created = client.request("POST", "/api/auth/tokens", {"name": f"{TOKEN_NAME}-{int(time.time())}", "password": password})
    if code not in (200, 201) or not created.get("token"):
        raise SetupError(f"Creation du jeton d'API refusee ({code}) : {_error_text(created)}")
    write_token(env_file, created["token"])
    return "configure"


def set_credentials(base_url: str, old_user: str, new_user: str, new_password: str, env_file: Path = ENV_FILE) -> str:
    """Change l'identifiant et le mot de passe du compte admin de Dockhand
    (PUT /api/users/{id}, avec le jeton d'API de dockhand.env : il reste valable
    apres le changement). Sans authentification active, la cree avec les nouveaux
    identifiants (ensure_auth). Retourne "modifie" ou "configure"."""
    token = read_token(env_file)
    client = Client(base_url, token)
    code, settings = client.request("GET", "/api/auth/settings")
    if not (code == 401 or (code == 200 and settings.get("authEnabled"))):
        ensure_auth(base_url, new_user, new_password, env_file)
        return "configure"

    code, users = client.request("GET", "/api/users")
    if code == 401:
        raise SetupError("Jeton d'API absent ou invalide (server/dockhand.env) : impossible de modifier le compte Dockhand.")
    if code != 200 or not isinstance(users, list):
        raise SetupError(f"Lecture des utilisateurs Dockhand impossible ({code}) : {_error_text(users)}")
    admins = [u for u in users if u.get("isAdmin")]
    target = next((u for u in users if u.get("username") == old_user), None) or (admins[0] if len(admins) == 1 else None)
    if target is None:
        raise SetupError(
            f"Compte Dockhand '{old_user}' introuvable parmi {len(users)} utilisateur(s) "
            f"({', '.join(u.get('username', '?') for u in users)}) : modification a faire dans l'interface de Dockhand."
        )
    code, updated = client.request("PUT", f"/api/users/{target['id']}", {"username": new_user, "password": new_password})
    if code == 409:
        raise SetupError(f"Dockhand a deja un utilisateur '{new_user}' : choisis un autre identifiant.")
    if code != 200:
        raise SetupError(f"Modification du compte Dockhand refusee ({code}) : {_error_text(updated)}")
    return "modifie"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ensure-env")
    auth = sub.add_parser("ensure-auth")
    auth.add_argument("--user", required=True)
    change = sub.add_parser("set-credentials")
    change.add_argument("--user", required=True, help="nouvel identifiant")
    change.add_argument("--from", dest="old_user", default="", help="ancien identifiant (defaut : l'unique admin)")
    args = parser.parse_args()

    base_url = os.environ.get("DOCKHAND_URL", DEFAULT_URL)
    try:
        if args.command == "ensure-env":
            # Avec l'authentification deja active, le jeton de dockhand.env sert.
            env_id = ensure_environment(Client(base_url, read_token(ENV_FILE)))
            print(f"[+] Environnement Dockhand pret (id {env_id}).")
        elif args.command == "set-credentials":
            password = os.environ.get("DOCKHAND_SETUP_PASSWORD", "")
            if not password:
                print("DOCKHAND_SETUP_PASSWORD doit etre defini.", file=sys.stderr)
                return 1
            state = set_credentials(base_url, args.old_user, args.user, password)
            print(f"[+] Compte Dockhand {state} (utilisateur '{args.user}').")
        else:
            password = os.environ.get("DOCKHAND_SETUP_PASSWORD", "")
            if not password:
                print("DOCKHAND_SETUP_PASSWORD doit etre defini.", file=sys.stderr)
                return 1
            state = ensure_auth(base_url, args.user, password)
            print("[=] Authentification Dockhand deja en place." if state == "deja"
                  else f"[+] Authentification Dockhand activee (utilisateur '{args.user}', jeton dans server/dockhand.env).")
    except SetupError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
