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

mkdir -p "$WORK/bin" "$WORK/cons" "$WORK/sys/eth0" "$WORK/sys/eth1" "$WORK/sys/wlan0/wireless"
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
    "-g GENERAL.CONNECTION device show "*) echo "${FAKE_DEV_CON:-}" ;;
    "connection show "*) [ -f "$NM_DIR/$3" ] ;;
    "connection add "*)
        name=$(sed -n 's/.*con-name \([^ ]*\).*/\1/p' <<< "$*"); echo "$*" > "$NM_DIR/$name" ;;
    "connection modify "*) echo "$*" >> "$NM_DIR/$3" ;;
    "connection up expo-lan-port") [ -z "${FAKE_NO_CARRIER:-}" ] ;;
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

echo "OK : reseau de l'expo (pont expo-lan)"
