# Cle SSH dediee a l'automatisation (Ansible) - partagee par
# fleet/deploy-fleet.sh (qui en injecte la partie publique dans les faux Pi,
# des leur creation) et server/lib/ansible-web.sh (qui monte la partie
# privee dans le conteneur ansible-web). Source, jamais execute seul.
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
