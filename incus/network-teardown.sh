#!/usr/bin/env bash
# Annule ce que network-setup.sh a mis en place : profils fake-pi/expo-gw et
# reseau expo-lan. Symetrique de network-setup.sh. A lancer APRES
# fleet/teardown-fleet.sh et gateway/teardown-gateway.sh (un profil/reseau
# encore utilise par une instance ne peut pas etre supprime).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

if ! command -v incus &>/dev/null; then
    echo "[=] incus n'est pas installe, rien a nettoyer cote profil/reseau Incus."
    exit 0
fi

for profile in fake-pi expo-gw; do
    if incus profile show "$profile" &>/dev/null; then
        if incus profile delete "$profile" 2>/tmp/expolab-profile-err; then
            echo "[-] Profil $profile supprime."
        else
            echo "[!] Impossible de supprimer le profil $profile (encore utilise ?) :" >&2
            cat /tmp/expolab-profile-err >&2
            echo "    -> lancer d'abord ../fleet/teardown-fleet.sh et ../gateway/teardown-gateway.sh" >&2
        fi
        rm -f /tmp/expolab-profile-err
    else
        echo "[=] Profil $profile deja absent."
    fi
done

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
