#!/usr/bin/env python3
"""Cree ou recree une stack Dockhand a partir d'un docker-compose.yml.

Implementation UNIQUE de ce mecanisme : appelee par
server/dockhand-api.sh (dockhand_upsert_stack, donc deploy-server.sh et
vpn/install.sh) et importee par caddy-admin (recreation des stacks
dependantes d'un changement de config TLS).

Supprimer puis recreer (plutot que mettre a jour) est le seul chemin
fiable pour rester synchronise avec nos fichiers locaux : Dockhand n'a pas
de PUT documente pour le contenu compose d'une stack existante, et
POST .../deploy rejoue seulement ce qu'il a deja en memoire. Une
recreation est aussi la seule facon d'obtenir qu'un conteneur relise un
env_file modifie - `docker restart` garde l'environnement fige a la
creation.

Les docker-compose.yml referencent des chemins ABSOLUS via ${REPO_ROOT}
(jamais resolus par Dockhand : ses binds relatifs partent de SON repertoire
de donnees), substitues ici avant l'envoi.

Usage : dockhand_stack.py upsert <nom> <docker-compose.yml> [--no-rebuild] [--no-wait]
Environnement : REPO_ROOT (obligatoire), DOCKHAND_URL (defaut
http://127.0.0.1:3000), DOCKHAND_TOKEN ou server/dockhand.env (optionnel,
seulement si l'authentification Dockhand est activee).
Codes de retour : 0 ok, 1 echec, 3 aucun environnement Dockhand configure.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:3000"
ENV_FILE = Path(__file__).resolve().parent / "dockhand.env"
RUNNING_STATUSES = {"", "pending", "running", "queued"}
EXIT_NO_ENV = 3


class DockhandError(Exception):
    pass


class NoEnvironment(DockhandError):
    """Dockhand n'a pas encore d'environnement (etape manuelle unique)."""


def _base_url() -> str:
    return os.environ.get("DOCKHAND_URL", DEFAULT_URL).rstrip("/")


def _token() -> str | None:
    token = os.environ.get("DOCKHAND_TOKEN")
    if token:
        return token
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("DOCKHAND_TOKEN="):
                return line.partition("=")[2].strip() or None
    return None


def _request(method: str, path: str, body: dict | None = None, timeout: int = 30):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(_base_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise DockhandError(f"Dockhand a repondu {exc.code} : {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise DockhandError(f"Impossible de joindre Dockhand : {exc.reason}") from exc
    return json.loads(raw) if raw else {}


def _unwrap(data, *keys):
    if isinstance(data, dict):
        for key in keys:
            if data.get(key) is not None:
                return data[key]
        return []
    return data or []


def get_env_id() -> int | None:
    envs = _unwrap(_request("GET", "/api/environments"), "environments", "data")
    return envs[0]["id"] if envs else None


def stack_exists(name: str, env_id: int) -> bool:
    stacks = _unwrap(_request("GET", f"/api/stacks?env={env_id}"), "stacks", "data")
    return any(s.get("name") == name for s in stacks)


def substitute_repo_root(compose: str, repo_root: str) -> str:
    # Equivalent de `envsubst '${REPO_ROOT}'` : cette seule variable, jamais
    # les autres $ qu'un compose peut contenir.
    return re.sub(r"\$\{REPO_ROOT\}|\$REPO_ROOT\b", lambda _: repo_root, compose)


def remove_cached_images(name: str) -> None:
    """Supprime les images "<stack>-*" (convention Compose v2
    <projet>-<service>) pour forcer une reconstruction complete : le
    `docker compose up -d` interne de Dockhand ne reconstruit JAMAIS une
    image deja presente, meme si son Dockerfile a change. Echecs ignores
    (image en cours d'utilisation, docker absent...)."""
    try:
        listing = subprocess.run(
            ["docker", "images", "-q", "--filter", f"reference={name}-*"],
            capture_output=True, text=True, timeout=30,
        )
        for image_id in sorted(set(listing.stdout.split())):
            subprocess.run(["docker", "rmi", "-f", image_id], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def wait_for_job(job_id: str, timeout: int) -> str:
    """Bloque jusqu'a la fin du job (build + deploiement asynchrones cote
    Dockhand) : sans ca, un appelant qui verifie tout de suite l'etat du
    conteneur tombe en pleine construction d'image - constate sur un Pi,
    ou une premiere construction depasse largement quelques secondes."""
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = _request("GET", f"/api/jobs/{job_id}").get("status", "")
        if status not in RUNNING_STATUSES:
            return status
        time.sleep(2)
    return "timeout"


def upsert_stack(
    name: str,
    compose_path: Path,
    repo_root: str,
    rebuild_images: bool = True,
    wait: bool = True,
    wait_timeout: int = 360,
) -> str:
    """Supprime (si presente) puis recree la stack. Retourne le statut
    final du job ("" si non attendu). Leve DockhandError, ou
    NoEnvironment si Dockhand n'a pas encore d'environnement."""
    env_id = get_env_id()
    if env_id is None:
        raise NoEnvironment("Aucun environnement Dockhand configure")

    compose = substitute_repo_root(Path(compose_path).read_text(), repo_root)

    if stack_exists(name, env_id):
        print(f"[=] Stack '{name}' deja presente, suppression avant recreation (pour appliquer nos fichiers locaux a jour)...")
        try:
            _request("DELETE", f"/api/stacks/{name}?env={env_id}", timeout=60)
        except DockhandError:
            pass  # disparue entre-temps : sans consequence, on recree de toute facon

    if rebuild_images:
        remove_cached_images(name)

    print(f"[+] Creation de la stack '{name}'...")
    result = _request(
        "POST", "/api/stacks", {"name": name, "compose": compose, "envId": env_id, "deploy": True}, timeout=60
    )
    job_id = result.get("jobId") if isinstance(result, dict) else None
    if not (wait and job_id):
        return ""

    status = wait_for_job(job_id, wait_timeout)
    if status == "timeout":
        print(f"[!] '{name}' : le deploiement (job {job_id}) prend plus de {wait_timeout // 60} minutes, on continue quand meme.", file=sys.stderr)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upsert")
    up.add_argument("name")
    up.add_argument("compose_file")
    up.add_argument("--no-rebuild", action="store_true")
    up.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()

    repo_root = os.environ.get("REPO_ROOT")
    if not repo_root:
        print("REPO_ROOT doit etre defini (chemin absolu du depot).", file=sys.stderr)
        return 1
    try:
        upsert_stack(
            args.name, Path(args.compose_file), repo_root,
            rebuild_images=not args.no_rebuild, wait=not args.no_wait,
        )
    except NoEnvironment:
        return EXIT_NO_ENV
    except DockhandError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
