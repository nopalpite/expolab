#!/usr/bin/env bash
# Deploie le conteneur expo-gw (DHCP/DNS/reverse-proxy dedie) et lui
# transfere la responsabilite du DHCP sur expo-lan (jusque-la assure par
# le DHCP integre d'Incus, cf incus/network-setup.sh).
#
# Usage: ./deploy-gateway.sh [chemin_inventory.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INVENTORY="${1:-$REPO_ROOT/fleet/inventory.yaml}"
NAME="expo-gw"
IMAGE="images:debian/12"
PROFILE="expo-gw"

if ! command -v incus &>/dev/null; then
    echo "incus introuvable. Lancer d'abord ../incus/install.sh" >&2
    exit 1
fi

if ! incus network show expo-lan &>/dev/null; then
    echo "Le reseau expo-lan n'existe pas. Lancer d'abord ../incus/network-setup.sh" >&2
    exit 1
fi

if ! incus profile show "$PROFILE" &>/dev/null; then
    echo "[+] Creation du profil $PROFILE..."
    incus profile create "$PROFILE"
    incus profile edit "$PROFILE" < "$REPO_ROOT/incus/profiles/expo-gw.yaml"
fi

if incus info "$NAME" &>/dev/null; then
    echo "[=] $NAME existe deja, on passe la creation/provisioning."
else
    echo "[+] Creation de $NAME..."
    incus launch "$IMAGE" "$NAME" --profile default --profile "$PROFILE" < /dev/null

    echo "[+] Attente du demarrage de $NAME..."
    for _ in $(seq 1 15); do
        if incus exec "$NAME" -- true < /dev/null &>/dev/null; then
            break
        fi
        sleep 2
    done

    echo "[+] Generation du Caddyfile a partir de $INVENTORY..."
    CADDYFILE_TMP="$(mktemp)"
    python3 "$SCRIPT_DIR/render-caddyfile.py" "$INVENTORY" > "$CADDYFILE_TMP"

    incus file push "$SCRIPT_DIR/dnsmasq.conf" "$NAME/root/dnsmasq.conf" < /dev/null
    incus file push "$SCRIPT_DIR/resolv.dnsmasq.upstream" "$NAME/root/resolv.dnsmasq.upstream" < /dev/null
    incus file push "$CADDYFILE_TMP" "$NAME/root/Caddyfile" < /dev/null
    incus file push "$SCRIPT_DIR/provision-gateway.sh" "$NAME/root/provision-gateway.sh" --mode 0755 < /dev/null
    rm -f "$CADDYFILE_TMP"

    incus exec "$NAME" -- /root/provision-gateway.sh < /dev/null
fi

echo "[+] Bascule du DHCP de expo-lan vers expo-gw..."
incus network set expo-lan ipv4.dhcp=false

echo "[+] Attente de la stabilisation du DHCP dedie (dnsmasq)..."
for _ in $(seq 1 15); do
    if incus exec "$NAME" -- systemctl is-active --quiet dnsmasq < /dev/null; then
        break
    fi
    sleep 2
done

echo "[+] Renouvellement des baux de la flotte existante aupres du nouveau DHCP..."
# Un simple `systemctl restart systemd-networkd` ne force pas un nouveau
# DHCPDISCOVER : le client garde son bail precedent (encore valide a ses
# yeux) tant qu'il n'a pas expire. Un restart complet du conteneur repart
# d'une interface reseau vierge et force une vraie renegociation. On
# reessaie si le bail n'est pas arrive du premier coup (le dnsmasq du
# gateway peut mettre un instant a etre pleinement pret).
for pi in $(incus list --format csv -c n 2>/dev/null | grep '^pi-' || true); do
    echo "    - $pi"
    for attempt in 1 2 3; do
        incus restart "$pi" < /dev/null || true
        sleep 5
        ip="$(incus list "$pi" --format csv -c 4 2>/dev/null | head -1)"
        if [ -n "$ip" ]; then
            break
        fi
        echo "      (pas encore de bail, nouvel essai $attempt/3)"
    done
done

cat <<EOF

[+] Gateway pret :
    IP          : 10.42.0.10
    DHCP        : dnsmasq, plage 10.42.0.100-250, domaine expolab.lan
    Reverse proxy : https://<nom-du-pi>.web.expolab.lan (TLS auto-signe Caddy)

Verification :
    incus exec expo-gw -- systemctl status dnsmasq caddy --no-pager
    incus exec expo-gw -- curl -sk https://pi-01.web.expolab.lan
EOF
