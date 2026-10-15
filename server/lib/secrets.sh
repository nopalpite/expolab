# Secrets generes une seule fois (jamais commites, voir .gitignore) -
# source par deploy-server.sh. Positionne BASTION_API_TOKEN,
# BASTION_ADMIN_USER, BASTION_ADMIN_PASSWORD, BASIC_AUTH_USER et
# BASIC_AUTH_PASSWORD (lus plus loin par ansible-web.sh/summary.sh).
# Les identifiants d'administration (Bastion, basic auth) sont ceux choisis
# par l'utilisateur : voir credentials.sh (setup_admin_credentials d'abord).

# _env_value <fichier> <CLE> : valeur apres le premier "=" (pas de `source` :
# les hash bcrypt contiennent des "$" que le shell interpreterait).
_env_value() {
    grep "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2-
}

# _env_quote <valeur> : valeur telle quelle si elle est "sure", entre
# apostrophes sinon (Docker Compose interpole $ et traite # dans un env_file
# non quote ; entre apostrophes le contenu est litteral).
_env_quote() {
    if [[ "$1" =~ ^[A-Za-z0-9_@%+=:,./-]*$ ]]; then
        printf '%s' "$1"
    else
        printf "'%s'" "$1"
    fi
}

# _env_unquote <valeur lue dans un env_file> : retire les apostrophes englobantes.
_env_unquote() {
    local v="$1" q="'"
    if [[ "$v" == "$q"*"$q" ]]; then
        v="${v#"$q"}"
        v="${v%"$q"}"
    fi
    printf '%s' "$v"
}

setup_bastion_secrets() {
    local env_file="$SCRIPT_DIR/stacks/bastion/bastion.env"

    if [ ! -f "$env_file" ]; then
        echo "[+] Premiere generation des secrets Bastion (server/stacks/bastion/bastion.env, non versionne)..."
        mkdir -p "$SCRIPT_DIR/stacks/bastion/config" "$SCRIPT_DIR/stacks/bastion/maps"
        # Format Fernet (cle de chiffrement des identifiants SSH/VNC
        # memorises) - juste du base64 urlsafe sur 32 octets aleatoires,
        # pas besoin du paquet "cryptography" (voir le README de Bastion).
        cat > "$env_file" <<EOT
BASTION_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
BASTION_CREDENTIALS_KEY=$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
EOT
        chmod 600 "$env_file"
    fi

    # BASTION_API_TOKEN active /api/machines cote Bastion (desactive par
    # defaut) - meme jeton lu par ansible-web (ansible-web.sh). Hors du
    # `if` ci-dessus : s'applique aussi a un bastion.env deja existant.
    if ! grep -q '^BASTION_API_TOKEN=' "$env_file"; then
        echo "[+] Ajout de BASTION_API_TOKEN a bastion.env (active /api/machines pour ansible-web)..."
        echo "BASTION_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> "$env_file"
    fi
    BASTION_API_TOKEN="$(_env_value "$env_file" BASTION_API_TOKEN)"

    # Identifiants admin de Bastion (acces SSH/VNC a tout le parc) : ceux
    # choisis par l'utilisateur (credentials.sh), jamais un mot de passe en dur
    # dans le depot public. Un bastion.env anterieur garde son mot de passe
    # genere ; son identifiant, auparavant fixe dans le compose, y est ajoute.
    if ! grep -q '^BASTION_ADMIN_PASSWORD=' "$env_file"; then
        echo "[+] Identifiants admin de Bastion (ceux choisis a l'installation)..."
        printf 'BASTION_ADMIN_USER=%s
BASTION_ADMIN_PASSWORD=%s
'             "$(_env_quote "$ADMIN_USER")" "$(_env_quote "$ADMIN_PASSWORD")" >> "$env_file"
    elif ! grep -q '^BASTION_ADMIN_USER=' "$env_file"; then
        echo "BASTION_ADMIN_USER=admin" >> "$env_file"
    fi
    BASTION_ADMIN_USER="$(_env_unquote "$(_env_value "$env_file" BASTION_ADMIN_USER)")"
    BASTION_ADMIN_PASSWORD="$(_env_unquote "$(_env_value "$env_file" BASTION_ADMIN_PASSWORD)")"
}

# Identifiants du basic_auth Caddy devant les UIs d'administration
# (services.yaml, `auth: true`). Le hash bcrypt contient des "$" : ce
# fichier n'est JAMAIS `source`, uniquement lu par render-caddyfile.py.
# Hash calcule avec le binaire caddy lui-meme (image officielle, deja
# requise par stacks/caddy/Dockerfile) - pas de dependance bcrypt cote hote.
setup_caddy_basic_auth() {
    local auth_env="$SCRIPT_DIR/stacks/caddy/auth.env"
    mkdir -p "$SCRIPT_DIR/stacks/caddy"

    if [ ! -f "$auth_env" ]; then
        echo "[+] Enregistrement des identifiants basic auth des UIs d'administration (server/stacks/caddy/auth.env)..."
        local hash
        hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$ADMIN_PASSWORD")"
        cat > "$auth_env" <<EOT
BASIC_AUTH_USER=$ADMIN_USER
BASIC_AUTH_PASSWORD=$ADMIN_PASSWORD
BASIC_AUTH_HASH=$hash
EOT
        chmod 600 "$auth_env"
    fi
    BASIC_AUTH_USER="$(_env_value "$auth_env" BASIC_AUTH_USER)"
    BASIC_AUTH_PASSWORD="$(_env_value "$auth_env" BASIC_AUTH_PASSWORD)"
}
