#!/usr/bin/env bash
# Supprime le conteneur expo-gw et redonne la main au DHCP integre d'Incus
# sur expo-lan (secours, cf incus/network-setup.sh). Symetrique de
# deploy-gateway.sh.
#
# Usage: ./teardown-gateway.sh [--yes]
set -euo pipefail

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
fi

if ! command -v incus &>/dev/null; then
    echo "[=] incus n'est pas installe, rien a faire."
    exit 0
fi

if incus info expo-gw &>/dev/null; then
    if [ "$ASSUME_YES" -ne 1 ]; then
        read -r -p "Supprimer le conteneur expo-gw ? [y/N] " ans
        [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
    fi
    incus delete expo-gw --force
    echo "[-] expo-gw supprime."
else
    echo "[=] expo-gw n'existe pas."
fi

if incus network show expo-lan &>/dev/null; then
    echo "[+] Reactivation du DHCP integre d'Incus sur expo-lan..."
    incus network set expo-lan ipv4.dhcp=true

    echo "[+] Redemarrage de la flotte pour reprendre un bail Incus..."
    # Un restart complet du conteneur force une vraie renegociation DHCP
    # (contrairement a un simple restart de systemd-networkd, qui garde le
    # bail precedent tant qu'il n'a pas expire).
    for pi in $(incus list --format csv -c n 2>/dev/null | grep '^pi-' || true); do
        incus restart "$pi" < /dev/null || true
    done
else
    echo "[=] Reseau expo-lan absent, rien a faire."
fi

echo "[+] Gateway demontee."
