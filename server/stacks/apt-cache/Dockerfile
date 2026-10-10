# Cache apt du lab (apt-cacher-ng) : les machines d'expo installent leurs
# paquets via ce cache, qui les telecharge en amont s'il ne les a pas et les
# garde. Les machines n'ont pas internet (expo-lan sans NAT), le serveur si :
# le cache est le seul chemin vers les paquets. APT_CACHE_OFFLINE=1 (serveur
# lui-meme coupe d'internet) le limite a ce qu'il a deja.
#
# apt verifie lui-meme les signatures des depots d'origine : ce cache ne peut
# pas alterer un paquet sans que ca se voie. Il ne gere que le HTTP (les
# depots Debian et Raspberry Pi OS le sont) : un depot HTTPS n'est pas relaye
# et echoue sur une machine isolee.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apt-cacher-ng && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
