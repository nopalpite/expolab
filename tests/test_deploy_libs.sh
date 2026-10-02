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
chmod +x "$WORK/bin/"*
export PATH="$WORK/bin:$PATH"

SCRIPT_DIR="$WORK/server"
REPO_ROOT="$WORK"
SERVICES="$SCRIPT_DIR/services.yaml"
export REPO_ROOT SCRIPT_DIR SERVICES

for lib in tls secrets ansible-web dashboard dnsmasq stacks summary; do
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/lib/$lib.sh"
done

fail() { echo "ECHEC : $*" >&2; exit 1; }
contains() { grep -qF -- "$2" "$1" || fail "'$2' absent de $1"; }

# Pas de terminal en CI : le prompt TLS doit etre saute (mode internal).
setup_tls < /dev/null
[ "$TLS_MODE" = "internal" ] || fail "TLS_MODE attendu internal, obtenu $TLS_MODE"
[ "$PUBLIC_DOMAIN" = "web.expolab.lan" ] || fail "PUBLIC_DOMAIN inattendu : $PUBLIC_DOMAIN"

setup_bastion_secrets
setup_caddy_basic_auth
contains "$SCRIPT_DIR/stacks/bastion/bastion.env" "BASTION_API_TOKEN="
contains "$SCRIPT_DIR/stacks/bastion/bastion.env" "BASTION_ADMIN_PASSWORD="
contains "$SCRIPT_DIR/stacks/caddy/auth.env" 'BASIC_AUTH_HASH=$2a$14$FAKEHASH'
[ -n "$BASTION_ADMIN_PASSWORD" ] || fail "mot de passe Bastion vide"
[ "$BASTION_ADMIN_PASSWORD" != "raspberry" ] || fail "mot de passe Bastion non genere"

# Idempotence : un second passage ne regenere rien.
before="$(cat "$SCRIPT_DIR/stacks/caddy/auth.env" "$SCRIPT_DIR/stacks/bastion/bastion.env")"
setup_bastion_secrets
setup_caddy_basic_auth
after="$(cat "$SCRIPT_DIR/stacks/caddy/auth.env" "$SCRIPT_DIR/stacks/bastion/bastion.env")"
[ "$before" = "$after" ] || fail "les secrets ont change au second passage"

render_caddyfile
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "ansible-web.web.expolab.lan {"
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "basic_auth"
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "not path /git/*"

setup_dnsmasq_files
setup_public_domain_dns
[ ! -s "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf" ] || fail "public-domain.conf devrait etre vide en mode internal"

setup_ansible_web
contains "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" "ANSIBLE_REMOTE_USER=pi"
contains "$SCRIPT_DIR/stacks/ansible-web/ansible-web.env" "ansible_ssh_pass=raspberry"
[ -f "$SCRIPT_DIR/stacks/ansible-web/automation_key/automation_ed25519" ] || fail "cle SSH non generee"

setup_dashboard
contains "$SCRIPT_DIR/stacks/dashboard/dashboard.env" "HOMEPAGE_ALLOWED_HOSTS=dashboard.web.expolab.lan,localhost:3001"
contains "$SCRIPT_DIR/stacks/dashboard/config/services.yaml" "https://ansible-web.web.expolab.lan"

print_summary | grep -qF "$BASIC_AUTH_PASSWORD" || fail "le resume n'affiche pas le mot de passe basic auth"

# Mode signed : domaine public propage partout.
TLS_MODE=signed TLS_SIGNED_DOMAIN=expolab.example.fr PUBLIC_DOMAIN=expolab.example.fr
render_caddyfile
setup_dashboard
setup_public_domain_dns
contains "$SCRIPT_DIR/stacks/caddy/Caddyfile" "dns ovh {"
contains "$SCRIPT_DIR/stacks/dashboard/dashboard.env" "dashboard.expolab.example.fr"
contains "$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf" "address=/expolab.example.fr/10.42.0.1"

# deploy_stacks : vpn-admin n'est redeploye que si le VPN est installe.
DEPLOYED=""
dockhand_upsert_stack() { DEPLOYED="$DEPLOYED $1"; }
DEPLOYED="" && deploy_stacks > /dev/null
case "$DEPLOYED" in *" vpn-admin"*) fail "vpn-admin deploye alors que le VPN n'est pas installe" ;; esac
case "$DEPLOYED" in *" ansible-web"*) ;; *) fail "ansible-web absent des stacks deployees" ;; esac
mkdir -p "$WORK/vpn/wireguard/config" && touch "$WORK/vpn/wireguard/config/wg0.conf"
DEPLOYED="" && deploy_stacks > /dev/null
case "$DEPLOYED" in *" vpn-admin"*) ;; *) fail "vpn-admin non redeploye alors que le VPN est installe" ;; esac

echo "OK : modules server/lib"
