#!/bin/bash
# Provisioning du conteneur expo-gw (DHCP/DNS via dnsmasq + reverse-proxy
# TLS auto-signe via Caddy). Execute A L'INTERIEUR du conteneur par
# deploy-gateway.sh, qui a prealablement pousse dnsmasq.conf et
# resolv.dnsmasq.upstream dans /root/. Le Caddyfile lui-meme est pousse et
# applique separement par deploy-gateway.sh (a chaque run, pas seulement a
# la creation) pour rester en phase avec gateway/services.yaml.
#
# Ordre important : les paquets sont installes AVANT de basculer le
# conteneur sur son IP statique / son propre resolveur, pour ne pas casser
# la resolution DNS necessaire a apt-get pendant le provisioning (le
# conteneur a une IP+DNS obtenus via le DHCP d'Incus au moment du lancement,
# encore actif a ce stade).
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "[+] Installation dnsmasq + Caddy..."
apt-get update -qq
apt-get install -y -qq dnsmasq debian-keyring debian-archive-keyring \
    apt-transport-https curl gnupg >/dev/null

curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
apt-get update -qq
apt-get install -y -qq caddy >/dev/null

echo "[+] Configuration dnsmasq..."
cp /root/resolv.dnsmasq.upstream /etc/resolv.dnsmasq.upstream
cp /root/dnsmasq.conf /etc/dnsmasq.d/expolab.conf
systemctl enable dnsmasq >/dev/null

systemctl enable caddy >/dev/null

echo "[+] Bascule sur IP statique 10.42.0.10 (etait en DHCP jusqu'ici)..."
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/10-eth0.network <<'EOF'
[Match]
Name=eth0

[Network]
Address=10.42.0.10/24
Gateway=10.42.0.1
DNS=1.1.1.1
EOF
# Le fichier de match par defaut de l'image (DHCP sur toutes les
# interfaces) est trie apres le notre (priorite alphabetique) donc notre
# config statique gagne pour eth0 specifiquement - mais systemd-networkd ne
# retire pas de lui-meme l'IP obtenue par le DHCP precedent (elle reste
# "orpheline" a cote de la nouvelle) : flush explicite necessaire.

echo "[+] Bascule du resolveur local sur dnsmasq (127.0.0.1)..."
systemctl disable --now systemd-resolved >/dev/null 2>&1 || true
rm -f /etc/resolv.conf
echo "nameserver 127.0.0.1" > /etc/resolv.conf

ip addr flush dev eth0
systemctl restart systemd-networkd
sleep 2
systemctl restart dnsmasq
systemctl restart caddy

echo "[+] Gateway pret (10.42.0.10, domaine expolab.lan)."
