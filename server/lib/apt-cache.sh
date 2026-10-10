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
    # Bind-mount d'un FICHIER : il doit exister avant le demarrage du
    # conteneur, sinon Docker cree un dossier a la place. Jamais ecrase : c'est
    # la liste de l'operateur.
    if [ ! -f "$dir/https-mirrors.conf" ]; then
        cat > "$dir/https-mirrors.conf" <<'EOT'
# Depots apt en HTTPS joignables par les machines de l'expo (qui n'ont pas
# internet). Une URL https:// par ligne. Le serveur telecharge le depot en
# HTTPS et le garde ; la machine le demande en HTTP sous la MEME adresse, par
# son proxy habituel (le cache). Seuls les depots listes ici sont joignables.
# Exemple :
#   https://download.docker.com/linux/debian
# puis, dans le role Ansible, une ligne de depot en http:// (et non https://) :
#   deb [signed-by=/etc/apt/keyrings/docker.asc] http://download.docker.com/linux/debian bookworm stable
# La cle GPG se recupere par le meme chemin : http://download.docker.com/linux/debian/gpg
# Pris en compte au redemarrage du cache : sudo ./server/apt-cache-mode.sh restart
EOT
    fi
}
