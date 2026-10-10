#!/usr/bin/env bash
# Test de fumee des modules server/lib/*.sh : execute les fonctions de
# generation de fichiers (aucun Docker/Incus reel) dans une copie
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
# chronyd/systemctl factices : setup_ntp ne doit rien installer ni redemarrer ici
printf '#!/usr/bin/env bash
exit 0
' > "$WORK/bin/chronyd"
printf '#!/usr/bin/env bash
exit 0
' > "$WORK/bin/systemctl"
chmod +x "$WORK/bin/"*
export PATH="$WORK/bin:$PATH"

SCRIPT_DIR="$WORK/server"
REPO_ROOT="$WORK"
SERVICES="$SCRIPT_DIR/services.yaml"
export REPO_ROOT SCRIPT_DIR SERVICES

export CHRONY_CONF_DIR="$WORK/chrony"
for lib in tls secrets credentials automation-key ansible-web apt-cache ntp lan-isolation dashboard dnsmasq stacks summary; do
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
contains "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" "ansible_ssh_pass=raspberry"
[ -f "$SCRIPT_DIR/stacks/ansible-web/automation_key/automation_ed25519" ] || fail "cle SSH non generee"

setup_ntp
contains "$CHRONY_CONF_DIR/expolab.conf" "allow 10.42.0.0/24"
contains "$CHRONY_CONF_DIR/expolab.conf" "local stratum 10"
setup_ntp  # idempotent : un second passage ne reecrit rien

# isolate_lan : coupe le NAT d'expo-lan, sauf EXPOLAB_LAN_INTERNET=1
cat > "$WORK/bin/incus" <<'EOT'
#!/usr/bin/env bash
case "$*" in
    "network get expo-lan ipv4.nat") cat "$NAT_STATE" ;;
    "network set expo-lan ipv4.nat=false") echo false > "$NAT_STATE" ;;
esac
EOT
chmod +x "$WORK/bin/incus"
export NAT_STATE="$WORK/nat"
echo true > "$NAT_STATE"
EXPOLAB_LAN_INTERNET=1 isolate_lan > /dev/null
[ "$(cat "$NAT_STATE")" = "true" ] || fail "EXPOLAB_LAN_INTERNET=1 doit laisser le NAT"
isolate_lan > /dev/null
[ "$(cat "$NAT_STATE")" = "false" ] || fail "le NAT d'expo-lan aurait du etre coupe"
isolate_lan > /dev/null  # idempotent : deja isole

setup_apt_cache
contains "$SCRIPT_DIR/stacks/apt-cache/apt-cache.env" "APT_CACHE_OFFLINE=0"
[ -f "$SCRIPT_DIR/stacks/apt-cache/https-mirrors.conf" ] || fail "liste des miroirs HTTPS non creee"
echo "https://download.docker.com/linux/debian" >> "$SCRIPT_DIR/stacks/apt-cache/https-mirrors.conf"
echo "APT_CACHE_OFFLINE=1" > "$SCRIPT_DIR/stacks/apt-cache/apt-cache.env"
setup_apt_cache  # ne doit jamais ecraser un mode deja choisi
contains "$SCRIPT_DIR/stacks/apt-cache/apt-cache.env" "APT_CACHE_OFFLINE=1"
contains "$SCRIPT_DIR/stacks/apt-cache/https-mirrors.conf" "https://download.docker.com/linux/debian"  # liste jamais ecrasee

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
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "frame-ancestors https://dashboard.expolab.example.fr"
contains "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf" "address=/expolab.example.fr/10.42.0.1"

# deploy_stacks : vpn-admin n'est redeploye que si le VPN est installe.
DEPLOYED=""
dockhand_upsert_stack() { DEPLOYED="$DEPLOYED $1"; }
DEPLOYED="" && deploy_stacks > /dev/null
case "$DEPLOYED" in *" vpn-admin"*) fail "vpn-admin deploye alors que le VPN n'est pas installe" ;; esac
case "$DEPLOYED" in *" ansible-web"*) ;; *) fail "ansible-web absent des stacks deployees" ;; esac
case "$DEPLOYED" in *" apt-cache"*) ;; *) fail "apt-cache absent des stacks deployees" ;; esac
mkdir -p "$WORK/vpn/wireguard/config" && touch "$WORK/vpn/wireguard/config/wg0.conf"
DEPLOYED="" && deploy_stacks > /dev/null
case "$DEPLOYED" in *" vpn-admin"*) ;; *) fail "vpn-admin non redeploye alors que le VPN est installe" ;; esac

echo "OK : modules server/lib"
