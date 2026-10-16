#!/usr/bin/env bash
# Cree le reseau de l'expo "expo-lan" sur une VRAIE carte reseau du serveur :
# les machines de l'expo (vrais Raspberry Pi) sont branchees sur un switch
# relie a cette carte. Le serveur y porte 10.42.0.1 et y sert DHCP/DNS
# (stack dnsmasq), l'heure, le cache apt et le reverse-proxy.
#
# expo-lan est un PONT (bridge NetworkManager) dont la carte choisie est le
# seul port, plutot que l'adresse posee directement sur la carte :
#   - le nom "expo-lan" ne depend pas de la carte (eth1, enx..., etc.) :
#     dnsmasq.conf, le VPN et les pages d'administration n'ont rien a changer ;
#   - un pont garde son adresse meme cable debranche (NetworkManager ignore
#     la porteuse des ponts) : dnsmasq demarre, et le DNS des clients VPN
#     (10.42.0.1) reste joignable, meme switch eteint.
#
# La carte choisie ne doit PAS etre celle qui donne internet au serveur
# (route par defaut) : elle passe entierement au reseau de l'expo.
# Pas de NAT : les machines n'ont pas acces a internet, leurs paquets passent
# par le cache apt du serveur. Depannage : network/lan-internet.sh on.
#
# Idempotent : relancer sans argument garde la carte deja choisie ; avec un
# autre nom, le pont change de carte.
#
# Usage: sudo ./setup.sh [interface] [--force] [--sans-carte]
#   interface    : carte reseau de l'expo (defaut : celle deja configuree, sinon
#                  $EXPO_LAN_IFACE, sinon eth1)
#   --force      : reprendre la carte meme si une autre connexion l'utilise
#   --sans-carte : cree le pont SEUL, sans le rattacher a une carte - pour un test
#                  du deploiement sur un serveur qui n'a qu'une carte Ethernet.
#                  Tous les services demarrent (ils n'ont besoin que du pont et de
#                  son adresse) mais aucune machine ne peut s'y connecter. Rattacher
#                  une carte plus tard : sudo ./setup.sh <interface> (le pont est
#                  conserve, la carte s'y ajoute).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

BRIDGE=expo-lan
PORT_CON=expo-lan-port
ADDRESS=10.42.0.1/24
SYS_CLASS_NET="${SYS_CLASS_NET:-/sys/class/net}"   # remplace par les tests

IFACE=""
FORCE=0
NO_PORT=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --sans-carte) NO_PORT=1 ;;
        -*) echo "Option inconnue : $arg" >&2; exit 2 ;;
        *) IFACE="$arg" ;;
    esac
done

if ! command -v nmcli &>/dev/null || ! systemctl is-active --quiet NetworkManager; then
    echo "NetworkManager est requis (reseau par defaut de Raspberry Pi OS) et n'est pas actif." >&2
    exit 1
fi

current="$(nmcli -g connection.interface-name connection show "$PORT_CON" 2>/dev/null || true)"
if [ "$NO_PORT" -eq 1 ]; then
    # Pont seul : aucune verification de carte. Mais jamais en retirant silencieusement une carte deja
    # rattachee (un reseau d'expo reel serait coupe) : on le refuse et on dit comment faire.
    if [ -n "$IFACE" ]; then
        echo "--sans-carte et une carte ($IFACE) sont incompatibles : choisir l'un ou l'autre." >&2
        exit 2
    fi
    if [ -n "$current" ]; then
        echo "Le pont est deja rattache a la carte '$current' : --sans-carte ne la retirera pas." >&2
        echo "Pour repartir de zero : sudo $(dirname "$0")/teardown.sh, puis relancer." >&2
        exit 1
    fi
