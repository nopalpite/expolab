# Fichiers de la stack apt-cache - source par deploy-server.sh. Rien n'est
# jamais ecrase : le mode et la liste de miroirs sont des choix de l'operateur
# (voir apt-cache-admin et server/apt-cache-mode.sh).
#   config/mode              online (defaut) ou offline
#   config/https-mirrors.conf  depots HTTPS joignables par les machines
#   apt-cache.env            APT_CACHE_ADMIN_SECRET (genere une fois)
# Migration depuis l'ancien format (apt-cache.env = APT_CACHE_OFFLINE=0/1,
# https-mirrors.conf a la racine de la stack).

setup_apt_cache() {
    local dir="$SCRIPT_DIR/stacks/apt-cache" cfg env_file
    cfg="$dir/config"
    env_file="$dir/apt-cache.env"
    mkdir -p "$dir/data" "$dir/logs" "$cfg"

    if [ ! -f "$cfg/mode" ]; then
        if grep -q '^APT_CACHE_OFFLINE=1' "$env_file" 2>/dev/null; then
            echo "offline" > "$cfg/mode"
        else
            echo "online" > "$cfg/mode"
        fi
    fi

    if [ ! -f "$cfg/https-mirrors.conf" ]; then
        if [ -f "$dir/https-mirrors.conf" ]; then
            mv "$dir/https-mirrors.conf" "$cfg/https-mirrors.conf"
        else
            cat > "$cfg/https-mirrors.conf" <<'EOT'
# Depots apt en HTTPS joignables par les machines de l'expo (qui n'ont pas
# internet). Une URL https:// par ligne. Le serveur telecharge le depot en
# HTTPS et le garde ; la machine le demande en HTTP sous la MEME adresse, par
# son proxy habituel (le cache). Seuls les depots listes ici sont joignables.
# Exemple :
#   https://download.docker.com/linux/debian
# puis, dans le role Ansible, une ligne de depot en http:// (et non https://) :
#   deb [signed-by=/etc/apt/keyrings/docker.asc] http://download.docker.com/linux/debian bookworm stable
# La cle GPG se recupere par le meme chemin : http://download.docker.com/linux/debian/gpg
# Modifiable depuis l'interface "Cache apt" du portail (bouton Appliquer) ou
# a la main, puis : sudo ./server/apt-cache-mode.sh restart
EOT
        fi
    fi

    if ! grep -q '^APT_CACHE_ADMIN_SECRET=' "$env_file" 2>/dev/null; then
        echo "[+] Generation du secret de la page d'administration integree d'apt-cacher-ng..."
        echo "APT_CACHE_ADMIN_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > "$env_file"
        chmod 600 "$env_file"
    fi
}
