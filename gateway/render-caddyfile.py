#!/usr/bin/env python3
# Genere un Caddyfile a partir de fleet/inventory.yaml : un vhost TLS
# auto-signe par faux Pi, reverse-proxy vers son port 80 (nginx-light
# installe par fleet/provision-fakepi.sh).
#
# Le nom public (vhost, ce que le client demande) et le nom backend (ce que
# Caddy interroge en interne) sont volontairement differents : le nom
# backend est l'enregistrement DHCP automatique de dnsmasq, qui pointe vers
# la vraie IP du faux Pi - si le vhost public utilisait ce meme nom, un
# client contournerait Caddy et taperait directement sur le faux Pi en
# HTTPS, port sur lequel rien n'ecoute (voir dnsmasq.conf, address=/web.../).
#
# Usage: render-caddyfile.py <chemin_inventory.yaml> [domaine_public] [domaine_backend]
import sys

import yaml

DEFAULT_PUBLIC_DOMAIN = "web.expolab.lan"
DEFAULT_BACKEND_DOMAIN = "expolab.lan"


def main() -> None:
    inventory_path = sys.argv[1]
    public_domain = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PUBLIC_DOMAIN
    backend_domain = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_BACKEND_DOMAIN

    with open(inventory_path) as f:
        data = yaml.safe_load(f) or {}

    for pi in data.get("fleet", []):
        name = pi["name"]
        print(f"{name}.{public_domain} {{")
        print("    tls internal")
        print(f"    reverse_proxy {name}.{backend_domain}:80")
        print("}")
        print()


if __name__ == "__main__":
    main()
