#!/usr/bin/env bash
# Cree le reseau LAN simule "expo-lan" pour la flotte de faux Pi : un bridge
# Incus purement virtuel, isole (pas de NAT sortant... note ci-dessous),
# qui ne depend d'AUCUNE interface physique de l'hote (fonctionne aussi bien
# sur un hote relie en Wi-Fi qu'en Ethernet).
#
# Pourquoi pas du macvlan sur une interface physique (eth0/wlan0) comme dans
# la premiere version : le besoin reel est de simuler un reseau local pour
# la flotte, pas de rendre les faux Pi visibles/joignables depuis le reseau
# physique/Internet. Le "serveur d'expo" (voir server/, DHCP/DNS/
# reverse-proxy) tourne directement sur l'hote et se branche sur ce meme
# reseau expo-lan via l'interface bridge elle-meme (10.42.0.1, deja portee
# par l'hote) - pas besoin d'un conteneur Incus dedie pour ca.
#
# En attendant que server/deploy-server.sh prenne le relais, le DHCP
# integre a Incus sur ce bridge sert a valider que chaque faux Pi recoit
# bien une IP via sa MAC. ipv4.nat=true est laisse actif pour l'instant
# uniquement pour permettre aux conteneurs de faire `apt-get install`
# pendant leur provisioning (trafic SORTANT uniquement, aucun port n'est
# expose depuis l'exterieur vers les conteneurs).
#
# Usage: sudo ./network-setup.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Ce script doit etre lance en root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[+] Creation du reseau expo-lan (bridge isole, DHCP integre)..."
if incus network show expo-lan &>/dev/null; then
    echo "[=] Le reseau expo-lan existe deja."
else
    incus network create expo-lan \
        ipv4.address=10.42.0.1/24 \
        ipv4.nat=true \
        ipv4.dhcp=true \
        ipv6.address=none
fi

echo "[+] Application du profil 'fake-pi' (ressources + reseau)..."
if incus profile show fake-pi &>/dev/null; then
    incus profile edit fake-pi < "$SCRIPT_DIR/profiles/fake-pi.yaml"
else
    incus profile create fake-pi
    incus profile edit fake-pi < "$SCRIPT_DIR/profiles/fake-pi.yaml"
fi

cat <<EOF

[+] Reseau pret.

Verification :
    incus network show expo-lan
    incus network list-leases expo-lan

Prochaine etape :
    ../fleet/deploy-fleet.sh
EOF
