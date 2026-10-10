#!/usr/bin/env bash
# Repetition du scenario "une carte meurt, on la remplace, on lance le
# playbook, SANS internet" - sur des faux Pi, avant l'exposition.
#
# Deroulement :
#   1. (--prime) machine "rehearsal-prime" creee et configuree EN LIGNE : son
#      provisioning et les roles Ansible passent par le cache apt du serveur,
#      qui telecharge et garde tout ce dont ils ont besoin (amorcage).
#   2. Coupure : NAT d'expo-lan desactive (les faux Pi n'ont plus internet) et
#      cache apt passe en mode hors ligne (il ne contacte plus l'amont).
#   3. Machine "rehearsal-offline" creee de zero (autre MAC, meme parcours
#      qu'une carte de remplacement) : provisioning, puis TOUS les roles de
#      site.yml depuis le conteneur ansible-web, par cle SSH (sans mot de
#      passe) et sudo sans mot de passe.
#   4. Rapport, puis restauration de l'etat initial (NAT, mode du cache,
#      machines de test supprimees) - meme en cas d'echec.
#
# Les autres faux Pi perdent internet pendant la repetition (restaure a la
# fin). Ne valide PAS le materiel reel (firmware, GPIO, Raspberry Pi OS) : un
# conteneur Debian ne le simule pas. Hors CI GitHub (necessite Incus).
#
# Usage: sudo ./offline-rehearsal.sh [--prime] [--keep]
#   --prime  amorce d'abord le cache en ligne (a faire au moins une fois)
#   --keep   conserve la machine de test a la fin (inspection manuelle)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CACHE_MODE="$REPO_ROOT/server/apt-cache-mode.sh"
WEB=expolab-ansible-web
NETWORK=expo-lan

PRIME=0
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --prime) PRIME=1 ;;
        --keep) KEEP=1 ;;
        *) echo "Option inconnue : $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo) : il change le mode du cache apt." >&2
    exit 1
fi

for tool in incus docker; do
    command -v "$tool" >/dev/null || { echo "$tool introuvable." >&2; exit 1; }
done
for container in expolab-apt-cache "$WEB"; do
    [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ] \
        || { echo "Le conteneur $container ne tourne pas (deploy-server.sh d'abord)." >&2; exit 1; }
done

ORIG_NAT="$(incus network get "$NETWORK" ipv4.nat)"
ORIG_CACHE="$("$CACHE_MODE" status | grep -q 'hors ligne' && echo offline || echo online)"
TMP="$(mktemp -d)"
RESULT="ECHEC"

cleanup() {
    echo
    echo "[+] Restauration (NAT=$ORIG_NAT, cache=$ORIG_CACHE)..."
    incus network set "$NETWORK" ipv4.nat="$ORIG_NAT" >/dev/null 2>&1 || true
    "$CACHE_MODE" "$ORIG_CACHE" >/dev/null 2>&1 || true
    if [ "$KEEP" -eq 0 ]; then
        incus delete -f rehearsal-prime rehearsal-offline >/dev/null 2>&1 || true
    fi
    docker exec "$WEB" rm -f /tmp/rehearsal-inv >/dev/null 2>&1 || true
    rm -rf "$TMP"
    echo "=== Repetition : $RESULT ==="
}
trap cleanup EXIT

# build_machine <nom> <mac> : cree et provisionne un faux Pi de zero, comme
# pour une carte de remplacement. Retourne son IP sur stdout.
build_machine() {
    local name="$1" mac="$2"
    incus delete -f "$name" >/dev/null 2>&1 || true
    printf 'fleet:\n- name: %s\n  mac: %s\n  role: rehearsal\n' "$name" "$mac" > "$TMP/inventory.yaml"
    echo "[+] Creation et provisionnement de $name..." >&2
    "$SCRIPT_DIR/deploy-fleet.sh" "$TMP/inventory.yaml" >&2 || return 1
    local ip="" i
    for i in $(seq 1 30); do
        ip="$(incus list "$name" -c4 --format csv | cut -d' ' -f1)"
        [ -n "$ip" ] && break
        sleep 2
    done
    [ -n "$ip" ] || return 1
    echo "$ip"
}

