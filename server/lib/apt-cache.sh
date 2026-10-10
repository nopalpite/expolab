# Fichiers de la stack apt-cache - source par deploy-server.sh.
# Le mode (en ligne / hors ligne) se change avec server/apt-cache-mode.sh :
# il ne doit jamais etre ecrase par un redeploiement, d'ou "jamais si deja
# present".

setup_apt_cache() {
    local dir="$SCRIPT_DIR/stacks/apt-cache"
    mkdir -p "$dir/data"
    if [ ! -f "$dir/apt-cache.env" ]; then
        echo "APT_CACHE_OFFLINE=0" > "$dir/apt-cache.env"
    fi
}
