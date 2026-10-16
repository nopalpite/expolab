#!/usr/bin/env bash
# Installe tout le serveur d'expo en une seule commande, dans l'ordre : le
# reseau de l'expo (pont expo-lan sur une vraie carte reseau), le serveur
# (Dockhand + stacks), le VPN. Point d'entree unique - chaque etape reste
# utilisable seule (voir README) si besoin de ne relancer qu'une partie.
#
# Idempotent de bout en bout : relancer cette meme commande apres coup
# ne repete que ce qui manque.
#
# Usage: sudo ./install.sh [carte_reseau_expo] [--force] [--sans-carte] [--wifi-ap]
#   carte_reseau_expo : carte reliee au switch des machines (defaut : celle
#                       deja configuree, sinon $EXPO_LAN_IFACE, sinon eth1)
#   --force           : voir network/setup.sh
#   --sans-carte      : test sans carte dediee a l'expo (pont seul, aucune machine
#                       ne peut s'y connecter) - voir network/setup.sh
#   --wifi-ap         : l'interface (une antenne Wi-Fi) devient un point d'acces
#                       pour l'expo ; mot de passe dans EXPO_WIFI_PASSWORD, SSID dans
#                       EXPO_WIFI_SSID (defaut expolab) - voir network/setup.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== 1/3 : Reseau de l'expo (expo-lan) ==="
"$SCRIPT_DIR/network/setup.sh" "$@"

echo
echo "=== 2/3 : Serveur d'expo (Dockhand + stacks) ==="
"$SCRIPT_DIR/server/deploy-server.sh"

echo
echo "=== 3/3 : VPN (WireGuard) ==="
"$SCRIPT_DIR/vpn/install.sh"

echo
bash "$SCRIPT_DIR/server/docker-cleanup.sh"

cat <<'EOF'

[+] Installation terminee.
EOF
