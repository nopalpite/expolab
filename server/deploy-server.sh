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

# Config TLS pilotee par un fichier plutot que des variables a la CLI :
# server/stacks/caddy/tls.env est la seule source de verite, modifiable a
# la main OU depuis caddy-admin (https://caddy.web.expolab.lan, section
# "Certificat TLS") - ce dernier l'ecrit puis redemarre le conteneur
# caddy lui-meme (necessaire pour qu'il relise OVH_* comme env_file, un
# simple rechargement a chaud de son Caddyfile ne suffit pas), donc un
# changement fait depuis l'UI est deja actif sans repasser par ce script.
# TLS_MODE=internal (defaut) : certificat auto-signe (CA interne Caddy),
# fonctionne hors-ligne, avertissement navigateur a accepter une fois par
# appareil. TLS_MODE=signed : vrai certificat Let's Encrypt via DNS-01
# chez OVH (voir server/render-caddyfile.py et stacks/caddy/Dockerfile).
mkdir -p "$SCRIPT_DIR/stacks/caddy/data" "$SCRIPT_DIR/stacks/caddy/config"
if [ ! -f "$SCRIPT_DIR/stacks/caddy/tls.env" ]; then
    PROMPT_TLS_MODE=internal
    PROMPT_TLS_SIGNED_DOMAIN=
    PROMPT_OVH_ENDPOINT=
    PROMPT_OVH_APPLICATION_KEY=
    PROMPT_OVH_APPLICATION_SECRET=
    PROMPT_OVH_CONSUMER_KEY=
    # Propose le choix a la toute premiere installation (ce fichier
    # n'existe pas encore), jamais aux runs suivants (idempotence, voir
    # l'usage de ce script) - et seulement si un terminal est attache,
    # jamais en execution non-interactive (CI, script automatise) : sans
    # ca un `read` sans entree bloquerait indefiniment. Reste modifiable
    # plus tard de toute facon, a la main dans ce fichier ou depuis
    # caddy-admin (section "Certificat TLS").
    if [ -t 0 ]; then
        echo
        read -r -p "Configurer un certificat TLS signe (Let's Encrypt via OVH, DNS-01) des maintenant ? Sinon certificat auto-signe. [y/N] " ans
        if [[ "$ans" =~ ^[yY]$ ]]; then
            read -r -p "Domaine public (ex: expolab.tondomaine.fr) : " PROMPT_TLS_SIGNED_DOMAIN
            read -r -p "Endpoint OVH [ovh-eu] : " ovh_endpoint_input
            PROMPT_OVH_ENDPOINT="${ovh_endpoint_input:-ovh-eu}"
            echo "Jeton API OVH a creer sur https://www.ovh.com/auth/api/createToken/ (droits GET/PUT/POST/DELETE sur /domain/zone/*) :"
            read -r -p "  Application key : " PROMPT_OVH_APPLICATION_KEY
            read -rs -p "  Application secret : " PROMPT_OVH_APPLICATION_SECRET
            echo
            read -rs -p "  Consumer key : " PROMPT_OVH_CONSUMER_KEY
            echo
            if [ -n "$PROMPT_TLS_SIGNED_DOMAIN" ] && [ -n "$PROMPT_OVH_APPLICATION_KEY" ] && [ -n "$PROMPT_OVH_APPLICATION_SECRET" ] && [ -n "$PROMPT_OVH_CONSUMER_KEY" ]; then
                PROMPT_TLS_MODE=signed
            else
                echo "[!] Champ(s) manquant(s) - reste en certificat auto-signe (completable plus tard via caddy-admin)." >&2
            fi
        fi
    fi
    cat > "$SCRIPT_DIR/stacks/caddy/tls.env" <<EOF
