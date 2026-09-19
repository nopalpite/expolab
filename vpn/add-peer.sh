#!/usr/bin/env bash
# Ajoute un pair VPN (ex: le laptop d'un admin) et genere son fichier de
# config client, pret a importer dans l'appli WireGuard.
#
# Usage: sudo ./add-peer.sh <nom> [endpoint_public:port]
#   endpoint_public:port : adresse a laquelle le CLIENT doit joindre ce
#   serveur (IP publique + redirection de port, ou IP locale si le client
#   reste sur le meme reseau). Defaut : IP actuelle de l'hote sur le LAN.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

NAME="${1:?usage: add-peer.sh <nom> [endpoint:port]}"
WG_DIR="/etc/wireguard"
WG_IFACE="wg0"
WG_PORT="51820"
LAN_SUBNET="10.42.0.0/24"
VPN_SUBNET="10.66.66.0/24"

if [ ! -f "$WG_DIR/$WG_IFACE.conf" ]; then
    echo "WireGuard n'est pas configure. Lancer d'abord sudo ./install.sh" >&2
    exit 1
fi

if grep -q "^# peer: $NAME\$" "$WG_DIR/$WG_IFACE.conf" 2>/dev/null; then
    echo "Un pair nomme '$NAME' existe deja dans $WG_IFACE.conf." >&2
    exit 1
fi

DEFAULT_ENDPOINT_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
ENDPOINT="${2:-${DEFAULT_ENDPOINT_IP}:${WG_PORT}}"

# Prochaine IP libre dans le sous-reseau VPN (.1 = serveur, on part de .2).
LAST_OCTET=1
for used in $(grep -oP '(?<=AllowedIPs = 10\.66\.66\.)\d+' "$WG_DIR/$WG_IFACE.conf" 2>/dev/null || true); do
    [ "$used" -gt "$LAST_OCTET" ] && LAST_OCTET="$used"
done
NEXT_OCTET=$((LAST_OCTET + 1))
PEER_IP="10.66.66.${NEXT_OCTET}"

echo "[+] Generation des cles pour '$NAME' (IP VPN: $PEER_IP)..."
umask 077
mkdir -p "$WG_DIR/peers"
PEER_PRIVATE_KEY="$(wg genkey)"
PEER_PUBLIC_KEY="$(echo "$PEER_PRIVATE_KEY" | wg pubkey)"
PRESHARED_KEY="$(wg genpsk)"
SERVER_PUBLIC_KEY="$(cat "$WG_DIR/server_public.key")"

echo "[+] Ajout du pair a $WG_IFACE.conf..."
cat >> "$WG_DIR/$WG_IFACE.conf" <<EOF

# peer: $NAME
[Peer]
PublicKey = $PEER_PUBLIC_KEY
PresharedKey = $PRESHARED_KEY
AllowedIPs = ${PEER_IP}/32
EOF

echo "[+] Application a chaud (sans couper le tunnel des autres pairs)..."
wg set "$WG_IFACE" peer "$PEER_PUBLIC_KEY" preshared-key <(echo "$PRESHARED_KEY") allowed-ips "${PEER_IP}/32"

CLIENT_CONF="$WG_DIR/peers/${NAME}.conf"
cat > "$CLIENT_CONF" <<EOF
[Interface]
PrivateKey = $PEER_PRIVATE_KEY
Address = ${PEER_IP}/32
DNS = 10.42.0.10

[Peer]
PublicKey = $SERVER_PUBLIC_KEY
PresharedKey = $PRESHARED_KEY
Endpoint = $ENDPOINT
AllowedIPs = $LAN_SUBNET, $VPN_SUBNET
PersistentKeepalive = 25
EOF
chmod 600 "$CLIENT_CONF"

cat <<EOF

[+] Pair '$NAME' ajoute (IP VPN: $PEER_IP).

Config client : $CLIENT_CONF
(a copier vers le poste client - jamais commitee dans le depot git)

Import rapide (si wg-quick est installe sur le poste client) :
    sudo wg-quick up $CLIENT_CONF
EOF
