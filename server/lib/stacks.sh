# Creation/redeploiement des stacks via l'API Dockhand - source par
# deploy-server.sh. Necessite dockhand_upsert_stack (dockhand-api.sh).

SERVER_STACKS="dnsmasq dnsmasq-admin caddy caddy-admin webui bastion dashboard git-mirror ansible-web"

deploy_stacks() {
    echo "[+] Creation/redeploiement des stacks applicatives via l'API Dockhand..."
    local stack
    for stack in $SERVER_STACKS; do
        dockhand_upsert_stack "$stack" "$SCRIPT_DIR/stacks/$stack/docker-compose.yml"
    done

    # vpn-admin est deploye par vpn/install.sh, avec wireguard qu'il pilote -
    # mais son image est construite depuis le code de ce depot (vpn-admin/) :
    # sans redeploiement ici, un changement de ce code n'etait jamais pris en
    # compte par ce script (constate : un correctif de liaison reseau
    # n'avait aucun effet). Seulement si le VPN est deja installe, sinon on
    # demarrerait un conteneur qui n'a rien a administrer.
    if [ -f "$REPO_ROOT/vpn/wireguard/config/wg0.conf" ]; then
        dockhand_upsert_stack vpn-admin "$SCRIPT_DIR/stacks/vpn-admin/docker-compose.yml"
    fi
}
