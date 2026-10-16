#!/usr/bin/env bash
# Deploie le "serveur d'expo" : Dockhand est lance seul (il ne peut pas se
# creer via sa propre API), puis chaque service (dnsmasq, dnsmasq-admin,
# caddy, caddy-admin, bastion, dashboard, git-mirror, ansible-web)
# est cree/redeploye comme stack Dockhand independante via son API REST -
# voir server/dockhand-api.sh. Le reseau de l'expo (pont expo-lan sur une
# vraie carte reseau) doit exister avant : network/setup.sh.
#
# Ce fichier ne contient que l'orchestration : la logique de chaque
# etape vit dans server/lib/*.sh (une fonction par responsabilite).
#
# Idempotent : relancer ce script (apres avoir modifie server/services.yaml
# ou le code d'une app par exemple) regenere le Caddyfile et redeploie les
# stacks.
#
# Usage: sudo ./deploy-server.sh [chemin_services.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICES="${1:-$SCRIPT_DIR/services.yaml}"
export REPO_ROOT

# shellcheck source=./dockhand-api.sh
source "$SCRIPT_DIR/dockhand-api.sh"
for lib in preflight tls secrets credentials automation-key ansible-web sd-flasher apt-cache ntp matter prises lan-isolation dashboard dnsmasq stacks summary; do
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/lib/$lib.sh"
done

preflight_checks
setup_ntp
setup_matter
setup_prises
setup_admin_credentials
bootstrap_dockhand

setup_tls
setup_bastion_secrets
setup_caddy_basic_auth
render_caddyfile
setup_dnsmasq_files
setup_public_domain_dns
setup_ansible_web
setup_sd_flasher
setup_apt_cache
setup_dashboard
mkdir -p "$SCRIPT_DIR/stacks/git-mirror/data"

deploy_stacks
isolate_lan
bash "$SCRIPT_DIR/docker-cleanup.sh"
print_summary
