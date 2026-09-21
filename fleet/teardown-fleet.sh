#!/usr/bin/env bash
# Supprime les instances de la flotte listees dans l'inventaire.
# Symetrique de deploy-fleet.sh.
#
# Usage: ./teardown-fleet.sh [--yes] [chemin_inventory.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
    shift
fi

INVENTORY="${1:-$SCRIPT_DIR/inventory.yaml}"

if ! command -v incus &>/dev/null; then
    echo "incus introuvable, rien a supprimer."
    exit 0
fi

TMP_LIST="$(mktemp)"
trap 'rm -f "$TMP_LIST"' EXIT

python3 - "$INVENTORY" > "$TMP_LIST" <<'EOF'
import sys, yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}

for pi in data.get("fleet", []):
    print(pi["name"])
EOF

mapfile -t NAMES < "$TMP_LIST"
EXISTING=()
for name in "${NAMES[@]}"; do
    if incus info "$name" &>/dev/null; then
        EXISTING+=("$name")
    fi
done

if [ "${#EXISTING[@]}" -eq 0 ]; then
    echo "[=] Aucune instance de l'inventaire n'existe, rien a faire."
    exit 0
fi

echo "Instances a supprimer : ${EXISTING[*]}"
if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Confirmer la suppression de ces ${#EXISTING[@]} instance(s) ? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
fi

for name in "${EXISTING[@]}"; do
    incus delete "$name" --force
    echo "[-] $name supprime."
done

echo "[+] Flotte nettoyee."
