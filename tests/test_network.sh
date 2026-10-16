#!/usr/bin/env bash
# Test de network/setup.sh et network/teardown.sh (pont expo-lan sur une vraie
# carte) avec de faux nmcli/ip/systemctl/id : aucun reseau reel touche.
# Le NAT (network/lan-internet.sh) est teste dans test_deploy_libs.sh.
# Lance en CI et a la main : bash tests/test_network.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }

mkdir -p "$WORK/bin" "$WORK/cons" "$WORK/sys/eth0" "$WORK/sys/eth1" "$WORK/sys/wlan0/wireless" "$WORK/sys/wlan1/wireless"
export PATH="$WORK/bin:$PATH" NM_DIR="$WORK/cons" NM_LOG="$WORK/nmcli.log" SYS_CLASS_NET="$WORK/sys"

printf '#!/usr/bin/env bash\necho 0\n' > "$WORK/bin/id"
printf '#!/usr/bin/env bash\nexit 0\n' > "$WORK/bin/systemctl"
cat > "$WORK/bin/ip" <<'EOT'
#!/usr/bin/env bash
case "$*" in
    "link show "*) [ -d "$SYS_CLASS_NET/$3" ] ;;
    "route show default") echo "default via 192.168.1.1 dev eth0 proto dhcp metric 100" ;;
    "-br link") for d in "$SYS_CLASS_NET"/*; do echo "$(basename "$d") UP"; done ;;
esac
EOT
# Faux nmcli : une connexion = un fichier (ses arguments de creation/modification).
cat > "$WORK/bin/nmcli" <<'EOT'
#!/usr/bin/env bash
echo "$*" >> "$NM_LOG"
case "$*" in
    "-g connection.interface-name connection show "*)
        f="$NM_DIR/$5"; [ -f "$f" ] || exit 10
        grep -o 'ifname [^ ]*\|connection.interface-name [^ ]*' "$f" | tail -1 | cut -d' ' -f2 ;;
    "-g connection.type connection show "*)
        f="$NM_DIR/$5"; [ -f "$f" ] || exit 10
        if grep -q 'add type wifi' "$f"; then echo 802-11-wireless; else echo 802-3-ethernet; fi ;;
    "-g 802-11-wireless.ssid connection show "*)
        f="$NM_DIR/$5"; [ -f "$f" ] || exit 10
        sed -n 's/.* ssid \([^ ]*\) .*/\1/p' "$f" ;;
    "-g GENERAL.CONNECTION device show "*) echo "${FAKE_DEV_CON:-}" ;;
    "connection show "*) [ -f "$NM_DIR/$3" ] ;;
    "connection add "*)
        name=$(sed -n 's/.*con-name \([^ ]*\).*/\1/p' <<< "$*"); echo "$*" > "$NM_DIR/$name" ;;
    "connection modify "*) echo "$*" >> "$NM_DIR/$3" ;;
    "connection up expo-lan-port") [ -z "${FAKE_NO_CARRIER:-}" ] ;;
    "device reapply "*) [ -n "${FAKE_BRIDGE_ACTIVE:-}" ] ;;   # seulement sur un pont deja actif
    "connection delete "*) rm -f "$NM_DIR/$3" ;;
esac
EOT
chmod +x "$WORK/bin/"*

SETUP="$REPO/network/setup.sh"

# Carte inconnue, Wi-Fi, ou porteuse de la route par defaut : refus, rien cree.
for bad in eth7 wlan0 eth0; do
    if bash "$SETUP" "$bad" > "$WORK/out.log" 2>&1; then fail "$bad aurait du etre refusee"; fi
done
grep -q "route par defaut" "$WORK/out.log" || fail "message route par defaut absent"
[ -z "$(ls "$NM_DIR")" ] || fail "connexion creee malgre un refus"

# Carte deja prise par une autre connexion : refus sans --force.
if FAKE_DEV_CON="Wired connection 2" bash "$SETUP" eth1 > "$WORK/out.log" 2>&1; then fail "carte occupee prise sans --force"; fi
grep -q -- "--force" "$WORK/out.log" || fail "--force non suggere"

# Cas nominal (cable debranche) : pont + port crees, sans echec.
: > "$NM_LOG"
FAKE_NO_CARRIER=1 bash "$SETUP" eth1 > "$WORK/out.log" 2>&1 || fail "setup.sh eth1 a echoue"
grep -q "type bridge ifname expo-lan con-name expo-lan .*ipv4.addresses 10.42.0.1/24 ipv4.never-default yes" "$NM_DIR/expo-lan" || fail "pont mal configure"
grep -q "ipv6.method link-local" "$NM_DIR/expo-lan" || fail "IPv6 lien local absent (prises Matter)"
grep -q "bridge.stp no" "$NM_DIR/expo-lan" || fail "STP non desactive"
grep -q "type ethernet ifname eth1 con-name expo-lan-port master expo-lan slave-type bridge" "$NM_DIR/expo-lan-port" || fail "port mal configure"
grep -q "rejoindra le pont" "$WORK/out.log" || fail "cable debranche non signale"

