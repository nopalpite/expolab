# Configuration TLS (tls.env), rendu du Caddyfile et DNS local du domaine
# public - source par deploy-server.sh. Positionne TLS_MODE,
# TLS_SIGNED_DOMAIN et PUBLIC_DOMAIN pour la suite du script.
#
# server/stacks/caddy/tls.env est la seule source de verite, modifiable a
# la main OU depuis caddy-admin (section "Certificat TLS") - ce dernier
# l'ecrit puis recree les stacks dependantes via l'API Dockhand (un
# rechargement a chaud du Caddyfile ne suffit pas pour OVH_* et
# HOMEPAGE_ALLOWED_HOSTS, lus une seule fois au demarrage de leur
# conteneur), donc un changement fait depuis l'UI est deja actif sans
# repasser par deploy-server.sh.
#   TLS_MODE=internal (defaut) : certificat auto-signe (CA interne Caddy),
#     fonctionne hors-ligne, avertissement navigateur a accepter une fois
#     par appareil.
#   TLS_MODE=signed : vrai certificat Let's Encrypt via DNS-01 chez OVH
#     (voir render-caddyfile.py et stacks/caddy/Dockerfile).

# Propose le choix a la toute premiere installation (tls.env absent),
# jamais aux runs suivants (idempotence) - et seulement si un terminal est
# attache : sans ca un `read` sans entree bloquerait indefiniment en
# execution non-interactive.
_prompt_tls_settings() {
    P_MODE=internal P_DOMAIN="" P_ENDPOINT="" P_KEY="" P_SECRET="" P_CONSUMER=""
    [ -t 0 ] || return 0

    echo
    local ans endpoint_input
    read -r -p "Configurer un certificat TLS signe (Let's Encrypt via OVH, DNS-01) des maintenant ? Sinon certificat auto-signe. [y/N] " ans
    [[ "$ans" =~ ^[yY]$ ]] || return 0

    read -r -p "Domaine public (ex: expolab.tondomaine.fr) : " P_DOMAIN
    read -r -p "Endpoint OVH [ovh-eu] : " endpoint_input
    P_ENDPOINT="${endpoint_input:-ovh-eu}"
    echo "Jeton API OVH a creer sur https://www.ovh.com/auth/api/createToken/ (droits GET/PUT/POST/DELETE sur /domain/zone/*) :"
    read -r -p "  Application key : " P_KEY
    read -rs -p "  Application secret : " P_SECRET
    echo
    read -rs -p "  Consumer key : " P_CONSUMER
    echo
    if [ -n "$P_DOMAIN" ] && [ -n "$P_KEY" ] && [ -n "$P_SECRET" ] && [ -n "$P_CONSUMER" ]; then
        P_MODE=signed
    else
        echo "[!] Champ(s) manquant(s) - reste en certificat auto-signe (completable plus tard via caddy-admin)." >&2
    fi
}

setup_tls() {
    local tls_env="$SCRIPT_DIR/stacks/caddy/tls.env"
    mkdir -p "$SCRIPT_DIR/stacks/caddy/data" "$SCRIPT_DIR/stacks/caddy/config"

    if [ ! -f "$tls_env" ]; then
        _prompt_tls_settings
        cat > "$tls_env" <<EOT
TLS_MODE=$P_MODE
TLS_SIGNED_DOMAIN=$P_DOMAIN
OVH_ENDPOINT=$P_ENDPOINT
OVH_APPLICATION_KEY=$P_KEY
OVH_APPLICATION_SECRET=$P_SECRET
OVH_CONSUMER_KEY=$P_CONSUMER
EOT
    fi
    chmod 600 "$tls_env"
    # shellcheck disable=SC1090
    source "$tls_env"

    TLS_MODE="${TLS_MODE:-internal}"
    if [ "$TLS_MODE" = "signed" ] && [ -z "${TLS_SIGNED_DOMAIN:-}" ]; then
        echo "[!] TLS_MODE=signed dans server/stacks/caddy/tls.env mais TLS_SIGNED_DOMAIN est vide - a completer a la main dans ce fichier ou via https://caddy.web.expolab.lan (section \"Certificat TLS\")." >&2
        exit 1
    fi
    PUBLIC_DOMAIN="${TLS_SIGNED_DOMAIN:-web.expolab.lan}"
}

render_caddyfile() {
    echo "[+] Generation du Caddyfile a partir de $SERVICES (TLS_MODE=$TLS_MODE)..."
    # ${TLS_SIGNED_DOMAIN:+...} n'ajoute cet argument que si la variable
    # est non vide (mode signed) - render-caddyfile.py doit sinon utiliser
    # son propre defaut (web.expolab.lan), jamais une chaine vide.
    python3 "$SCRIPT_DIR/render-caddyfile.py" "$SERVICES" "$TLS_MODE" ${TLS_SIGNED_DOMAIN:+"$TLS_SIGNED_DOMAIN"} > "$SCRIPT_DIR/stacks/caddy/Caddyfile"
}

# dnsmasq.conf inclut inconditionnellement ce fichier (conf-file=, erreur
# au demarrage s'il est absent) - vide en TLS_MODE=internal, sinon une
# seule ligne qui fait pointer le domaine signe vers l'hote (10.42.0.1),
# exactement comme web.expolab.lan dans dnsmasq.conf : sans ca, un appareil
# connecte au VPN resoudrait dashboard.$TLS_SIGNED_DOMAIN via le DNS public
# (DNS-01 ne cree jamais d'enregistrement A) au lieu de Caddy en local.
setup_public_domain_dns() {
    local conf="$SCRIPT_DIR/stacks/dnsmasq/admin-config/public-domain.conf"
    if [ "$TLS_MODE" = "signed" ]; then
        echo "address=/$TLS_SIGNED_DOMAIN/10.42.0.1" > "$conf"
    else
        : > "$conf"
    fi
}
