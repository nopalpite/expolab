#!/usr/bin/env bash
# Deploie le "serveur d'expo" : Dockhand est lance seul (il ne peut pas se
# creer via sa propre API), puis chaque service (dnsmasq, dnsmasq-admin,
# caddy, caddy-admin, webui, bastion, dashboard) est cree/redeploye comme
# stack Dockhand independante via son API REST - voir
# server/dockhand-api.sh. C'est ce qui, en vraie vie,
# tournerait sur le vrai serveur de l'exposition (Incus n'existe pas
# la-bas, il ne sert qu'a simuler la flotte ici dans le lab).
#
# Idempotent : relancer ce script (apres avoir modifie server/services.yaml
# ou le code de webui/ par exemple) regenere le Caddyfile et redeploie les
# stacks sans repeter la bascule DHCP si elle a deja eu lieu.
#
# Usage: sudo ./deploy-server.sh [chemin_services.yaml]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICES="${1:-$SCRIPT_DIR/services.yaml}"
export REPO_ROOT

# shellcheck source=./dockhand-api.sh
source "$SCRIPT_DIR/dockhand-api.sh"

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
    REAL_USER="${SUDO_USER:-$USER}"
    usermod -aG docker "$REAL_USER"
    echo "[+] Utilisateur $REAL_USER ajoute au groupe docker (deconnexion/reconnexion necessaire)."
fi

if ! command -v envsubst &>/dev/null; then
    echo "[+] Installation de gettext-base (envsubst, pour les chemins absolus des stacks)..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gettext-base
fi

echo "[+] (Re)demarrage de Dockhand (bootstrap)..."
(cd "$SCRIPT_DIR" && docker compose up -d)

dockhand_wait_healthy
dockhand_require_env

echo "[+] Generation du Caddyfile a partir de $SERVICES..."
mkdir -p "$SCRIPT_DIR/stacks/caddy"
python3 "$SCRIPT_DIR/render-caddyfile.py" "$SERVICES" > "$SCRIPT_DIR/stacks/caddy/Caddyfile"

if [ ! -f "$SCRIPT_DIR/stacks/bastion/bastion.env" ]; then
    echo "[+] Premiere generation des secrets Bastion (server/stacks/bastion/bastion.env, non versionne)..."
    mkdir -p "$SCRIPT_DIR/stacks/bastion/config" "$SCRIPT_DIR/stacks/bastion/maps"
    BASTION_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    # Format Fernet (cle de chiffrement des identifiants SSH/VNC memorises)
    # - juste du base64 urlsafe standard sur 32 octets aleatoires, pas
    # besoin du paquet "cryptography" pour la generer (voir le README de
    # Bastion).
    BASTION_CREDENTIALS_KEY="$(python3 -c 'import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
    cat > "$SCRIPT_DIR/stacks/bastion/bastion.env" <<EOF
BASTION_SECRET_KEY=$BASTION_SECRET_KEY
BASTION_CREDENTIALS_KEY=$BASTION_CREDENTIALS_KEY
EOF
    chmod 600 "$SCRIPT_DIR/stacks/bastion/bastion.env"
fi

# BASTION_API_TOKEN active /api/machines cote Bastion (desactive par
# defaut, voir son README) - meme jeton ecrit dans bastion.env (cote
# serveur) et bastion-ansible.env (cote client) pour que la stack
# bastion-ansible ci-dessous puisse s'authentifier sans rien configurer
# a la main. Bloc hors du "if [ ! -f bastion.env ]" ci-dessus : s'applique
# aussi a un bastion.env deja existant (deploiement anterieur a cette
# fonctionnalite) et pas seulement a la toute premiere generation.
if ! grep -q '^BASTION_API_TOKEN=' "$SCRIPT_DIR/stacks/bastion/bastion.env" 2>/dev/null; then
    echo "[+] Ajout de BASTION_API_TOKEN a bastion.env (active /api/machines pour bastion-ansible)..."
    BASTION_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    echo "BASTION_API_TOKEN=$BASTION_API_TOKEN" >> "$SCRIPT_DIR/stacks/bastion/bastion.env"
else
    BASTION_API_TOKEN="$(grep '^BASTION_API_TOKEN=' "$SCRIPT_DIR/stacks/bastion/bastion.env" | cut -d= -f2-)"
fi

# Cle SSH dediee automatisation (Phase 3 de la roadmap Ansible) : a
# importer a la main dans le Key Store de Semaphore (voir le message de
# fin de script) - jamais montee dans un conteneur, Semaphore s'execute
# a partir du depot bastion-ansible clone via git-mirror, pas d'une
# image publiee par ce depot.
mkdir -p "$SCRIPT_DIR/stacks/semaphore/automation_key" "$SCRIPT_DIR/stacks/semaphore/data"
# L'image semaphoreui/semaphore tourne en non-root fixe (UID 1001) - un
# dossier cree ici (root, via sudo) reste sinon root:root et le conteneur
# ne peut pas y ouvrir sa base SQLite ("unable to open database file (14)",
# constate en pratique au premier deploiement).
chown -R 1001:1001 "$SCRIPT_DIR/stacks/semaphore/data"
if [ ! -f "$SCRIPT_DIR/stacks/semaphore/automation_key/automation_ed25519" ]; then
    echo "[+] Generation de la cle SSH dediee automatisation du lab (distincte de toute cle de vraie prod)..."
    ssh-keygen -t ed25519 -N "" -C "expolab-semaphore" -f "$SCRIPT_DIR/stacks/semaphore/automation_key/automation_ed25519" -q
