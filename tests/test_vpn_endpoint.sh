#!/usr/bin/env bash
# Test de vpn/lib/endpoint.sh : choix de l'adresse (Endpoint) donnee aux pairs
# WireGuard - argument > fichier public-endpoint > IP locale. Lance en CI et a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }

# shellcheck source=../vpn/lib/endpoint.sh
source "$REPO/vpn/lib/endpoint.sh"

# --- validation ---
for ok in "82.230.154.153:51820" "vpn.exemple.fr:51820" "vpn.expolab.nopalpite.fr:443" "a:1" "h-1.example.org:65535"; do
    valid_endpoint "$ok" || fail "'$ok' aurait du etre valide"
done
for bad in "" "sans-port" "vpn.exemple.fr:" "vpn.exemple.fr:0" "vpn.exemple.fr:65536" "vpn.exemple.fr:abc" ":51820" "-vpn.fr:51820" "vpn fr:51820" 'vpn$(x).fr:51820' "vpn.fr:51820;rm"; do
    ! valid_endpoint "$bad" || fail "'$bad' aurait du etre refuse"
done

# --- ordre de resolution ---
mkdir -p "$WORK/cfg"
resolve_public_endpoint "" "$WORK/cfg" "192.168.1.77" 51820
[ "$ENDPOINT" = "192.168.1.77:51820" ] && [ "$ENDPOINT_SOURCE" = "locale" ] || fail "sans configuration : IP locale attendue, obtenu $ENDPOINT ($ENDPOINT_SOURCE)"

printf '  vpn.exemple.fr:51821 \n' > "$WORK/cfg/public-endpoint"
resolve_public_endpoint "" "$WORK/cfg" "192.168.1.77" 51820
[ "$ENDPOINT" = "vpn.exemple.fr:51821" ] && [ "$ENDPOINT_SOURCE" = "configuree" ] || fail "fichier public-endpoint non utilise : $ENDPOINT ($ENDPOINT_SOURCE)"

resolve_public_endpoint "1.2.3.4:5000" "$WORK/cfg" "192.168.1.77" 51820
[ "$ENDPOINT" = "1.2.3.4:5000" ] && [ "$ENDPOINT_SOURCE" = "argument" ] || fail "l'argument explicite doit l'emporter : $ENDPOINT"

# fichier vide ou que des espaces : comme s'il n'y en avait pas
: > "$WORK/cfg/public-endpoint"
resolve_public_endpoint "" "$WORK/cfg" "192.168.1.77" 51820
[ "$ENDPOINT_SOURCE" = "locale" ] || fail "fichier vide : IP locale attendue"

# valeurs invalides : refusees avec un message (jamais ecrites dans une config cliente)
( resolve_public_endpoint "n'importe quoi" "$WORK/cfg" "192.168.1.77" 51820 ) 2> "$WORK/err.txt" && fail "argument invalide accepte"
grep -q "Endpoint invalide" "$WORK/err.txt" || fail "message d'erreur de l'argument absent"
echo 'vpn.exemple.fr' > "$WORK/cfg/public-endpoint"   # port manquant
( resolve_public_endpoint "" "$WORK/cfg" "192.168.1.77" 51820 ) 2> "$WORK/err.txt" && fail "fichier invalide accepte"
grep -q "public-endpoint invalide" "$WORK/err.txt" || fail "message d'erreur du fichier absent"

echo "OK : vpn/lib/endpoint.sh"
