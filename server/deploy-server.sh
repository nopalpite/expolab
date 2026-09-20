#!/usr/bin/env bash
# Deploie le "serveur d'expo" (DHCP/DNS/reverse-proxy + Dockhand/webui) en
# Docker DIRECTEMENT sur l'hote - c'est le stack qui, en vraie vie, tourne
# sur le vrai serveur de l'exposition (Incus n'existe pas la-bas, il ne
# sert qu'a simuler la flotte ici dans le lab).
#
# Idempotent : relancer ce script (apres avoir modifie server/services.yaml
# ou le code de webui/ par exemple) regenere le Caddyfile et reconstruit/
# redemarre le stack sans repeter la bascule DHCP si elle a deja eu lieu.
#
# Usage: sudo ./deploy-server.sh [chemin_services.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICES="${1:-$SCRIPT_DIR/services.yaml}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

if ! command -v incus &>/dev/null; then
    echo "incus introuvable. Lancer d'abord ../incus/install.sh" >&2
    exit 1
fi

if ! incus network show expo-lan &>/dev/null; then
    echo "Le reseau expo-lan n'existe pas. Lancer d'abord ../incus/network-setup.sh" >&2
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo "[+] Installation de Docker (script officiel get.docker.com)..."
    curl -fsSL https://get.docker.com | sh
    REAL_USER="${SUDO_USER:-$USER}"
    usermod -aG docker "$REAL_USER"
    echo "[+] Utilisateur $REAL_USER ajoute au groupe docker (deconnexion/reconnexion necessaire)."
fi

echo "[+] Generation du Caddyfile a partir de $SERVICES..."
python3 "$SCRIPT_DIR/render-caddyfile.py" "$SERVICES" > "$SCRIPT_DIR/Caddyfile"

if [ ! -f "$SCRIPT_DIR/bastion.env" ]; then
    echo "[+] Premiere generation des secrets Bastion (server/bastion.env, non versionne)..."
    BASTION_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    # Format Fernet (cle de chiffrement des identifiants SSH/VNC memorises)
    # - juste du base64 urlsafe standard sur 32 octets aleatoires, pas
    # besoin du paquet "cryptography" pour la generer (voir le README de
    # Bastion).
    BASTION_CREDENTIALS_KEY="$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
    cat > "$SCRIPT_DIR/bastion.env" <<EOF
BASTION_SECRET_KEY=$BASTION_SECRET_KEY
BASTION_CREDENTIALS_KEY=$BASTION_CREDENTIALS_KEY
EOF
    chmod 600 "$SCRIPT_DIR/bastion.env"
fi

echo "[+] (Re)demarrage du stack Docker..."
(cd "$SCRIPT_DIR" && docker compose up -d --build)

# La premiere fois (DHCP integre d'Incus encore actif), on bascule et on
# renouvelle les baux de tout ce qui tourne deja sur expo-lan. Si deja
# bascule lors d'un run precedent, rien a refaire ici.
if [ "$(incus network get expo-lan ipv4.dhcp)" = "true" ]; then
    echo "[+] Attente de la stabilisation de dnsmasq..."
    for _ in $(seq 1 15); do
        if [ "$(docker inspect -f '{{.State.Running}}' expolab-dnsmasq 2>/dev/null)" = "true" ]; then
            break
        fi
        sleep 2
    done
    sleep 3

    echo "[+] Bascule du DHCP/DNS de expo-lan vers dnsmasq (hote)..."
    incus network set expo-lan ipv4.dhcp=false
    # ipv4.dhcp=false seul ne suffit pas : le dnsmasq integre d'Incus reste
    # actif pour le DNS et garde le port 53 sur 10.42.0.1, empechant notre
    # propre dnsmasq de demarrer ("Address already in use"). dns.mode=none
    # l'arrete completement.
    incus network set expo-lan dns.mode=none

    echo "[+] Renouvellement des baux des conteneurs existants aupres du nouveau DHCP..."
    # Un simple `systemctl restart systemd-networkd` ne force pas un
    # nouveau DHCPDISCOVER : le client garde son bail precedent tant qu'il
    # n'a pas expire. Un restart complet du conteneur repart d'une
    # interface reseau vierge et force une vraie renegociation.
    for c in $(incus list --format csv -c n 2>/dev/null || true); do
        echo "    - $c"
        for attempt in 1 2 3; do
            incus restart "$c" < /dev/null || true
            sleep 5
            ip="$(incus list "$c" --format csv -c 4 2>/dev/null | head -1)"
            if [ -n "$ip" ]; then
                break
            fi
            echo "      (pas encore de bail, nouvel essai $attempt/3)"
        done
    done
else
    echo "[=] DHCP deja bascule vers dnsmasq (rien a refaire)."
fi

cat <<EOF

[+] Serveur d'expo pret :
    DHCP/DNS      : dnsmasq, plage 10.42.0.100-250, domaine expolab.lan
    Reverse proxy : https://<service>.web.expolab.lan (TLS auto-signe Caddy)
                    services definis dans server/services.yaml

Verification :
    docker compose -f $SCRIPT_DIR/docker-compose.yml ps
    curl -k https://fleet.web.expolab.lan
EOF
