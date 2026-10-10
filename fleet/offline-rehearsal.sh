#!/usr/bin/env bash
# Repetition du scenario "une carte meurt, on la remplace, on lance le
# playbook" quand les machines n'ont PAS acces a internet (etat normal de
# l'expo : seul le serveur sort, via le cache apt qui telecharge a la demande).
#
# Deroulement :
#   1. NAT d'expo-lan coupe (si ce n'est pas deja le cas) : les faux Pi
#      n'atteignent plus internet.
#   2. Machine "rehearsal-offline" creee de zero (nom et MAC inedits, meme
#      parcours qu'une carte de remplacement) : provisioning par le cache apt.
#   3. Verification qu'elle n'a vraiment aucun acces a internet.
#   4. TOUS les roles de site.yml appliques depuis le conteneur ansible-web,
#      par cle SSH (sans mot de passe) et sudo sans mot de passe.
#   5. Rapport, puis restauration du NAT d'origine et suppression de la
#      machine de test - meme en cas d'echec.
#
# Un role qui telecharge autre chose que des paquets Debian (pip, curl, un
# depot apt en HTTPS non declare...) echoue ici : c'est le but, on le decouvre
# avant l'expo. Les autres faux Pi perdent internet pendant la repetition si
# le NAT etait actif (restaure a la fin). Ne valide PAS le materiel reel
# (firmware, GPIO, Raspberry Pi OS). Hors CI GitHub (necessite Incus).
#
# Usage: ./offline-rehearsal.sh [--keep]
#   --keep  conserve la machine de test a la fin (inspection manuelle)
# (membre des groupes incus-admin et docker, ou root)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAN_INTERNET="$REPO_ROOT/incus/lan-internet.sh"
WEB=expolab-ansible-web
MACHINE=rehearsal-offline
MAC=dc:a6:32:00:98:02

KEEP=0
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        *) echo "Option inconnue : $arg" >&2; exit 2 ;;
    esac
done

for tool in incus docker; do
    command -v "$tool" >/dev/null || { echo "$tool introuvable." >&2; exit 1; }
done
for container in expolab-apt-cache "$WEB"; do
    [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ] \
        || { echo "Le conteneur $container ne tourne pas (deploy-server.sh d'abord)." >&2; exit 1; }
done

ORIG_NAT="$(incus network get expo-lan ipv4.nat)"
TMP="$(mktemp -d)"
RESULT="ECHEC"

cleanup() {
    echo
    echo "[+] Restauration (NAT d'origine : $ORIG_NAT)..."
    incus network set expo-lan ipv4.nat="$ORIG_NAT" >/dev/null 2>&1 || true
    [ "$KEEP" -eq 0 ] && incus delete -f "$MACHINE" >/dev/null 2>&1
    docker exec "$WEB" rm -f /tmp/rehearsal-inv >/dev/null 2>&1 || true
    rm -rf "$TMP"
    echo "=== Repetition : $RESULT ==="
}
trap cleanup EXIT

echo "=== Isolement des machines ==="
"$LAN_INTERNET" off

echo "[+] Creation et provisionnement de $MACHINE (de zero)..."
incus delete -f "$MACHINE" >/dev/null 2>&1 || true
printf 'fleet:\n- name: %s\n  mac: %s\n  role: rehearsal\n' "$MACHINE" "$MAC" > "$TMP/inventory.yaml"
"$SCRIPT_DIR/deploy-fleet.sh" "$TMP/inventory.yaml" || { echo "Echec du provisionnement." >&2; exit 1; }

IP=""
for _ in $(seq 1 30); do
    IP="$(incus list "$MACHINE" -c4 --format csv | cut -d' ' -f1)"
    [ -n "$IP" ] && break
    sleep 2
done
[ -n "$IP" ] || { echo "$MACHINE n'a pas d'IP." >&2; exit 1; }
echo "[+] $MACHINE -> $IP"

echo "[+] Verification : la machine n'a aucun acces a internet..."
if incus exec "$MACHINE" -- timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null; then
    echo "Echec : $MACHINE atteint internet (NAT toujours actif ?) - repetition non concluante." >&2
    exit 1
fi

# Tous les roles de site.yml : la machine est mise dans chaque groupe, sans
# passer par Bastion (elle n'y est pas enregistree).
GROUPS_LIST="$(docker exec -w /repo "$WEB" python3 -c \
    "import yaml; print(' '.join(p['hosts'] for p in yaml.safe_load(open('site.yml'))))")" || exit 1
[ -n "$GROUPS_LIST" ] || { echo "Aucun role dans site.yml (miroir pas synchronise ?)" >&2; exit 1; }
INV=""
for g in $GROUPS_LIST; do INV="${INV}[$g]\n$MACHINE ansible_host=$IP\n"; done
docker exec "$WEB" sh -c "printf '$INV' > /tmp/rehearsal-inv"

echo "[+] Authentification par cle seule (sans mot de passe) vers $MACHINE..."
docker exec "$WEB" ssh -i /keys/automation_ed25519 -o BatchMode=yes -o PasswordAuthentication=no \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "pi@$IP" 'sudo -n true' \
    || { echo "Echec : cle SSH ou sudo sans mot de passe non fonctionnels." >&2; exit 1; }

echo "[+] ansible-playbook site.yml sur $MACHINE (groupes : $GROUPS_LIST)..."
docker exec -w /repo "$WEB" ansible-playbook -i /tmp/rehearsal-inv site.yml --limit "$MACHINE" \
    2>&1 | tee "$TMP/run.log"
# `tee` masque le code de retour d'ansible : on lit le bilan.
if grep -qE "failed=0" "$TMP/run.log" && ! grep -qE "unreachable=[1-9]|failed=[1-9]" "$TMP/run.log"; then
    RESULT="SUCCES - machine reconstruite sans acces a internet"
else
    cp "$TMP/run.log" /tmp/offline-rehearsal-failure.log 2>/dev/null || true
    echo "Journal conserve : /tmp/offline-rehearsal-failure.log" >&2
fi
