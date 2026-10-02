# Helpers partages pour piloter Dockhand via son API REST plutot que
# `docker compose` directement - source par server/deploy-server.sh et
# vpn/install.sh, pas executable seul.
#
# L'API n'a besoin d'aucune authentification tant qu'elle n'a pas ete
# activee dans Dockhand (Settings > Authentication, desactive par defaut
# au premier lancement). Si l'utilisateur l'active malgre tout, deposer un
# token dans server/dockhand.env (DOCKHAND_TOKEN=dh_..., jamais commite) -
# ce fichier est alors lu et envoye en Bearer automatiquement.
#
# Noms de champs (compose/envId/deploy) devines a partir du manuel public
# (https://dockhand.pro/manual/#api-reference), qui ne documente pas le
# schema JSON exact - a verifier/ajuster contre l'instance reelle au
# premier deploiement (activer FEAT_API_DOCS=true temporairement pour
# consulter /api/docs/ui si un champ ne correspond pas).

DOCKHAND_URL="${DOCKHAND_URL:-http://127.0.0.1:3000}"
DOCKHAND_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKHAND_ENV_FILE="${DOCKHAND_ENV_FILE:-$DOCKHAND_SCRIPT_DIR/dockhand.env}"

dockhand_auth_header() {
    if [ -f "$DOCKHAND_ENV_FILE" ]; then
        # shellcheck disable=SC1090
        source "$DOCKHAND_ENV_FILE"
    fi
    if [ -n "${DOCKHAND_TOKEN:-}" ]; then
        printf 'Authorization: Bearer %s' "$DOCKHAND_TOKEN"
    fi
}

dockhand_curl() {
    local auth
    auth="$(dockhand_auth_header)"
    if [ -n "$auth" ]; then
        curl -sf -H "$auth" "$@"
    else
        curl -sf "$@"
    fi
}

dockhand_wait_healthy() {
    echo "[+] Attente de Dockhand ($DOCKHAND_URL)..."
    for _ in $(seq 1 30); do
        if dockhand_curl "$DOCKHAND_URL/api/health" &>/dev/null; then
            return 0
        fi
        sleep 2
    done
    echo "Dockhand ne repond pas sur $DOCKHAND_URL apres 60s." >&2
    return 1
}

# Echoue (silencieusement) si aucun environnement n'est configure -
# l'appelant doit alors guider l'utilisateur vers l'etape manuelle
# (aucun endpoint API ne permet de creer un environnement, voir le plan).
dockhand_get_env_id() {
    dockhand_curl "$DOCKHAND_URL/api/environments" | python3 -c '
import json, sys
envs = json.load(sys.stdin)
if isinstance(envs, dict):
    envs = envs.get("environments") or envs.get("data") or []
if not envs:
    sys.exit(1)
print(envs[0]["id"])
'
}

# Bloque jusqu'a ce qu'un environnement Dockhand soit configure, plutot
# que d'echouer immediatement - aucun endpoint API ne permet de creer un
# environnement a la place de l'utilisateur (etape UI unique), mais rien
# n'empeche d'attendre que ce soit fait au lieu de le forcer a relancer
# le script lui-meme une fois le clic effectue dans le navigateur.
dockhand_require_env() {
    if dockhand_get_env_id >/dev/null 2>&1; then
        return 0
    fi
    # $DOCKHAND_URL vise 127.0.0.1 (correct pour nos propres appels curl,
    # qui partent DU Pi) - mais inutile pour un humain qui doit ouvrir
    # cette URL depuis SON PROPRE navigateur, sur un autre poste. Detecte
    # l'IP LAN reelle de l'hote pour l'affichage (meme technique que
    # vpn/add-peer.sh pour son IP de endpoint par defaut).
    local lan_ip display_url
    lan_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
    display_url="http://${lan_ip:-<ip-du-pi>}:3000"
    cat >&2 <<EOF

[!] Aucun environnement Dockhand configure - etape manuelle unique requise
    (aucun endpoint API ne permet de la faire a notre place) :

    1. Ouvrir $display_url dans un navigateur
    2. Settings > Environments > confirmer/ajouter l'environnement local
       (Unix socket - deja monte dans le conteneur dockhand)

En attente (verifie toutes les 5s, Ctrl+C pour annuler)...
EOF
    while ! dockhand_get_env_id >/dev/null 2>&1; do
        printf '.' >&2
        sleep 5
    done
    echo >&2
    echo "[+] Environnement Dockhand detecte, on poursuit." >&2
}

# dockhand_upsert_stack <nom> <fichier_docker-compose.yml>
# Cree la stack si absente, la recree sinon (idempotent) - l'implementation
# (suppression + recreation, substitution de ${REPO_ROOT}, reconstruction
# forcee des images, attente de la fin du job) vit dans dockhand_stack.py,
# partagee avec caddy-admin : un seul endroit a maintenir. $REPO_ROOT doit
# etre exporte par l'appelant.
dockhand_upsert_stack() {
    local name="$1" compose_file="$2" rc

    : "${REPO_ROOT:?REPO_ROOT doit etre exporte avant un appel a dockhand_upsert_stack}"

    while true; do
        rc=0
        python3 "$DOCKHAND_SCRIPT_DIR/dockhand_stack.py" upsert "$name" "$compose_file" || rc=$?
        if [ "$rc" -eq 3 ]; then
            # Aucun environnement Dockhand : etape manuelle unique, on
            # attend qu'elle soit faite puis on reessaie.
            dockhand_require_env
            continue
        fi
        return "$rc"
    done
}
