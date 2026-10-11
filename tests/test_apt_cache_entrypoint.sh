#!/usr/bin/env bash
# Test de la configuration generee par server/stacks/apt-cache/entrypoint.sh
# (mode, miroirs HTTPS -> regles Remap, verrou de la page d'administration),
# sans lancer apt-cacher-ng. Lance en CI et a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRYPOINT="$REPO/server/stacks/apt-cache/entrypoint.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "ECHEC : $*" >&2; exit 1; }
render() {  # render : ecrit $WORK/conf/zz_expolab.conf a partir de $WORK/expolab/
    rm -rf "$WORK/conf"; mkdir -p "$WORK/conf" "$WORK/expolab"
    ACNG_CONF_DIR="$WORK/conf" ACNG_EXPOLAB_DIR="$WORK/expolab" ACNG_RENDER_ONLY=1 \
        sh "$ENTRYPOINT" > "$WORK/out.log" 2> "$WORK/err.log"
}
conf="$WORK/conf/zz_expolab.conf"

# Sans rien dans le dossier de config : en ligne, aucune regle, pas de verrou.
rm -rf "$WORK/expolab"
render
grep -q "^BindAddress: 10.42.0.1$" "$conf" || fail "adresse d'ecoute absente"
grep -q "^OfflineMode: 0$" "$conf" || fail "mode en ligne par defaut absent"
! grep -q "Remap-\|VfilePatternEx\|AdminAuth" "$conf" || fail "regle inattendue sans config"

# Mode hors ligne, avec ou sans retour a la ligne / espaces.
mkdir -p "$WORK/expolab"
printf 'offline \n' > "$WORK/expolab/mode"
render
grep -q "^OfflineMode: 1$" "$conf" || fail "mode offline non pris en compte"
grep -q "mode = offline" "$WORK/out.log" 2>/dev/null || true
echo online > "$WORK/expolab/mode"
render
grep -q "^OfflineMode: 0$" "$conf" || fail "mode online non pris en compte"

# Un mode invalide arrete le demarrage (jamais un mode devine).
echo nimporte > "$WORK/expolab/mode"
rm -rf "$WORK/conf"; mkdir -p "$WORK/conf"
if ACNG_CONF_DIR="$WORK/conf" ACNG_EXPOLAB_DIR="$WORK/expolab" ACNG_RENDER_ONLY=1 sh "$ENTRYPOINT" > /dev/null 2> "$WORK/err.log"; then
    fail "un mode invalide aurait du echouer"
fi
grep -q "mode invalide" "$WORK/err.log" || fail "message d'erreur du mode absent"
echo online > "$WORK/expolab/mode"

# Page d'administration integree : verrouillee quand un secret est fourni.
rm -rf "$WORK/conf"; mkdir -p "$WORK/conf"
APT_CACHE_ADMIN_SECRET=s3cr3t-Abc ACNG_CONF_DIR="$WORK/conf" ACNG_EXPOLAB_DIR="$WORK/expolab" ACNG_RENDER_ONLY=1 sh "$ENTRYPOINT" > /dev/null 2>&1
grep -qxF "AdminAuth: admin:s3cr3t-Abc" "$conf" || fail "AdminAuth absent alors qu'un secret est fourni"

# Fichier de commentaires seulement : aucune regle.
printf '# exemple\n#   https://download.docker.com/linux/debian\n\n' > "$WORK/expolab/https-mirrors.conf"
render
! grep -q "Remap-" "$conf" || fail "un commentaire a genere une regle"

# Deux depots valides, trois invalides (http, ligne a deux champs, caracteres interdits).
cat > "$WORK/expolab/https-mirrors.conf" <<'EOT'
# depots HTTPS
https://download.docker.com/linux/debian
https://packages.example.org/apt/
http://pas-https.example.org/debian
https://deux.example.org/a b
https://mauvais.example.org/$(rm -rf)
EOT
render
grep -qxF "Remap-https1: download.docker.com/linux/debian ; https://download.docker.com/linux/debian" "$conf" || fail "regle docker absente"
grep -qxF "Remap-https2: packages.example.org/apt ; https://packages.example.org/apt" "$conf" || fail "regle 2 absente (slash final non retire ?)"
[ "$(grep -c "^Remap-" "$conf")" = "2" ] || fail "des lignes invalides ont genere des regles"
grep -q "^VfilePatternEx:" "$conf" || fail "motif des cles GPG absent"
[ "$(grep -c "ligne invalide ignoree" "$WORK/err.log")" = "3" ] || fail "lignes invalides non signalees"

echo "OK : apt-cache entrypoint (mode, miroirs HTTPS, verrou admin)"
