#!/usr/bin/env bash
# Acces des machines de l'expo a internet, via le NAT du bridge expo-lan.
#
# Etat normal : OFF. Les machines n'ont aucun acces a internet ; leurs
# paquets passent par le cache apt du serveur (stack apt-cache), qui lui a
# acces a internet. Seul le serveur sort.
#
#   off     : isole les machines (etat normal, applique par deploy-server.sh)
#   on      : rend temporairement internet aux machines (depannage)
#   status  : affiche l'etat courant
#
# Usage: ./lan-internet.sh on|off|status   (membre du groupe incus-admin, ou root)
set -euo pipefail

NETWORK=expo-lan

case "${1:-status}" in
    on)  value=true ;;
    off) value=false ;;
    status)
        if [ "$(incus network get "$NETWORK" ipv4.nat)" = "true" ]; then
            echo "les machines ont acces a internet (NAT actif)"
        else
            echo "machines isolees (pas d'acces a internet, seul le serveur sort)"
        fi
        exit 0
        ;;
    *) echo "Usage: $0 on|off|status" >&2; exit 2 ;;
esac

incus network set "$NETWORK" ipv4.nat="$value"
echo "[+] $NETWORK : ipv4.nat=$value"
