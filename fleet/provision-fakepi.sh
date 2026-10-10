#!/bin/bash
# Provisioning "premier boot" execute A L'INTERIEUR de chaque conteneur
# faux-Pi (pousse et lance par deploy-fleet.sh via `incus exec`).
#
# Usage: provision-fakepi.sh <name> <role> [username] [password]
set -euo pipefail

NAME="${1:?nom du faux Pi manquant}"
ROLE="${2:-sans-ecran}"
FAKEPI_USER="${3:-pi}"
FAKEPI_PASSWORD="${4:-raspberry}"

export DEBIAN_FRONTEND=noninteractive

# Proxy apt vers le cache du serveur (pas de repli direct : les machines n'ont
# pas internet) et serveur de temps - AVANT le premier apt-get, pour que ce
# provisioning lui-meme passe par le cache (qui telecharge pour lui). Voir
# image/machine-setup.sh, pousse par deploy-fleet.sh.
if [ -x /root/machine-setup.sh ]; then
    /root/machine-setup.sh --stage network
    # Les faux Pi n'ont pas internet : sans le cache du serveur, apt ne peut
    # rien installer. Message clair plutot qu'une erreur apt obscure. On
    # attend d'abord le DHCP (le conteneur vient de demarrer).
    cache_up=0
    for _ in $(seq 1 30); do
        if timeout 3 bash -c 'exec 3<>/dev/tcp/10.42.0.1/3142' 2>/dev/null; then
            cache_up=1
            break
        fi
        sleep 2
    done
    if [ "$cache_up" -ne 1 ]; then
        echo "Cache apt du serveur (10.42.0.1:3142) injoignable apres 60 s : lancer d'abord server/deploy-server.sh" >&2
        exit 1
    fi
fi

apt-get update -qq
apt-get install -y -qq openssh-server sudo avahi-daemon python3 >/dev/null
# python3 : deja present sur un vrai Raspberry Pi OS (realisme du lab),
# et requis par Ansible pour executer ses modules sur la machine geree
# (voir bastion-ansible) - sans lui, meme "Gathering Facts" echoue.

echo "[+] Hostname -> $NAME"
hostnamectl set-hostname "$NAME"
echo "$NAME" > /etc/hostname
if grep -q '^127.0.1.1' /etc/hosts; then
    sed -i "s/^127.0.1.1.*/127.0.1.1\t$NAME/" /etc/hosts
else
    echo -e "127.0.1.1\t$NAME" >> /etc/hosts
fi

if ! id "$FAKEPI_USER" >/dev/null 2>&1; then
    echo "[+] Creation de l'utilisateur $FAKEPI_USER"
    useradd -m -s /bin/bash -G sudo "$FAKEPI_USER"
fi
echo "$FAKEPI_USER:$FAKEPI_PASSWORD" | chpasswd

# Identifiants pi/raspberry par defaut pour coller au comportement
# historique des vrais Raspberry Pi (realisme du lab) - configurables par
# entree dans fleet/inventory.yaml (ou via la webui). A changer avant toute
# exposition au-dela du reseau isole du lab.

# Cle d'automatisation + sudo sans mot de passe (comme l'image de la vraie
# vie). Le mot de passe reste actif : Bastion s'en sert pour l'acces humain.
if [ -x /root/machine-setup.sh ] && [ -f /root/automation.pub ]; then
    /root/machine-setup.sh --stage access --user "$FAKEPI_USER" --pubkey-file /root/automation.pub
fi

sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config

echo "role=$ROLE" > /etc/expolab-role
echo "expolab fake-pi :: $NAME (role: $ROLE)" > /etc/motd

systemctl enable --now ssh
systemctl enable --now avahi-daemon

echo "[+] Provisioning de $NAME termine."
