#!/usr/bin/env bash
# Bascule le cache apt (stack apt-cache) entre :
#   online  : telecharge en amont ce qu'il n'a pas, et le garde (defaut, mode
#             normal : les machines n'ont pas internet, le serveur si)
#   offline : ne sert QUE son cache, ne contacte jamais internet - seulement
#             si le SERVEUR lui-meme est coupe d'internet
#   restart : recree le cache dans son mode courant (a faire apres avoir modifie
#             stacks/apt-cache/https-mirrors.conf)
#   status  : affiche le mode courant
#
# Une variable d'environnement n'est lue qu'au demarrage d'un conteneur : la
# bascule recree donc la stack (sans reconstruire l'image).
#
# Usage: sudo ./apt-cache-mode.sh online|offline|restart|status
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export REPO_ROOT
ENV_FILE="$SCRIPT_DIR/stacks/apt-cache/apt-cache.env"

current() { grep '^APT_CACHE_OFFLINE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo 0; }

case "${1:-status}" in
    online) value=0 ;;
    restart) value="$(current)"; FORCE=1 ;;
    offline) value=1 ;;
    status)
        [ "$(current)" = "1" ] && echo "hors ligne (sert uniquement son cache)" || echo "en ligne"
        exit 0
        ;;
    *) echo "Usage: $0 online|offline|restart|status" >&2; exit 2 ;;
esac

if [ "${FORCE:-0}" != "1" ] && [ "$(current)" = "$value" ]; then
    echo "[=] Deja en mode $1."
    exit 0
fi

mkdir -p "$(dirname "$ENV_FILE")"
echo "APT_CACHE_OFFLINE=$value" > "$ENV_FILE"
echo "[+] Cache apt ($1) : recreation de la stack..."
python3 "$SCRIPT_DIR/dockhand_stack.py" upsert apt-cache "$SCRIPT_DIR/stacks/apt-cache/docker-compose.yml" --no-rebuild
