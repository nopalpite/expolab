# Serveur de temps local pour expo-lan - source par deploy-server.sh.
#
# Une carte de remplacement demarre avec une horloge fausse (le Pi n'a pas
# toujours de pile RTC), et sans internet rien ne la remet a l'heure : apt
# refuse alors un fichier Release "pas encore valide", Ansible et les
# journaux deviennent incoherents. Le serveur sert donc l'heure au lab,
# y compris sans synchronisation amont (`local stratum 10`) - son horloge
# est alors celle de son RTC. Les machines pointent dessus via
# image/machine-setup.sh (et l'option DHCP NTP de dnsmasq.conf).
#
# Remplace systemd-timesyncd (client seulement) par chrony sur l'hote.

CHRONY_CONF_DIR="${CHRONY_CONF_DIR:-/etc/chrony/conf.d}"

setup_ntp() {
    if ! command -v chronyd &>/dev/null; then
        echo "[+] Installation de chrony (serveur de temps pour expo-lan)..."
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq chrony
    fi

    mkdir -p "$CHRONY_CONF_DIR"
    local conf="$CHRONY_CONF_DIR/expolab.conf" new
    new="$(cat <<EOT
# Genere par deploy-server.sh (server/lib/ntp.sh) - ne pas editer a la main.
allow 10.42.0.0/24
local stratum 10
EOT
)"
    if [ "$(cat "$conf" 2>/dev/null)" != "$new" ]; then
        echo "$new" > "$conf"
        systemctl enable chrony >/dev/null 2>&1 || true
        systemctl restart chrony
    fi
}
