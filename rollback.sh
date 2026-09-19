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

echo "=== 1/3 : suppression de la flotte ==="
"$SCRIPT_DIR/fleet/teardown-fleet.sh" "${YES_FLAG[@]}"

echo
echo "=== 2/3 : demontage du reseau (profil, macvlan, interface hote) ==="
"$SCRIPT_DIR/incus/network-teardown.sh"

echo
echo "=== 3/3 : desinstallation d'Incus ==="
"$SCRIPT_DIR/incus/uninstall.sh" "${YES_FLAG[@]}"

echo
echo "[+] Rollback termine : l'hote est revenu a son etat d'avant expolab."
