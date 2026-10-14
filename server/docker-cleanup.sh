#!/usr/bin/env bash
# Libere la place prise par Docker : cache de construction et images sans nom.
#
# Chaque installation reconstruit les images (install.sh, deploy-server.sh) et rien
# ne nettoyait le cache : constate 7 Go de cache inutilise sur une carte de 29 Go.
# On garde les KEEP_STORAGE derniers Mo/Go de cache (les reconstructions suivantes
# restent rapides) et on supprime le reste ; les images encore utilisees par un
# conteneur ne sont jamais touchees. Appele en fin d'installation, utilisable a la
# main. Un echec ne doit JAMAIS faire echouer l'installation : on ne fait que du menage.
#
# Usage: server/docker-cleanup.sh        (KEEP_STORAGE=2GB par defaut)
set -uo pipefail

KEEP_STORAGE="${KEEP_STORAGE:-2GB}"

if ! command -v docker >/dev/null 2>&1; then
    exit 0
fi

echo "[+] Nettoyage de Docker (cache de construction au-dela de $KEEP_STORAGE, images sans nom)..."
# Le nom de l'option a change : --max-used-space (Docker recent), --keep-storage (ancien) ;
# en dernier recours, tout ce qui a plus de 3 jours.
if ! docker builder prune -f --max-used-space "$KEEP_STORAGE" > /dev/null 2>&1; then
    docker builder prune -f --keep-storage "$KEEP_STORAGE" > /dev/null 2>&1         || docker builder prune -f --filter until=72h > /dev/null 2>&1 || true
fi
docker image prune -f 2>&1 | tail -1 || true
exit 0
