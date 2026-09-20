#!/usr/bin/env bash
# Desinstalle WireGuard et retire toute trace de la config VPN. Symetrique
# de install.sh. Contourne l'API Dockhand comme server/teardown-server.sh
# (docker compose down directement, plus robuste).
#
# Usage: sudo ./uninstall.sh [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
fi

WG_DIR="$SCRIPT_DIR/wireguard/config"

if ! docker inspect wireguard &>/dev/null && [ ! -d "$WG_DIR" ]; then
    echo "[=] WireGuard n'est pas installe, rien a faire."
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    echo "Ceci va arreter le VPN, supprimer tous les pairs et la stack Docker"
    echo "'wireguard'."
    read -r -p "Continuer ? [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
fi

if command -v docker &>/dev/null; then
    echo "[+] Arret de la stack 'wireguard'..."
    TMP_COMPOSE="$(mktemp)"
    REPO_ROOT="$REPO_ROOT" envsubst '${REPO_ROOT}' < "$SCRIPT_DIR/wireguard/docker-compose.yml" > "$TMP_COMPOSE"
    docker compose -f "$TMP_COMPOSE" -p wireguard down -v || true
    rm -f "$TMP_COMPOSE"
    docker image rm expolab-wireguard 2>/dev/null || true
fi

echo "[+] Suppression de la configuration..."
rm -rf "$WG_DIR"
rm -f /etc/sysctl.d/99-expolab-wg.conf

# Supprimer le fichier sysctl.d ne desactive pas l'IP forwarding deja actif
# en memoire (il ne fait qu'empecher sa reactivation au prochain boot) -
# desactivation explicite pour un retour reel a l'etat initial.
echo "[+] Desactivation de l'IP forwarding (etait desactive par defaut)..."
sysctl -w net.ipv4.ip_forward=0 >/dev/null

echo "[+] VPN desinstalle. L'hote est revenu a l'etat pre-install.sh."
