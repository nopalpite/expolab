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
# auth (optionnel) : `auth: true` protege le vhost par le PORTAIL (service
# `dashboard`, voir portal/app.py) via `forward_auth` : Caddy lui demande a
# chaque requete (/auth/verify) si le cookie de session partage - ou un basic
# auth, pour curl et les clients git - est valide. Une seule connexion pour
# tous les services. Ferme par defaut : portail arrete ou en erreur => acces
# refuse (jamais de vhost ouvert). `auth_except: [/git/*]` exempte des chemins
# (ex: git-mirror, dont le protocole git HTTP est utilise par des clients sans
# navigateur). Echoue (code 1) si un service demande l'auth alors que le
# portail n'est pas declare. Sans effet de securite si les backends restent
# joignables directement sur le LAN - voir la liaison sur 127.0.0.1 dans les
# docker-compose.yml.
#
# Affichage dans le portail : les services sont ouverts dans des cadres
# (iframe) de https://dashboard.<domaine>. Sauf `framed: false`, Caddy retire
# X-Frame-Options et le `frame-ancestors` du service, puis impose
# `frame-ancestors https://dashboard.<domaine>` : le cadre n'est autorise que
# depuis le portail, jamais depuis un site tiers.
#
# Usage: render-caddyfile.py <chemin_services.yaml> [tls_mode] [domaine]
#   tls_mode : internal (defaut) ou signed
#   domaine  : defaut web.expolab.lan (internal) - obligatoire en signed
import sys

import yaml

DEFAULT_PUBLIC_DOMAIN = "web.expolab.lan"
DEFAULT_BACKEND_HOST = "localhost"
PORTAL_NAME = "dashboard"


def portal_backend(services: list[dict]) -> str | None:
    """host:port du portail (service `dashboard`), ou None s'il n'est pas declare."""
    for svc in services:
        if svc.get("name") == PORTAL_NAME:
            return f"{svc.get('backend_host', DEFAULT_BACKEND_HOST)}:{svc['backend_port']}"
    return None


def auth_block_lines(svc: dict, portal: str) -> list[str]:
    except_paths = svc.get("auth_except") or []
    lines = []
    matcher = ""
    if except_paths:
        lines += ["    @protected {", f"        not path {' '.join(except_paths)}", "    }"]
        matcher = " @protected"
    lines += [f"    forward_auth{matcher} {portal} {{", "        uri /auth/verify", "    }"]
    return lines


def is_framed(svc: dict) -> bool:
    return svc.get("name") != PORTAL_NAME and svc.get("framed") is not False


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

    with open(services_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    services = data.get("services") or []

    if not services:
        print("# Aucun service configure dans server/services.yaml.")
        return

    portal = portal_backend(services)
    protected = [s["name"] for s in services if s.get("auth")]
    if protected and portal is None:
        print(
            f"auth: true sur {', '.join(protected)} mais le portail (service "
            f"'{PORTAL_NAME}') n'est pas declare - refuse de generer un Caddyfile "
            "ou ces services seraient ouverts.",
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
            for line in auth_block_lines(svc, portal):
                print(line)
        framed = is_framed(svc)
        if framed:
            print(f'    header +Content-Security-Policy "frame-ancestors https://{PORTAL_NAME}.{public_domain}"')
        for route in svc.get("extra_routes", []):
            route_host = route.get("backend_host", backend_host)
            print(f"    reverse_proxy {route['path']} {route_host}:{route['backend_port']}")
        if framed:
            print(f"    reverse_proxy {backend_host}:{port} {{")
            print("        header_down -X-Frame-Options")
            print('        header_down Content-Security-Policy "(?i)frame-ancestors[^;]*;?" ""')
            print("    }")
        else:
            print(f"    reverse_proxy {backend_host}:{port}")
        print("}")
        print()


if __name__ == "__main__":
    main()
