# Fichiers de la stack dashboard (le portail) - source par deploy-server.sh.
# Necessite TLS_MODE et PUBLIC_DOMAIN (tls.sh). Voir stacks/dashboard/docker-compose.yml.
#   dashboard.env   domaine public + mode TLS, regenere a chaque run (et par
#                   caddy-admin quand le domaine change)
#   portal.env      PORTAL_SECRET, genere UNE fois (invalide toutes les
#                   sessions s'il change : jamais regenere)
#   services.yaml   copie de server/services.yaml (titres, groupes...) - voir
#                   la note du compose sur le bind-mount de fichier

setup_dashboard() {
    local dir="$SCRIPT_DIR/stacks/dashboard"
    mkdir -p "$dir"

    printf 'PORTAL_DOMAIN=%s\nPORTAL_TLS_MODE=%s\n' "$PUBLIC_DOMAIN" "$TLS_MODE" > "$dir/dashboard.env"

    if [ ! -f "$dir/portal.env" ]; then
        echo "[+] Generation de la cle de session du portail (server/stacks/dashboard/portal.env, non versionne)..."
        echo "PORTAL_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > "$dir/portal.env"
        chmod 600 "$dir/portal.env"
    fi

    sync_portal_services
}

# cp ecrit DANS le fichier existant (meme inode) : le conteneur voit la mise a jour.
sync_portal_services() {
    local dest="$SCRIPT_DIR/stacks/dashboard/services.yaml"
    [ -f "$dest" ] || : > "$dest"
    cp "${SERVICES:-$SCRIPT_DIR/services.yaml}" "$dest"
}
