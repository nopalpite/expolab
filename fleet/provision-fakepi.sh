#!/bin/bash
# Provisioning "premier boot" execute A L'INTERIEUR de chaque conteneur
# faux-Pi (pousse et lance par deploy-fleet.sh via `incus exec`).
#
# Usage: provision-fakepi.sh <name> <role>
set -euo pipefail

NAME="${1:?nom du faux Pi manquant}"
ROLE="${2:-sans-ecran}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq openssh-server sudo avahi-daemon >/dev/null

echo "[+] Hostname -> $NAME"
hostnamectl set-hostname "$NAME"
echo "$NAME" > /etc/hostname
if grep -q '^127.0.1.1' /etc/hosts; then
    sed -i "s/^127.0.1.1.*/127.0.1.1\t$NAME/" /etc/hosts
else
    echo -e "127.0.1.1\t$NAME" >> /etc/hosts
fi

if ! id pi >/dev/null 2>&1; then
    echo "[+] Creation de l'utilisateur pi"
    useradd -m -s /bin/bash -G sudo pi
    echo "pi:raspberry" | chpasswd
fi

# Mot de passe par defaut "raspberry" pour coller au comportement historique
# des vrais Raspberry Pi (realisme du lab) - A CHANGER avant toute exposition
# au-dela du reseau isole du lab.

sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

echo "role=$ROLE" > /etc/expolab-role
echo "expolab fake-pi :: $NAME (role: $ROLE)" > /etc/motd

systemctl enable --now ssh
systemctl enable --now avahi-daemon

echo "[+] Provisioning de $NAME termine."
