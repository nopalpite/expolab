#!/usr/bin/env bash
# Tests de la tenue aux coupures (server/recover-containers.sh, server/lib/resilience.sh) avec de faux docker / systemctl : rien n'est touche sur l'hote.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }
mkdir -p "$WORK/bin"
export PATH="$WORK/bin:$PATH"

# --- relance des conteneurs coupes ---------------------------------------------------------
# Faux docker : nom politique code.
cat > "$WORK/containers" <<'EOT'
expolab-dnsmasq unless-stopped 255
expolab-arrete-a-la-main unless-stopped 0
expolab-tue unless-stopped 137
expolab-unique no 255
expolab-toujours always 255
EOT
cat > "$WORK/bin/docker" <<EOT
#!/usr/bin/env bash
case "\$1" in
    ps) cut -d' ' -f1 "$WORK/containers" ;;
    inspect) grep "^\${@: -1} " "$WORK/containers" | cut -d' ' -f2- ;;
    start) echo "\$2" >> "$WORK/started" ;;
esac
EOT
chmod +x "$WORK/bin/docker"
bash "$REPO/server/recover-containers.sh" > "$WORK/recover.log"
[ "$(sort "$WORK/started" | tr '\n' ' ')" = "expolab-dnsmasq expolab-toujours " ] \
    || fail "relance inattendue : $(tr '\n' ' ' < "$WORK/started")"
grep -q "2 conteneur(s) relance(s)" "$WORK/recover.log" || fail "bilan de relance absent"

# --- installation (timer, watchdog, journal, logs Docker) ---------------------------------
printf '#!/usr/bin/env bash\necho "$*" >> "%s"\n' "$WORK/systemctl.log" > "$WORK/bin/systemctl"
chmod +x "$WORK/bin/systemctl"
export EXPOLAB_ETC="$WORK/etc" EXPOLAB_SYSTEMD_DIR="$WORK/units" EXPOLAB_JOURNAL_DIR="$WORK/journal" REPO_ROOT="/home/pi/expolab"
# shellcheck source=../server/lib/resilience.sh
source "$REPO/server/lib/resilience.sh"
setup_resilience > "$WORK/setup.log"
grep -q "ExecStart=/usr/bin/env bash /home/pi/expolab/server/recover-containers.sh" "$WORK/units/expolab-recover.service" \
    || fail "service de relance mal ecrit"
grep -q "OnBootSec=1min" "$WORK/units/expolab-recover.timer" || fail "relance pas lancee au demarrage"
grep -q "RuntimeWatchdogSec=15s" "$WORK/etc/systemd/system.conf.d/expolab-watchdog.conf" || fail "watchdog absent"
grep -q "Storage=persistent" "$WORK/etc/systemd/journald.conf.d/expolab.conf" || fail "journal non persistant"
grep -q '"max-size": "10m"' "$WORK/etc/docker/daemon.json" || fail "rotation des logs Docker absente"
grep -q "^enable --now expolab-recover.timer$" "$WORK/systemctl.log" || fail "relance des conteneurs non activee"
grep -q "^restart docker$" "$WORK/systemctl.log" || fail "Docker non redemarre apres son reglage"

# Deuxieme deploiement : rien ne change, rien n'est redemarre.
: > "$WORK/systemctl.log"
setup_resilience > "$WORK/setup2.log"
! grep -q -E "^(restart|daemon-reexec|daemon-reload)" "$WORK/systemctl.log" || fail "redemarrage inutile au redeploiement"

# Un daemon.json qui n'est pas le notre n'est jamais ecrase.
echo '{"data-root": "/mnt/docker"}' > "$WORK/etc/docker/daemon.json"
setup_resilience > "$WORK/setup3.log" 2>&1
grep -q "data-root" "$WORK/etc/docker/daemon.json" || fail "daemon.json existant ecrase"
grep -q "rotation des logs Docker non appliquee" "$WORK/setup3.log" || fail "daemon.json etranger non signale"

echo "OK : relance des conteneurs, watchdog, journal et logs Docker"
