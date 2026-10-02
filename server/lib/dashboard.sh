# Fichiers generes de la stack dashboard (Homepage) - source par
# deploy-server.sh. Necessite TLS_MODE et PUBLIC_DOMAIN (tls.sh).
# Regeneres a chaque run : dependent du domaine public actuel (voir les
# commentaires de stacks/dashboard/docker-compose.yml et de
# config/services.yaml.template).

setup_dashboard() {
    local dir="$SCRIPT_DIR/stacks/dashboard"
    if [ "$TLS_MODE" = "signed" ]; then
        echo "HOMEPAGE_ALLOWED_HOSTS=dashboard.web.expolab.lan,dashboard.$PUBLIC_DOMAIN,localhost:3001" > "$dir/dashboard.env"
    else
        echo "HOMEPAGE_ALLOWED_HOSTS=dashboard.web.expolab.lan,localhost:3001" > "$dir/dashboard.env"
    fi

    sed "s/__PUBLIC_DOMAIN__/$PUBLIC_DOMAIN/g" "$dir/config/services.yaml.template" > "$dir/config/services.yaml"
}