fi

if [ ! -f "$SCRIPT_DIR/stacks/semaphore/semaphore.env" ]; then
    echo "[+] Premiere generation des secrets Semaphore (server/stacks/semaphore/semaphore.env, non versionne)..."
    SEMAPHORE_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    # cookie_hash/cookie_encryption/access_key_encryption : generes une
    # seule fois et persistes ici plutot que laisses a un eventuel defaut
    # auto-genere par Semaphore - garantit qu'un redeploiement de la
    # stack (delete+recreate, voir dockhand_upsert_stack) ne deconnecte
    # pas tout le monde ni ne rende les identifiants du Key Store
    # illisibles.
    SEMAPHORE_COOKIE_HASH="$(python3 -c 'import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
    SEMAPHORE_COOKIE_ENCRYPTION="$(python3 -c 'import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
    SEMAPHORE_ACCESS_KEY_ENCRYPTION="$(python3 -c 'import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
    cat > "$SCRIPT_DIR/stacks/semaphore/semaphore.env" <<EOF
SEMAPHORE_ADMIN=admin
SEMAPHORE_ADMIN_PASSWORD=$SEMAPHORE_ADMIN_PASSWORD
SEMAPHORE_ADMIN_NAME=Admin
SEMAPHORE_ADMIN_EMAIL=admin@expolab.lan
SEMAPHORE_COOKIE_HASH=$SEMAPHORE_COOKIE_HASH
SEMAPHORE_COOKIE_ENCRYPTION=$SEMAPHORE_COOKIE_ENCRYPTION
SEMAPHORE_ACCESS_KEY_ENCRYPTION=$SEMAPHORE_ACCESS_KEY_ENCRYPTION
EOF
    chmod 600 "$SCRIPT_DIR/stacks/semaphore/semaphore.env"
fi

# BASTION_URL/BASTION_API_TOKEN a coller dans le Variable Group Semaphore
# (voir le message de fin de script) - fichier de reference uniquement,
# rien ne le monte dans un conteneur.
cat > "$SCRIPT_DIR/stacks/semaphore/bastion-ansible-vars.env" <<EOF
BASTION_URL=http://127.0.0.1:5000
BASTION_API_TOKEN=$BASTION_API_TOKEN
EOF
chmod 600 "$SCRIPT_DIR/stacks/semaphore/bastion-ansible-vars.env"

mkdir -p "$SCRIPT_DIR/stacks/dnsmasq/data"

# dnsmasq refuse de demarrer si dhcp-hostsfile pointe vers un fichier
# absent (voir dnsmasq.conf) - cree vide au besoin, jamais ecrase si deja
# present (gere ensuite par dnsmasq-admin).
mkdir -p "$SCRIPT_DIR/stacks/dnsmasq/admin-config"
touch "$SCRIPT_DIR/stacks/dnsmasq/admin-config/reservations.conf"

mkdir -p "$SCRIPT_DIR/stacks/git-mirror/data"

echo "[+] Creation/redeploiement des stacks applicatives via l'API Dockhand..."
for stack in dnsmasq dnsmasq-admin caddy caddy-admin webui bastion dashboard git-mirror semaphore; do
    dockhand_upsert_stack "$stack" "$SCRIPT_DIR/stacks/$stack/docker-compose.yml"
done

# La premiere fois (DHCP integre d'Incus encore actif), on bascule et on
# renouvelle les baux de tout ce qui tourne deja sur expo-lan. Si deja
# bascule lors d'un run precedent, rien a refaire ici.
if [ "$(incus network get expo-lan ipv4.dhcp)" = "true" ]; then
    echo "[+] Attente de la stabilisation de dnsmasq..."
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
    # Un simple `systemctl restart systemd-networkd` ne force pas un
    # nouveau DHCPDISCOVER : le client garde son bail precedent tant qu'il
    # n'a pas expire. Un restart complet du conteneur repart d'une
    # interface reseau vierge et force une vraie renegociation.
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
else
    echo "[=] DHCP deja bascule vers dnsmasq (rien a refaire)."
fi

LAN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
cat <<EOF

[+] Serveur d'expo pret :
    Dashboard     : https://dashboard.web.expolab.lan (liens vers tous les services)
    Dockhand      : http://${LAN_IP:-<ip-du-pi>}:3000 (toutes les stacks pilotables ici)
    DHCP/DNS      : dnsmasq, plage 10.42.0.100-250, domaine expolab.lan
    Reverse proxy : https://<service>.web.expolab.lan (TLS auto-signe Caddy)
                    services definis dans server/services.yaml

Verification :
    curl -k https://dashboard.web.expolab.lan

Semaphore (executeur bastion-ansible) - configuration manuelle unique a
faire dans son UI (https://semaphore.web.expolab.lan, admin/$(grep '^SEMAPHORE_ADMIN_PASSWORD=' "$SCRIPT_DIR/stacks/semaphore/semaphore.env" | cut -d= -f2-)) :
    1. Repository -> mirroir git-mirror de bastion-ansible
    2. Key Store  -> importer $SCRIPT_DIR/stacks/semaphore/automation_key/automation_ed25519
    3. Inventory  -> fichier inventory/bastion_inventory.py, credential = cle ci-dessus
    4. Variable Group -> valeurs dans $SCRIPT_DIR/stacks/semaphore/bastion-ansible-vars.env
    5. Task Template -> playbook site.yml
    Detail complet : README de bastion-ansible, section "Executeur : Semaphore UI".
EOF
