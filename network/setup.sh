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
# Usage: sudo ./setup.sh [interface] [--force] [--sans-carte] [--wifi-ap]
#   interface    : carte reseau de l'expo (defaut : celle deja configuree, sinon
#                  $EXPO_LAN_IFACE, sinon eth1)
#   --force      : reprendre la carte meme si une autre connexion l'utilise
#   --sans-carte : cree le pont SEUL, sans le rattacher a une carte - pour un test
#                  du deploiement sur un serveur qui n'a qu'une carte Ethernet.
#                  Tous les services demarrent (ils n'ont besoin que du pont et de
#                  son adresse) mais aucune machine ne peut s'y connecter. Rattacher
#                  une carte plus tard : sudo ./setup.sh <interface> (le pont est
#                  conserve, la carte s'y ajoute).
#   --wifi-ap    : l'interface (obligatoire : une carte Wi-Fi, ex. une antenne USB
#                  wlan1) devient un POINT D'ACCES dont les clients sont sur le pont
#                  expo-lan, au lieu d'une carte Ethernet vers un switch. 2,4 GHz,
#                  WPA2. Reglages par l'environnement : EXPO_WIFI_SSID (defaut
#                  expolab), EXPO_WIFI_PASSWORD (8 a 63 caracteres ; demande au
#                  terminal si absent - jamais ecrit dans le depot ni dans un
#                  fichier de ce script ; NetworkManager le garde, lisible par root
#                  seulement), EXPO_WIFI_CHANNEL (1 a 11, defaut 6). Relance sans mot
#                  de passe : le point d'acces deja configure est conserve.
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
WIFI_AP=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --sans-carte) NO_PORT=1 ;;
        --wifi-ap) WIFI_AP=1 ;;
        -*) echo "Option inconnue : $arg" >&2; exit 2 ;;
        *) IFACE="$arg" ;;
    esac
done

if ! command -v nmcli &>/dev/null || ! systemctl is-active --quiet NetworkManager; then
    echo "NetworkManager est requis (reseau par defaut de Raspberry Pi OS) et n'est pas actif." >&2
    exit 1
fi

if [ "$NO_PORT" -eq 1 ] && [ "$WIFI_AP" -eq 1 ]; then
    echo "--sans-carte et --wifi-ap sont incompatibles : choisir l'un ou l'autre." >&2
    exit 2
fi

current="$(nmcli -g connection.interface-name connection show "$PORT_CON" 2>/dev/null || true)"
port_type="$(nmcli -g connection.type connection show "$PORT_CON" 2>/dev/null || true)"
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
    if [ "$WIFI_AP" -eq 1 ]; then
        IFACE="${IFACE:-$current}"
        if [ -z "$IFACE" ]; then
            echo "--wifi-ap demande la carte Wi-Fi a utiliser : sudo $0 <interface> --wifi-ap" >&2
            exit 2
        fi
    else
        IFACE="${IFACE:-${current:-${EXPO_LAN_IFACE:-eth1}}}"
    fi

    if ! ip link show "$IFACE" &>/dev/null; then
        echo "Carte reseau '$IFACE' introuvable. Cartes presentes :" >&2
        ip -br link | awk '$1 != "lo" {print "    " $1}' >&2
        echo "Relancer avec le bon nom : sudo $0 <interface>" >&2
        echo "(ou --sans-carte pour tester sans carte dediee a l'expo)" >&2
        exit 1
    fi
    if [ -d "$SYS_CLASS_NET/$IFACE/wireless" ]; then
        if [ "$WIFI_AP" -ne 1 ]; then
            echo "'$IFACE' est une carte Wi-Fi : le reseau de l'expo doit etre une carte Ethernet (switch)." >&2
            echo "(ou --wifi-ap pour en faire un point d'acces Wi-Fi)" >&2
            exit 1
        fi
    elif [ "$WIFI_AP" -eq 1 ]; then
        echo "'$IFACE' n'est pas une carte Wi-Fi : --wifi-ap demande une antenne Wi-Fi (ex. wlan1)." >&2
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