# run_roles <nom> <ip> : applique TOUS les roles de site.yml (le conteneur
# ansible-web et son environnement : cle SSH, utilisateur, inventaire). La
# machine est mise dans chaque groupe de role, sans passer par Bastion.
run_roles() {
    local name="$1" ip="$2" groups
    groups="$(docker exec -w /repo "$WEB" python3 -c \
        "import yaml; print(' '.join(p['hosts'] for p in yaml.safe_load(open('site.yml'))))")" || return 1
    [ -n "$groups" ] || { echo "Aucun role dans site.yml (miroir pas synchronise ?)" >&2; return 1; }

    local inv="" g
    for g in $groups; do inv="${inv}[$g]\n$name ansible_host=$ip\n"; done
    docker exec "$WEB" sh -c "printf '$inv' > /tmp/rehearsal-inv"

    echo "[+] Auth par cle seule (sans mot de passe) vers $name..."
    docker exec "$WEB" ssh -i /keys/automation_ed25519 -o BatchMode=yes -o PasswordAuthentication=no \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "pi@$ip" 'sudo -n true' \
        || { echo "Echec : cle SSH ou sudo sans mot de passe non fonctionnels sur $name" >&2; return 1; }

    echo "[+] ansible-playbook site.yml sur $name (groupes : $groups)..."
    docker exec -w /repo "$WEB" ansible-playbook -i /tmp/rehearsal-inv site.yml --limit "$name" \
        2>&1 | tee "$TMP/run-$name.log"
    # `tee` masque le code de retour d'ansible : on lit le bilan.
    grep -qE "failed=0" "$TMP/run-$name.log" && ! grep -qE "unreachable=[1-9]|failed=[1-9]" "$TMP/run-$name.log"
}

echo "[+] Synchronisation du depot Ansible (ansible-web)..."
curl -fsS "http://127.0.0.1:5055/api/tags" >/dev/null || { echo "ansible-web ne repond pas / miroir absent." >&2; exit 1; }

if [ "$PRIME" -eq 1 ]; then
    echo
    echo "=== 1/2 : amorcage du cache, EN LIGNE ==="
    incus network set "$NETWORK" ipv4.nat=true
    "$CACHE_MODE" online
    prime_ip="$(build_machine rehearsal-prime dc:a6:32:00:98:01)" || { echo "Echec de la creation de rehearsal-prime" >&2; exit 1; }
    run_roles rehearsal-prime "$prime_ip" || { echo "Echec de l'amorcage" >&2; exit 1; }
    incus delete -f rehearsal-prime >/dev/null 2>&1 || true
fi

echo
echo "=== Coupure d'internet ==="
incus network set "$NETWORK" ipv4.nat=false
"$CACHE_MODE" offline
sleep 3

offline_ip=""
offline_ip="$(build_machine rehearsal-offline dc:a6:32:00:98:02)" || { echo "Echec de la creation de rehearsal-offline (cache amorce ? --prime)" >&2; exit 1; }

echo "[+] Verification : la machine n'a vraiment plus internet..."
if incus exec rehearsal-offline -- timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null; then
    echo "Echec : rehearsal-offline atteint encore internet (NAT toujours actif ?) - repetition non concluante." >&2
    exit 1
fi

if run_roles rehearsal-offline "$offline_ip"; then
    RESULT="SUCCES - machine reconstruite sans internet"
else
    echo "Echec des roles hors ligne : voir $TMP/run-rehearsal-offline.log (conserve jusqu'a la fin du script)" >&2
    cp "$TMP/run-rehearsal-offline.log" /tmp/offline-rehearsal-failure.log 2>/dev/null || true
    echo "Copie du journal : /tmp/offline-rehearsal-failure.log" >&2
fi
