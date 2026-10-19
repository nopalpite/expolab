#!/usr/bin/env bash
# Test de fumee des modules server/lib/*.sh : execute les fonctions de
# generation de fichiers (aucun Docker reel) dans une copie
# temporaire de server/, avec des faux `docker`/`ip` en tete du PATH, et
# verifie le contenu produit. Lance en CI (.github/workflows/ci.yml) et
# a la main : ./tests/test_deploy_libs.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp -r "$REPO/server" "$WORK/server"
mkdir -p "$WORK/bin"

# Faux docker : seul `docker run ... caddy hash-password` est utilise ici.
cat > "$WORK/bin/docker" <<'EOT'
#!/usr/bin/env bash
case "$*" in
    *hash-password*) echo '$2a$14$FAKEHASHFORTESTSONLYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' ;;
    *) exit 0 ;;
esac
EOT
printf '#!/usr/bin/env bash\necho "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.77"\n' > "$WORK/bin/ip"
# python3 peut etre absent/ombre selon la plateforme (shim du Store sous Windows)
if ! python3 -c 'pass' 2>/dev/null; then
    printf '#!/usr/bin/env bash\nexec python "$@"\n' > "$WORK/bin/python3"
fi
# systemctl factice : setup_ntp ne doit rien arreter ni redemarrer ici
printf '#!/usr/bin/env bash
exit 1
' > "$WORK/bin/systemctl"
chmod +x "$WORK/bin/"*
export PATH="$WORK/bin:$PATH"

SCRIPT_DIR="$WORK/server"
REPO_ROOT="$WORK"
SERVICES="$SCRIPT_DIR/services.yaml"
export REPO_ROOT SCRIPT_DIR SERVICES

for lib in tls secrets credentials automation-key ansible-web sd-flasher apt-cache ntp matter prises audio lan-isolation dashboard dnsmasq stacks summary; do
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/lib/$lib.sh"
done

fail() { echo "ECHEC : $*" >&2; exit 1; }
contains() { grep -qF -- "$2" "$1" || fail "'$2' absent de $1"; }

# Pas de terminal en CI : le prompt TLS doit etre saute (mode internal).
setup_tls < /dev/null
[ "$TLS_MODE" = "internal" ] || fail "TLS_MODE attendu internal, obtenu $TLS_MODE"
[ "$PUBLIC_DOMAIN" = "web.expolab.lan" ] || fail "PUBLIC_DOMAIN inattendu : $PUBLIC_DOMAIN"

# --- identifiants d'administration : un seul couple, choisi par l'utilisateur ---
# Sans terminal ni variables : refus net (pas de mot de passe que personne n'a choisi).
( setup_admin_credentials < /dev/null ) > /dev/null 2>&1 && fail "identifiants absents : le script aurait du s'arreter"
# Variables invalides : refusees.
( EXPOLAB_ADMIN_USER=ab EXPOLAB_ADMIN_PASSWORD=Mot-de-passe-1 setup_admin_credentials ) > /dev/null 2>&1 && fail "identifiant trop court accepte"
( EXPOLAB_ADMIN_USER=admin EXPOLAB_ADMIN_PASSWORD=court setup_admin_credentials ) > /dev/null 2>&1 && fail "mot de passe trop court accepte"
( EXPOLAB_ADMIN_USER=admin EXPOLAB_ADMIN_PASSWORD="avec'apostrophe" setup_admin_credentials ) > /dev/null 2>&1 && fail "apostrophe acceptee"
# Valides, avec des caracteres que Docker Compose interpreterait dans un env_file.
export EXPOLAB_ADMIN_USER=gestion.expo
export EXPOLAB_ADMIN_PASSWORD='Mdp $HOME #1 "ok" \x'
setup_admin_credentials > /dev/null
[ "$ADMIN_USER" = "gestion.expo" ] || fail "ADMIN_USER inattendu : $ADMIN_USER"
[ "$ADMIN_PASSWORD" = "$EXPOLAB_ADMIN_PASSWORD" ] || fail "ADMIN_PASSWORD altere"

