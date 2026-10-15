# Message de fin de deploiement - source par deploy-server.sh. Necessite
# PUBLIC_DOMAIN/TLS_MODE (tls.sh) et les secrets de secrets.sh.

# Un bastion.env anterieur a l'identifiant unique garde son ancien mot de passe
# genere : on le rappelle plutot que de le laisser introuvable.
legacy_bastion_note() {
    if [ "$BASTION_ADMIN_USER" != "$ADMIN_USER" ] || [ "$BASTION_ADMIN_PASSWORD" != "$ADMIN_PASSWORD" ]; then
        printf '    Bastion (identifiants anterieurs, inchanges) : %s / %s\n' "$BASTION_ADMIN_USER" "$BASTION_ADMIN_PASSWORD"
    fi
}

print_summary() {
    local lan_ip mirror
    lan_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
    mirror="$(grep '^mirror_name:' "$SCRIPT_DIR/stacks/ansible-web/config.yaml" | cut -d' ' -f2-)"

    cat <<EOT

[+] Serveur d'expo pret - ouvre https://dashboard.$PUBLIC_DOMAIN dans ton
    navigateur pour commencer (liens vers tous les services du lab).

    Dockhand      : http://${lan_ip:-<ip-du-pi>}:3000 (toutes les stacks pilotables ici)
    DHCP/DNS      : dnsmasq, plage 10.42.0.100-250, domaine expolab.lan
    Reverse proxy : https://<service>.$PUBLIC_DOMAIN (TLS_MODE=$TLS_MODE)
                    services definis dans server/services.yaml

Identifiants d'administration (ceux que tu as choisis a l'installation, les
memes partout, conserves dans server/stacks/caddy/auth.env) : $ADMIN_USER
    Dockhand, UIs d'administration (dnsmasq, caddy, vpn, git-mirror,
    ansible-web) derriere le basic auth Caddy, Bastion.
$(legacy_bastion_note)
Certificat TLS (actuellement $TLS_MODE) : modifiable a tout moment sur
    https://caddy.$PUBLIC_DOMAIN, section "Certificat TLS".

Verification :
    curl -k https://dashboard.$PUBLIC_DOMAIN

Ansible (executeur bastion-ansible) : https://ansible-web.$PUBLIC_DOMAIN
    Etape manuelle unique si pas deja fait : creer un mirroir nomme
    '$mirror'
    dans git-mirror (https://git-mirror.$PUBLIC_DOMAIN), URL =
    https://github.com/nopalpite/bastion-ansible.git - sans ca, ansible-web
    ne trouve rien a cloner (message d'erreur explicite dans son UI).
EOT
}
