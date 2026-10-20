# Fichiers de la stack ansible-web (cle SSH, config, env) - source par
# deploy-server.sh. Necessite BASTION_API_TOKEN (secrets.sh) et
# ensure_automation_key (automation-key.sh).

setup_ansible_web() {
    local dir="$SCRIPT_DIR/stacks/ansible-web"
    mkdir -p "$dir/automation_key" "$dir/repo" "$dir/runs" "$dir/deploy/requests" "$dir/deploy/status"

    # Cle SSH dediee automatisation - montee directement dans le conteneur
    # (voir son docker-compose.yml et ANSIBLE_PRIVATE_KEY_FILE ci-dessous),
    # jamais dans le contenu clone depuis git-mirror. Sa partie publique est
    # injectee dans les machines par le flasheur de cartes SD.
    ensure_automation_key "$REPO_ROOT"

    # Bind-mount d'un FICHIER (config.yaml) : doit exister avant le
    # demarrage du conteneur, sinon Docker cree un dossier a la place.
    if [ ! -f "$dir/config.yaml" ]; then
        echo "mirror_name: ansible" > "$dir/config.yaml"
    fi

    cat > "$dir/ansible-web.env" <<EOT
BASTION_URL=http://127.0.0.1:5000
BASTION_API_TOKEN=$BASTION_API_TOKEN
ANSIBLE_PRIVATE_KEY_FILE=/keys/automation_ed25519
# Bastion n'expose pas de nom d'utilisateur SSH dans /api/machines - Ansible
# se rabat sinon sur root. Compte cree sur les machines par le flasheur
# (cle d'automatisation, sudo sans mot de passe) : EXPOLAB_ANSIBLE_USER.
ANSIBLE_REMOTE_USER=${EXPOLAB_ANSIBLE_USER:-pi}
# Une carte remplacee garde son nom et son IP mais a une nouvelle cle d'hote :
# la verification stricte d'ansible.cfg bloquerait chaque remplacement.
ANSIBLE_HOST_KEY_CHECKING=false
# Jeton des agents audio (setup_audio, audio.sh) : le role audio de bastion-ansible
# l'installe sur chaque machine. Lu ici, jamais versionne.
AUDIO_AGENT_TOKEN=$(cat "$SCRIPT_DIR/stacks/audio-admin/data/agent.token" 2>/dev/null)
EOT
    chmod 600 "$dir/ansible-web.env"
}
