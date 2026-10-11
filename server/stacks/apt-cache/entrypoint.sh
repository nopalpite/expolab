#!/bin/sh
# Genere la config d'apt-cacher-ng a partir de l'environnement, puis lance le
# demon au premier plan.
#   APT_CACHE_BIND     adresse d'ecoute (defaut 10.42.0.1 = hote sur expo-lan :
#                      JAMAIS 0.0.0.0, le cache ne doit pas etre joignable
#                      depuis le reseau physique)
#   APT_CACHE_PORT     port (defaut 3142)
#   APT_CACHE_ADMIN_SECRET  mot de passe de la page d'administration INTEGREE
#                      d'apt-cacher-ng (acng-report.html), sinon ouverte a toutes
#                      les machines (purge, "massive download"...)
#   /etc/apt-cacher-ng-expolab/ (dossier monte en lecture seule) :
#     mode               "online" (defaut) ou "offline" = ne sert que le cache,
#                        ne contacte jamais l'amont
#     https-mirrors.conf depots HTTPS joignables par les machines, une URL
#                        https:// par ligne (voir ce fichier)
#   Lus au demarrage : un `docker restart` suffit a les prendre en compte
#   (apt-cache-admin et server/apt-cache-mode.sh les modifient puis redemarrent).
set -eu

CONF_DIR="${ACNG_CONF_DIR:-/etc/apt-cacher-ng}"   # surchargeable pour les tests
EXPOLAB_DIR="${ACNG_EXPOLAB_DIR:-/etc/apt-cacher-ng-expolab}"
BIND="${APT_CACHE_BIND:-10.42.0.1}"
PORT="${APT_CACHE_PORT:-3142}"
MODE=online
[ -f "$EXPOLAB_DIR/mode" ] && MODE="$(tr -d ' \t\r\n' < "$EXPOLAB_DIR/mode")"
case "$MODE" in
    online) OFFLINE=0 ;;
    offline) OFFLINE=1 ;;
    *) echo "mode invalide dans $EXPOLAB_DIR/mode : '$MODE' (online ou offline attendu)" >&2; exit 1 ;;
esac

cat > "$CONF_DIR/zz_expolab.conf" <<EOF
BindAddress: $BIND
Port: $PORT
OfflineMode: $OFFLINE
EOF
# Page d'administration integree : verrouillee par un secret genere au deploiement.
if [ -n "${APT_CACHE_ADMIN_SECRET:-}" ]; then
    echo "AdminAuth: admin:$APT_CACHE_ADMIN_SECRET" >> "$CONF_DIR/zz_expolab.conf"
fi

# Miroirs HTTPS : les machines n'ont pas internet, le serveur si. Un depot
# declare ici (https://hote/chemin) est demande en HTTP par les machines, sous
# la meme adresse (http://hote/chemin, via le proxy) ; le cache le telecharge
# en HTTPS et le garde. Seuls les depots listes sont joignables ainsi.
MIRRORS="$EXPOLAB_DIR/https-mirrors.conf"
count=0
if [ -f "$MIRRORS" ]; then
    while read -r url extra; do
        case "$url" in ''|'#'*) continue ;; esac
        if ! echo "$url" | grep -Eq '^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$' || [ -n "$extra" ]; then
            echo "https-mirrors.conf : ligne invalide ignoree : $url $extra" >&2
            continue
        fi
        url="${url%/}"
        count=$((count + 1))
        echo "Remap-https$count: ${url#https://} ; $url" >> "$CONF_DIR/zz_expolab.conf"
    done < "$MIRRORS"
fi
# apt-cacher-ng refuse (403) les types de fichiers qu'il ne connait pas : la
# cle GPG d'un depot tiers (.../gpg, .gpg, .asc, .key) doit pouvoir passer.
if [ "$count" -gt 0 ]; then
    echo 'VfilePatternEx: (^|/)(gpg|[^/]+\.(gpg|asc|key))$' >> "$CONF_DIR/zz_expolab.conf"
fi

# ACNG_RENDER_ONLY=1 : ne genere que la configuration (tests).
[ "${ACNG_RENDER_ONLY:-0}" = "1" ] && exit 0

# Le volume du cache est un bind-mount cree par root cote hote.
mkdir -p /var/cache/apt-cacher-ng /var/log/apt-cacher-ng
chown -R apt-cacher-ng:apt-cacher-ng /var/cache/apt-cacher-ng /var/log/apt-cacher-ng

echo "apt-cacher-ng : ecoute sur $BIND:$PORT, mode = $MODE, miroirs HTTPS = $count"
exec /usr/sbin/apt-cacher-ng -c /etc/apt-cacher-ng ForeGround=1
