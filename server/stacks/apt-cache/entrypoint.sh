#!/bin/sh
# Genere la config d'apt-cacher-ng a partir de l'environnement, puis lance le
# demon au premier plan.
#   APT_CACHE_BIND     adresse d'ecoute (defaut 10.42.0.1 = hote sur expo-lan :
#                      JAMAIS 0.0.0.0, le cache ne doit pas etre joignable
#                      depuis le reseau physique)
#   APT_CACHE_PORT     port (defaut 3142)
#   APT_CACHE_OFFLINE  1 = ne sert que le cache, ne contacte jamais l'amont
set -eu

BIND="${APT_CACHE_BIND:-10.42.0.1}"
PORT="${APT_CACHE_PORT:-3142}"
OFFLINE="${APT_CACHE_OFFLINE:-0}"
case "$OFFLINE" in 0|1) ;; *) echo "APT_CACHE_OFFLINE doit valoir 0 ou 1" >&2; exit 1 ;; esac

cat > /etc/apt-cacher-ng/zz_expolab.conf <<EOF
BindAddress: $BIND
Port: $PORT
OfflineMode: $OFFLINE
EOF

# Le volume du cache est un bind-mount cree par root cote hote.
mkdir -p /var/cache/apt-cacher-ng /var/log/apt-cacher-ng
chown -R apt-cacher-ng:apt-cacher-ng /var/cache/apt-cacher-ng /var/log/apt-cacher-ng

echo "apt-cacher-ng : ecoute sur $BIND:$PORT, mode hors ligne = $OFFLINE"
exec /usr/sbin/apt-cacher-ng -c /etc/apt-cacher-ng ForeGround=1
