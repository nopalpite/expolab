#!/bin/bash
# Provisioning "premier boot" de expo-apps : installe Docker + Docker
# Compose. Execute A L'INTERIEUR du conteneur par deploy-apps.sh, une
# seule fois a la creation - le deploiement/mise a jour de la stack
# elle-meme (docker-compose.yml, webui/) est gere separement par
# deploy-apps.sh a chaque run, pas ici.
set -euo pipefail

NAME="${1:?nom du conteneur manquant}"

export DEBIAN_FRONTEND=noninteractive

echo "[+] Hostname -> $NAME"
hostnamectl set-hostname "$NAME"
echo "$NAME" > /etc/hostname
if grep -q '^127.0.1.1' /etc/hosts; then
    sed -i "s/^127.0.1.1.*/127.0.1.1\t$NAME/" /etc/hosts
else
    echo -e "127.0.1.1\t$NAME" >> /etc/hosts
fi

apt-get update -qq
apt-get install -y -qq ca-certificates curl >/dev/null

echo "[+] Installation de Docker (script officiel get.docker.com)..."
curl -fsSL https://get.docker.com | sh >/dev/null

mkdir -p /opt/expo-apps

echo "[+] expo-apps pret (Docker installe)."
