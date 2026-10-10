#!/usr/bin/env bash
# Configuration commune a toute machine de l'expo - vraie (a lancer une
# fois sur la carte "modele", voir README, section "Remplacer une machine")
# ou simulee (pousse et lance par fleet/deploy-fleet.sh dans chaque faux Pi).
# Une seule implementation pour les deux : ce que le lab valide est
# exactement ce que porte l'image de la vraie vie.
#
# Deux etapes, idempotentes, a lancer en root :
#   --stage network : proxy apt vers le cache du serveur (avec repli en
#                     connexion directe si le cache ne repond pas) et
#                     serveur de temps du serveur. A faire AVANT le premier
#                     `apt-get`.
#   --stage access  : cle publique d'automatisation dans authorized_keys et
#                     sudo sans mot de passe pour l'utilisateur donne -
#                     remplace l'authentification par mot de passe.
#   (--stage all fait les deux, defaut)
#
# Usage: machine-setup.sh [--stage network|access|all] [--user NOM]
#                         [--pubkey-file FICHIER] [--server-ip IP]
#
# Variables (surtout pour les tests) : MACHINE_SETUP_ROOT (prefixe toutes
# les ecritures), MACHINE_SETUP_HOME (home de l'utilisateur),
# EXPOLAB_SERVER_IP (defaut 10.42.0.1), EXPOLAB_APT_PROXY_PORT (3142).
set -euo pipefail

ROOT="${MACHINE_SETUP_ROOT:-}"
SERVER_IP="${EXPOLAB_SERVER_IP:-10.42.0.1}"
APT_PROXY_PORT="${EXPOLAB_APT_PROXY_PORT:-3142}"
STAGE="all"
USER_NAME=""
PUBKEY_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --stage) STAGE="$2"; shift 2 ;;
        --user) USER_NAME="$2"; shift 2 ;;
        --pubkey-file) PUBKEY_FILE="$2"; shift 2 ;;
        --server-ip) SERVER_IP="$2"; shift 2 ;;
        *) echo "Option inconnue : $1" >&2; exit 2 ;;
    esac
done

stage_network() {
    install -d "$ROOT/usr/local/bin" "$ROOT/etc/apt/apt.conf.d"

    # Script appele par apt avant chaque operation reseau : proxy du
    # serveur s'il repond, connexion directe sinon (installation initiale
    # avant que le serveur existe, ou machine branchee ailleurs).
    cat > "$ROOT/usr/local/bin/expolab-apt-proxy" <<EOF
#!/usr/bin/env bash
if timeout 2 bash -c 'exec 3<>/dev/tcp/$SERVER_IP/$APT_PROXY_PORT' 2>/dev/null; then
    echo "http://$SERVER_IP:$APT_PROXY_PORT"
else
    echo "DIRECT"
fi
EOF
    chmod 755 "$ROOT/usr/local/bin/expolab-apt-proxy"

    cat > "$ROOT/etc/apt/apt.conf.d/01expolab-proxy" <<EOF
Acquire::http::Proxy-Auto-Detect "/usr/local/bin/expolab-apt-proxy";
EOF

    # Une carte neuve demarre avec une horloge fausse : apt refuse alors un
    # fichier Release "pas encore valide". Le serveur sert l'heure au lab.
    if [ -d "$ROOT/etc/systemd" ]; then
        install -d "$ROOT/etc/systemd/timesyncd.conf.d"
        printf '[Time]\nNTP=%s\n' "$SERVER_IP" > "$ROOT/etc/systemd/timesyncd.conf.d/expolab.conf"
    fi
    echo "[+] Reseau : proxy apt (repli direct) et serveur de temps -> $SERVER_IP"
}

stage_access() {
    if [ -z "$USER_NAME" ] || [ -z "$PUBKEY_FILE" ]; then
        echo "--stage access exige --user et --pubkey-file" >&2
        exit 2
    fi
    if [ ! -s "$PUBKEY_FILE" ]; then
        echo "Cle publique introuvable ou vide : $PUBKEY_FILE" >&2
        exit 2
    fi

    local home_dir="${MACHINE_SETUP_HOME:-$(getent passwd "$USER_NAME" | cut -d: -f6)}"
    if [ -z "$home_dir" ]; then
        echo "Utilisateur inconnu : $USER_NAME" >&2
        exit 2
    fi

    mkdir -p "$home_dir/.ssh"
    chmod 700 "$home_dir/.ssh"
    touch "$home_dir/.ssh/authorized_keys"
    local key
    key="$(cat "$PUBKEY_FILE")"
    grep -qxF "$key" "$home_dir/.ssh/authorized_keys" || echo "$key" >> "$home_dir/.ssh/authorized_keys"
    chmod 600 "$home_dir/.ssh/authorized_keys"
    if [ "$(id -u)" -eq 0 ] && id "$USER_NAME" >/dev/null 2>&1; then
        chown -R "$USER_NAME:$(id -gn "$USER_NAME")" "$home_dir/.ssh"
    fi

    install -d "$ROOT/etc/sudoers.d"
    local sudoers="$ROOT/etc/sudoers.d/90-expolab-automation"
    rm -f "$sudoers"  # lecture seule (440) apres un premier passage : on recree
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$USER_NAME" > "$sudoers"
    chmod 440 "$sudoers"
    # Un fichier sudoers invalide verrouille sudo : valide quand c'est possible.
    if [ -z "$ROOT" ] && command -v visudo >/dev/null 2>&1; then
        visudo -cf "$sudoers" >/dev/null || { rm -f "$sudoers"; echo "sudoers invalide, retire" >&2; exit 1; }
    fi
    echo "[+] Acces : cle d'automatisation et sudo sans mot de passe pour $USER_NAME"
}

case "$STAGE" in
    network) stage_network ;;
    access) stage_access ;;
    all) stage_network; stage_access ;;
    *) echo "--stage doit valoir network, access ou all" >&2; exit 2 ;;
esac
