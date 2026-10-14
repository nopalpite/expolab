# Image du serveur de temps (chrony). Tourne en network_mode: host (voir
# docker-compose.yml) : elle ecoute l'UDP 123 de l'hote sur expo-lan et regle
# l'horloge de l'hote (le noyau est partage) - d'ou la capacite SYS_TIME.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chrony && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
