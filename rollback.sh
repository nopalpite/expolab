#!/usr/bin/env bash
# Revient a l'etat initial de l'hote (avant install.sh) : supprime le VPN,
# le serveur d'expo et le reseau expo-lan. A lancer directement sur le
# serveur.
#
# Usage: sudo ./rollback.sh [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

YES_FLAG=()
if [[ "${1:-}" == "--yes" ]]; then
    YES_FLAG=(--yes)
fi

echo "=== 1/3 : suppression du VPN (WireGuard) ==="
"$SCRIPT_DIR/vpn/uninstall.sh" "${YES_FLAG[@]}"

echo
echo "=== 2/3 : suppression du serveur d'expo (Dockhand et toutes les stacks) ==="
"$SCRIPT_DIR/server/teardown-server.sh" "${YES_FLAG[@]}"

echo
echo "=== 3/3 : demontage du reseau de l'expo (pont expo-lan) ==="
"$SCRIPT_DIR/network/teardown.sh"

echo
echo "[+] Rollback termine : l'hote est revenu a son etat d'avant expolab."
