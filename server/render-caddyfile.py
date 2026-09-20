#!/usr/bin/env python3
# Genere un Caddyfile a partir de server/services.yaml : un vhost TLS
# auto-signe par service, reverse-proxy vers son port backend.
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
# Usage: render-caddyfile.py <chemin_services.yaml> [domaine_public]
import sys

import yaml

DEFAULT_PUBLIC_DOMAIN = "web.expolab.lan"
DEFAULT_BACKEND_HOST = "localhost"


def main() -> None:
    services_path = sys.argv[1]
    public_domain = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PUBLIC_DOMAIN

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
        print("    tls internal")
        for route in svc.get("extra_routes", []):
            route_host = route.get("backend_host", backend_host)
            print(f"    reverse_proxy {route['path']} {route_host}:{route['backend_port']}")
        print(f"    reverse_proxy {backend_host}:{port}")
        print("}")
        print()


if __name__ == "__main__":
    main()
