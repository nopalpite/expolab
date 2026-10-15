#!/usr/bin/env bash
# Acces des machines de l'expo a internet, via un NAT du serveur.
#
# Etat normal : OFF. Les machines n'ont aucun acces a internet ; leurs
# paquets passent par le cache apt du serveur (stack apt-cache), qui lui a
# acces a internet. Seul le serveur sort.
#
#   off     : isole les machines (etat normal, applique par deploy-server.sh)
#   on      : rend temporairement internet aux machines (depannage) - les
#             regles ne survivent pas a un redemarrage du serveur
#   status  : affiche l'etat courant
#
# Regles iptables reperees par leur commentaire, inserees en tete de FORWARD
# (avant celles de Docker, dont la politique par defaut est DROP).
#
# Usage: sudo ./lan-internet.sh on|off|status
set -euo pipefail

BRIDGE=expo-lan
SUBNET=10.42.0.0/24
TAG=expolab-lan-internet

NAT_RULE=(POSTROUTING -s "$SUBNET" ! -o "$BRIDGE" -m comment --comment "$TAG" -j MASQUERADE)
OUT_RULE=(FORWARD -i "$BRIDGE" ! -o "$BRIDGE" -m comment --comment "$TAG" -j ACCEPT)
BACK_RULE=(FORWARD -o "$BRIDGE" -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "$TAG" -j ACCEPT)

nat_active() {
    iptables -t nat -C "${NAT_RULE[@]}" 2>/dev/null
}

case "${1:-status}" in
    status)
        if nat_active; then
            echo "les machines ont acces a internet (NAT actif)"
        else
            echo "machines isolees (pas d'acces a internet, seul le serveur sort)"
        fi
        ;;
    on)
        sysctl -qw net.ipv4.ip_forward=1
        iptables -t nat -C "${NAT_RULE[@]}" 2>/dev/null || iptables -t nat -A "${NAT_RULE[@]}"
        iptables -C "${BACK_RULE[@]}" 2>/dev/null || iptables -I "${BACK_RULE[0]}" 1 "${BACK_RULE[@]:1}"
        iptables -C "${OUT_RULE[@]}" 2>/dev/null || iptables -I "${OUT_RULE[0]}" 1 "${OUT_RULE[@]:1}"
        echo "[+] $BRIDGE : NAT actif (machines avec acces a internet)"
        ;;
    off)
        while iptables -t nat -C "${NAT_RULE[@]}" 2>/dev/null; do iptables -t nat -D "${NAT_RULE[@]}"; done
        while iptables -C "${OUT_RULE[@]}" 2>/dev/null; do iptables -D "${OUT_RULE[@]}"; done
        while iptables -C "${BACK_RULE[@]}" 2>/dev/null; do iptables -D "${BACK_RULE[@]}"; done
        echo "[+] $BRIDGE : pas de NAT (machines isolees)"
        ;;
    *) echo "Usage: $0 on|off|status" >&2; exit 2 ;;
esac
