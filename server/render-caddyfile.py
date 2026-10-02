#!/usr/bin/env python3
# Genere un Caddyfile a partir de server/services.yaml : un vhost par
# service, reverse-proxy vers son port backend.
#
# Deux modes TLS (voir TLS_MODE dans deploy-server.sh) :
# - internal (defaut) : certificat auto-signe par la CA interne de Caddy,
#   domaine *.web.expolab.lan - fonctionne hors-ligne, mais chaque
#   navigateur/appareil doit accepter l'avertissement une fois.
# - signed : certificat Let's Encrypt via challenge DNS-01 chez OVH (voir
#   server/stacks/caddy/Dockerfile pour le plugin caddy-dns/ovh) - vrai
#   certificat reconnu, sur un vrai domaine public (ex:
#   expolab.tondomaine.fr). Ne necessite aucun port ouvert (contrairement
#   a un challenge HTTP-01) : seule l'API DNS d'OVH est contactee, en
#   sortant. Les identifiants OVH_* sont lus par Caddy lui-meme depuis
#   son environnement ({env.OVH_...}) - jamais ecrits dans ce Caddyfile.
#
# Le nom public (vhost, ce que le client demande) reste distinct du
# backend meme si le backend par defaut est desormais "localhost" (tous
# les services de ce stack Docker tournent sur l'hote) : ca laisse la
# possibilite d'exposer un jour un service ailleurs (ex: un faux Pi via
# <nom>.expolab.lan) sans changer le format.
#
# extra_routes (optionnel) : un service peut avoir besoin de faire
# suivre un chemin precis vers un AUTRE port avant sa route principale
# (ex: Bastion, dont le pont VNC via websockify tourne sur un port a
# part - voir son README, section "Derriere un reverse proxy"). Chaque
# entree {path, backend_port} devient une regle `reverse_proxy <path>
# ...` placee avant la regle catch-all, Caddy evaluant les routes d'un
# meme bloc dans l'ordre ou elles apparaissent.
#
# auth (optionnel) : `auth: true` protege le vhost par un basic_auth Caddy
# (identifiant/hash bcrypt lus dans stacks/caddy/auth.env, ou dans les
# variables BASIC_AUTH_USER/BASIC_AUTH_HASH - voir load_basic_auth()).
# `auth_except: [/git/*]` exempte des chemins (ex: git-mirror, dont le
# protocole git HTTP est utilise par des clients sans navigateur). Echoue
# franchement (code 1) si un service demande l'auth sans identifiants
# disponibles : jamais de vhost silencieusement ouvert. Sans effet de
# securite si les backends restent joignables directement sur le LAN -
# voir la liaison sur 127.0.0.1 dans les docker-compose.yml.
#
# Usage: render-caddyfile.py <chemin_services.yaml> [tls_mode] [domaine]
#   tls_mode : internal (defaut) ou signed
#   domaine  : defaut web.expolab.lan (internal) - obligatoire en signed
import os
import sys
from pathlib import Path

import yaml

DEFAULT_PUBLIC_DOMAIN = "web.expolab.lan"
DEFAULT_BACKEND_HOST = "localhost"
AUTH_ENV_PATH = Path(__file__).resolve().parent / "stacks" / "caddy" / "auth.env"


def load_basic_auth() -> tuple[str, str] | None:
    """(utilisateur, hash bcrypt) ou None si non configure.

    Lu a la main (pas `source`) : le hash bcrypt contient des `$` que le
    shell interpreterait.
    """
    user = os.environ.get("BASIC_AUTH_USER")
    pw_hash = os.environ.get("BASIC_AUTH_HASH")
    if not (user and pw_hash) and AUTH_ENV_PATH.exists():
        values = {}
        for line in AUTH_ENV_PATH.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        user = user or values.get("BASIC_AUTH_USER")
        pw_hash = pw_hash or values.get("BASIC_AUTH_HASH")
    return (user, pw_hash) if user and pw_hash else None


def auth_block_lines(svc: dict, credentials: tuple[str, str]) -> list[str]:
    user, pw_hash = credentials
    except_paths = svc.get("auth_except") or []
    lines = []
    matcher = ""
    if except_paths:
        lines += ["    @protected {", f"        not path {' '.join(except_paths)}", "    }"]
        matcher = " @protected"
    lines += [f"    basic_auth{matcher} {{", f"        {user} {pw_hash}", "    }"]
    return lines


def tls_block_lines(tls_mode: str) -> list[str]:
    if tls_mode == "signed":
        return [
            "    tls {",
            "        dns ovh {",
            "            endpoint {env.OVH_ENDPOINT}",
            "            application_key {env.OVH_APPLICATION_KEY}",
            "            application_secret {env.OVH_APPLICATION_SECRET}",
            "            consumer_key {env.OVH_CONSUMER_KEY}",
            "        }",
            "    }",
        ]
    return ["    tls internal"]


def main() -> None:
    services_path = sys.argv[1]
    tls_mode = sys.argv[2] if len(sys.argv) > 2 else "internal"
    default_domain = DEFAULT_PUBLIC_DOMAIN if tls_mode != "signed" else None
    public_domain = sys.argv[3] if len(sys.argv) > 3 else default_domain
    if not public_domain:
        print("Usage: render-caddyfile.py <services.yaml> signed <domaine>", file=sys.stderr)
        sys.exit(1)

    with open(services_path) as f:
        data = yaml.safe_load(f) or {}

    services = data.get("services") or []

    if not services:
        print("# Aucun service configure dans server/services.yaml.")
        return

    credentials = load_basic_auth()
    protected = [s["name"] for s in services if s.get("auth")]
    if protected and credentials is None:
        print(
            f"auth: true sur {', '.join(protected)} mais aucun identifiant "
            f"(BASIC_AUTH_USER/BASIC_AUTH_HASH ou {AUTH_ENV_PATH}) - refuse de "
            "generer un Caddyfile ou ces services seraient ouverts.",
            file=sys.stderr,
        )
        sys.exit(1)

    for svc in services:
        name = svc["name"]
        port = svc["backend_port"]
        backend_host = svc.get("backend_host", DEFAULT_BACKEND_HOST)
        print(f"{name}.{public_domain} {{")
        for line in tls_block_lines(tls_mode):
            print(line)
        if svc.get("auth"):
            for line in auth_block_lines(svc, credentials):
                print(line)
        for route in svc.get("extra_routes", []):
            route_host = route.get("backend_host", backend_host)
            print(f"    reverse_proxy {route['path']} {route_host}:{route['backend_port']}")
        print(f"    reverse_proxy {backend_host}:{port}")
        print("}")
        print()


if __name__ == "__main__":
    main()
