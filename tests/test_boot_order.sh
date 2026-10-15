#!/usr/bin/env bash
# Test de la tenue au redemarrage de l'hote (coupure de courant) :
# server/stacks/dnsmasq/entrypoint.sh attend le pont expo-lan au lieu de
# planter en boucle. Faux ip/dnsmasq, aucun service reel. Lance en CI et a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }

mkdir -p "$WORK/bin"
export PATH="$WORK/bin:$PATH"

# --- entrypoint dnsmasq -----------------------------------------------------------
# Faux `ip` : l'interface n'a pas d'adresse aux 3 premiers appels, puis l'obtient
# (celle declaree par dnsmasq.conf - ici le conf du depot, donc expo-lan / 10.42.0.1).
export DNSMASQ_CONF="$REPO/server/stacks/dnsmasq/dnsmasq.conf"
cat > "$WORK/bin/ip" <<'EOT'
#!/usr/bin/env bash
n=$(cat "$FAKE_IP_COUNT" 2>/dev/null || echo 0)
echo $((n + 1)) > "$FAKE_IP_COUNT"
if [ "$n" -ge "${FAKE_IP_READY_AFTER:-3}" ]; then
    echo "5: $6    inet ${FAKE_IP_ADDR:-10.42.0.1}/24 brd 10.42.0.255 scope global $6"
fi
EOT
cat > "$WORK/bin/dnsmasq" <<'EOT'
#!/usr/bin/env bash
echo "dnsmasq lance : $*" > "$FAKE_DNSMASQ_LOG"
EOT
chmod +x "$WORK/bin/ip" "$WORK/bin/dnsmasq"
export FAKE_IP_COUNT="$WORK/ip.count" FAKE_DNSMASQ_LOG="$WORK/dnsmasq.log" DNSMASQ_WAIT_INTERVAL=0.05

rm -f "$FAKE_IP_COUNT"
sh "$REPO/server/stacks/dnsmasq/entrypoint.sh" > "$WORK/out.log" 2>&1 || fail "l'entrypoint doit reussir une fois l'interface apparue"
grep -q "en attente de l'interface expo-lan" "$WORK/out.log" || fail "pas de message d'attente"
grep -q "disponible apres" "$WORK/out.log" || fail "pas de message de reprise"
[ "$(cat "$FAKE_IP_COUNT")" -ge 4 ] || fail "dnsmasq lance sans avoir attendu l'interface"
grep -q "dnsmasq lance : -k --conf-file=$DNSMASQ_CONF" "$FAKE_DNSMASQ_LOG" || fail "dnsmasq pas lance avec les bons arguments"

# Interface deja prete : aucune attente, aucun message d'attente.
rm -f "$FAKE_IP_COUNT" "$FAKE_DNSMASQ_LOG"
FAKE_IP_READY_AFTER=0 sh "$REPO/server/stacks/dnsmasq/entrypoint.sh" > "$WORK/out.log" 2>&1
! grep -q "en attente" "$WORK/out.log" || fail "attente inutile alors que l'interface est prete"
[ -s "$FAKE_DNSMASQ_LOG" ] || fail "dnsmasq pas lance"

# L'interface et l'adresse suivent dnsmasq.conf (pas de nom en dur).
printf 'interface=eth9
dhcp-option=option:router,192.168.50.1
' > "$WORK/other.conf"
rm -f "$FAKE_IP_COUNT"
DNSMASQ_CONF="$WORK/other.conf" FAKE_IP_ADDR=192.168.50.1 sh "$REPO/server/stacks/dnsmasq/entrypoint.sh" > "$WORK/out.log" 2>&1 || fail "conf alternative non suivie"
grep -q "interface eth9 (192.168.50.1)" "$WORK/out.log" || fail "interface/adresse non lues dans dnsmasq.conf"

# Interface qui n'apparait jamais : arret en ERREUR visible, pas d'attente infinie silencieuse.
rm -f "$FAKE_IP_COUNT" "$FAKE_DNSMASQ_LOG"
if FAKE_IP_READY_AFTER=100000 DNSMASQ_WAIT_MAX=5 sh "$REPO/server/stacks/dnsmasq/entrypoint.sh" > "$WORK/out.log" 2> "$WORK/err.log"; then
    fail "doit echouer si l'interface n'apparait jamais"
fi
grep -q "n'est pas apparue" "$WORK/err.log" || fail "message d'erreur absent"
[ ! -s "$FAKE_DNSMASQ_LOG" ] || fail "dnsmasq lance alors que l'interface n'existe pas"

echo "OK : tenue au redemarrage (dnsmasq attend le pont)"
