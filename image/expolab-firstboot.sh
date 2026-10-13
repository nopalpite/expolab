#!/bin/sh
# Premier demarrage d'une carte flashee par sd-flasher. Installe dans le MASTER
# (voir image/README-master.md) et lance par expolab-firstboot.service.
#
# sd-flasher a deja pose, AVANT le demarrage (partition racine) : /etc/hostname,
# /etc/hosts, /etc/machine-id vide, cles SSH d'hote supprimees. Reste ici ce qui
# se fait mieux sur place, a partir de /boot/firmware/expolab.conf :
#   - cles SSH d'hote regenerees (uniques a cette carte) ;
#   - cle(s) autorisee(s) de l'utilisateur ;
#   - Wi-Fi (NetworkManager) ;
#   - filet : hostname re-applique s'il differe de celui du fichier.
# Le fichier est supprime a la fin (il contient le mot de passe Wi-Fi) ; sans
# fichier, le script ne fait que regenerer les cles d'hote manquantes.
#
# Le fichier est LU, jamais execute (pas de `source`) : lignes CLE=VALEUR, cles
# connues uniquement.
set -eu

CONF=""
for candidate in /boot/firmware/expolab.conf /boot/expolab.conf; do
    [ -f "$candidate" ] && CONF="$candidate" && break
done

HOSTNAME_WANTED="" WIFI_SSID="" WIFI_PSK="" WIFI_COUNTRY="FR" AUTHORIZED_KEYS_B64="" SSH_USER=""

if [ -n "$CONF" ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            HOSTNAME) HOSTNAME_WANTED=$value ;;
            WIFI_SSID) WIFI_SSID=$value ;;
            WIFI_PSK) WIFI_PSK=$value ;;
            WIFI_COUNTRY) WIFI_COUNTRY=$value ;;
            AUTHORIZED_KEYS_B64) AUTHORIZED_KEYS_B64=$value ;;
            SSH_USER) SSH_USER=$value ;;
        esac
    done < "$CONF"
fi

# Cles d'hote : aucune apres le flash (supprimees) -> en generer de neuves.
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    ssh-keygen -A
fi

if [ -n "$HOSTNAME_WANTED" ] && [ "$(cat /etc/hostname 2>/dev/null)" != "$HOSTNAME_WANTED" ]; then
    echo "$HOSTNAME_WANTED" > /etc/hostname
    hostname "$HOSTNAME_WANTED"
fi

# Cles autorisees : utilisateur nomme dans le fichier, sinon le premier compte
# normal (uid 1000) - la carte master a un seul utilisateur.
if [ -n "$AUTHORIZED_KEYS_B64" ]; then
    user=${SSH_USER:-$(getent passwd 1000 | cut -d: -f1)}
    if [ -n "$user" ]; then
        home=$(getent passwd "$user" | cut -d: -f6)
        install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
        echo "$AUTHORIZED_KEYS_B64" | base64 -d >> "$home/.ssh/authorized_keys"
        sort -u "$home/.ssh/authorized_keys" -o "$home/.ssh/authorized_keys"
        chown "$user:$user" "$home/.ssh/authorized_keys"
        chmod 600 "$home/.ssh/authorized_keys"
    fi
fi

# Wi-Fi : profil NetworkManager ecrit directement (le reseau n'est pas encore up).
if [ -n "$WIFI_SSID" ] && [ -d /etc/NetworkManager/system-connections ]; then
    profile=/etc/NetworkManager/system-connections/expolab-wifi.nmconnection
    umask 077
    cat > "$profile" <<EOT
[connection]
id=expolab-wifi
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=$WIFI_SSID

[wifi-security]
key-mgmt=wpa-psk
psk=$WIFI_PSK

[ipv4]
method=auto

[ipv6]
method=auto
EOT
    chmod 600 "$profile"
    command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi || true
    command -v iw >/dev/null 2>&1 && iw reg set "$WIFI_COUNTRY" || true
fi

[ -n "$CONF" ] && rm -f "$CONF"
exit 0
