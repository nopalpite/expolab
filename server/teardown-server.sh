#!/usr/bin/env bash
# Arrete/supprime le stack Docker du serveur d'expo et reactive le DHCP
# integre d'Incus sur expo-lan (secours). Symetrique de deploy-server.sh.
#
# Usage: sudo ./teardown-server.sh [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
fi

if command -v docker &>/dev/null && [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    if [ "$ASSUME_YES" -ne 1 ]; then
        read -r -p "Arreter/supprimer le stack Docker (dnsmasq, caddy, dockhand, webui, bastion) ? [y/N] " ans
        [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
    fi
    echo "[+] Arret du stack Docker..."
    (cd "$SCRIPT_DIR" && docker compose down -v)
    echo "[-] Stack arrete."
else
    echo "[=] Docker ou docker-compose.yml absent, rien a arreter."
fi

if incus network show expo-lan &>/dev/null; then
    echo "[+] Reactivation du DHCP/DNS integre d'Incus sur expo-lan..."
    incus network set expo-lan ipv4.dhcp=true
    incus network unset expo-lan dns.mode

    echo "[+] Redemarrage de la flotte pour reprendre un bail Incus..."
    for pi in $(incus list --format csv -c n 2>/dev/null | grep '^pi-' || true); do
        incus restart "$pi" < /dev/null || true
    done
else
    echo "[=] Reseau expo-lan absent, rien a faire."
fi

rm -f "$SCRIPT_DIR/Caddyfile"

if command -v docker &>/dev/null; then
    if [ "$ASSUME_YES" -ne 1 ]; then
        read -r -p "Desinstaller Docker de l'hote (symetrique de son installation par deploy-server.sh) ? [y/N] " ans
    else
        ans="y"
    fi
    if [[ "$ans" =~ ^[yY]$ ]]; then
        echo "[+] Desinstallation de Docker..."
        apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin 2>/dev/null || true
        apt-get autoremove -y
        rm -rf /var/lib/docker /var/lib/containerd
        REAL_USER="${SUDO_USER:-$USER}"
        gpasswd -d "$REAL_USER" docker 2>/dev/null || true
    fi
fi

echo "[+] Serveur d'expo demonte."
