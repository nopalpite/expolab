#!/usr/bin/env bash
# Bascule le cache apt (stack apt-cache) entre :
#   online  : telecharge en amont ce qu'il n'a pas, et le garde (defaut, mode
#             normal : les machines n'ont pas internet, le serveur si)
#   offline : ne sert QUE son cache, ne contacte jamais internet - seulement
#             si le SERVEUR lui-meme est coupe d'internet
#   restart : redemarre le cache dans son mode courant (a faire apres avoir
#             modifie stacks/apt-cache/config/https-mirrors.conf a la main)
#   status  : affiche le mode courant
#
# Le mode est le fichier stacks/apt-cache/config/mode, relu au demarrage du
# conteneur : un simple `docker restart` suffit (pas de recreation de stack).
# L'interface "Cache apt" du portail fait la meme chose.
#
# Usage: sudo ./apt-cache-mode.sh online|offline|restart|status
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE_FILE="$SCRIPT_DIR/stacks/apt-cache/config/mode"
CONTAINER=expolab-apt-cache

current() { tr -d ' \t\r\n' < "$MODE_FILE" 2>/dev/null || echo online; }

case "${1:-status}" in
    online|offline)
        if [ "$(current)" = "$1" ]; then
            echo "[=] Deja en mode $1."
            exit 0
        fi
        mkdir -p "$(dirname "$MODE_FILE")"
        echo "$1" > "$MODE_FILE"
        ;;
    restart) ;;
    status)
        [ "$(current)" = "offline" ] && echo "hors ligne (sert uniquement son cache)" || echo "en ligne"
        exit 0
        ;;
    *) echo "Usage: $0 online|offline|restart|status" >&2; exit 2 ;;
esac

echo "[+] Cache apt ($(current)) : redemarrage..."
docker restart "$CONTAINER" > /dev/null
