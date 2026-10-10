#!/bin/sh
# Genere la config d'apt-cacher-ng a partir de l'environnement, puis lance le
# demon au premier plan.
#   APT_CACHE_BIND     adresse d'ecoute (defaut 10.42.0.1 = hote sur expo-lan :
#                      JAMAIS 0.0.0.0, le cache ne doit pas etre joignable
#                      depuis le reseau physique)
#   APT_CACHE_PORT     port (defaut 3142)
#   APT_CACHE_OFFLINE  1 = ne sert que le cache, ne contacte jamais l'amont
#   /etc/apt-cacher-ng-expolab/https-mirrors.conf : depots HTTPS joignables par
#                      les machines, une URL https:// par ligne (voir ce fichier)
set -eu

CONF_DIR="${ACNG_CONF_DIR:-/etc/apt-cacher-ng}"   # surchargeable pour les tests
BIND="${APT_CACHE_BIND:-10.42.0.1}"
PORT="${APT_CACHE_PORT:-3142}"
OFFLINE="${APT_CACHE_OFFLINE:-0}"
case "$OFFLINE" in 0|1) ;; *) echo "APT_CACHE_OFFLINE doit valoir 0 ou 1" >&2; exit 1 ;; esac

cat > "$CONF_DIR/zz_expolab.conf" <<EOF
BindAddress: $BIND
Port: $PORT
OfflineMode: $OFFLINE
EOF

# Miroirs HTTPS : les machines n'ont pas internet, le serveur si. Un depot
# declare ici (https://hote/chemin) est demande en HTTP par les machines, sous
# la meme adresse (http://hote/chemin, via le proxy) ; le cache le telecharge
# en HTTPS et le garde. Seuls les depots listes sont joignables ainsi.
MIRRORS="${ACNG_MIRRORS_FILE:-/etc/apt-cacher-ng-expolab/https-mirrors.conf}"
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

echo "apt-cacher-ng : ecoute sur $BIND:$PORT, mode hors ligne = $OFFLINE, miroirs HTTPS = $count"
exec /usr/sbin/apt-cacher-ng -c /etc/apt-cacher-ng ForeGround=1
