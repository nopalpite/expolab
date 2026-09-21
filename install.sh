#!/usr/bin/env bash
# Installe tout le lab en une seule commande, dans l'ordre : Incus, le
# reseau expo-lan, la flotte de faux Pi, le serveur d'expo (Dockhand +
# stacks), le VPN. Point d'entree unique - chaque etape reste utilisable
# seule (voir README) si besoin de ne relancer qu'une partie.
#
# Idempotent de bout en bout : relancer cette meme commande apres coup
# ne repete que ce qui manque. A l'etape Dockhand (aucun endpoint API ne
# permet de creer un environnement a notre place), le script patiente
# tout seul jusqu'a ce que l'environnement soit confirme dans l'UI - pas
# besoin de le relancer soi-meme.
#
# Usage: sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_USER="${SUDO_USER:-$USER}"

echo "=== 1/5 : Installation d'Incus ==="
"$SCRIPT_DIR/incus/install.sh"

echo
echo "=== 2/5 : Reseau expo-lan ==="
"$SCRIPT_DIR/incus/network-setup.sh"

echo
echo "=== 3/5 : Deploiement de la flotte ==="
# incus/install.sh vient d'ajouter $REAL_USER au groupe incus-admin, pas
# encore actif dans cette session shell (normalement il faudrait se
# reconnecter) - `sg` applique le groupe pour cette seule commande, sans
# avoir besoin de se deconnecter/reconnecter.
sudo -u "$REAL_USER" sg incus-admin -c "'$SCRIPT_DIR/fleet/deploy-fleet.sh'"

echo
echo "=== 4/5 : Serveur d'expo (Dockhand + stacks) ==="
"$SCRIPT_DIR/server/deploy-server.sh"

echo
echo "=== 5/5 : VPN (WireGuard) ==="
"$SCRIPT_DIR/vpn/install.sh"

cat <<'EOF'

[+] Installation terminee.
EOF
