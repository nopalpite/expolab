#!/bin/sh
# Genere la config de chrony a partir du dossier de configuration, puis lance le
# demon au premier plan.
#
# /etc/ntp-expolab/ (dossier monte en lecture seule ; ntp-admin le modifie puis
# redemarre le conteneur) :
#   mode            "online" (defaut) : synchronise sur les serveurs amont ;
#                   "offline" : n'en contacte aucun, sert l'horloge du serveur
#   upstream.conf   serveurs amont, un nom ou une adresse par ligne (mode online)
#   allow.conf      reseaux autorises a demander l'heure, un CIDR par ligne
#                   (defaut 10.42.0.0/24 : expo-lan, jamais le reseau physique)
#   local-fallback  "on" (defaut) : sans amont joignable, sert quand meme l'heure
#                   du serveur (strate 10) - une machine neuve a une horloge fausse et
#                   sans cela apt refuse un Release "pas encore valide". Force a "on"
#                   en mode offline (sinon le serveur ne servirait rien d'utile).
# Les lignes invalides sont ignorees avec un message : un fichier abime ne doit pas
# empecher le serveur de temps de demarrer.
set -eu

IN_DIR="${NTP_CONFIG_DIR:-/etc/ntp-expolab}"      # surchargeables pour les tests
OUT="${NTP_OUT:-/etc/chrony/chrony.conf}"

MODE=online
[ -f "$IN_DIR/mode" ] && MODE="$(tr -d ' \t\r\n' < "$IN_DIR/mode")"
case "$MODE" in
    online|offline) ;;
    *) echo "mode invalide dans $IN_DIR/mode : '$MODE' (online ou offline attendu)" >&2; exit 1 ;;
esac

FALLBACK=on
[ -f "$IN_DIR/local-fallback" ] && FALLBACK="$(tr -d ' \t\r\n' < "$IN_DIR/local-fallback")"
[ "$MODE" = offline ] && FALLBACK=on

{
    echo "# Genere par server/stacks/ntp/entrypoint.sh - ne pas editer (voir ntp-admin)."
    echo "driftfile /var/lib/chrony/chrony.drift"
    # Une horloge tres fausse (carte sans pile RTC) est corrigee d'un coup au lieu d'etre
    # rattrapee lentement : saut permis lors des 3 premieres mises a jour si l'ecart depasse 1 s.
    echo "makestep 1 3"
    # Pas de port de commande reseau : ntp-admin parle a chronyd par la socket Unix partagee.
    echo "cmdport 0"
} > "$OUT"

# Adresse IPv4 ou reseau CIDR valide ? (chronyd refuse de demarrer sur une adresse invalide :
# une faute de frappe dans allow.conf ferait boucler le conteneur.)
valid_net() {
    _ip="${1%%/*}"
    case "$1" in
        */*)
            _mask="${1#*/}"
            case "$_mask" in ''|*[!0-9]*) return 1 ;; esac   # "10.0.0.0/" et "10.0.0.0/a" sont invalides
            [ "$_mask" -le 32 ] || return 1
            ;;
    esac
    _old="$IFS"; IFS=.; set -- $_ip; IFS="$_old"
    [ "$#" -eq 4 ] || return 1
    for _o in "$@"; do
        case "$_o" in ''|*[!0-9]*) return 1 ;; esac
        [ "$_o" -le 255 ] || return 1
    done
}

# Reseaux autorises
allowed=0
if [ -f "$IN_DIR/allow.conf" ]; then
    while read -r net extra; do
        case "$net" in ''|'#'*) continue ;; esac
        if ! valid_net "$net" || [ -n "$extra" ]; then
            echo "allow.conf : ligne invalide ignoree : $net $extra" >&2
            continue
        fi
        echo "allow $net" >> "$OUT"
        allowed=$((allowed + 1))
    done < "$IN_DIR/allow.conf"
else
    echo "allow 10.42.0.0/24" >> "$OUT"
    allowed=1
fi
[ "$allowed" -gt 0 ] || echo "allow.conf : aucun reseau autorise, personne ne pourra demander l'heure." >&2

# Serveurs amont (mode online uniquement)
if [ "$MODE" = online ] && [ -f "$IN_DIR/upstream.conf" ]; then
    while read -r host extra; do
        case "$host" in ''|'#'*) continue ;; esac
        if ! echo "$host" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' || [ -n "$extra" ]; then
            echo "upstream.conf : ligne invalide ignoree : $host $extra" >&2
            continue
        fi
        echo "server $host iburst" >> "$OUT"
    done < "$IN_DIR/upstream.conf"
fi

[ "$FALLBACK" = on ] && echo "local stratum 10" >> "$OUT"

[ -z "${NTP_RENDER_ONLY:-}" ] || exit 0

mkdir -p /run/chrony /var/lib/chrony
# Dossier de la socket de commande (monte depuis l'hote) : chrony refuse ("Wrong permissions")
# et desactive la socket s'il est plus ouvert que 750 - ntp-admin n'aurait alors aucun etat.
chmod 750 /run/chrony
# /run/chrony est un dossier de l'HOTE : apres un arret brutal (coupure de courant), le
# fichier PID du chronyd precedent y reste. Il contient "1" - le PID de chronyd dans son
# conteneur - et le nouveau chronyd, lui aussi PID 1, se croit deja lance et s'arrete en
# boucle ("Another chronyd may already be running"). Ce conteneur est le seul chronyd qui
# utilise ce dossier : un PID present ici est toujours celui d'un demarrage precedent.
rm -f /run/chrony/chronyd.pid
# root : le conteneur garde SYS_TIME et la socket de commande reste lisible par ntp-admin.
exec chronyd -d -u root
