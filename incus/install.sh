#!/usr/bin/env bash
# Installe Incus sur le Raspberry Pi 5 hôte (Raspberry Pi OS / Debian arm64).
# A executer avec sudo, directement sur le Pi 5.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

echo "[+] Installation des prerequis..."
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg python3-yaml

echo "[+] Ajout du depot Incus (Zabbly)..."
mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc

# Raspberry Pi OS est base sur Debian : on reutilise le codename Debian (bookworm).
# Si l'hote est autre chose (Ubuntu Server...), verifier que le codename est
# bien supporte par le depot Zabbly : https://github.com/zabbly/incus
. /etc/os-release
CODENAME="${VERSION_CODENAME:-bookworm}"

cat > /etc/apt/sources.list.d/zabbly-incus-stable.list <<EOF
deb [signed-by=/etc/apt/keyrings/zabbly.asc] https://pkgs.zabbly.com/incus/stable ${CODENAME} main
EOF

apt-get update -qq
apt-get install -y incus incus-client

REAL_USER="${SUDO_USER:-$USER}"
usermod -aG incus-admin "$REAL_USER"
echo "[+] Utilisateur $REAL_USER ajoute au groupe incus-admin (deconnexion/reconnexion necessaire)."

echo "[+] Initialisation d'Incus (stockage local 'dir', reseau NAT par defaut)..."
incus admin init --auto --storage-backend=dir

cat <<EOF

[+] Incus est installe.

Prochaine etape :
    sudo ./network-setup.sh eth0

(remplacer eth0 par l'interface physique reellement utilisee sur le Pi 5)
EOF
