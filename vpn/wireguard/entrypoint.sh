#!/usr/bin/env bash
# wg-quick up/down ne sont pas concus pour tourner en premier plan (up
# demarre l'interface puis se termine immediatement) - ce wrapper la
# garde en vie comme PID 1 du conteneur et coupe proprement le tunnel a
# l'arret (docker stop envoie SIGTERM).
set -euo pipefail

cleanup() {
    echo "[+] Arret du tunnel wg0..."
    wg-quick down wg0 || true
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "[+] Demarrage du tunnel wg0..."
wg-quick up wg0

echo "[+] wg0 actif, en attente (SIGTERM pour arreter proprement)..."
tail -f /dev/null &
wait $!
