# Fichiers de la stack ntp (serveur de temps) - source par deploy-server.sh. Rien
# n'est jamais ecrase : les reglages sont des choix de l'operateur (voir ntp-admin).
#   config/mode            online (defaut) ou offline
#   config/upstream.conf   serveurs amont, un par ligne
#   config/allow.conf      reseaux autorises a demander l'heure (defaut : expo-lan)
#   config/local-fallback  on (defaut) : sert l'heure du serveur sans amont joignable
#
# Une carte de remplacement demarre avec une horloge fausse (le Pi n'a pas toujours
# de pile RTC), et sans internet rien ne la remet a l'heure : apt refuse alors un
# fichier Release "pas encore valide", Ansible et les journaux deviennent
# incoherents. Le serveur sert donc l'heure au lab, y compris sans synchronisation
# amont (`local stratum 10`). Les machines pointent dessus par l'option DHCP NTP de
# dnsmasq.conf (et image/machine-setup.sh).
#
# Avant, chrony etait installe sur l'hote ; il vit maintenant dans le conteneur
# (stack ntp, interface : ntp-admin). Les deux ne peuvent pas coexister (meme port
# UDP 123) : le chrony de l'hote est arrete et desactive ici.

setup_ntp() {
    local dir="$SCRIPT_DIR/stacks/ntp" cfg
    cfg="$dir/config"
    mkdir -p "$dir/data" "$dir/run" "$cfg"

    [ -f "$cfg/mode" ] || echo "online" > "$cfg/mode"
    if [ ! -f "$cfg/upstream.conf" ]; then
        cat > "$cfg/upstream.conf" <<'EOT'
0.pool.ntp.org
1.pool.ntp.org
2.pool.ntp.org
3.pool.ntp.org
EOT
    fi
    [ -f "$cfg/allow.conf" ] || echo "10.42.0.0/24" > "$cfg/allow.conf"
    [ -f "$cfg/local-fallback" ] || echo "on" > "$cfg/local-fallback"

    # Migration : le chrony de l'hote tient le port 123, le conteneur ne pourrait pas demarrer.
    if command -v systemctl &>/dev/null && systemctl is-active --quiet chrony 2>/dev/null; then
        echo "[+] Serveur de temps : le chrony de l'hote est remplace par la stack ntp (arret et desactivation)..."
        systemctl disable --now chrony >/dev/null 2>&1 || true
    fi
}