setup_bastion_secrets
setup_caddy_basic_auth
contains "$SCRIPT_DIR/stacks/bastion/bastion.env" "BASTION_API_TOKEN="
contains "$SCRIPT_DIR/stacks/bastion/bastion.env" "BASTION_ADMIN_USER=gestion.expo"
# Mot de passe a caracteres speciaux : entre apostrophes dans le fichier, intact une fois relu.
contains "$SCRIPT_DIR/stacks/bastion/bastion.env" "BASTION_ADMIN_PASSWORD='Mdp"
[ "$BASTION_ADMIN_USER" = "gestion.expo" ] || fail "identifiant Bastion inattendu : $BASTION_ADMIN_USER"
[ "$BASTION_ADMIN_PASSWORD" = "$ADMIN_PASSWORD" ] || fail "mot de passe Bastion different du mot de passe choisi"
contains "$SCRIPT_DIR/stacks/caddy/auth.env" 'BASIC_AUTH_HASH=$2a$14$FAKEHASH'
contains "$SCRIPT_DIR/stacks/caddy/auth.env" "BASIC_AUTH_USER=gestion.expo"
[ "$BASIC_AUTH_USER" = "$ADMIN_USER" ] && [ "$BASIC_AUTH_PASSWORD" = "$ADMIN_PASSWORD" ] || fail "basic auth different des identifiants choisis"

# Une installation existante garde ses identifiants : variables ignorees, rien de redemande.
( unset ADMIN_USER ADMIN_PASSWORD; EXPOLAB_ADMIN_USER=autre EXPOLAB_ADMIN_PASSWORD=Autre-mdp-123 setup_admin_credentials < /dev/null > /dev/null && [ "$ADMIN_USER" = "gestion.expo" ] ) || fail "les identifiants existants n'ont pas ete repris"

# Idempotence : un second passage ne regenere rien.
before="$(cat "$SCRIPT_DIR/stacks/caddy/auth.env" "$SCRIPT_DIR/stacks/bastion/bastion.env")"
setup_bastion_secrets
setup_caddy_basic_auth
after="$(cat "$SCRIPT_DIR/stacks/caddy/auth.env" "$SCRIPT_DIR/stacks/bastion/bastion.env")"
[ "$before" = "$after" ] || fail "les secrets ont change au second passage"

render_caddyfile
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "ansible-web.web.expolab.lan {"
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "forward_auth @protected localhost:3001"
! grep -q "basic_auth" "$SCRIPT_DIR/stacks/caddy/Caddyfile" || fail "basic_auth encore genere : la session est geree par le portail"
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "not path /git/*"

setup_dnsmasq_files
setup_public_domain_dns
[ ! -s "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf" ] || fail "public-domain.conf devrait etre vide en mode internal"

setup_ansible_web
contains "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" "ANSIBLE_REMOTE_USER=pi"
! grep -q "ansible_ssh_pass" "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" || fail "mot de passe de lab encore passe a ansible"
[ -f "$SCRIPT_DIR/stacks/ansible-web/automation_key/automation_ed25519" ] || fail "cle SSH non generee"

setup_matter
setup_prises
[ -d "$SCRIPT_DIR/stacks/prises-admin/data" ] || fail "dossier de la stack prises-admin non cree"
[ -d "$SCRIPT_DIR/stacks/matter/data" ] || fail "dossier de la stack matter non cree"
# Console audio : jeton des agents genere une fois, prive, jamais regenere.
setup_audio
TOKEN="$SCRIPT_DIR/stacks/audio-admin/data/agent.token"
[ -s "$TOKEN" ] || fail "jeton des agents audio non genere"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) ;; *)   # pas de modes Unix sous Windows
    [ "$(stat -c %a "$TOKEN")" = "600" ] || fail "jeton des agents lisible par d'autres" ;;
esac
before="$(cat "$TOKEN")"
setup_audio
[ "$(cat "$TOKEN")" = "$before" ] || fail "jeton des agents regenere (couperait les machines installees)"
# Le role audio de bastion-ansible le lit dans l'environnement d'ansible-web.
setup_ansible_web
contains "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" "AUDIO_AGENT_TOKEN=$before"

setup_ntp
NTP="$SCRIPT_DIR/stacks/ntp/config"
contains "$NTP/allow.conf" "10.42.0.0/24"
contains "$NTP/mode" "online"
contains "$NTP/local-fallback" "on"
contains "$NTP/upstream.conf" "pool.ntp.org"
printf 'offline
' > "$NTP/mode"
setup_ntp  # idempotent : un second passage n'ecrase jamais un choix de l'operateur
contains "$NTP/mode" "offline"

# isolate_lan : coupe le NAT d'expo-lan (network/lan-internet.sh), sauf EXPOLAB_LAN_INTERNET=1
cp -r "$REPO/network" "$WORK/network"
cat > "$WORK/bin/iptables" <<'EOT'
#!/usr/bin/env bash
# Faux iptables : une regle par ligne dans $IPT_STATE ("table chaine regle...").
table=filter
[ "$1" = "-t" ] && { table="$2"; shift 2; }
op="$1"; chain="$2"; shift 2
[ "$op" = "-I" ] && [[ "$1" =~ ^[0-9]+$ ]] && shift
rule="$table $chain $*"
touch "$IPT_STATE"
case "$op" in
    -C) grep -qxF -- "$rule" "$IPT_STATE" ;;
    -A|-I) echo "$rule" >> "$IPT_STATE" ;;
    -D) grep -vxF -- "$rule" "$IPT_STATE" > "$IPT_STATE.tmp" || true; mv "$IPT_STATE.tmp" "$IPT_STATE" ;;
