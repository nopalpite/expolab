#!/usr/bin/env bash
# Deploie le conteneur expo-apps (hote Docker) et sa stack (Dockhand,
# webui expolab). Recoit son IP par DHCP aupres de expo-gw comme un
# membre normal du LAN simule (pas besoin d'IP statique, contrairement a
# expo-gw).
#
# Idempotent : relancer ce script (apres avoir modifie le code de webui/
# par exemple) repousse le code et reconstruit/redemarre la stack Docker
# sans recreer le conteneur.
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
    echo "[=] $NAME existe deja."
else
    echo "[+] Creation de $NAME..."
    incus launch "$IMAGE" "$NAME" --profile default --profile "$PROFILE" < /dev/null

    echo "[+] Attente du demarrage de $NAME..."
    # `incus exec -- true` reussit des que le canal exec repond, avant que
    # systemd/dbus n'ait fini de demarrer - insuffisant ici car
    # provision-apps.sh appelle hostnamectl (qui parle a systemd-hostnamed
    # via dbus). Le profil expo-apps (security.nesting=true) semble mettre
    # un peu plus de temps a atteindre cet etat que les profils sans
    # nesting (fake-pi, expo-gw).
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

    incus file push "$SCRIPT_DIR/provision-apps.sh" "$NAME/root/provision-apps.sh" --mode 0755 < /dev/null
    incus exec "$NAME" -- /root/provision-apps.sh "$NAME" < /dev/null
fi

echo "[+] Montage live de fleet/ dans expo-apps (source de verite partagee avec la webui, pas une copie)..."
# shift=true indispensable : sans ca, les fichiers apparaissent appartenir
# a "nobody:nogroup" a l'interieur du conteneur non-privilegie (l'uid/gid
# reels de l'hote ne correspondent a rien dans son espace de noms) et la
# webui ne peut pas ecrire dans inventory.yaml (deja vu en prod).
if ! incus config device show "$NAME" 2>/dev/null | grep -q '^expolab-fleet:'; then
    incus config device add "$NAME" expolab-fleet disk source="$REPO_ROOT/fleet" path=/opt/expolab/fleet shift=true < /dev/null
else
    incus config device set "$NAME" expolab-fleet shift=true < /dev/null
fi

echo "[+] Mise a jour du code applicatif (docker-compose.yml, webui/)..."
incus exec "$NAME" -- mkdir -p /opt/expo-apps < /dev/null
incus file push "$SCRIPT_DIR/docker-compose.yml" "$NAME/opt/expo-apps/docker-compose.yml" < /dev/null
incus file push -r "$REPO_ROOT/webui" "$NAME/opt/expo-apps/" < /dev/null

echo "[+] (Re)demarrage de la stack Docker..."
incus exec "$NAME" -- bash -c 'cd /opt/expo-apps && docker compose up -d --build' < /dev/null

cat <<EOF

[+] expo-apps pret.
    Dockhand : port 3000
    webui    : port 5050 (gere fleet/inventory.yaml + declenche deploy-fleet.sh)

Pas encore expose via Caddy. Ajouter une entree dans gateway/services.yaml
puis relancer gateway/deploy-gateway.sh, par exemple :
    - name: fleet
      backend_host: expo-apps
      backend_port: 5050
EOF
