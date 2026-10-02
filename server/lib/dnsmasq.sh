# Fichiers de la stack dnsmasq + bascule du DHCP/DNS d'Incus - source par
# deploy-server.sh.

# dnsmasq refuse de demarrer si dhcp-hostsfile (voir dnsmasq.conf) pointe
# vers un fichier absent - cree vide au besoin, jamais ecrase si deja
# present (gere ensuite par dnsmasq-admin).
setup_dnsmasq_files() {
    mkdir -p "$SCRIPT_DIR/stacks/dnsmasq/data" "$SCRIPT_DIR/stacks/dnsmasq/admin-config"
    touch "$SCRIPT_DIR/stacks/dnsmasq/admin-config/reservations.conf"
}

# La premiere fois (DHCP integre d'Incus encore actif), on bascule et on
# renouvelle les baux de tout ce qui tourne deja sur expo-lan. Si deja
# bascule lors d'un run precedent, rien a refaire.
switch_dhcp_to_dnsmasq() {
    if [ "$(incus network get expo-lan ipv4.dhcp)" != "true" ]; then
        echo "[=] DHCP deja bascule vers dnsmasq (rien a refaire)."
        return 0
    fi

    echo "[+] Attente de la stabilisation de dnsmasq..."
    local _
    for _ in $(seq 1 15); do
        if [ "$(docker inspect -f '{{.State.Running}}' expolab-dnsmasq 2>/dev/null)" = "true" ]; then
            break
        fi
        sleep 2
    done
    sleep 3

    echo "[+] Bascule du DHCP/DNS de expo-lan vers dnsmasq (hote)..."
    incus network set expo-lan ipv4.dhcp=false
    # ipv4.dhcp=false seul ne suffit pas : le dnsmasq integre d'Incus reste
    # actif pour le DNS et garde le port 53 sur 10.42.0.1, empechant notre
    # propre dnsmasq de demarrer ("Address already in use"). dns.mode=none
    # l'arrete completement.
    incus network set expo-lan dns.mode=none

    echo "[+] Renouvellement des baux des conteneurs existants aupres du nouveau DHCP..."
    # Un simple `systemctl restart systemd-networkd` ne force pas un nouveau
    # DHCPDISCOVER : le client garde son bail precedent tant qu'il n'a pas
    # expire. Un restart complet du conteneur repart d'une interface reseau
    # vierge et force une vraie renegociation.
    local c attempt ip
    for c in $(incus list --format csv -c n 2>/dev/null || true); do
        echo "    - $c"
        for attempt in 1 2 3; do
            incus restart "$c" < /dev/null || true
            sleep 5
            ip="$(incus list "$c" --format csv -c 4 2>/dev/null | head -1)"
            if [ -n "$ip" ]; then
                break
            fi
            echo "      (pas encore de bail, nouvel essai $attempt/3)"
        done
    done
}
