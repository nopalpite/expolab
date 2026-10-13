#!/bin/sh
# Attend que l'interface d'ecoute de dnsmasq porte son adresse, puis le lance.
#
# L'interface et l'adresse sont LUES dans dnsmasq.conf (lignes `interface=` et
# `dhcp-option=option:router,`) : ce script suit la configuration, il ne depend ni
# d'Incus ni d'un nom en dur. Aujourd'hui expo-lan est un pont cree par Incus et il
# peut ne pas exister encore au demarrage de l'hote ; avec un vrai reseau, ce sera
# une carte reseau qui monte plus ou moins vite - meme attente.
#
# Sans cette attente, dnsmasq ("bind-interfaces") s'arrete aussitot et le conteneur
# boucle en erreur - or le DNS du VPN est cette adresse. Au bout de MAX attentes
# (10 min par defaut) on s'arrete en ERREUR : une interface mal nommee doit se voir,
# pas laisser un conteneur "en marche" qui ne sert personne.
CONF="${DNSMASQ_CONF:-/etc/dnsmasq.conf}"
INTERVAL="${DNSMASQ_WAIT_INTERVAL:-2}"
MAX="${DNSMASQ_WAIT_MAX:-300}"
LOG_EVERY=15   # un message toutes les LOG_EVERY attentes

IFACE=$(sed -n 's/^interface=\([^[:space:]#]*\).*/\1/p' "$CONF" 2>/dev/null | head -1)
ADDR=$(sed -n 's/^dhcp-option=option:router,\([0-9.]*\).*/\1/p' "$CONF" 2>/dev/null | head -1)
IFACE="${DNSMASQ_WAIT_IFACE:-${IFACE:-expo-lan}}"
ADDR="${DNSMASQ_WAIT_ADDR:-${ADDR:-10.42.0.1}}"

waits=0
while ! ip -4 -o addr show dev "$IFACE" 2>/dev/null | grep -q "inet $ADDR/"; do
    if [ "$waits" -ge "$MAX" ]; then
        echo "dnsmasq : l'interface $IFACE ($ADDR) n'est pas apparue apres $waits attentes - verifier le reseau de l'hote et 'interface='/'dhcp-option=option:router' dans dnsmasq.conf." >&2
        exit 1
    fi
    if [ $((waits % LOG_EVERY)) -eq 0 ]; then
        echo "dnsmasq : en attente de l'interface $IFACE ($ADDR)..."
    fi
    waits=$((waits + 1))
    sleep "$INTERVAL"
done
[ "$waits" -gt 0 ] && echo "dnsmasq : $IFACE disponible apres $waits attente(s), demarrage."

exec dnsmasq -k --conf-file="$CONF"
