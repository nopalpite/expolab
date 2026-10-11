#!/usr/bin/env bash
# Change le couple identifiant / mot de passe d'administration PARTOUT, en une
# fois (voir lib/credentials.sh : un seul couple pour toutes les interfaces) :
#   1. Dockhand : le compte admin est modifie par son API (le jeton d'API de
#      server/dockhand.env reste valable) ;
#   2. portail et UIs d'administration : server/stacks/caddy/auth.env (hash
#      bcrypt). Le portail relit le fichier tout seul ; les sessions ouvertes
#      sont invalidees, il faut se reconnecter avec le nouveau couple ;
#   3. Bastion : bastion.env, puis recreation de sa stack (un env_file n'est
#      relu qu'a la creation du conteneur).
#
# Usage : sudo ./server/set-credentials.sh [-y]
#   -y   ne demande pas de confirmation
#   Sans terminal : EXPOLAB_ADMIN_USER et EXPOLAB_ADMIN_PASSWORD (les NOUVELLES
#   valeurs, 8 caracteres minimum pour le mot de passe).
#
# Dockhand est modifie en premier : s'il refuse (identifiant deja pris, jeton
# invalide...), rien d'autre n'a bouge.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export REPO_ROOT

# shellcheck source=./dockhand-api.sh
source "$SCRIPT_DIR/dockhand-api.sh"
for lib in secrets credentials; do
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/lib/$lib.sh"
done

AUTH_ENV="$SCRIPT_DIR/stacks/caddy/auth.env"
BASTION_ENV="$SCRIPT_DIR/stacks/bastion/bastion.env"

# Positionne ADMIN_USER / ADMIN_PASSWORD (nouvelles valeurs, validees) : depuis
# l'environnement, sinon par saisie interactive.
collect_new_credentials() {
    local current_user="$1" msg
    if [ -n "${EXPOLAB_ADMIN_USER:-}" ] && [ -n "${EXPOLAB_ADMIN_PASSWORD:-}" ]; then
        msg="$(_check_admin_user "$EXPOLAB_ADMIN_USER")" || { echo "EXPOLAB_ADMIN_USER : $msg" >&2; return 1; }
        msg="$(_check_admin_password "$EXPOLAB_ADMIN_PASSWORD")" || { echo "EXPOLAB_ADMIN_PASSWORD : $msg" >&2; return 1; }
        ADMIN_USER="$EXPOLAB_ADMIN_USER"
        ADMIN_PASSWORD="$EXPOLAB_ADMIN_PASSWORD"
        return 0
    fi
    if [ ! -t 0 ]; then
        echo "[!] Aucun terminal : definir EXPOLAB_ADMIN_USER et EXPOLAB_ADMIN_PASSWORD (nouvelles valeurs)." >&2
        return 1
    fi
    cat <<EOT

[+] Nouveaux identifiants d'administration (utilisateur actuel : $current_user) :
    ils remplaceront l'ancien couple pour TOUTES les interfaces.
EOT
    _read_admin_credentials "$current_user"
}

update_dockhand_account() {
    dockhand_wait_healthy
    DOCKHAND_SETUP_PASSWORD="$ADMIN_PASSWORD" python3 "$DOCKHAND_SCRIPT_DIR/dockhand_setup.py" set-credentials \
        --user "$ADMIN_USER" --from "$1"
}

update_caddy_auth() {
    local hash
    hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$ADMIN_PASSWORD")"
    # Ecriture SUR PLACE (meme fichier, pas de renommage) : le portail monte
    # auth.env comme un fichier, un remplacement le laisserait perime.
    cat > "$AUTH_ENV" <<EOT
BASIC_AUTH_USER=$ADMIN_USER
BASIC_AUTH_PASSWORD=$ADMIN_PASSWORD
BASIC_AUTH_HASH=$hash
EOT
    chmod 600 "$AUTH_ENV"
}

update_bastion_env() {
    local tmp
    [ -f "$BASTION_ENV" ] || return 0   # Bastion pas installe
    tmp="$(mktemp)"
    grep -v -e '^BASTION_ADMIN_USER=' -e '^BASTION_ADMIN_PASSWORD=' "$BASTION_ENV" > "$tmp" || true
    printf 'BASTION_ADMIN_USER=%s\nBASTION_ADMIN_PASSWORD=%s\n' \
        "$(_env_quote "$ADMIN_USER")" "$(_env_quote "$ADMIN_PASSWORD")" >> "$tmp"
    cat "$tmp" > "$BASTION_ENV"
    rm -f "$tmp"
}

redeploy_bastion() {
    [ -f "$BASTION_ENV" ] || return 0
    dockhand_upsert_stack bastion "$SCRIPT_DIR/stacks/bastion/docker-compose.yml"
}

main() {
    local assume_yes=0 old_user old_password ans
    [ "${1:-}" = "-y" ] && assume_yes=1

    if [ "$(id -u)" -ne 0 ]; then
        echo "Ce script doit etre lance en root (sudo) : il lit et modifie des fichiers de secrets." >&2
        exit 1
    fi
    if [ ! -f "$AUTH_ENV" ]; then
        echo "Aucune installation trouvee ($AUTH_ENV absent) : lancer d'abord server/deploy-server.sh." >&2
        exit 1
    fi
    old_user="$(_env_value "$AUTH_ENV" BASIC_AUTH_USER)"
    old_password="$(_env_value "$AUTH_ENV" BASIC_AUTH_PASSWORD)"

    collect_new_credentials "$old_user"
    if [ "$ADMIN_USER" = "$old_user" ] && [ "$ADMIN_PASSWORD" = "$old_password" ]; then
        echo "[=] Identiques aux identifiants actuels : rien a changer."
        exit 0
    fi

    if [ "$assume_yes" -eq 0 ] && [ -t 0 ]; then
        echo
        read -r -p "Appliquer '$ADMIN_USER' a Dockhand, Bastion et au portail (sessions ouvertes fermees) ? [y/N] " ans
        [[ "$ans" =~ ^[yY]$ ]] || { echo "Annule, rien n'a ete modifie."; exit 1; }
    fi

    echo "[1/3] Dockhand..."
    update_dockhand_account "$old_user"
    echo "[2/3] Portail et UIs d'administration (server/stacks/caddy/auth.env)..."
    update_caddy_auth
    echo "[3/3] Bastion (bastion.env, recreation de la stack)..."
    update_bastion_env
    redeploy_bastion

    cat <<EOT

[+] Identifiants d'administration changes (utilisateur : $ADMIN_USER) : Dockhand,
    portail et UIs d'administration, Bastion. Les sessions ouvertes sont fermees :
    reconnecte-toi avec le nouveau couple.
EOT
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
