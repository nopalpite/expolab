# Secrets generes une seule fois (jamais commites, voir .gitignore) -
# source par deploy-server.sh. Positionne BASTION_API_TOKEN,
# BASTION_ADMIN_PASSWORD, BASIC_AUTH_USER et BASIC_AUTH_PASSWORD (lus plus
# loin par ansible-web.sh/summary.sh).

# Mot de passe "pi" par defaut des faux Pi (fleet/provision-fakepi.sh) :
# valeur de lab connue, pas un secret - utilisee comme filet de secours
# par ansible-web (voir ansible-web.sh). Surchargeable via l'environnement.
LAB_FAKEPI_PASSWORD="${LAB_FAKEPI_PASSWORD:-raspberry}"

# _env_value <fichier> <CLE> : valeur apres le premier "=" (pas de `source` :
# les hash bcrypt contiennent des "$" que le shell interpreterait).
_env_value() {
    grep "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2-
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

    # Mot de passe admin de Bastion (acces SSH/VNC a tout le parc) : genere
    # plutot que "raspberry" en dur dans le depot public. Meme logique que
    # ci-dessus pour un bastion.env anterieur a ce changement.
    if ! grep -q '^BASTION_ADMIN_PASSWORD=' "$env_file"; then
        echo "[+] Generation du mot de passe admin Bastion (affiche a la fin du script)..."
        echo "BASTION_ADMIN_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')" >> "$env_file"
    fi
    BASTION_ADMIN_PASSWORD="$(_env_value "$env_file" BASTION_ADMIN_PASSWORD)"
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
        echo "[+] Generation des identifiants basic auth des UIs d'administration (server/stacks/caddy/auth.env)..."
        local password hash
        password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
        hash="$(docker run --rm caddy:2 caddy hash-password --plaintext "$password")"
        cat > "$auth_env" <<EOT
BASIC_AUTH_USER=admin
BASIC_AUTH_PASSWORD=$password
BASIC_AUTH_HASH=$hash
EOT
        chmod 600 "$auth_env"
    fi
    BASIC_AUTH_USER="$(_env_value "$auth_env" BASIC_AUTH_USER)"
    BASIC_AUTH_PASSWORD="$(_env_value "$auth_env" BASIC_AUTH_PASSWORD)"
}