TLS_MODE=$PROMPT_TLS_MODE
TLS_SIGNED_DOMAIN=$PROMPT_TLS_SIGNED_DOMAIN
OVH_ENDPOINT=$PROMPT_OVH_ENDPOINT
OVH_APPLICATION_KEY=$PROMPT_OVH_APPLICATION_KEY
OVH_APPLICATION_SECRET=$PROMPT_OVH_APPLICATION_SECRET
OVH_CONSUMER_KEY=$PROMPT_OVH_CONSUMER_KEY
EOF
fi
chmod 600 "$SCRIPT_DIR/stacks/caddy/tls.env"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/stacks/caddy/tls.env"
TLS_MODE="${TLS_MODE:-internal}"
if [ "$TLS_MODE" = "signed" ] && [ -z "${TLS_SIGNED_DOMAIN:-}" ]; then
    echo "[!] TLS_MODE=signed dans server/stacks/caddy/tls.env mais TLS_SIGNED_DOMAIN est vide - a completer a la main dans ce fichier ou via https://caddy.web.expolab.lan (section \"Certificat TLS\")." >&2
    exit 1
fi
PUBLIC_DOMAIN="${TLS_SIGNED_DOMAIN:-web.expolab.lan}"

echo "[+] Generation du Caddyfile a partir de $SERVICES (TLS_MODE=$TLS_MODE)..."
# ${TLS_SIGNED_DOMAIN:+...} n'ajoute cet argument que si la variable est
# non vide (mode signed) - render-caddyfile.py doit sinon utiliser son
# propre defaut (web.expolab.lan), jamais une chaine vide.
python3 "$SCRIPT_DIR/render-caddyfile.py" "$SERVICES" "$TLS_MODE" ${TLS_SIGNED_DOMAIN:+"$TLS_SIGNED_DOMAIN"} > "$SCRIPT_DIR/stacks/caddy/Caddyfile"

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

# Cle SSH dediee automatisation (Phase 3 de la roadmap Ansible) - montee
# directement dans le conteneur ansible-web (voir son docker-compose.yml
# et ANSIBLE_PRIVATE_KEY_FILE ci-dessous), jamais dans le contenu clone
# depuis git-mirror.
mkdir -p "$SCRIPT_DIR/stacks/ansible-web/automation_key" "$SCRIPT_DIR/stacks/ansible-web/repo" "$SCRIPT_DIR/stacks/ansible-web/runs"
if [ ! -f "$SCRIPT_DIR/stacks/ansible-web/automation_key/automation_ed25519" ]; then
    echo "[+] Generation de la cle SSH dediee automatisation du lab (distincte de toute cle de vraie prod)..."
    ssh-keygen -t ed25519 -N "" -C "expolab-ansible-web" -f "$SCRIPT_DIR/stacks/ansible-web/automation_key/automation_ed25519" -q
fi

# ansible-web refuse de demarrer si son bind-mount fichier (config.yaml)
# pointe vers un chemin absent (meme contrainte que dhcp-hostsfile/tls.env
# ailleurs dans ce script) - cree vide au besoin, jamais ecrase si deja
# present (edite ensuite depuis ansible-web lui-meme).
if [ ! -f "$SCRIPT_DIR/stacks/ansible-web/config.yaml" ]; then
    echo "mirror_name: ansible" > "$SCRIPT_DIR/stacks/ansible-web/config.yaml"
fi

# BASTION_URL/BASTION_API_TOKEN/ANSIBLE_* directement dans l'environnement
# du conteneur (contrairement a Semaphore, pas de configuration manuelle
# via une UI tierce a faire ici - ansible-web les lit directement).
cat > "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" <<EOF
BASTION_URL=http://127.0.0.1:5000
BASTION_API_TOKEN=$BASTION_API_TOKEN
ANSIBLE_PRIVATE_KEY_FILE=/keys/automation_ed25519
# Uniquement pour ce lab : les faux Pi sont recrees souvent (nouvelle cle
# hote a chaque fois), la verification stricte d'ansible.cfg y serait
# juste une nuisance permanente.
ANSIBLE_HOST_KEY_CHECKING=false
EOF
chmod 600 "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env"

