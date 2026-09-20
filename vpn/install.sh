#!/usr/bin/env bash
# Genere la config WireGuard et deploie la stack "wireguard" via l'API
# Dockhand (voir server/dockhand-api.sh) - la patte WAN/VPN qui permet un
# acces distant au lab (cf README, architecture cible "deux pattes" du
# serveur dedie).
#
# Conteneurise (vpn/wireguard/) plutot qu'installe directement sur l'hote
# comme avant : coherence avec le reste du serveur d'expo, desormais
# entierement pilotable depuis Dockhand. L'hote n'a plus besoin du paquet
# wireguard-tools du tout - toutes les commandes `wg` passent par `docker
# exec wireguard`.
#
# Usage: sudo ./install.sh [nom_interface_bridge_expo-lan]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export REPO_ROOT

# shellcheck source=../server/dockhand-api.sh
source "$REPO_ROOT/server/dockhand-api.sh"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

BRIDGE_IFACE="${1:-expo-lan}"
WG_PORT="51820"
WG_SUBNET="10.66.66.0/24"
WG_SERVER_IP="10.66.66.1/24"
WG_DIR="$SCRIPT_DIR/wireguard/config"

if ! ip link show "$BRIDGE_IFACE" &>/dev/null; then
    echo "Interface '$BRIDGE_IFACE' introuvable. Lancer d'abord incus/network-setup.sh" >&2
    exit 1
fi

if ! docker inspect dockhand &>/dev/null; then
    echo "Dockhand n'est pas demarre. Lancer d'abord ../server/deploy-server.sh" >&2
    exit 1
fi

mkdir -p "$WG_DIR/peers"
chmod 700 "$WG_DIR"

echo "[+] Construction de l'image wireguard (pour generer les cles avant deploiement)..."
docker build -q -t expolab-wireguard "$SCRIPT_DIR/wireguard" >/dev/null

if [ ! -f "$WG_DIR/server_private.key" ]; then
    echo "[+] Generation de la paire de cles du serveur..."
    umask 077
    # wg genkey/pubkey sont de purs calculs crypto, aucun besoin d'une
    # interface montee - wireguard-tools ne vit que dans l'image du
    # conteneur desormais (l'hote n'a plus le paquet), d'ou ce detour via
    # une instance jetable de notre propre image.
    docker run --rm --entrypoint wg expolab-wireguard genkey > "$WG_DIR/server_private.key"
    docker run --rm -i --entrypoint wg expolab-wireguard pubkey \
        < "$WG_DIR/server_private.key" > "$WG_DIR/server_public.key"
else
    echo "[=] Cle serveur deja presente, conservee."
fi

SERVER_PRIVATE_KEY="$(cat "$WG_DIR/server_private.key")"

if [ ! -f "$WG_DIR/wg0.conf" ]; then
    echo "[+] Generation de $WG_DIR/wg0.conf..."
    umask 077
    cat > "$WG_DIR/wg0.conf" <<EOF
[Interface]
Address = $WG_SERVER_IP
ListenPort = $WG_PORT
PrivateKey = $SERVER_PRIVATE_KEY
PostUp = iptables -A FORWARD -i %i -o $BRIDGE_IFACE -j ACCEPT; iptables -A FORWARD -i $BRIDGE_IFACE -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -o $BRIDGE_IFACE -j ACCEPT; iptables -D FORWARD -i $BRIDGE_IFACE -o %i -j ACCEPT

# Les pairs (clients VPN) sont ajoutes ci-dessous par vpn/add-peer.sh.
EOF
else
    echo "[=] $WG_DIR/wg0.conf deja present, conserve (peers existants preserves)."
fi

echo "[+] Activation de l'IP forwarding..."
cat > /etc/sysctl.d/99-expolab-wg.conf <<'EOF'
net.ipv4.ip_forward=1
EOF
sysctl -p /etc/sysctl.d/99-expolab-wg.conf >/dev/null

dockhand_wait_healthy
dockhand_require_env

echo "[+] Creation/redeploiement de la stack 'wireguard'..."
dockhand_upsert_stack wireguard "$SCRIPT_DIR/wireguard/docker-compose.yml"

cat <<EOF

[+] VPN pret :
    Stack Dockhand : wireguard (port UDP $WG_PORT)
    Sous-reseau VPN : $WG_SUBNET
    Cle publique serveur : $(cat "$WG_DIR/server_public.key")

Pour ajouter un pair (ex: votre laptop) :
    sudo ./add-peer.sh mon-laptop

IMPORTANT : pour un acces depuis l'exterieur de votre reseau local, il faut
rediriger le port UDP $WG_PORT vers ce Pi depuis votre routeur (hors
perimetre de ce script, specifique a votre box/routeur).
EOF
