# Tenue du serveur face aux coupures de courant et aux blocages - source par deploy-server.sh.
#
#   - expolab-recover.timer : relance les conteneurs que Docker n'a pas su relancer apres un
#     arret brutal (server/recover-containers.sh), 1 min apres le demarrage puis toutes les 2 min ;
#   - watchdog materiel : si le systeme se fige, la puce du Pi le redemarre (15 s, le maximum
#     de son watchdog) ;
#   - logs Docker tournants (10 Mo x 3 par conteneur) : moins d'ecritures sur la carte SD, et
#     jamais une carte pleine a cause d'un conteneur bavard ;
#   - journal systeme persistant mais borne (200 Mo) : apres une coupure ou un plantage, on
#     peut lire ce qui s'est passe (journalctl -b -1).
# Idempotent : relance Docker ou journald seulement quand leur reglage change.
# Chemins surchargeables pour les tests (EXPOLAB_ETC, EXPOLAB_SYSTEMD_DIR, EXPOLAB_JOURNAL_DIR).

RESILIENCE_DOCKER_DAEMON='{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}'

# install_if_changed <fichier> <contenu> : ecrit le fichier s'il differe ; code 0 si change.
install_if_changed() {
    local file="$1" content="$2"
    if [ -f "$file" ] && [ "$(cat "$file")" = "$content" ]; then
        return 1
    fi
    mkdir -p "$(dirname "$file")"
    printf '%s\n' "$content" > "$file"
    return 0
}

setup_resilience() {
    local etc="${EXPOLAB_ETC:-/etc}" units="${EXPOLAB_SYSTEMD_DIR:-/etc/systemd/system}" changed_units=0

    # --- conteneurs non relances apres un arret brutal ---
    install_if_changed "$units/expolab-recover.service" "[Unit]
Description=expolab : relance des conteneurs coupes par un arret brutal
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash $REPO_ROOT/server/recover-containers.sh" && changed_units=1
    install_if_changed "$units/expolab-recover.timer" "[Unit]
Description=expolab : verification reguliere des conteneurs coupes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target" && changed_units=1

    if [ "$changed_units" = 1 ]; then
        systemctl daemon-reload
    fi
    systemctl enable --now expolab-recover.timer > /dev/null 2>&1 \
        || echo "[!] Impossible d'activer expolab-recover.timer." >&2
    echo "[+] Relance des conteneurs coupes par un arret brutal activee."

    # --- watchdog materiel ---
    if install_if_changed "$etc/systemd/system.conf.d/expolab-watchdog.conf" "# Ecrit par expolab (server/lib/resilience.sh) : systeme fige = redemarrage par le watchdog du Pi.
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min"; then
        systemctl daemon-reexec
        echo "[+] Watchdog materiel active (redemarrage si le systeme se fige)."
    fi

    # --- journal systeme persistant, borne ---
    if install_if_changed "$etc/systemd/journald.conf.d/expolab.conf" "# Ecrit par expolab (server/lib/resilience.sh) : garde le journal d'un demarrage a l'autre.
[Journal]
Storage=persistent
SystemMaxUse=200M"; then
        mkdir -p "${EXPOLAB_JOURNAL_DIR:-/var/log/journal}"
        systemctl restart systemd-journald
        # Sinon le journal du demarrage en cours reste en memoire (/run) jusqu'au prochain.
        journalctl --flush 2> /dev/null || true
        echo "[+] Journal systeme persistant (200 Mo max) : journalctl -b -1 apres une coupure."
    fi

    # --- logs Docker tournants ---
    local daemon="$etc/docker/daemon.json"
    if [ -f "$daemon" ] && ! grep -q '"max-size"' "$daemon"; then
        echo "[!] $daemon existe deja (pas ecrit par expolab) : rotation des logs Docker non appliquee." >&2
    elif install_if_changed "$daemon" "$RESILIENCE_DOCKER_DAEMON"; then
        # Ne vaut que pour les conteneurs crees ensuite : deploy-server.sh les recree tous.
        systemctl restart docker
        echo "[+] Logs Docker tournants (10 Mo x 3 par conteneur) : Docker redemarre."
    fi
}
