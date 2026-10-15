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
