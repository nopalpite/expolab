#!/usr/bin/env bash
# Lit fleet/inventory.yaml et cree/provisionne les conteneurs manquants.
# Idempotent : une instance deja presente est laissee telle quelle.
#
# Usage: ./deploy-fleet.sh [chemin_inventory.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVENTORY="${1:-$SCRIPT_DIR/inventory.yaml}"
IMAGE="images:debian/12"
PROFILE="fake-pi"

if ! command -v incus &>/dev/null; then
    echo "incus introuvable. Lancer d'abord ../incus/install.sh" >&2
    exit 1
fi

if ! incus profile show "$PROFILE" &>/dev/null; then
    echo "Le profil '$PROFILE' n'existe pas. Lancer d'abord ../incus/network-setup.sh" >&2
    exit 1
fi

TMP_TSV="$(mktemp)"
trap 'rm -f "$TMP_TSV"' EXIT

python3 - "$INVENTORY" > "$TMP_TSV" <<'EOF'
import sys, yaml

with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}

for pi in data.get("fleet", []):
    name = pi["name"]
    mac = pi["mac"]
    role = pi.get("role", "sans-ecran")
    username = pi.get("username", "pi")
    password = pi.get("password", "raspberry")
    print(f"{name}\t{mac}\t{role}\t{username}\t{password}")
EOF

# Lu entierement en memoire (pas de `done < fichier` sur la boucle) : des
# commandes comme `incus exec` forwardent le stdin herite par defaut, et
# consommeraient sinon le fichier TSV en cours de lecture par `read`,
# corrompant les iterations suivantes.
mapfile -t FLEET_LINES < "$TMP_TSV"

for line in "${FLEET_LINES[@]}"; do
    IFS=$'\t' read -r name mac role username password <<< "$line"
    [ -z "${name:-}" ] && continue

    if incus info "$name" &>/dev/null; then
        echo "[=] $name existe deja, on passe."
        continue
    fi

    echo "[+] Creation de $name (mac=$mac, role=$role, user=$username)"
    # init (pas launch) + override MAC + un seul start : evite un premier
    # boot avec une MAC aleatoire suivi d'un `incus restart` pour corriger -
    # cet `incus restart` s'est avere se bloquer indefiniment de facon
    # intermittente sur ce lab (operation serveur qui aboutit reellement,
    # mais dont le suivi cote demon Incus ne se termine jamais - la
    # commande CLI attend alors un signal de fin qui ne vient jamais).
    incus init "$IMAGE" "$name" --profile default --profile "$PROFILE" < /dev/null
    incus config device override "$name" eth0 hwaddr="$mac" < /dev/null
    incus start "$name" < /dev/null

    echo "[+] Attente du demarrage reseau de $name..."
    for _ in $(seq 1 15); do
        if incus exec "$name" -- true < /dev/null &>/dev/null; then
            break
        fi
        sleep 2
    done

    incus file push "$SCRIPT_DIR/provision-fakepi.sh" "$name/root/provision-fakepi.sh" --mode 0755 < /dev/null
    incus exec "$name" -- /root/provision-fakepi.sh "$name" "$role" "$username" "$password" < /dev/null

    echo "[+] $name pret."
done

echo
echo "[+] Etat de la flotte :"
incus list
