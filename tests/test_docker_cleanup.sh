#!/usr/bin/env bash
# Test de server/docker-cleanup.sh : nettoie le cache de construction (en gardant une
# partie) et les images sans nom, sans jamais echouer. Faux docker, aucun conteneur reel.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail() { echo "ECHEC : $*" >&2; exit 1; }

mkdir -p "$WORK/bin"
cat > "$WORK/bin/docker" <<'EOT'
#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
[ -z "${FAKE_DOCKER_FAIL:-}" ] || { echo "erreur simulee" >&2; exit 1; }
# anciennes versions : --max-used-space n'existe pas ; tres anciennes : --keep-storage non plus
[ -z "${FAKE_NO_MAX_USED:-}" ] || ! grep -q -- "--max-used-space" <<< "$*" || { echo "unknown flag" >&2; exit 125; }
[ -z "${FAKE_NO_KEEP:-}" ] || ! grep -q -- "--keep-storage" <<< "$*" || { echo "unknown flag" >&2; exit 125; }
echo "Total: 1GB"
EOT
chmod +x "$WORK/bin/docker"
export FAKE_DOCKER_LOG="$WORK/docker.log"
export PATH="$WORK/bin:$PATH"

bash "$REPO/server/docker-cleanup.sh" > "$WORK/out.log"
grep -q "^builder prune -f --max-used-space 2GB$" "$FAKE_DOCKER_LOG" || fail "cache de construction non nettoye avec --max-used-space"
grep -q "^image prune -f$" "$FAKE_DOCKER_LOG" || fail "images sans nom non nettoyees"
! grep -qE "system prune|image prune -a|volume|rm|stop|kill" "$FAKE_DOCKER_LOG" || fail "commande trop large : le menage ne doit toucher ni volumes, ni conteneurs, ni images utilisees"

: > "$FAKE_DOCKER_LOG"
KEEP_STORAGE=5GB bash "$REPO/server/docker-cleanup.sh" > /dev/null
grep -q "max-used-space 5GB" "$FAKE_DOCKER_LOG" || fail "KEEP_STORAGE ignore"

# Docker plus ancien : repli sur --keep-storage, puis sur un filtre d'age.
: > "$FAKE_DOCKER_LOG"
FAKE_NO_MAX_USED=1 bash "$REPO/server/docker-cleanup.sh" > /dev/null
grep -q "builder prune -f --keep-storage 2GB" "$FAKE_DOCKER_LOG" || fail "pas de repli sur --keep-storage"
: > "$FAKE_DOCKER_LOG"
FAKE_NO_MAX_USED=1 FAKE_NO_KEEP=1 bash "$REPO/server/docker-cleanup.sh" > /dev/null
grep -q "builder prune -f --filter until=72h" "$FAKE_DOCKER_LOG" || fail "pas de repli sur le filtre d'age"

# Un echec de docker ne doit pas faire echouer l'installation.
FAKE_DOCKER_FAIL=1 bash "$REPO/server/docker-cleanup.sh" > /dev/null 2>&1 || fail "un echec de docker a fait echouer le nettoyage"

# Sans docker : rien a faire, pas d'erreur (PATH vide : ni docker, ni aucun autre outil).
EMPTY="$WORK/empty"; mkdir -p "$EMPTY"
BASH_BIN="$(command -v bash)"
PATH="$EMPTY" "$BASH_BIN" "$REPO/server/docker-cleanup.sh" > "$WORK/out.log" 2>&1 || fail "echec sans docker"
[ ! -s "$WORK/out.log" ] || fail "sortie inattendue sans docker : $(cat "$WORK/out.log")"

echo "OK : server/docker-cleanup.sh"
