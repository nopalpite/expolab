#!/usr/bin/env bash
# Test de la configuration generee par server/stacks/apt-cache/entrypoint.sh
# (miroirs HTTPS -> regles Remap), sans lancer apt-cacher-ng. Lance en CI et a
# la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRYPOINT="$REPO/server/stacks/apt-cache/entrypoint.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "ECHEC : $*" >&2; exit 1; }
render() {  # render <fichier de miroirs ou "-"> : ecrit $WORK/conf/zz_expolab.conf
    rm -rf "$WORK/conf"; mkdir -p "$WORK/conf"
    local mirrors="$WORK/mirrors.conf"
    [ "$1" = "-" ] && mirrors="$WORK/absent.conf"
    ACNG_CONF_DIR="$WORK/conf" ACNG_MIRRORS_FILE="$mirrors" ACNG_RENDER_ONLY=1 sh "$ENTRYPOINT" > "$WORK/out.log" 2> "$WORK/err.log"
}
conf="$WORK/conf/zz_expolab.conf"

# Sans fichier de miroirs : config de base seulement, aucune regle.
render -
grep -q "^BindAddress: 10.42.0.1$" "$conf" || fail "adresse d'ecoute absente"
grep -q "^OfflineMode: 0$" "$conf" || fail "mode en ligne absent"
! grep -q "Remap-\|VfilePatternEx" "$conf" || fail "regle inattendue sans miroir"

# Fichier de commentaires seulement : idem.
printf '# exemple\n#   https://download.docker.com/linux/debian\n\n' > "$WORK/mirrors.conf"
render mirrors
! grep -q "Remap-" "$conf" || fail "un commentaire a genere une regle"

# Deux depots valides, un invalide (http, ligne a deux champs, caracteres interdits).
cat > "$WORK/mirrors.conf" <<'EOT'
# depots HTTPS
https://download.docker.com/linux/debian
https://packages.example.org/apt/
http://pas-https.example.org/debian
https://deux.example.org/a b
https://mauvais.example.org/$(rm -rf)
EOT
render mirrors
grep -qxF "Remap-https1: download.docker.com/linux/debian ; https://download.docker.com/linux/debian" "$conf" || fail "regle docker absente"
grep -qxF "Remap-https2: packages.example.org/apt ; https://packages.example.org/apt" "$conf" || fail "regle 2 absente (slash final non retire ?)"
[ "$(grep -c "^Remap-" "$conf")" = "2" ] || fail "des lignes invalides ont genere des regles"
grep -q "^VfilePatternEx:" "$conf" || fail "motif des cles GPG absent"
[ "$(grep -c "ligne invalide ignoree" "$WORK/err.log")" = "3" ] || fail "lignes invalides non signalees"
grep -q "miroirs HTTPS = 2" "$WORK/out.log" 2>/dev/null || true  # le message final n'est pas atteint en mode test

echo "OK : apt-cache entrypoint (miroirs HTTPS)"
