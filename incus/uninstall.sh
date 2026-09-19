#!/usr/bin/env bash
# Desinstalle Incus et retire le depot Zabbly. Symetrique de install.sh.
#
# ATTENTION : purge TOUTES les donnees Incus restantes (/var/lib/incus),
# pas seulement celles d'expolab - normalement il ne doit plus rien y avoir
# si network-teardown.sh et fleet/teardown-fleet.sh ont deja ete executes.
#
# Usage: sudo ./uninstall.sh [--yes]
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
fi

if ! command -v incus &>/dev/null; then
    echo "[=] incus n'est pas installe, rien a faire."
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    echo "Ceci va desinstaller Incus et supprimer /var/lib/incus (conteneurs,"
    echo "images, reseaux restants inclus)."
    read -r -p "Continuer ? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
fi

systemctl stop incus 2>/dev/null || true

# Les daemons dnsmasq lances par Incus pour chaque reseau (dont le
# incusbr0 par defaut cree par `incus admin init`) tournent sous
# l'utilisateur systeme "incus" mais ne sont pas rattaches a l'unite
# systemd incus.service : `systemctl stop incus` ne les arrete pas, et ils
# bloqueraient ensuite `userdel incus` plus bas.
pkill -u incus 2>/dev/null || true
sleep 1

echo "[+] Suppression des paquets Incus..."
apt-get purge -y incus incus-client
apt-get autoremove -y

echo "[+] Suppression des donnees residuelles..."
rm -rf /var/lib/incus

# apt purge ne supprime pas les interfaces reseau live creees par
# `incus admin init` (ex: incusbr0, bridge par defaut) - elles survivent
# tant qu'on ne les retire pas explicitement.
echo "[+] Nettoyage des interfaces reseau residuelles..."
for iface in incusbr0 incusbr1; do
    ip link delete "$iface" 2>/dev/null && echo "    - $iface retiree" || true
done

# Par convention Debian, le postrm du paquet ne supprime pas l'utilisateur
# et les groupes systeme crees a l'installation (evite la reutilisation
# d'UID/GID) - on les retire explicitement pour un retour propre a l'etat
# initial.
echo "[+] Nettoyage de l'utilisateur/groupes systeme incus..."
userdel incus 2>/dev/null && echo "    - utilisateur incus retire" || true
groupdel incus 2>/dev/null && echo "    - groupe incus retire" || true
groupdel incus-admin 2>/dev/null && echo "    - groupe incus-admin retire" || true

echo "[+] Retrait du depot Zabbly..."
rm -f /etc/apt/sources.list.d/zabbly-incus-stable.list
rm -f /etc/apt/keyrings/zabbly.asc
apt-get update -qq

REAL_USER="${SUDO_USER:-$USER}"
gpasswd -d "$REAL_USER" incus-admin 2>/dev/null || true

echo "[+] Incus desinstalle. L'hote est revenu a l'etat pre-install.sh."
