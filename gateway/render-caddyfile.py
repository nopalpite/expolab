#!/usr/bin/env python3
# Genere un Caddyfile a partir de gateway/services.yaml : un vhost TLS
# auto-signe par service applicatif deploye sur expo-lan (Gitea, Bastion,
# etc.), reverse-proxy vers son port backend.
#
# Le nom public (vhost, ce que le client demande) et le nom backend (ce que
# Caddy interroge en interne) sont volontairement differents : le nom
# backend est l'enregistrement DHCP automatique de dnsmasq, qui pointe vers
# la vraie IP du conteneur de service - si le vhost public utilisait ce
# meme nom, un client contournerait Caddy et taperait directement sur le
# conteneur en HTTPS, port sur lequel rien n'ecoute forcement (voir
# dnsmasq.conf, address=/web.../).
#
# Usage: render-caddyfile.py <chemin_services.yaml> [domaine_public] [domaine_backend]
import sys

import yaml

DEFAULT_PUBLIC_DOMAIN = "web.expolab.lan"
DEFAULT_BACKEND_DOMAIN = "expolab.lan"


def main() -> None:
    services_path = sys.argv[1]
    public_domain = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PUBLIC_DOMAIN
    backend_domain = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_BACKEND_DOMAIN

    with open(services_path) as f:
        data = yaml.safe_load(f) or {}

    services = data.get("services") or []

    if not services:
        print("# Aucun service configure dans gateway/services.yaml.")
        return

    for svc in services:
        name = svc["name"]
        port = svc["backend_port"]
        print(f"{name}.{public_domain} {{")
        print("    tls internal")
        print(f"    reverse_proxy {name}.{backend_domain}:{port}")
        print("}")
        print()


if __name__ == "__main__":
    main()
