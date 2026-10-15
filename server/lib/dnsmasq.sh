# Fichiers de la stack dnsmasq - source par deploy-server.sh.

# dnsmasq refuse de demarrer si dhcp-hostsfile (voir dnsmasq.conf) pointe
# vers un fichier absent - cree vide au besoin, jamais ecrase si deja
# present (gere ensuite par dnsmasq-admin).
setup_dnsmasq_files() {
    mkdir -p "$SCRIPT_DIR/stacks/dnsmasq/data" "$SCRIPT_DIR/stacks/dnsmasq/admin-config"
    touch "$SCRIPT_DIR/stacks/dnsmasq/admin-config/reservations.conf"
}
