#!/usr/bin/env bash
# Deploie le conteneur expo-gw (DHCP/DNS/reverse-proxy dedie) et lui
# transfere la responsabilite du DHCP sur expo-lan (jusque-la assure par
# le DHCP integre d'Incus, cf incus/network-setup.sh).
#
# Idempotent : relancer ce script (apres avoir modifie gateway/services.yaml
# par exemple) regenere et applique le Caddyfile sans recreer le conteneur.
#
# Usage: ./deploy-gateway.sh [chemin_services.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICES="${1:-$SCRIPT_DIR/services.yaml}"
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

FRESH=0
if incus info "$NAME" &>/dev/null; then
    echo "[=] $NAME existe deja."
else
    FRESH=1
    echo "[+] Creation de $NAME..."
    incus launch "$IMAGE" "$NAME" --profile default --profile "$PROFILE" < /dev/null

    echo "[+] Attente du demarrage de $NAME..."
    for _ in $(seq 1 15); do
        if incus exec "$NAME" -- true < /dev/null &>/dev/null; then
            break
        fi
        sleep 2
    done

    incus file push "$SCRIPT_DIR/dnsmasq.conf" "$NAME/root/dnsmasq.conf" < /dev/null
    incus file push "$SCRIPT_DIR/provision-gateway.sh" "$NAME/root/provision-gateway.sh" --mode 0755 < /dev/null

    incus exec "$NAME" -- /root/provision-gateway.sh < /dev/null
fi

# DNS amont de dnsmasq : la passerelle de l'hote plutot qu'un resolveur
# public fige. Sur certains reseaux (constate sur le Wi-Fi du lab), le DNS
# public en UDP:53 direct (1.1.1.1, 9.9.9.9) est bloque/filtre alors que le
# routeur local repond normalement - la passerelle detectee dynamiquement
# est donc plus fiable, et reste portable d'un reseau a l'autre. Regenere a
# chaque run (pas seulement a la creation) pour suivre un changement de
# reseau. Conserve les resolveurs publics en repli.
echo "[+] Mise a jour du DNS amont de dnsmasq..."
HOST_GATEWAY_IP="$(ip route show default 0.0.0.0/0 2>/dev/null | awk '{print $3; exit}')"
UPSTREAM_TMP="$(mktemp)"
{
    [ -n "$HOST_GATEWAY_IP" ] && echo "nameserver $HOST_GATEWAY_IP"
    echo "nameserver 1.1.1.1"
    echo "nameserver 9.9.9.9"
} > "$UPSTREAM_TMP"
incus file push "$UPSTREAM_TMP" "$NAME/root/resolv.dnsmasq.upstream" < /dev/null
rm -f "$UPSTREAM_TMP"
incus exec "$NAME" -- cp /root/resolv.dnsmasq.upstream /etc/resolv.dnsmasq.upstream < /dev/null
incus exec "$NAME" -- systemctl restart dnsmasq < /dev/null

echo "[+] Generation/mise a jour du Caddyfile a partir de $SERVICES..."
CADDYFILE_TMP="$(mktemp)"
python3 "$SCRIPT_DIR/render-caddyfile.py" "$SERVICES" > "$CADDYFILE_TMP"
incus file push "$CADDYFILE_TMP" "$NAME/root/Caddyfile" < /dev/null
rm -f "$CADDYFILE_TMP"
incus exec "$NAME" -- cp /root/Caddyfile /etc/caddy/Caddyfile < /dev/null
incus exec "$NAME" -- systemctl reload caddy < /dev/null

if [ "$FRESH" -eq 1 ]; then
    echo "[+] Bascule du DHCP de expo-lan vers expo-gw..."
    incus network set expo-lan ipv4.dhcp=false

    echo "[+] Attente de la stabilisation du DHCP dedie (dnsmasq)..."
    for _ in $(seq 1 15); do
        if incus exec "$NAME" -- systemctl is-active --quiet dnsmasq < /dev/null; then
            break
        fi
        sleep 2
    done

    echo "[+] Renouvellement des baux des autres conteneurs aupres du nouveau DHCP..."
    # Un simple `systemctl restart systemd-networkd` ne force pas un nouveau
    # DHCPDISCOVER : le client garde son bail precedent (encore valide a ses
    # yeux) tant qu'il n'a pas expire. Un restart complet du conteneur repart
    # d'une interface reseau vierge et force une vraie renegociation. On
    # reessaie si le bail n'est pas arrive du premier coup (le dnsmasq du
    # gateway peut mettre un instant a etre pleinement pret).
    #
    # Tous les conteneurs sauf expo-gw lui-meme (flotte, expo-apps, et tout
    # ce qui sera ajoute plus tard) : sans ca, leur nom ne serait jamais
    # enregistre aupres du nouveau dnsmasq et resterait impossible a
    # resoudre en <nom>.expolab.lan.
    for c in $(incus list --format csv -c n 2>/dev/null | grep -v "^${NAME}\$" || true); do
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
fi

cat <<EOF

[+] Gateway pret :
    IP            : 10.42.0.10
    DHCP          : dnsmasq, plage 10.42.0.100-250, domaine expolab.lan
    Reverse proxy : https://<service>.web.expolab.lan (TLS auto-signe Caddy)
                    services definis dans gateway/services.yaml

Verification :
    incus exec expo-gw -- systemctl status dnsmasq caddy --no-pager
    incus exec expo-gw -- cat /etc/caddy/Caddyfile
EOF
