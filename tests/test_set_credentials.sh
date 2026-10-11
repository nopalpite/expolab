#!/usr/bin/env bash
# Test de server/set-credentials.sh : saisie validee, reecriture de auth.env
# (sur place) et de bastion.env, sans Docker ni Dockhand reels. Lance en CI et
# a la main.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "ECHEC : $*" >&2; exit 1; }
contains() { grep -qF -- "$2" "$1" || fail "'$2' absent de $1"; }

cp -r "$REPO/server" "$WORK/server"
mkdir -p "$WORK/bin" "$WORK/server/stacks/caddy" "$WORK/server/stacks/bastion"
# Faux docker : seul `caddy hash-password` est utilise.
cat > "$WORK/bin/docker" <<'EOT'
#!/usr/bin/env bash
case "$*" in
    *hash-password*) echo '$2a$14$FAKEHASHFORTESTSONLYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' ;;
    *) exit 0 ;;
esac
EOT
chmod +x "$WORK/bin/docker"
export PATH="$WORK/bin:$PATH"

# Installation existante (anterieure au couple unique : Bastion a son propre mot de passe).
cat > "$WORK/server/stacks/caddy/auth.env" <<'EOT'
BASIC_AUTH_USER=admin
BASIC_AUTH_PASSWORD=ancien-mdp-caddy
BASIC_AUTH_HASH=$2a$14$OLDHASH
EOT
cat > "$WORK/server/stacks/bastion/bastion.env" <<'EOT'
BASTION_SECRET_KEY=cle-de-session
BASTION_CREDENTIALS_KEY=cle-chiffrement
BASTION_API_TOKEN=jeton-api
BASTION_ADMIN_PASSWORD=ancien-mdp-bastion
EOT
chmod 600 "$WORK/server/stacks/caddy/auth.env"

# On charge les fonctions sans lancer main (garde BASH_SOURCE).
# shellcheck disable=SC1091
source "$WORK/server/set-credentials.sh"

# --- saisie par l'environnement : validee ---
( EXPOLAB_ADMIN_USER=ab EXPOLAB_ADMIN_PASSWORD=Mot-de-passe-1 collect_new_credentials admin < /dev/null ) > /dev/null 2>&1 && fail "identifiant trop court accepte"
( EXPOLAB_ADMIN_USER=gestion EXPOLAB_ADMIN_PASSWORD=court collect_new_credentials admin < /dev/null ) > /dev/null 2>&1 && fail "mot de passe trop court accepte"
( collect_new_credentials admin < /dev/null ) > /dev/null 2>&1 && fail "sans terminal ni variables, la saisie aurait du echouer"
export EXPOLAB_ADMIN_USER=gestion.expo
export EXPOLAB_ADMIN_PASSWORD='Nouveau $mdp #1 "ok"'
collect_new_credentials admin < /dev/null
[ "$ADMIN_USER" = "gestion.expo" ] && [ "$ADMIN_PASSWORD" = "$EXPOLAB_ADMIN_PASSWORD" ] || fail "valeurs de l'environnement non reprises"

# --- auth.env : reecrit SUR PLACE (le portail le monte comme un fichier) ---
inode_before="$(stat -c %i "$AUTH_ENV")"
update_caddy_auth
[ "$inode_before" = "$(stat -c %i "$AUTH_ENV")" ] || fail "auth.env remplace par renommage : le portail verrait l'ancien contenu"
contains "$AUTH_ENV" "BASIC_AUTH_USER=gestion.expo"
contains "$AUTH_ENV" 'BASIC_AUTH_PASSWORD=Nouveau $mdp #1 "ok"'
contains "$AUTH_ENV" 'BASIC_AUTH_HASH=$2a$14$FAKEHASH'
! grep -q "OLDHASH\|ancien-mdp-caddy" "$AUTH_ENV" || fail "ancienne valeur encore presente dans auth.env"
[ "$(_env_value "$AUTH_ENV" BASIC_AUTH_USER)" = "gestion.expo" ] || fail "auth.env relu autrement"

# --- bastion.env : couple remplace, le reste intact, idempotent ---
update_bastion_env
update_bastion_env
contains "$BASTION_ENV" "BASTION_SECRET_KEY=cle-de-session"
contains "$BASTION_ENV" "BASTION_CREDENTIALS_KEY=cle-chiffrement"
contains "$BASTION_ENV" "BASTION_API_TOKEN=jeton-api"
contains "$BASTION_ENV" "BASTION_ADMIN_USER=gestion.expo"
contains "$BASTION_ENV" "BASTION_ADMIN_PASSWORD='Nouveau \$mdp #1 \"ok\"'"  # entre apostrophes : litteral pour Docker Compose
[ "$(grep -c '^BASTION_ADMIN_USER=' "$BASTION_ENV")" = "1" ] || fail "identifiant Bastion duplique"
[ "$(grep -c '^BASTION_ADMIN_PASSWORD=' "$BASTION_ENV")" = "1" ] || fail "mot de passe Bastion duplique"
! grep -q "ancien-mdp-bastion" "$BASTION_ENV" || fail "ancien mot de passe Bastion conserve"
[ "$(_env_unquote "$(_env_value "$BASTION_ENV" BASTION_ADMIN_PASSWORD)")" = "$ADMIN_PASSWORD" ] || fail "mot de passe Bastion relu autrement"

# --- sans Bastion installe : rien a faire ---
rm -f "$BASTION_ENV"
update_bastion_env
[ ! -e "$BASTION_ENV" ] || fail "bastion.env cree alors que Bastion n'est pas installe"

echo "OK : server/set-credentials.sh"
