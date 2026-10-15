# Identifiants d'administration - source par deploy-server.sh. Positionne
# ADMIN_USER et ADMIN_PASSWORD : UN SEUL couple, choisi par l'utilisateur a la
# premiere installation, applique a toutes les interfaces (Dockhand, Bastion,
# UIs d'administration derriere le basic auth Caddy). Ils sont ensuite lus
# dans server/stacks/caddy/auth.env (non versionne), jamais redemandes.
#
# Ordre : identifiants deja enregistres (installation precedente) > variables
# EXPOLAB_ADMIN_USER / EXPOLAB_ADMIN_PASSWORD (installation sans terminal) >
# saisie interactive. Sans rien de tout ca, le script s'arrete : mieux vaut
# refuser que laisser des interfaces d'administration avec un mot de passe que
# personne n'a choisi.

ADMIN_USER_RE='^[A-Za-z0-9._-]{3,32}$'
ADMIN_PASSWORD_MIN=8

# _check_admin_user <nom> : message d'erreur sur stdout, code != 0 si invalide.
_check_admin_user() {
    if [[ "$1" =~ $ADMIN_USER_RE ]]; then
        return 0
    fi
    echo "Identifiant invalide : 3 a 32 caracteres parmi lettres, chiffres, '.', '_' et '-'."
    return 1
}

# _check_admin_password <mot de passe> : idem. Le mot de passe est ecrit dans
# des fichiers env lus par Docker Compose (entre apostrophes) : l'apostrophe et
# les retours a la ligne sont les seuls caracteres qu'on ne sait pas y mettre.
_check_admin_password() {
    local pw="$1"
    if [ "${#pw}" -lt "$ADMIN_PASSWORD_MIN" ]; then
        echo "Mot de passe trop court ($ADMIN_PASSWORD_MIN caracteres minimum)."
        return 1
    fi
    case "$pw" in
        *"'"*|*$'\n'*|*$'\r'*)
            echo "Mot de passe invalide : ni apostrophe (') ni retour a la ligne."
            return 1
            ;;
    esac
    return 0
}

# _read_admin_credentials <identifiant par defaut> : saisie interactive de
# l'identifiant puis du mot de passe (masque, confirme), jusqu'a ce qu'ils
# soient valides. Positionne ADMIN_USER et ADMIN_PASSWORD. Terminal requis.
_read_admin_credentials() {
    local default_user="$1" user pw1 pw2 msg
    while true; do
        read -r -p "    Identifiant [$default_user] : " user
        user="${user:-$default_user}"
        if msg="$(_check_admin_user "$user")"; then break; fi
        echo "    $msg"
    done
    while true; do
        read -rs -p "    Mot de passe ($ADMIN_PASSWORD_MIN caracteres minimum) : " pw1
        echo
        if ! msg="$(_check_admin_password "$pw1")"; then
            echo "    $msg"
            continue
        fi
        read -rs -p "    Confirmer le mot de passe : " pw2
        echo
        if [ "$pw1" != "$pw2" ]; then
            echo "    Les deux saisies different, recommencer."
            continue
        fi
        break
    done
    ADMIN_USER="$user"
    ADMIN_PASSWORD="$pw1"
}

setup_admin_credentials() {
    local auth_env="$SCRIPT_DIR/stacks/caddy/auth.env" msg

    # 1. Installation precedente : on reprend ses identifiants.
    if [ -f "$auth_env" ]; then
        ADMIN_USER="$(_env_value "$auth_env" BASIC_AUTH_USER)"
        ADMIN_PASSWORD="$(_env_value "$auth_env" BASIC_AUTH_PASSWORD)"
        if [ -n "$ADMIN_USER" ] && [ -n "$ADMIN_PASSWORD" ]; then
            echo "[=] Identifiants d'administration deja definis (utilisateur : $ADMIN_USER)."
            return 0
        fi
    fi

    # 2. Variables d'environnement (installation sans terminal).
    if [ -n "${EXPOLAB_ADMIN_USER:-}" ] && [ -n "${EXPOLAB_ADMIN_PASSWORD:-}" ]; then
        msg="$(_check_admin_user "$EXPOLAB_ADMIN_USER")" || { echo "EXPOLAB_ADMIN_USER : $msg" >&2; return 1; }
        msg="$(_check_admin_password "$EXPOLAB_ADMIN_PASSWORD")" || { echo "EXPOLAB_ADMIN_PASSWORD : $msg" >&2; return 1; }
        ADMIN_USER="$EXPOLAB_ADMIN_USER"
        ADMIN_PASSWORD="$EXPOLAB_ADMIN_PASSWORD"
        echo "[+] Identifiants d'administration pris dans l'environnement (utilisateur : $ADMIN_USER)."
        return 0
    fi

    # 3. Saisie interactive.
    if [ ! -t 0 ]; then
        cat >&2 <<'EOT'
[!] Identifiants d'administration non definis et aucun terminal pour les saisir.
    Lancer le script depuis un terminal, ou definir EXPOLAB_ADMIN_USER et
    EXPOLAB_ADMIN_PASSWORD (8 caracteres minimum) avant de le relancer.
EOT
        return 1
    fi

    cat <<'EOT'

[+] Identifiants d'administration : ce couple identifiant / mot de passe sera
    utilise pour TOUTES les interfaces (Dockhand, Bastion, dnsmasq, Caddy,
    git-mirror, ansible-web, VPN, cartes SD). A choisir maintenant, une seule fois.
EOT
    _read_admin_credentials admin
    echo "[+] Identifiants enregistres (utilisateur : $ADMIN_USER)."
}
