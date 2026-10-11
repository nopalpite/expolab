# Adresse a laquelle un CLIENT joint le serveur WireGuard (ligne `Endpoint =` de
# sa config) - source par vpn/add-peer.sh.
#
# Derriere un routeur qui redirige le port UDP, un client a l'EXTERIEUR doit
# viser l'adresse PUBLIQUE (IP ou nom DNS/DDNS), pas l'IP locale du Pi. Elle se
# regle une fois, dans vpn/wireguard/config/public-endpoint (une ligne
# "hote:port"), depuis la page VPN du portail ou a la main ; add-peer.sh
# l'utilise ensuite pour tous les nouveaux pairs.
#
# Ordre : argument explicite > fichier public-endpoint > IP locale du Pi
# (utilisable seulement depuis le reseau local).

ENDPOINT_RE='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?:[0-9]{1,5}$'

# valid_endpoint <hote:port> : hote = IPv4 ou nom DNS, port 1-65535.
valid_endpoint() {
    [[ "$1" =~ $ENDPOINT_RE ]] || return 1
    local port="${1##*:}"
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ]
}

# resolve_public_endpoint <explicite|""> <dossier wireguard/config> <ip locale> <port>
# Positionne ENDPOINT et ENDPOINT_SOURCE (argument | configuree | locale).
# Retourne 1 (message sur stderr) si une valeur fournie est invalide.
resolve_public_endpoint() {
    local explicit="$1" wg_dir="$2" lan_ip="$3" port="$4" configured=""
    if [ -n "$explicit" ]; then
        valid_endpoint "$explicit" || { echo "Endpoint invalide : '$explicit' (attendu : hote:port)" >&2; return 1; }
        ENDPOINT="$explicit"; ENDPOINT_SOURCE="argument"
        return 0
    fi
    if [ -f "$wg_dir/public-endpoint" ]; then
        configured="$(tr -d ' \t\r\n' < "$wg_dir/public-endpoint")"
    fi
    if [ -n "$configured" ]; then
        valid_endpoint "$configured" || { echo "public-endpoint invalide : '$configured' (attendu : hote:port)" >&2; return 1; }
        ENDPOINT="$configured"; ENDPOINT_SOURCE="configuree"
        return 0
    fi
    ENDPOINT="${lan_ip}:${port}"; ENDPOINT_SOURCE="locale"
}
