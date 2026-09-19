#!/usr/bin/env bash
# Annule ce que network-setup.sh a mis en place : profil fake-pi et
# reseau expo-lan. Symetrique de network-setup.sh. A lancer APRES
# fleet/teardown-fleet.sh (un profil/reseau encore utilise par une
# instance ne peut pas etre supprime). Note : server/teardown-server.sh
# n'a pas de profil Incus a nettoyer, son stack tourne directement sur
# l'hote.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

if ! command -v incus &>/dev/null; then
    echo "[=] incus n'est pas installe, rien a nettoyer cote profil/reseau Incus."
    exit 0
fi

if incus profile show fake-pi &>/dev/null; then
    if incus profile delete fake-pi 2>/tmp/expolab-profile-err; then
        echo "[-] Profil fake-pi supprime."
    else
        echo "[!] Impossible de supprimer le profil fake-pi (encore utilise ?) :" >&2
        cat /tmp/expolab-profile-err >&2
        echo "    -> lancer d'abord ../fleet/teardown-fleet.sh" >&2
    fi
    rm -f /tmp/expolab-profile-err
else
    echo "[=] Profil fake-pi deja absent."
fi

if incus network show expo-lan &>/dev/null; then
    if incus network delete expo-lan 2>/tmp/expolab-network-err; then
        echo "[-] Reseau expo-lan supprime."
    else
        echo "[!] Impossible de supprimer le reseau expo-lan :" >&2
        cat /tmp/expolab-network-err >&2
    fi
    rm -f /tmp/expolab-network-err
else
    echo "[=] Reseau expo-lan deja absent."
fi

echo "[+] Reseau demonte."