# Relance sans argument : garde eth1, ne recree rien.
: > "$NM_LOG"
bash "$SETUP" > "$WORK/out.log" 2>&1 || fail "relance sans argument a echoue"
! grep -q "connection add" "$NM_LOG" || fail "pas idempotent : connexion recreee"
grep -q "sur la carte eth1" "$WORK/out.log" || fail "carte deja choisie non reprise"

# Avec --force, la connexion concurrente est desactivee.
: > "$NM_LOG"
FAKE_DEV_CON="Wired connection 2" bash "$SETUP" eth1 --force > /dev/null 2>&1 || fail "--force a echoue"
grep -q "^connection down Wired connection 2$" "$NM_LOG" || fail "connexion concurrente non desactivee"

# Demontage : les deux connexions disparaissent, idempotent.
bash "$REPO/network/teardown.sh" > /dev/null
[ -z "$(ls "$NM_DIR")" ] || fail "connexions restantes apres teardown"
bash "$REPO/network/teardown.sh" > /dev/null || fail "teardown non idempotent"

# --sans-carte (test sur un serveur a une seule carte) : pont seul, aucune carte exigee.
rm -rf "$WORK/sys/eth1"
if bash "$SETUP" > "$WORK/out.log" 2>&1; then fail "eth1 absente acceptee sans --sans-carte"; fi
grep -q -- "--sans-carte" "$WORK/out.log" || fail "--sans-carte non suggere quand la carte manque"
[ -z "$(ls "$NM_DIR")" ] || fail "connexion creee malgre un refus"

: > "$NM_LOG"
bash "$SETUP" --sans-carte > "$WORK/out.log" 2>&1 || fail "--sans-carte a echoue"
grep -q "ipv4.addresses 10.42.0.1/24 ipv4.never-default yes" "$NM_DIR/expo-lan" || fail "pont --sans-carte mal configure"
[ ! -e "$NM_DIR/expo-lan-port" ] || fail "port cree malgre --sans-carte"
grep -q "^connection up expo-lan$" "$NM_LOG" || fail "pont non active"

# Pont deja actif (relance pour un reglage) : applique sans le desactiver, pas de coupure.
: > "$NM_LOG"
FAKE_BRIDGE_ACTIVE=1 bash "$SETUP" --sans-carte > "$WORK/out.log" 2>&1 || fail "relance sur pont actif en echec"
grep -q "^device reapply expo-lan$" "$NM_LOG" || fail "reglage non applique au pont actif"
! grep -q "^connection up expo-lan$" "$NM_LOG" || fail "pont reactive (coupure du reseau de l'expo)"
! grep -q "^connection \(add\|modify\|up\).*expo-lan-port" "$NM_LOG" || fail "--sans-carte a touche au port"
grep -q "SANS carte" "$WORK/out.log" || fail "mode sans carte non annonce"

# Relance : idempotent, le pont n'est pas recree.
: > "$NM_LOG"
bash "$SETUP" --sans-carte > /dev/null 2>&1 || fail "--sans-carte non rejouable"
! grep -q "connection add" "$NM_LOG" || fail "--sans-carte pas idempotent"

# Incompatible avec une carte explicite.
if bash "$SETUP" eth0 --sans-carte > "$WORK/out.log" 2>&1; then fail "--sans-carte + carte acceptes"; fi
grep -q "incompatibles" "$WORK/out.log" || fail "incompatibilite non expliquee"

# Une carte rattachee plus tard rejoint le pont existant (sans le recreer).
mkdir -p "$WORK/sys/eth1"
: > "$NM_LOG"
FAKE_NO_CARRIER=1 bash "$SETUP" eth1 > /dev/null 2>&1 || fail "rattachement d'une carte a un pont sans carte"
grep -q "type ethernet ifname eth1 con-name expo-lan-port master expo-lan" "$NM_DIR/expo-lan-port" || fail "port non cree apres coup"

# Mais --sans-carte ne retire jamais une carte deja rattachee.
if bash "$SETUP" --sans-carte > "$WORK/out.log" 2>&1; then fail "--sans-carte a ecrase un reseau d'expo en service"; fi
grep -q "teardown.sh" "$WORK/out.log" || fail "teardown non suggere"
[ -f "$NM_DIR/expo-lan-port" ] || fail "port supprime par --sans-carte"

bash "$REPO/network/teardown.sh" > /dev/null
[ -z "$(ls "$NM_DIR")" ] || fail "connexions restantes apres teardown (sans carte)"

