#!/usr/bin/env bash
# Deploie le conteneur expo-apps (hote Docker) et sa stack (Dockhand pour
# l'instant). Recoit son IP par DHCP aupres de expo-gw comme un membre
# normal du LAN simule (pas besoin d'IP statique, contrairement a expo-gw).
#
# Usage: ./deploy-apps.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NAME="expo-apps"
IMAGE="images:debian/12"
PROFILE="expo-apps"

if ! command -v incus &>/dev/null; then
    echo "incus introuvable. Lancer d'abord ../incus/install.sh" >&2
    exit 1
fi

if ! incus network show expo-lan &>/dev/null; then
    echo "Le reseau expo-lan n'existe pas. Lancer d'abord ../incus/network-setup.sh" >&2
    exit 1
fi

if ! incus profile show "$PROFILE" &>/dev/null; then
    echo "[+] Creation du profil $PROFILE..."
    incus profile create "$PROFILE"
    incus profile edit "$PROFILE" < "$REPO_ROOT/incus/profiles/expo-apps.yaml"
fi

if incus info "$NAME" &>/dev/null; then
    echo "[=] $NAME existe deja, rien a faire."
    echo "    (pour re-appliquer la stack : incus exec $NAME -- bash -c 'cd /opt/expo-apps && docker compose up -d')"
    exit 0
fi

echo "[+] Creation de $NAME..."
incus launch "$IMAGE" "$NAME" --profile default --profile "$PROFILE" < /dev/null

echo "[+] Attente du demarrage de $NAME..."
# `incus exec -- true` reussit des que le canal exec repond, avant que
# systemd/dbus n'ait fini de demarrer - insuffisant ici car provision-apps.sh
# appelle hostnamectl (qui parle a systemd-hostnamed via dbus). Le profil
# expo-apps (security.nesting=true) semble mettre un peu plus de temps a
# atteindre cet etat que les profils sans nesting (fake-pi, expo-gw).
for _ in $(seq 1 30); do
    if incus exec "$NAME" -- test -S /run/dbus/system_bus_socket < /dev/null &>/dev/null; then
        break
    fi
    sleep 2
done

echo "[+] Attente de la resolution DNS (apt-get/curl en ont besoin)..."
for _ in $(seq 1 15); do
    if incus exec "$NAME" -- getent hosts deb.debian.org < /dev/null &>/dev/null; then
        break
    fi
    sleep 2
done

incus file push "$SCRIPT_DIR/docker-compose.yml" "$NAME/root/docker-compose.yml" < /dev/null
incus file push "$SCRIPT_DIR/provision-apps.sh" "$NAME/root/provision-apps.sh" --mode 0755 < /dev/null

incus exec "$NAME" -- /root/provision-apps.sh "$NAME" < /dev/null

cat <<EOF

[+] expo-apps pret.

Dockhand tourne sur le port 3000 de expo-apps - pas encore expose via
Caddy. Ajouter une entree dans gateway/services.yaml puis relancer
gateway/deploy-gateway.sh pour l'exposer en https://dockhand.web.expolab.lan
EOF