# HOMEPAGE_ALLOWED_HOSTS meme raison - regenere a chaque run (voir le
# commentaire du meme nom dans stacks/dashboard/docker-compose.yml).
if [ "$TLS_MODE" = "signed" ]; then
    echo "HOMEPAGE_ALLOWED_HOSTS=dashboard.web.expolab.lan,dashboard.$PUBLIC_DOMAIN,localhost:3001" > "$SCRIPT_DIR/stacks/dashboard/dashboard.env"
else
    echo "HOMEPAGE_ALLOWED_HOSTS=dashboard.web.expolab.lan,localhost:3001" > "$SCRIPT_DIR/stacks/dashboard/dashboard.env"
fi

# Liste de liens Homepage - meme raison, voir l'en-tete de
# services.yaml.template (source editable a la main, jamais services.yaml
# lui-meme, regenere ici a chaque run).
sed "s/__PUBLIC_DOMAIN__/$PUBLIC_DOMAIN/g" "$SCRIPT_DIR/stacks/dashboard/config/services.yaml.template" > "$SCRIPT_DIR/stacks/dashboard/config/services.yaml"

mkdir -p "$SCRIPT_DIR/stacks/dnsmasq/data"

# dnsmasq refuse de demarrer si dhcp-hostsfile pointe vers un fichier
# absent (voir dnsmasq.conf) - cree vide au besoin, jamais ecrase si deja
# present (gere ensuite par dnsmasq-admin).
mkdir -p "$SCRIPT_DIR/stacks/dnsmasq/admin-config"
touch "$SCRIPT_DIR/stacks/dnsmasq/admin-config/reservations.conf"

# dnsmasq.conf inclut inconditionnellement ce fichier (conf-file=, meme
# contrainte que dhcp-hostsfile ci-dessus : erreur au demarrage s'il est
# absent) - vide en TLS_MODE=internal, sinon une seule ligne qui fait
# pointer le domaine signe vers l'hote (10.42.0.1), exactement comme
# web.expolab.lan dans dnsmasq.conf : sans ca, un appareil connecte au VPN
# resoudrait dashboard.$TLS_SIGNED_DOMAIN via le DNS public (aucun
# enregistrement A n'y pointe vers ce lab, DNS-01 n'en cree jamais) au
# lieu de Caddy en local.
if [ "$TLS_MODE" = "signed" ]; then
    echo "address=/$TLS_SIGNED_DOMAIN/10.42.0.1" > "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf"
else
    : > "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf"
fi

mkdir -p "$SCRIPT_DIR/stacks/git-mirror/data"

echo "[+] Creation/redeploiement des stacks applicatives via l'API Dockhand..."
for stack in dnsmasq dnsmasq-admin caddy caddy-admin webui bastion dashboard git-mirror ansible-web; do
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

[+] Serveur d'expo pret - ouvre https://dashboard.$PUBLIC_DOMAIN dans ton
    navigateur pour commencer (liens vers tous les services du lab).

    Dockhand      : http://${LAN_IP:-<ip-du-pi>}:3000 (toutes les stacks pilotables ici)
    DHCP/DNS      : dnsmasq, plage 10.42.0.100-250, domaine expolab.lan
    Reverse proxy : https://<service>.$PUBLIC_DOMAIN (TLS_MODE=$TLS_MODE)
                    services definis dans server/services.yaml

Certificat TLS (actuellement $TLS_MODE) : modifiable a tout moment sur
    https://caddy.$PUBLIC_DOMAIN, section "Certificat TLS".

Verification :
    curl -k https://dashboard.$PUBLIC_DOMAIN

Ansible (executeur bastion-ansible) : https://ansible-web.$PUBLIC_DOMAIN
    Etape manuelle unique si pas deja fait : creer un mirroir nomme
    '$(grep '^mirror_name:' "$SCRIPT_DIR/stacks/ansible-web/config.yaml" | cut -d' ' -f2-)'
    dans git-mirror (https://git-mirror.$PUBLIC_DOMAIN), URL =
    https://github.com/nopalpite/bastion-ansible.git - sans ca, ansible-web
    ne trouve rien a cloner (message d'erreur explicite dans son UI).
EOF
