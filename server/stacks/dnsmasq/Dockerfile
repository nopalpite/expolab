# Image dnsmasq minimale. Tourne en network_mode: host (cf
# docker-compose.yml) : dnsmasq a besoin d'un acces bas niveau a
# l'interface expo-lan (broadcast DHCP), ce qu'un reseau Docker isole ne
# permet pas.
FROM debian:trixie-slim

# dnsmasq-utils : dhcp_release, utilise par dnsmasq-admin pour liberer le
# bail d'une machine morte sans redemarrer dnsmasq (voir son app.py).
RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq dnsmasq-utils && \
    rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["dnsmasq", "-k", "--conf-file=/etc/dnsmasq.conf"]
