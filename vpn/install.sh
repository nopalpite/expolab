#!/usr/bin/env bash
# Installe WireGuard sur l'hote et l'attache au reseau expo-lan : la patte
# WAN/VPN qui permet un acces distant au lab (cf README, architecture
# cible "deux pattes" du serveur dedie).
#
# Tourne sur l'HOTE directement (pas dans un conteneur) : donner une
# deuxieme interface reseau a expo-gw demanderait du macvlan sur
# l'interface physique, qui ne fonctionne pas de facon fiable en Wi-Fi
# (meme constat que pour expo-lan, cf incus/network-setup.sh). L'hote a
# deja naturellement les deux pattes : son interface physique (WAN reel) et
# le bridge expo-lan (LAN simule) - c'est le point de frontiere naturel.
#
# Usage: sudo ./install.sh [nom_interface_bridge_expo-lan]
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

BRIDGE_IFACE="${1:-expo-lan}"
WG_IFACE="wg0"
WG_PORT="51820"
WG_SUBNET="10.66.66.0/24"
WG_SERVER_IP="10.66.66.1/24"
WG_DIR="/etc/wireguard"

if ! ip link show "$BRIDGE_IFACE" &>/dev/null; then
    echo "Interface '$BRIDGE_IFACE' introuvable. Lancer d'abord incus/network-setup.sh" >&2
    exit 1
fi

echo "[+] Installation de wireguard-tools + iptables..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard iptables >/dev/null

mkdir -p "$WG_DIR/peers"
chmod 700 "$WG_DIR"

if [ ! -f "$WG_DIR/server_private.key" ]; then
    echo "[+] Generation de la paire de cles du serveur..."
    umask 077
    wg genkey | tee "$WG_DIR/server_private.key" | wg pubkey > "$WG_DIR/server_public.key"
else
    echo "[=] Cle serveur deja presente, conservee."
fi

SERVER_PRIVATE_KEY="$(cat "$WG_DIR/server_private.key")"

if [ ! -f "$WG_DIR/$WG_IFACE.conf" ]; then
    echo "[+] Generation de $WG_DIR/$WG_IFACE.conf..."
    umask 077
    cat > "$WG_DIR/$WG_IFACE.conf" <<EOF
[Interface]
Address = $WG_SERVER_IP
ListenPort = $WG_PORT
PrivateKey = $SERVER_PRIVATE_KEY
PostUp = iptables -A FORWARD -i %i -o $BRIDGE_IFACE -j ACCEPT; iptables -A FORWARD -i $BRIDGE_IFACE -o %i -j ACCEPT
PostDown = iptables -D FORWARD -i %i -o $BRIDGE_IFACE -j ACCEPT; iptables -D FORWARD -i $BRIDGE_IFACE -o %i -j ACCEPT

# Les pairs (clients VPN) sont ajoutes ci-dessous par vpn/add-peer.sh.
EOF
else
    echo "[=] $WG_DIR/$WG_IFACE.conf deja present, conserve (peers existants preserves)."
fi

echo "[+] Activation de l'IP forwarding..."
cat > /etc/sysctl.d/99-expolab-wg.conf <<'EOF'
net.ipv4.ip_forward=1
EOF
sysctl -p /etc/sysctl.d/99-expolab-wg.conf >/dev/null

echo "[+] Demarrage de wg-quick@$WG_IFACE..."
systemctl enable --now "wg-quick@$WG_IFACE"

cat <<EOF

[+] VPN pret :
    Interface   : $WG_IFACE (port UDP $WG_PORT)
    Sous-reseau VPN : $WG_SUBNET
    Cle publique serveur : $(cat "$WG_DIR/server_public.key")

Pour ajouter un pair (ex: votre laptop) :
    sudo ./add-peer.sh mon-laptop

IMPORTANT : pour un acces depuis l'exterieur de votre reseau local, il faut
rediriger le port UDP $WG_PORT vers ce Pi depuis votre routeur (hors
perimetre de ce script, specifique a votre box/routeur).
EOF