else
    IFACE="${IFACE:-${current:-${EXPO_LAN_IFACE:-eth1}}}"

    if ! ip link show "$IFACE" &>/dev/null; then
        echo "Carte reseau '$IFACE' introuvable. Cartes presentes :" >&2
        ip -br link | awk '$1 != "lo" {print "    " $1}' >&2
        echo "Relancer avec le bon nom : sudo $0 <interface>" >&2
        echo "(ou --sans-carte pour tester sans carte dediee a l'expo)" >&2
        exit 1
    fi
    if [ -d "$SYS_CLASS_NET/$IFACE/wireless" ]; then
        echo "'$IFACE' est une carte Wi-Fi : le reseau de l'expo doit etre une carte Ethernet (switch)." >&2
        exit 1
    fi
    if ip route show default | grep -q " dev $IFACE\( \|$\)"; then
        echo "'$IFACE' porte la route par defaut (acces internet du serveur) : la donner a l'expo couperait cet acces." >&2
        echo "Choisir l'autre carte (ex: un adaptateur USB-Ethernet)." >&2
        exit 1
    fi

    # Une autre connexion active sur la carte (ex: "Wired connection 2", creee
    # toute seule par NetworkManager au branchement du cable) : on ne la prend
    # pas sans le dire.
    other="$(nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null || true)"
    if [ -n "$other" ] && [ "$other" != "$PORT_CON" ]; then
        if [ "$FORCE" -ne 1 ]; then
            echo "'$IFACE' est utilisee par la connexion '$other'." >&2
            echo "Relancer avec --force pour la donner au reseau de l'expo (la connexion '$other' sera desactivee)." >&2
            exit 1
        fi
        echo "[+] Desactivation de la connexion '$other' sur $IFACE..."
        nmcli connection down "$other" >/dev/null
    fi
fi

echo "[+] Pont $BRIDGE ($ADDRESS, sans passerelle ; IPv6 en lien local seulement)..."
# IPv6 en LIEN LOCAL seulement (fe80::, aucune adresse routable, aucune annonce de routeur) : les
# prises connectees Matter (stack matter) dialoguent avec le serveur en IPv6 local. Sans IPv6 sur le
# pont, le serveur ne les joint pas.
if nmcli connection show "$BRIDGE" &>/dev/null; then
    nmcli connection modify "$BRIDGE" ipv4.method manual ipv4.addresses "$ADDRESS" ipv6.method link-local
else
    # stp no : un seul port, rien a proteger, et pas de delai de 30 s avant
    # que la carte ne transmette (le premier DHCP d'une machine attendrait).
    # never-default : jamais de route par defaut vers l'expo.
    nmcli connection add type bridge ifname "$BRIDGE" con-name "$BRIDGE" \
        bridge.stp no \
        ipv4.method manual ipv4.addresses "$ADDRESS" ipv4.never-default yes \
        ipv6.method link-local \
        connection.autoconnect yes >/dev/null
fi

# Pont deja actif : changements appliques SANS le desactiver (pas de coupure du reseau de l'expo).
nmcli device reapply "$BRIDGE" >/dev/null 2>&1 || nmcli connection up "$BRIDGE" >/dev/null

if [ "$NO_PORT" -eq 0 ]; then
    echo "[+] Carte $IFACE rattachee au pont..."
    if nmcli connection show "$PORT_CON" &>/dev/null; then
        nmcli connection modify "$PORT_CON" connection.interface-name "$IFACE"
    else
        # Priorite haute : au branchement, NetworkManager prend ce profil plutot
        # que d'en creer un en DHCP sur la carte.
        nmcli connection add type ethernet ifname "$IFACE" con-name "$PORT_CON" \
            master "$BRIDGE" slave-type bridge \
            connection.autoconnect yes connection.autoconnect-priority 100 >/dev/null
    fi

    if ! nmcli connection up "$PORT_CON" >/dev/null 2>&1; then
        echo "[=] $IFACE pas encore active (cable debranche ?) : elle rejoindra le pont des son branchement."
    fi
fi

if [ "$NO_PORT" -eq 1 ]; then
    cat <<EOF

[+] Reseau de l'expo pret : $BRIDGE ($ADDRESS), SANS carte (mode test).
    Aucune machine ne peut s'y connecter. Pour rattacher une carte plus tard :
        sudo $0 <interface>

Verification :
    ip -br addr show $BRIDGE

Prochaine etape :
    sudo ../server/deploy-server.sh
EOF
    exit 0
fi

cat <<EOF

[+] Reseau de l'expo pret : $BRIDGE ($ADDRESS) sur la carte $IFACE.

Verification :
    ip -br addr show $BRIDGE
    nmcli connection show --active

Prochaine etape :
    sudo ../server/deploy-server.sh
EOF
