#!/usr/bin/env bash
# Revient a l'etat initial de l'hote (avant install.sh) : supprime la
# flotte, demonte le reseau, desinstalle Incus. A lancer directement sur
# le Pi 5.
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

echo "=== 1/5 : suppression du VPN (WireGuard) ==="
"$SCRIPT_DIR/vpn/uninstall.sh" "${YES_FLAG[@]}"

echo
echo "=== 2/5 : suppression du gateway DHCP/DNS/reverse-proxy ==="
"$SCRIPT_DIR/gateway/teardown-gateway.sh" "${YES_FLAG[@]}"

echo
echo "=== 3/5 : suppression de la flotte ==="
"$SCRIPT_DIR/fleet/teardown-fleet.sh" "${YES_FLAG[@]}"

echo
echo "=== 4/5 : demontage du reseau (profils, reseau expo-lan) ==="
"$SCRIPT_DIR/incus/network-teardown.sh"

echo
echo "=== 5/5 : desinstallation d'Incus ==="
"$SCRIPT_DIR/incus/uninstall.sh" "${YES_FLAG[@]}"

echo
echo "[+] Rollback termine : l'hote est revenu a son etat d'avant expolab."
