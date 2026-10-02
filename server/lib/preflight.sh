# Verifications prealables + bootstrap de Dockhand - source par
# deploy-server.sh (jamais execute seul). Suppose SCRIPT_DIR, REPO_ROOT et
# dockhand-api.sh deja charges par l'appelant.

preflight_checks() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Ce script doit etre lance en root (sudo)." >&2
        exit 1
    fi

    if ! command -v incus &>/dev/null; then
        echo "incus introuvable. Lancer d'abord ../incus/install.sh" >&2
        exit 1
    fi

    if ! incus network show expo-lan &>/dev/null; then
        echo "Le reseau expo-lan n'existe pas. Lancer d'abord ../incus/network-setup.sh" >&2
        exit 1
    fi

    if ! command -v docker &>/dev/null; then
        echo "[+] Installation de Docker (script officiel get.docker.com)..."
        curl -fsSL https://get.docker.com | sh
        local real_user="${SUDO_USER:-$USER}"
        usermod -aG docker "$real_user"
        echo "[+] Utilisateur $real_user ajoute au groupe docker (deconnexion/reconnexion necessaire)."
    fi

    if ! command -v envsubst &>/dev/null; then
        echo "[+] Installation de gettext-base (envsubst, pour les chemins absolus des stacks)..."
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gettext-base
    fi
}

bootstrap_dockhand() {
    echo "[+] (Re)demarrage de Dockhand (bootstrap)..."
    (cd "$SCRIPT_DIR" && docker compose up -d)

    dockhand_wait_healthy
    dockhand_require_env
}
