#!/usr/bin/env bash
# Supprime le conteneur expo-apps. Symetrique de deploy-apps.sh.
#
# Usage: ./teardown-apps.sh [--yes]
set -euo pipefail

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
fi

if ! command -v incus &>/dev/null; then
    echo "[=] incus n'est pas installe, rien a faire."
    exit 0
fi

if ! incus info expo-apps &>/dev/null; then
    echo "[=] expo-apps n'existe pas."
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Supprimer le conteneur expo-apps (Dockhand + services applicatifs) ? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
fi

incus delete expo-apps --force
echo "[-] expo-apps supprime."

# Docker (avec security.nesting) dans expo-apps semble activer l'IP
# forwarding au niveau de l'hote, pas seulement dans le netns du
# conteneur - remise a zero explicite pour un retour reel a l'etat
# initial (best-effort : ignore silencieusement si lance sans privileges
# root, ex. hors de rollback.sh).
sysctl -w net.ipv4.ip_forward=0 >/dev/null 2>&1 || true
