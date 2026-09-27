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
# Usage: render-caddyfile.py <chemin_services.yaml> [tls_mode] [domaine]
#   tls_mode : internal (defaut) ou signed
#   domaine  : defaut web.expolab.lan (internal) - obligatoire en signed
import sys

import yaml

DEFAULT_PUBLIC_DOMAIN = "web.expolab.lan"
DEFAULT_BACKEND_HOST = "localhost"


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

    for svc in services:
        name = svc["name"]
        port = svc["backend_port"]
        backend_host = svc.get("backend_host", DEFAULT_BACKEND_HOST)
        print(f"{name}.{public_domain} {{")
        for line in tls_block_lines(tls_mode):
            print(line)
        for route in svc.get("extra_routes", []):
            route_host = route.get("backend_host", backend_host)
            print(f"    reverse_proxy {route['path']} {route_host}:{route['backend_port']}")
        print(f"    reverse_proxy {backend_host}:{port}")
        print("}")
        print()


if __name__ == "__main__":
    main()