esac
EOT
printf '#!/usr/bin/env bash
exit 0
' > "$WORK/bin/sysctl"
chmod +x "$WORK/bin/iptables" "$WORK/bin/sysctl"
export IPT_STATE="$WORK/iptables.rules"
bash "$WORK/network/lan-internet.sh" on > /dev/null
[ "$(wc -l < "$IPT_STATE")" -eq 3 ] || fail "lan-internet.sh on : 3 regles attendues"
bash "$WORK/network/lan-internet.sh" on > /dev/null
[ "$(wc -l < "$IPT_STATE")" -eq 3 ] || fail "lan-internet.sh on n'est pas idempotent"
bash "$WORK/network/lan-internet.sh" status | grep -q "NAT actif" || fail "status : NAT actif attendu"
EXPOLAB_LAN_INTERNET=1 isolate_lan > /dev/null
[ -s "$IPT_STATE" ] || fail "EXPOLAB_LAN_INTERNET=1 doit laisser le NAT"
isolate_lan > /dev/null
[ ! -s "$IPT_STATE" ] || fail "le NAT d'expo-lan aurait du etre coupe"
isolate_lan > /dev/null  # idempotent : deja isole
bash "$WORK/network/lan-internet.sh" status | grep -q "isolees" || fail "status : machines isolees attendu"

setup_apt_cache
APT="$SCRIPT_DIR/stacks/apt-cache"
[ "$(cat "$APT/config/mode")" = "online" ] || fail "mode par defaut du cache apt : online attendu"
[ -f "$APT/config/https-mirrors.conf" ] || fail "liste des miroirs HTTPS non creee"
[ -d "$APT/logs" ] && [ -d "$APT/data" ] || fail "dossiers data/ et logs/ du cache apt non crees"
contains "$APT/apt-cache.env" "APT_CACHE_ADMIN_SECRET="
secret="$(cat "$APT/apt-cache.env")"
echo "https://download.docker.com/linux/debian" >> "$APT/config/https-mirrors.conf"
echo "offline" > "$APT/config/mode"
setup_apt_cache  # ne doit jamais ecraser un mode, une liste ni un secret deja choisis
[ "$(cat "$APT/config/mode")" = "offline" ] || fail "mode ecrase par un redeploiement"
contains "$APT/config/https-mirrors.conf" "https://download.docker.com/linux/debian"  # liste jamais ecrasee
[ "$secret" = "$(cat "$APT/apt-cache.env")" ] || fail "secret du cache apt regenere"

# Migration depuis l'ancien format (APT_CACHE_OFFLINE dans apt-cache.env, miroirs a la racine).
rm -rf "$APT/config" "$APT/apt-cache.env"
printf 'APT_CACHE_OFFLINE=1
' > "$APT/apt-cache.env"
printf 'https://packages.example.org/apt
' > "$APT/https-mirrors.conf"
setup_apt_cache
[ "$(cat "$APT/config/mode")" = "offline" ] || fail "ancien mode offline non migre"
contains "$APT/config/https-mirrors.conf" "https://packages.example.org/apt"
[ ! -e "$APT/https-mirrors.conf" ] || fail "ancienne liste de miroirs non deplacee"
! grep -q "APT_CACHE_OFFLINE" "$APT/apt-cache.env" || fail "ancien format d'apt-cache.env conserve"
contains "$APT/apt-cache.env" "APT_CACHE_ADMIN_SECRET="

setup_dashboard
contains "$SCRIPT_DIR/stacks/dashboard/dashboard.env" "PORTAL_DOMAIN=web.expolab.lan"
contains "$SCRIPT_DIR/stacks/dashboard/dashboard.env" "PORTAL_TLS_MODE=internal"
contains "$SCRIPT_DIR/stacks/dashboard/portal.env" "PORTAL_SECRET="
contains "$SCRIPT_DIR/stacks/dashboard/services.yaml" "name: ansible-web"  # copie de services.yaml pour le portail
secret_before="$(cat "$SCRIPT_DIR/stacks/dashboard/portal.env")"
setup_dashboard  # la cle de session ne change jamais (sinon toutes les sessions sauteraient)
[ "$secret_before" = "$(cat "$SCRIPT_DIR/stacks/dashboard/portal.env")" ] || fail "PORTAL_SECRET regenere"

