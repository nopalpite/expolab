# Image WireGuard minimale, meme forme que server/stacks/dnsmasq/Dockerfile.
# Tourne en network_mode: host (cf docker-compose.yml) : les regles
# PostUp/PostDown de wg0.conf visent directement l'interface expo-lan de
# l'hote, ce qu'un reseau Docker isole ne permet pas.
FROM debian:trixie-slim

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard-tools iptables iproute2 && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# wg-quick lui-meme se termine des l'interface montee (pas concu pour
# tourner en premier plan) - entrypoint.sh le garde comme PID 1 du
# conteneur et coupe proprement le tunnel a l'arret.
ENTRYPOINT ["/entrypoint.sh"]
