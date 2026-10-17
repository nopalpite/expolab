# Cle SSH dediee a l'automatisation (Ansible) - partie publique injectee
# dans les machines par le flasheur de cartes SD (sd-flasher.sh), partie
# privee montee dans le conteneur ansible-web (ansible-web.sh). Source, jamais execute seul.
# Idempotent : ne regenere jamais une cle existante.

# Chemin unique de la cle, relatif a la racine du depot.
AUTOMATION_KEY_REL="server/stacks/ansible-web/automation_key/automation_ed25519"

# ensure_automation_key <racine_du_depot>
ensure_automation_key() {
    local key="$1/$AUTOMATION_KEY_REL"
    mkdir -p "$(dirname "$key")"
    if [ ! -f "$key" ]; then
        echo "[+] Generation de la cle SSH dediee automatisation du lab (distincte de toute cle de vraie prod)..."
        ssh-keygen -t ed25519 -N "" -C "expolab-automation" -f "$key" -q
    fi
}

# --- Cle d'ACCES (Bastion) -------------------------------------------------------------
# Seconde paire, DISTINCTE de la cle d'automatisation : celle-ci sert aux acces HUMAINS
# ouverts par Bastion (terminal SSH, fichiers), l'autre aux taches Ansible - une connexion
# avec la cle d'automatisation est ainsi toujours un job, jamais une session. Partie
# publique injectee par le flasheur dans chaque carte, partie privee montee en lecture
# seule dans le conteneur Bastion (cle SSH par defaut) : jamais saisie, jamais copiee dans
# un navigateur. Idempotent : ne regenere jamais une cle existante (la perdre couperait
# l'acces aux machines deja flashees : elle est a sauvegarder avec les donnees du serveur).
ACCESS_KEY_REL="server/stacks/bastion/access_key/bastion_ed25519"

# ensure_access_key <racine_du_depot>
ensure_access_key() {
    local key="$1/$ACCESS_KEY_REL"
    mkdir -p "$(dirname "$key")"
    chmod 700 "$(dirname "$key")"
    if [ ! -f "$key" ]; then
        echo "[+] Generation de la cle SSH d'acces de Bastion (distincte de la cle d'automatisation)..."
        ssh-keygen -t ed25519 -N "" -C "expolab-bastion" -f "$key" -q
    fi
}

# Compte SSH par defaut de Bastion : le meme que celui d'Ansible (celui que le flasheur
# cree sur les cartes), BASTION_DEFAULT_SSH_USER dans access_key/access.env (env_file du
# compose de Bastion, recree a chaque deploiement).
setup_access_key() {
    ensure_access_key "$REPO_ROOT"
    printf 'BASTION_DEFAULT_SSH_USER=%s
' "${EXPOLAB_ANSIBLE_USER:-pi}" > "$REPO_ROOT/$(dirname "$ACCESS_KEY_REL")/access.env"
}
