#!/usr/bin/env bash
# Defait network/setup.sh : supprime le pont expo-lan et le rattachement de
# la carte. La carte redevient une carte ordinaire geree par NetworkManager.
# A lancer APRES server/teardown-server.sh (dnsmasq ecoute sur le pont).
#
# Usage: sudo ./teardown.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

if ! command -v nmcli &>/dev/null; then
    echo "[=] NetworkManager absent, rien a nettoyer."
    exit 0
fi

for con in expo-lan-port expo-lan; do
    if nmcli connection show "$con" &>/dev/null; then
        nmcli connection delete "$con" >/dev/null
        echo "[-] Connexion $con supprimee."
    else
        echo "[=] Connexion $con deja absente."
    fi
done

echo "[+] Reseau de l'expo demonte."
