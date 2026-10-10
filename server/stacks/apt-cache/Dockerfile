# Cache apt du lab (apt-cacher-ng) : les machines d'expo installent leurs
# paquets via ce cache, qui les telecharge en amont s'il ne les a pas et les
# garde. Une fois "amorce" (voir fleet/offline-rehearsal.sh --prime), il
# permet de reconstruire une machine SANS internet ; en mode hors ligne
# (APT_CACHE_OFFLINE=1) il ne tente plus jamais le reseau.
#
# apt verifie lui-meme les signatures des depots d'origine : ce cache ne peut
# pas alterer un paquet sans que ca se voie. Il ne gere que le HTTP (les
# depots Debian et Raspberry Pi OS le sont) : un depot HTTPS passe en direct
# et echoue hors ligne.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apt-cacher-ng && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
