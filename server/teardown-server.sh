#!/usr/bin/env bash
# Arrete/supprime le serveur d'expo (Dockhand + toutes les stacks
# applicatives). Symetrique de deploy-server.sh. Le reseau expo-lan reste :
# voir network/teardown.sh.
#
# Contourne volontairement l'API Dockhand (contrairement au deploiement) :
# `docker compose down` directement sur chaque stack, plus simple et plus
# robuste (fonctionne meme si Dockhand est deja casse/injoignable). Sans
# consequence sur un "etat fantome" cote Dockhand : son propre volume de
# donnees est aussi supprime ci-dessous, donc toute trace qu'il aurait
# gardee en memoire des stacks disparait de toute facon.
#
# Usage: sudo ./teardown-server.sh [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export REPO_ROOT

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

ASSUME_YES=0
if [[ "${1:-}" == "--yes" ]]; then
    ASSUME_YES=1
fi

if command -v docker &>/dev/null; then
    if [ "$ASSUME_YES" -ne 1 ]; then
        read -r -p "Arreter/supprimer le serveur d'expo (dockhand, dnsmasq, dnsmasq-admin, caddy, caddy-admin, bastion, dashboard, git-mirror, apt-cache, apt-cache-admin, ntp, ntp-admin, ansible-web, sd-flasher) ? [y/N] " ans
        [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule."; exit 0; }
    fi

    TMP_COMPOSE="$(mktemp)"
    trap 'rm -f "$TMP_COMPOSE"' EXIT

    for stack in dnsmasq dnsmasq-admin caddy caddy-admin bastion dashboard git-mirror apt-cache apt-cache-admin ntp ntp-admin ansible-web sd-flasher matter prises-admin audio-admin; do
        compose_file="$SCRIPT_DIR/stacks/$stack/docker-compose.yml"
        if [ -f "$compose_file" ]; then
            echo "[+] Arret de la stack '$stack'..."
            envsubst '${REPO_ROOT}' < "$compose_file" > "$TMP_COMPOSE"
            docker compose -f "$TMP_COMPOSE" -p "$stack" down -v || true
        fi
    done

    rm -f "$TMP_COMPOSE"
    trap - EXIT

    if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        echo "[+] Arret de Dockhand..."
        (cd "$SCRIPT_DIR" && docker compose down -v)
    fi
    echo "[-] Serveur d'expo arrete."
else
    echo "[=] Docker absent, rien a arreter."
fi

rm -f "$SCRIPT_DIR/stacks/caddy/Caddyfile"

# Relance des conteneurs coupes (server/lib/resilience.sh) : plus rien a relancer. Le
# watchdog et le journal persistant restent en place.
systemctl disable --now expolab-recover.timer 2>/dev/null || true

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