# Point d'acces : reglages et mot de passe verifies AVANT de toucher a NetworkManager.
KEEP_AP=0
if [ "$WIFI_AP" -eq 1 ]; then
    SSID="${EXPO_WIFI_SSID:-expolab}"
    CHANNEL="${EXPO_WIFI_CHANNEL:-6}"
    PSK="${EXPO_WIFI_PASSWORD:-}"
    case "$CHANNEL" in
        [1-9]|1[01]) ;;
        *) echo "EXPO_WIFI_CHANNEL : un canal de 1 a 11 est attendu (2,4 GHz)." >&2; exit 2 ;;
    esac
    if [ -z "$PSK" ] && [ "$port_type" = "802-11-wireless" ] && [ "$current" = "$IFACE" ]; then
        KEEP_AP=1     # deja configure sur cette antenne : on le garde tel quel
    elif [ -z "$PSK" ]; then
        if [ ! -t 0 ]; then
            echo "Mot de passe du point d'acces absent : definir EXPO_WIFI_PASSWORD (8 a 63 caracteres) ou lancer depuis un terminal." >&2
            exit 1
        fi
        read -rsp "Mot de passe du Wi-Fi '$SSID' (8 a 63 caracteres) : " PSK; echo
    fi
    if [ "$KEEP_AP" -eq 0 ] && { [ "${#PSK}" -lt 8 ] || [ "${#PSK}" -gt 63 ]; }; then
        echo "Le mot de passe Wi-Fi doit faire de 8 a 63 caracteres." >&2
        exit 1
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

if [ "$WIFI_AP" -eq 1 ]; then
    if [ "$KEEP_AP" -eq 1 ]; then
        echo "[=] Point d'acces deja configure sur $IFACE : conserve (SSID $(nmcli -g 802-11-wireless.ssid connection show "$PORT_CON"))."
    else
        echo "[+] Point d'acces Wi-Fi '$SSID' sur $IFACE (WPA2, canal $CHANNEL), rattache au pont..."
        # Un profil Wi-Fi ne se transforme pas d'un profil Ethernet (et inversement) : on repart de zero.
        nmcli connection delete "$PORT_CON" >/dev/null 2>&1 || true
        # Le mot de passe passe par la ligne de commande de nmcli (visible un instant dans la liste
        # des processus de CE serveur) : acceptable ici, jamais ecrit ailleurs que dans NetworkManager.
        nmcli connection add type wifi ifname "$IFACE" con-name "$PORT_CON" ssid "$SSID" \
            802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel "$CHANNEL" \
            802-11-wireless-security.key-mgmt wpa-psk 802-11-wireless-security.psk "$PSK" \
            802-11-wireless-security.proto rsn 802-11-wireless-security.pairwise ccmp \
            802-11-wireless-security.group ccmp \
            master "$BRIDGE" slave-type bridge \
            connection.autoconnect yes connection.autoconnect-priority 100 >/dev/null
    fi
    if ! up_out="$(nmcli connection up "$PORT_CON" 2>&1)"; then
        echo "[!] Le point d'acces ne demarre pas : $up_out" >&2
        echo "    Il reste configure et sera retente au prochain demarrage de $IFACE." >&2
        exit 1
    fi
elif [ "$NO_PORT" -eq 0 ]; then
    echo "[+] Carte $IFACE rattachee au pont..."
    if [ -n "$port_type" ] && [ "$port_type" != "802-3-ethernet" ]; then
        # L'ancien rattachement etait un point d'acces Wi-Fi : autre type de profil, on le remplace.
        nmcli connection delete "$PORT_CON" >/dev/null 2>&1 || true
    fi
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

if [ "$WIFI_AP" -eq 1 ]; then
    cat <<EOF

[+] Reseau de l'expo pret : $BRIDGE ($ADDRESS) en Wi-Fi, point d'acces sur $IFACE.
    Les machines se connectent au reseau Wi-Fi '$(nmcli -g 802-11-wireless.ssid connection show "$PORT_CON")' (2,4 GHz, WPA2) et
    recoivent leur adresse du DHCP du serveur.

Verification :
    ip -br addr show $BRIDGE
    nmcli connection show --active

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
