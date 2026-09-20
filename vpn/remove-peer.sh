#!/usr/bin/env bash
# Retire un pair VPN (symetrique de add-peer.sh).
#
# Usage: sudo ./remove-peer.sh <nom>
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="${1:?usage: remove-peer.sh <nom>}"
WG_DIR="$SCRIPT_DIR/wireguard/config"
CONF="$WG_DIR/wg0.conf"

if [ ! -f "$CONF" ]; then
    echo "WireGuard n'est pas configure, rien a faire."
    exit 0
fi

PEER_PUBLIC_KEY="$(awk -v name="# peer: $NAME" '
    $0 == name { found=1; next }
    found && /^PublicKey/ { print $3; exit }
' "$CONF")"

if [ -z "$PEER_PUBLIC_KEY" ]; then
    echo "Aucun pair nomme '$NAME' trouve dans $CONF." >&2
    exit 1
fi

if docker inspect wireguard &>/dev/null; then
    echo "[+] Retrait a chaud du pair..."
    docker exec wireguard wg set wg0 peer "$PEER_PUBLIC_KEY" remove 2>/dev/null || true
else
    echo "[=] Conteneur 'wireguard' non demarre, retrait a chaud ignore (sera effectif au prochain demarrage)."
fi

echo "[+] Retrait du bloc [Peer] de $CONF..."
python3 - "$CONF" "$NAME" <<'EOF'
import sys

conf_path, name = sys.argv[1], sys.argv[2]
marker = f"# peer: {name}"

with open(conf_path) as f:
    content = f.read()

# Chaque pair est un bloc separe par des lignes vides (cf le format ecrit
# par add-peer.sh : "\n# peer: <nom>\n[Peer]\n...").
blocks = content.split("\n\n")
kept = [b for b in blocks if marker not in b]

with open(conf_path, "w") as f:
    f.write("\n\n".join(kept).rstrip("\n") + "\n")
EOF

rm -f "$WG_DIR/peers/${NAME}.conf"

echo "[+] Pair '$NAME' retire."
