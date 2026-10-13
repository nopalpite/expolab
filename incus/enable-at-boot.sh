#!/usr/bin/env bash
# Fait demarrer Incus AU DEMARRAGE de l'hote. Idempotent.
#
# Pourquoi : le paquet Incus (depot Zabbly) installe incus.service SANS demarrage
# automatique ("indirect") : le demon ne se lance que lorsqu'un client ouvre sa
# socket. Apres une coupure de courant personne ne l'ouvre, donc le pont
# expo-lan (10.42.0.1) n'existe pas, dnsmasq ne peut pas demarrer, et les
# clients du VPN - dont le DNS est 10.42.0.1 - perdent la resolution des noms.
# Constate apres une coupure de courant (le demon ne s'est lance qu'au passage de
# install.sh, qui appelle `incus`).
#
# Appele par incus/install.sh et par le deploiement du serveur (installations
# existantes). SYSTEMD_DIR sert aux tests.
set -euo pipefail

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
LINK="$SYSTEMD_DIR/multi-user.target.wants/incus.service"

if [ -e "$LINK" ]; then
    echo "[=] Incus demarre deja au demarrage de l'hote."
    exit 0
fi
if ! systemctl cat incus.service >/dev/null 2>&1; then
    echo "incus.service introuvable : Incus n'est pas installe (lancer incus/install.sh)." >&2
    exit 1
fi

echo "[+] Incus : demarrage automatique au demarrage de l'hote (pont expo-lan, flotte)..."
# add-wants : le paquet n'a pas de section [Install] pour incus.service, donc
# `systemctl enable incus` ne ferait rien.
systemctl add-wants multi-user.target incus.service
