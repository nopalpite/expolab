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
        print(f"    reverse_proxy {backend_host}:{port}")
        print("}")
        print()


if __name__ == "__main__":
    main()
