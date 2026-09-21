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
# Cree la stack si absente, la redeploie sinon (idempotent).
#
# Les binds relatifs ("./x") d'une stack creee par l'API Dockhand se
# resolvent dans SON propre repertoire de donnees gere, pas dans ce depot
# git - donc chaque docker-compose.yml de stack utilise des chemins
# ABSOLUS via ${REPO_ROOT} (jamais mis en scene par Dockhand, voir le
# manuel), substitue ici avant l'envoi. $REPO_ROOT doit etre exporte par
# l'appelant.
dockhand_upsert_stack() {
    local name="$1" compose_file="$2" env_id compose_content payload

    : "${REPO_ROOT:?REPO_ROOT doit etre exporte avant un appel a dockhand_upsert_stack}"
    env_id="$(dockhand_get_env_id)" || { dockhand_require_env; env_id="$(dockhand_get_env_id)"; }
    compose_content="$(REPO_ROOT="$REPO_ROOT" envsubst '${REPO_ROOT}' < "$compose_file")"

    # Pas de PUT documente pour mettre a jour le contenu compose d'une
    # stack existante (seuls les git-stacks en ont un) - POST .../deploy
    # ne fait que rejouer ce que Dockhand a DEJA en memoire, sans jamais
    # relire nos fichiers locaux. Direct constate en pratique : un
    # correctif local (ex: retirer un sysctl invalide) redeploye "avec
    # succes" ne changeait rien, l'ancienne definition cassee restait
    # active. Seul chemin fiable pour rester synchronise avec nos
    # fichiers : supprimer puis recreer a chaque fois.
    if dockhand_curl "$DOCKHAND_URL/api/stacks?env=$env_id" | python3 -c "
import json, sys
stacks = json.load(sys.stdin)
if isinstance(stacks, dict):
    stacks = stacks.get('stacks') or stacks.get('data') or []
sys.exit(0 if any(s.get('name') == '$name' for s in stacks) else 1)
" 2>/dev/null; then
        echo "[=] Stack '$name' deja presente, suppression avant recreation (pour appliquer nos fichiers locaux a jour)..."
        dockhand_curl -X DELETE "$DOCKHAND_URL/api/stacks/$name?env=$env_id" >/dev/null || true
    fi

    # Pour les stacks avec `build:` (dnsmasq, webui, wireguard) : le
    # `docker compose up -d` que Dockhand lance en interne ne reconstruit
    # PAS une image deja presente localement, meme si le Dockerfile a
    # change entre-temps (compose ne suit pas son contenu, juste son
    # existence). Constate en pratique : le correctif "ajouter iproute2"
    # au Dockerfile wireguard n'avait aucun effet tant que l'ancienne
    # image `wireguard-wireguard` restait en cache. Supprimer ici toute
    # image nommee "<stack>-*" (convention Compose v2 <projet>-<service>)
    # force une reconstruction complete a chaque upsert.
    docker images -q --filter "reference=${name}-*" 2>/dev/null | sort -u | while read -r img; do
        [ -n "$img" ] && docker rmi -f "$img" >/dev/null 2>&1 || true
    done

    echo "[+] Creation de la stack '$name'..."
    payload="$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "compose": sys.argv[2],
    "envId": int(sys.argv[3]),
    "deploy": True,
}))
' "$name" "$compose_content" "$env_id")"
    dockhand_curl -X POST "$DOCKHAND_URL/api/stacks" \
        -H 'Content-Type: application/json' \
        -d "$payload" >/dev/null
}
