# Image dnsmasq minimale. Tourne en network_mode: host (cf
# docker-compose.yml) : dnsmasq a besoin d'un acces bas niveau a
# l'interface expo-lan (broadcast DHCP), ce qu'un reseau Docker isole ne
# permet pas.
FROM debian:trixie-slim

# iproute2 : l'entrypoint attend que l'interface expo-lan porte son adresse
# (pont cree par network/setup.sh) avant de lancer dnsmasq.
# dnsmasq-utils : dhcp_release, utilise par dnsmasq-admin pour liberer le
# bail d'une machine morte sans redemarrer dnsmasq (voir son app.py).
# iputils-arping : savoir si une machine repond encore (ARP sur expo-lan)
# avant de liberer son bail automatiquement.
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq dnsmasq-utils iputils-arping iproute2 && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
