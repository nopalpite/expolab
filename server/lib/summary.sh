# Message de fin de deploiement - source par deploy-server.sh. Necessite
# PUBLIC_DOMAIN/TLS_MODE (tls.sh) et les secrets de secrets.sh.

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

Identifiants (generes une seule fois, conserves dans des fichiers non
versionnes - a noter) :
    UIs d'administration (dockhand, fleet, dnsmasq, caddy, vpn, git-mirror,
    ansible-web) : $BASIC_AUTH_USER / $BASIC_AUTH_PASSWORD
        (basic auth Caddy - server/stacks/caddy/auth.env)
    Bastion : admin / $BASTION_ADMIN_PASSWORD
        (server/stacks/bastion/bastion.env)

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
