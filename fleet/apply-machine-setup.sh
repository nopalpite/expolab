#!/usr/bin/env bash
# Applique la configuration commune (proxy apt, heure, cle d'automatisation,
# sudo sans mot de passe - voir image/machine-setup.sh) a des faux Pi
# DEJA crees avant que deploy-fleet.sh ne la fournisse. Idempotent.
#
# Usage: ./apply-machine-setup.sh [nom ...]   (defaut : toute la flotte)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PUBKEY="$REPO_ROOT/server/stacks/ansible-web/automation_key/automation_ed25519.pub"
USERNAME="${FAKEPI_USER:-pi}"

if [ ! -f "$PUBKEY" ]; then
    echo "Cle publique d'automatisation absente ($PUBKEY) - lancer deploy-fleet.sh ou deploy-server.sh." >&2
    exit 1
fi

if [ $# -gt 0 ]; then
    NAMES=("$@")
else
    mapfile -t NAMES < <(incus list --format csv -c n 2>/dev/null | grep -v '^$')
fi

for name in "${NAMES[@]}"; do
    echo "[+] $name"
    incus file push "$REPO_ROOT/image/machine-setup.sh" "$name/root/machine-setup.sh" --mode 0755 < /dev/null
    incus file push "$PUBKEY" "$name/root/automation.pub" --mode 0644 < /dev/null
    incus exec "$name" -- /root/machine-setup.sh --stage all --user "$USERNAME" --pubkey-file /root/automation.pub < /dev/null
done