# --wifi-ap : l'antenne Wi-Fi devient un point d'acces rattache au pont.
PW="mdp-de-test-12"
unset EXPO_WIFI_PASSWORD EXPO_WIFI_SSID EXPO_WIFI_CHANNEL
# Refus : carte Ethernet, pas d'interface, --sans-carte, Wi-Fi sans --wifi-ap, route par defaut (wlan0 ici n'en a pas,
# donc on teste le refus d'une carte Wi-Fi en mode Ethernet).
if EXPO_WIFI_PASSWORD="$PW" bash "$SETUP" eth1 --wifi-ap > "$WORK/out.log" 2>&1; then fail "--wifi-ap sur une carte Ethernet accepte"; fi
grep -q "pas une carte Wi-Fi" "$WORK/out.log" || fail "refus d'une carte Ethernet non explique"
if EXPO_WIFI_PASSWORD="$PW" bash "$SETUP" --wifi-ap > "$WORK/out.log" 2>&1; then fail "--wifi-ap sans interface accepte"; fi
if EXPO_WIFI_PASSWORD="$PW" bash "$SETUP" wlan1 --wifi-ap --sans-carte > "$WORK/out.log" 2>&1; then fail "--wifi-ap + --sans-carte acceptes"; fi
if bash "$SETUP" wlan1 > "$WORK/out.log" 2>&1; then fail "carte Wi-Fi acceptee sans --wifi-ap"; fi
grep -q -- "--wifi-ap" "$WORK/out.log" || fail "--wifi-ap non suggere pour une carte Wi-Fi"
# Mot de passe absent (pas de terminal) ou trop court : refus AVANT toute modification.
if bash "$SETUP" wlan1 --wifi-ap < /dev/null > "$WORK/out.log" 2>&1; then fail "point d'acces sans mot de passe"; fi
grep -q "EXPO_WIFI_PASSWORD" "$WORK/out.log" || fail "mot de passe absent non explique"
if EXPO_WIFI_PASSWORD="court" bash "$SETUP" wlan1 --wifi-ap > "$WORK/out.log" 2>&1; then fail "mot de passe trop court accepte"; fi
if EXPO_WIFI_PASSWORD="$PW" EXPO_WIFI_CHANNEL=14 bash "$SETUP" wlan1 --wifi-ap > "$WORK/out.log" 2>&1; then fail "canal 14 accepte"; fi
[ -z "$(ls "$NM_DIR")" ] || fail "connexion creee malgre un refus (point d'acces)"

# Cas nominal : SSID par defaut, WPA2, canal 6, port du pont, mot de passe jamais affiche.
: > "$NM_LOG"
EXPO_WIFI_PASSWORD="$PW" bash "$SETUP" wlan1 --wifi-ap > "$WORK/out.log" 2>&1 || fail "--wifi-ap a echoue"
grep -q "type wifi ifname wlan1 con-name expo-lan-port ssid expolab " "$NM_DIR/expo-lan-port" || fail "SSID par defaut absent"
grep -q "802-11-wireless.mode ap" "$NM_DIR/expo-lan-port" || fail "mode point d'acces absent"
grep -q "802-11-wireless.band bg 802-11-wireless.channel 6" "$NM_DIR/expo-lan-port" || fail "bande/canal incorrects"
grep -q "802-11-wireless-security.key-mgmt wpa-psk" "$NM_DIR/expo-lan-port" || fail "WPA2 absent"
grep -q "master expo-lan slave-type bridge" "$NM_DIR/expo-lan-port" || fail "antenne non rattachee au pont"
! grep -q "$PW" "$WORK/out.log" || fail "mot de passe affiche"
grep -q "Wi-Fi 'expolab'\|reseau Wi-Fi 'expolab'" "$WORK/out.log" || fail "SSID non annonce"

# Relance sans mot de passe : conserve, rien n'est recree.
: > "$NM_LOG"
bash "$SETUP" wlan1 --wifi-ap < /dev/null > "$WORK/out.log" 2>&1 || fail "relance sans mot de passe a echoue"
! grep -q "connection add\|connection delete" "$NM_LOG" || fail "point d'acces recree malgre la relance sans mot de passe"
grep -q "conserve" "$WORK/out.log" || fail "conservation non signalee"

# Relance avec mot de passe et autre SSID/canal : le profil est recree.
: > "$NM_LOG"
EXPO_WIFI_PASSWORD="autre-mdp-123" EXPO_WIFI_SSID=salle2 EXPO_WIFI_CHANNEL=11 bash "$SETUP" wlan1 --wifi-ap > /dev/null 2>&1 || fail "reconfiguration a echoue"
grep -q "^connection delete expo-lan-port$" "$NM_LOG" || fail "ancien profil non supprime"
grep -q "ssid salle2 " "$NM_DIR/expo-lan-port" || fail "nouveau SSID absent"
grep -q "channel 11" "$NM_DIR/expo-lan-port" || fail "nouveau canal absent"

# Retour a un switch Ethernet : le profil Wi-Fi est remplace par un profil Ethernet.
: > "$NM_LOG"
FAKE_NO_CARRIER=1 bash "$SETUP" eth1 > /dev/null 2>&1 || fail "retour a Ethernet a echoue"
grep -q "type ethernet ifname eth1 con-name expo-lan-port" "$NM_DIR/expo-lan-port" || fail "profil Ethernet absent"
! grep -q "type wifi" "$NM_DIR/expo-lan-port" || fail "profil Wi-Fi conserve"

bash "$REPO/network/teardown.sh" > /dev/null
[ -z "$(ls "$NM_DIR")" ] || fail "connexions restantes apres teardown (Wi-Fi)"

echo "OK : reseau de l'expo (pont expo-lan)"
