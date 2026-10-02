#!/usr/bin/env bash
# Deploie le "serveur d'expo" : Dockhand est lance seul (il ne peut pas se
# creer via sa propre API), puis chaque service (dnsmasq, dnsmasq-admin,
# caddy, caddy-admin, webui, bastion, dashboard, git-mirror, ansible-web)
# est cree/redeploye comme stack Dockhand independante via son API REST -
# voir server/dockhand-api.sh. C'est ce qui, en vraie vie, tournerait sur
# le vrai serveur de l'exposition (Incus n'existe pas la-bas, il ne sert
# qu'a simuler la flotte ici dans le lab).
#
# Ce fichier ne contient que l'orchestration : la logique de chaque
# etape vit dans server/lib/*.sh (une fonction par responsabilite).
#
# Idempotent : relancer ce script (apres avoir modifie server/services.yaml
# ou le code d'une app par exemple) regenere le Caddyfile et redeploie les
# stacks sans repeter la bascule DHCP si elle a deja eu lieu.
#
# Usage: sudo ./deploy-server.sh [chemin_services.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICES="${1:-$SCRIPT_DIR/services.yaml}"
export REPO_ROOT

# shellcheck source=./dockhand-api.sh
source "$SCRIPT_DIR/dockhand-api.sh"
for lib in preflight tls secrets ansible-web dashboard dnsmasq summary; do
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/lib/$lib.sh"
done

preflight_checks
bootstrap_dockhand

setup_tls
setup_bastion_secrets
setup_caddy_basic_auth
render_caddyfile
setup_dnsmasq_files
setup_public_domain_dns
setup_ansible_web
setup_dashboard
mkdir -p "$SCRIPT_DIR/stacks/git-mirror/data"

echo "[+] Creation/redeploiement des stacks applicatives via l'API Dockhand..."
for stack in dnsmasq dnsmasq-admin caddy caddy-admin webui bastion dashboard git-mirror ansible-web; do
    dockhand_upsert_stack "$stack" "$SCRIPT_DIR/stacks/$stack/docker-compose.yml"
done

switch_dhcp_to_dnsmasq
print_summary
