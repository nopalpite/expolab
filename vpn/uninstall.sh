#!/usr/bin/env bash
# Desinstalle WireGuard et retire toute trace de la config VPN. Symetrique
# de install.sh.
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

WG_IFACE="wg0"
WG_DIR="/etc/wireguard"

if ! dpkg -l wireguard 2>/dev/null | grep -q '^ii'; then
    echo "[=] wireguard n'est pas installe, rien a faire."
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    echo "Ceci va arreter le VPN, supprimer tous les pairs et desinstaller"
    echo "wireguard-tools."
    read -r -p "Continuer ? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
fi

echo "[+] Arret du tunnel..."
systemctl disable --now "wg-quick@$WG_IFACE" 2>/dev/null || true

echo "[+] Suppression de la configuration..."
rm -rf "$WG_DIR"
rm -f /etc/sysctl.d/99-expolab-wg.conf

# Supprimer le fichier sysctl.d ne desactive pas l'IP forwarding deja actif
# en memoire (il ne fait qu'empecher sa reactivation au prochain boot) -
# desactivation explicite pour un retour reel a l'etat initial.
echo "[+] Desactivation de l'IP forwarding (etait desactive par defaut)..."
sysctl -w net.ipv4.ip_forward=0 >/dev/null

echo "[+] Desinstallation des paquets..."
apt-get purge -y wireguard wireguard-tools 2>/dev/null || true
apt-get autoremove -y

echo "[+] VPN desinstalle. L'hote est revenu a l'etat pre-install.sh."