summary="$(print_summary)"
echo "$summary" | grep -qF "gestion.expo" || fail "le resume n'affiche pas l'identifiant"
! echo "$summary" | grep -qF "$ADMIN_PASSWORD" || fail "le resume affiche le mot de passe choisi"

# Mode signed : domaine public propage partout.
TLS_MODE=signed TLS_SIGNED_DOMAIN=expolab.example.fr PUBLIC_DOMAIN=expolab.example.fr
render_caddyfile
setup_dashboard
setup_public_domain_dns
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "dns ovh {"
contains "$SCRIPT_DIR/stacks/dashboard/dashboard.env" "PORTAL_DOMAIN=expolab.example.fr"
contains "$SCRIPT_DIR/stacks/dashboard/dashboard.env" "PORTAL_TLS_MODE=signed"
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "frame-ancestors 'self' https://dashboard.expolab.example.fr"
contains "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf" "address=/expolab.example.fr/10.42.0.1"

# setup_sd_flasher : dossiers crees, cle PUBLIQUE copiee (jamais la privee), rien d'ecrase.
FLASH="$SCRIPT_DIR/stacks/sd-flasher"
ensure_automation_key "$REPO_ROOT"
setup_sd_flasher
[ -d "$FLASH/images" ] && [ -d "$FLASH/data" ] || fail "dossiers sd-flasher absents"
contains "$FLASH/keys/automation.pub" "expolab-automation"
[ ! -e "$FLASH/keys/automation_ed25519" ] || fail "cle privee copiee dans sd-flasher"
# Cle d'acces de Bastion : seconde paire, distincte de celle d'Ansible, jamais regeneree.
setup_access_key
ACCESS="$SCRIPT_DIR/stacks/bastion/access_key/bastion_ed25519"
[ -f "$ACCESS" ] && [ -f "$ACCESS.pub" ] || fail "cle d'acces de Bastion non generee"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) ;; *)   # pas de modes Unix sous Windows
    [ "$(stat -c %a "$ACCESS")" = "600" ] || fail "cle privee d'acces lisible par d'autres" ;;
esac
contains "$ACCESS.pub" "expolab-bastion"
contains "$SCRIPT_DIR/stacks/bastion/access_key/access.env" "BASTION_DEFAULT_SSH_USER=pi"
! cmp -s "$ACCESS.pub" "$SCRIPT_DIR/stacks/ansible-web/automation_key/automation_ed25519.pub" || fail "meme cle pour Bastion et Ansible"
before="$(cat "$ACCESS.pub")"
setup_access_key
[ "$(cat "$ACCESS.pub")" = "$before" ] || fail "cle d'acces regeneree"
setup_sd_flasher
contains "$FLASH/keys/access.pub" "expolab-bastion"
[ ! -e "$FLASH/keys/bastion_ed25519" ] || fail "cle privee d'acces copiee dans sd-flasher"
echo "master" > "$FLASH/images/master.img"
setup_sd_flasher
[ "$(cat "$FLASH/images/master.img")" = "master" ] || fail "image sd-flasher ecrasee"


# deploy_stacks : vpn-admin n'est redeploye que si le VPN est installe.
DEPLOYED=""
dockhand_upsert_stack() { DEPLOYED="$DEPLOYED $1"; }
DEPLOYED="" && deploy_stacks > /dev/null
case "$DEPLOYED" in *" vpn-admin"*) fail "vpn-admin deploye alors que le VPN n'est pas installe" ;; esac
case "$DEPLOYED" in *" ansible-web"*) ;; *) fail "ansible-web absent des stacks deployees" ;; esac
case "$DEPLOYED" in *" apt-cache"*) ;; *) fail "apt-cache absent des stacks deployees" ;; esac
case "$DEPLOYED" in *" apt-cache-admin"*) ;; *) fail "apt-cache-admin absent des stacks deployees" ;; esac
case "$DEPLOYED" in *" ntp "*) ;; *) fail "ntp absent des stacks deployees" ;; esac
case "$DEPLOYED" in *" ntp-admin"*) ;; *) fail "ntp-admin absent des stacks deployees" ;; esac
case "$DEPLOYED" in *" sd-flasher"*) ;; *) fail "sd-flasher absent des stacks deployees" ;; esac
mkdir -p "$WORK/vpn/wireguard/config" && touch "$WORK/vpn/wireguard/config/wg0.conf"
DEPLOYED="" && deploy_stacks > /dev/null
case "$DEPLOYED" in *" vpn-admin"*) ;; *) fail "vpn-admin non redeploye alors que le VPN est installe" ;; esac

echo "OK : modules server/lib"
