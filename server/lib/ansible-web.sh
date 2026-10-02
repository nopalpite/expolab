# Fichiers de la stack ansible-web (cle SSH, config, env) - source par
# deploy-server.sh. Necessite BASTION_API_TOKEN et LAB_FAKEPI_PASSWORD
# (secrets.sh).

setup_ansible_web() {
    local dir="$SCRIPT_DIR/stacks/ansible-web"
    mkdir -p "$dir/automation_key" "$dir/repo" "$dir/runs"

    # Cle SSH dediee automatisation - montee directement dans le conteneur
    # (voir son docker-compose.yml et ANSIBLE_PRIVATE_KEY_FILE ci-dessous),
    # jamais dans le contenu clone depuis git-mirror.
    if [ ! -f "$dir/automation_key/automation_ed25519" ]; then
        echo "[+] Generation de la cle SSH dediee automatisation du lab (distincte de toute cle de vraie prod)..."
        ssh-keygen -t ed25519 -N "" -C "expolab-ansible-web" -f "$dir/automation_key/automation_ed25519" -q
    fi

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
# se rabat sinon sur root, inexistant sur les faux Pi (utilisateur "pi", voir
# fleet/provision-fakepi.sh). Fige pour ce lab ou tout le monde est "pi".
ANSIBLE_REMOTE_USER=pi
# Mot de passe des faux Pi (LAB_FAKEPI_PASSWORD, secrets.sh). ansible_ssh_pass :
# la cle ci-dessus n'est jamais injectee dans authorized_keys des faux Pi
# (configures en mot de passe uniquement) - filet de secours pour CE lab.
# Un vrai parc utiliserait Ansible Vault ou du sudo NOPASSWD sur la cle.
ANSIBLE_WEB_EXTRA_ARGS=--extra-vars ansible_become_pass=$LAB_FAKEPI_PASSWORD --extra-vars ansible_ssh_pass=$LAB_FAKEPI_PASSWORD
# Les faux Pi sont recrees souvent (nouvelle cle hote a chaque fois) : la
# verification stricte d'ansible.cfg y serait une nuisance permanente.
ANSIBLE_HOST_KEY_CHECKING=false
EOT
    chmod 600 "$dir/ansible-web.env"
}
