#!/usr/bin/env python3
"""expolab caddy-admin : ajouter/retirer un service expose par Caddy.

Edite server/services.yaml (la vraie source de verite, deja utilisee par
server/render-caddyfile.py et server/deploy-server.sh) puis regenere le
Caddyfile et le pousse a Caddy via SON PROPRE admin API
(http://127.0.0.1:2019/load, atteignable car Caddy tourne en
network_mode: host comme ce conteneur) - rechargement a chaud, sans
redemarrer le conteneur Caddy ni repasser par Dockhand.

/api/discover liste les conteneurs Docker a ports publies pas encore
references dans services.yaml - suggestion seulement, jamais ajoutee
automatiquement : le formulaire de creation est juste pre-rempli, il
faut toujours relire/ajuster et cliquer "Ajouter" soi-meme. Les
conteneurs en network_mode: host (dnsmasq, caddy, bastion, wireguard...)
n'ont pas de "port publie" au sens Docker - indetectables par ce biais,
a ajouter a la main comme aujourd'hui.
"""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from ruamel.yaml import YAML

app = Flask(__name__)

SERVER_DIR = Path("/server")
SERVICES_PATH = SERVER_DIR / "services.yaml"
RENDER_SCRIPT = SERVER_DIR / "render-caddyfile.py"
CADDYFILE_PATH = SERVER_DIR / "stacks" / "caddy" / "Caddyfile"
CADDY_ADMIN_URL = "http://127.0.0.1:2019/load"
DOCKHAND_URL = "http://127.0.0.1:3000"
# Necessaire pour resoudre les binds ${REPO_ROOT} des docker-compose.yml
# d'AUTRES stacks (caddy, dashboard) avant de les repousser a
# Dockhand - meme raison que server/dockhand-api.sh. Fourni par
# server/stacks/caddy-admin/docker-compose.yml.
REPO_ROOT = os.environ.get("REPO_ROOT", "")

# Config TLS (mode interne/signe + identifiants OVH pour le DNS-01) -
# meme fichier que celui lu par server/deploy-server.sh, seule source de
# verite commune aux deux (voir son en-tete pour le format).
TLS_PATH = SERVER_DIR / "stacks" / "caddy" / "tls.env"
TLS_KEYS = [
    "TLS_MODE",
    "TLS_SIGNED_DOMAIN",
    "OVH_ENDPOINT",
    "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_CONSUMER_KEY",
]
# Jamais renvoyes en clair par GET /api/tls - seul un booleen "deja
# configure" l'est (voir api_tls_get). Un champ laisse vide dans le
# formulaire de sauvegarde garde alors la valeur deja enregistree.
SECRET_TLS_KEYS = {"OVH_APPLICATION_KEY", "OVH_APPLICATION_SECRET", "OVH_CONSUMER_KEY"}

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

# ruamel (round-trip) plutot que PyYAML : PyYAML relit/reecrit une
# structure Python pure, ce qui perd TOUS les commentaires du fichier a
# la premiere modification faite depuis cette page (constate en
# pratique : l'entete documentant le schema de services.yaml disparaissait
# des le premier ajout/retrait). ruamel garde les commentaires attaches
# aux noeuds qu'il n'a pas touches. L'indentation choisie reproduit le
# style deja utilise dans le fichier (liste alignee sous 2 espaces).
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.preserve_quotes = True


def parse_extra_routes(raw) -> tuple[list[dict] | None, str | None]:
    """Valide et normalise la liste extra_routes soumise par le formulaire."""
    if not raw:
        return [], None
    if not isinstance(raw, list):
        return None, "extra_routes invalide"
    routes = []
    for entry in raw:
        path = (entry.get("path") or "").strip()
        port = entry.get("backend_port")
        if not path:
            continue
        if not path.startswith("/"):
            return None, f"Chemin invalide '{path}' (doit commencer par /)"
        try:
            port = int(port)
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            return None, f"Port invalide pour le chemin '{path}' (1-65535)"
        routes.append({"path": path, "backend_port": port})
    return routes, None


def load_services() -> dict:
    with open(SERVICES_PATH) as f:
        return _yaml.load(f) or {"services": []}


def used_ports(services: list[dict], exclude_name: str | None = None) -> dict[int, str]:
    """port -> description de qui l'utilise deja (service principal ou extra_route)."""
    used = {}
    for s in services:
        if s["name"] == exclude_name:
            continue
        used[s["backend_port"]] = s["name"]
        for route in s.get("extra_routes", []):
            used[route["backend_port"]] = f"{s['name']} ({route['path']})"
    return used


def discover_containers() -> list[dict]:
    """Conteneurs Docker a port(s) publie(s) pas encore dans services.yaml."""
    ids_result = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=10)
    if ids_result.returncode != 0:
        return []
    ids = ids_result.stdout.split()
    if not ids:
        return []

    inspect_result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}|{{json .NetworkSettings.Ports}}"] + ids,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if inspect_result.returncode != 0:
        return []

    used = used_ports(load_services().get("services", []))
    suggestions = []
    seen_ports = set()
    for line in inspect_result.stdout.splitlines():
        if "|" not in line:
            continue
        name, ports_json = line.split("|", 1)
        name = name.lstrip("/")
        try:
            ports = json.loads(ports_json) or {}
        except json.JSONDecodeError:
            continue
        host_ports = set()
        for bindings in ports.values():
            for b in bindings or []:
                host_port = b.get("HostPort")
                if host_port:
                    host_ports.add(int(host_port))
        for port in sorted(host_ports):
            if port in used or port in seen_ports:
                continue
            seen_ports.add(port)
            suggestions.append({"container": name, "port": port})
    return suggestions


def load_tls() -> dict:
    values = {k: "" for k in TLS_KEYS}
    if TLS_PATH.exists():
        for line in TLS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in values:
                values[key] = value
    if not values["TLS_MODE"]:
        values["TLS_MODE"] = "internal"
    return values


def save_tls(values: dict) -> None:
    # Ecriture atomique, meme raison que save_services ci-dessous.
    tmp_path = TLS_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        for key in TLS_KEYS:
            f.write(f"{key}={values.get(key, '')}\n")
    os.replace(tmp_path, TLS_PATH)
    os.chmod(TLS_PATH, 0o600)


def save_services(data) -> None:
    # Ecriture atomique (fichier temporaire + rename) : evite toute
    # fenetre ou une lecture concurrente verrait un fichier partiellement
    # ecrit, et garantit qu'un crash en cours d'ecriture ne laisse jamais
    # un services.yaml tronque a la place.
    tmp_path = SERVICES_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        _yaml.dump(data, f)
    os.replace(tmp_path, SERVICES_PATH)


def dockhand_env_id() -> int | None:
    try:
        with urllib.request.urlopen(f"{DOCKHAND_URL}/api/environments", timeout=10) as resp:
            envs = json.loads(resp.read())
        if isinstance(envs, dict):
            envs = envs.get("environments") or envs.get("data") or []
        return envs[0]["id"] if envs else None
    except Exception:
        return None


def dockhand_recreate_stack(name: str, compose_path: Path) -> tuple[bool, str]:
    """Supprime puis recree une stack Dockhand (meme principe que
    dockhand_upsert_stack dans server/dockhand-api.sh, reimplemente ici en
    Python) - seul moyen fiable pour qu'un conteneur relise un env_file
    modifie : `docker restart` garde l'environnement fige a la creation du
    conteneur, constate en pratique (identifiants/domaine mis a jour
    restes ignores apres un simple restart)."""
    env_id = dockhand_env_id()
    if env_id is None:
        return False, "Aucun environnement Dockhand configure"
    compose_content = compose_path.read_text().replace("${REPO_ROOT}", REPO_ROOT)

    del_req = urllib.request.Request(f"{DOCKHAND_URL}/api/stacks/{name}?env={env_id}", method="DELETE")
    try:
        urllib.request.urlopen(del_req, timeout=15)
    except urllib.error.HTTPError:
        pass  # stack deja absente ou jamais deployee - sans consequence
    except urllib.error.URLError as exc:
        return False, f"Impossible de joindre Dockhand : {exc.reason}"

    payload = json.dumps({"name": name, "compose": compose_content, "envId": env_id, "deploy": True}).encode()
    create_req = urllib.request.Request(
        f"{DOCKHAND_URL}/api/stacks",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(create_req, timeout=30)
    except urllib.error.HTTPError as exc:
        return False, f"Dockhand : {exc.read().decode(errors='replace')}"
    except urllib.error.URLError as exc:
        return False, f"Impossible de joindre Dockhand : {exc.reason}"
    return True, ""


def sync_public_domain_dependents(tls: dict) -> None:
    """Met a jour dashboard.env pour qu'il suive le domaine public actuel
    - meme logique que deploy-server.sh (dupliquee ici : ce chemin est
    une sauvegarde depuis l'UI, pas un redeploiement complet via ce
    script). Sans ca, Homepage refuse les requetes sur le nouveau domaine
    (validation de Host cote applicatif, independante de Caddy) -
    ansible-web n'a pas cette contrainte, rien a synchroniser pour lui."""
    domain = tls["TLS_SIGNED_DOMAIN"] if tls["TLS_MODE"] == "signed" else "web.expolab.lan"

    if tls["TLS_MODE"] == "signed":
        hosts = f"dashboard.web.expolab.lan,dashboard.{domain},localhost:3001"
    else:
        hosts = "dashboard.web.expolab.lan,localhost:3001"
    dashboard_env = SERVER_DIR / "stacks" / "dashboard" / "dashboard.env"
    dashboard_env.write_text(f"HOMEPAGE_ALLOWED_HOSTS={hosts}\n")

    # Liste de liens Homepage - meme raison, voir l'en-tete de
    # services.yaml.template (source editable a la main, jamais
    # services.yaml lui-meme, regenere ici a chaque sauvegarde TLS).
    dashboard_links_template = SERVER_DIR / "stacks" / "dashboard" / "config" / "services.yaml.template"
    dashboard_links = SERVER_DIR / "stacks" / "dashboard" / "config" / "services.yaml"
    dashboard_links.write_text(dashboard_links_template.read_text().replace("__PUBLIC_DOMAIN__", domain))


def save_and_reload() -> tuple[bool, str]:
    """Regenere le Caddyfile (services.yaml + tls.env actuels) et le pousse a Caddy.

    Toujours relire tls.env ici (pas seulement depuis api_tls_update) :
    sinon un simple ajout/retrait de service depuis ce meme fichier
    regenererait le Caddyfile en mode "internal" par defaut et
    ecraserait silencieusement un mode "signed" deja actif pour tous les
    autres vhosts.
    """
    tls = load_tls()
    mode = tls["TLS_MODE"]
    args = ["python3", str(RENDER_SCRIPT), str(SERVICES_PATH), mode]
    if mode == "signed":
        args.append(tls["TLS_SIGNED_DOMAIN"])
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return False, result.stderr

    caddyfile_content = result.stdout
    CADDYFILE_PATH.write_text(caddyfile_content)

    req = urllib.request.Request(
        CADDY_ADMIN_URL,
        data=caddyfile_content.encode(),
        headers={"Content-Type": "text/caddyfile", "Cache-Control": "must-revalidate"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                return False, f"Caddy admin API a repondu {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"Caddy admin API : {exc.read().decode(errors='replace')}"
    except urllib.error.URLError as exc:
        return False, f"Impossible de joindre l'admin API Caddy : {exc.reason}"
    return True, ""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/services", methods=["GET"])
def api_services_list():
    return jsonify(load_services())


@app.route("/api/discover")
def api_discover():
    return jsonify({"suggestions": discover_containers()})


@app.route("/api/tls", methods=["GET"])
def api_tls_get():
    tls = load_tls()
    safe = {k: v for k, v in tls.items() if k not in SECRET_TLS_KEYS}
    safe["ovh_application_key_set"] = bool(tls["OVH_APPLICATION_KEY"])
    safe["ovh_application_secret_set"] = bool(tls["OVH_APPLICATION_SECRET"])
    safe["ovh_consumer_key_set"] = bool(tls["OVH_CONSUMER_KEY"])
    return jsonify(safe)


@app.route("/api/tls", methods=["PUT"])
def api_tls_update():
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("tls_mode")
    if mode not in ("internal", "signed"):
        return jsonify({"error": "Mode TLS invalide"}), 400

    current = load_tls()
    domain = (body.get("tls_signed_domain") or "").strip()
    if mode == "signed" and not domain:
        return jsonify({"error": "Domaine requis en mode signe"}), 400

    new_values = dict(current)
    new_values["TLS_MODE"] = mode
    new_values["TLS_SIGNED_DOMAIN"] = domain
    new_values["OVH_ENDPOINT"] = (body.get("ovh_endpoint") or "ovh-eu").strip()

    # Champ laisse vide dans le formulaire = garde la valeur deja
    # enregistree (jamais renvoyee au frontend, voir api_tls_get) - seule
    # une nouvelle saisie la remplace.
    for field, key in (
        ("ovh_application_key", "OVH_APPLICATION_KEY"),
        ("ovh_application_secret", "OVH_APPLICATION_SECRET"),
        ("ovh_consumer_key", "OVH_CONSUMER_KEY"),
    ):
        value = (body.get(field) or "").strip()
        if value:
            new_values[key] = value

    if mode == "signed" and not (new_values["OVH_APPLICATION_KEY"] and new_values["OVH_APPLICATION_SECRET"] and new_values["OVH_CONSUMER_KEY"]):
        return jsonify({"error": "Identifiants OVH incomplets (application_key/secret/consumer_key)"}), 400

    save_tls(new_values)
    sync_public_domain_dependents(new_values)

    ok, err = save_and_reload()
    if not ok:
        return jsonify({"error": f"Configuration enregistree mais rechargement Caddy echoue : {err}"}), 500

    # Un rechargement a chaud (ci-dessus) suffit pour le contenu du
    # Caddyfile (domaines, extra_routes...) mais jamais pour des variables
    # d'environnement (OVH_* de Caddy, HOMEPAGE_ALLOWED_HOSTS de dashboard) :
    # chaque conteneur les lit une seule fois, au demarrage - seule une
    # recreation complete les rafraichit. Recreer 'caddy' coupe brievement
    # CETTE MEME requete (cette page est elle-meme servie via Caddy) : une
    # erreur reseau ici, cote navigateur, est attendue et sans gravite -
    # la sauvegarde a deja eu lieu avant ce point.
    errors = []
    for name, compose_rel in (
        ("caddy", "stacks/caddy/docker-compose.yml"),
        ("dashboard", "stacks/dashboard/docker-compose.yml"),
    ):
        recreate_ok, recreate_err = dockhand_recreate_stack(name, SERVER_DIR / compose_rel)
        if not recreate_ok:
            errors.append(f"{name}: {recreate_err}")

    if errors:
        return jsonify({"error": "Configuration enregistree mais redeploiement incomplet : " + "; ".join(errors)}), 500

    return jsonify({"ok": True})


@app.route("/api/services", methods=["POST"])
def api_services_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip().lower()
    port = body.get("backend_port")

    if not NAME_RE.match(name):
        return jsonify({"error": "Nom invalide (minuscules/chiffres/tirets, 3-32 caracteres, doit commencer par une lettre)"}), 400
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Port invalide (1-65535)"}), 400

    extra_routes, err = parse_extra_routes(body.get("extra_routes"))
    if err:
        return jsonify({"error": err}), 400

    data = load_services()
    services = data.setdefault("services", [])
    if any(s["name"] == name for s in services):
        return jsonify({"error": f"'{name}' existe deja"}), 409

    used = used_ports(services)
    if port in used:
        return jsonify({"error": f"Port {port} deja utilise par '{used[port]}'"}), 409
    for route in extra_routes:
        if route["backend_port"] in used:
            return jsonify({"error": f"Port {route['backend_port']} (route {route['path']}) deja utilise par '{used[route['backend_port']]}'"}), 409

    entry = {"name": name, "backend_port": port}
    if body.get("auth"):
        entry["auth"] = True
    if extra_routes:
        entry["extra_routes"] = extra_routes
    services.append(entry)
    save_services(data)

    ok, err = save_and_reload()
    if not ok:
        return jsonify({"error": f"Service ajoute mais rechargement Caddy echoue : {err}"}), 500
    return jsonify({"ok": True}), 201


@app.route("/api/services/<name>", methods=["PUT"])
def api_services_update(name: str):
    body = request.get_json(force=True, silent=True) or {}
    port = body.get("backend_port")

    try:
        port = int(port)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Port invalide (1-65535)"}), 400

    extra_routes, err = parse_extra_routes(body.get("extra_routes"))
    if err:
        return jsonify({"error": err}), 400

    data = load_services()
    services = data.get("services", [])
    target = next((s for s in services if s["name"] == name), None)
    if target is None:
        return jsonify({"error": f"'{name}' introuvable"}), 404

    used = used_ports(services, exclude_name=name)
    if port in used:
        return jsonify({"error": f"Port {port} deja utilise par '{used[port]}'"}), 409
    for route in extra_routes:
        if route["backend_port"] in used:
            return jsonify({"error": f"Port {route['backend_port']} (route {route['path']}) deja utilise par '{used[route['backend_port']]}'"}), 409

    target["backend_port"] = port
    if body.get("auth"):
        target["auth"] = True
    else:
        # Garde auth_except (config avancee, edite a la main dans
        # services.yaml) mais retire le drapeau lui-meme.
        target.pop("auth", None)
    if extra_routes:
        target["extra_routes"] = extra_routes
    else:
        target.pop("extra_routes", None)
    save_services(data)

    ok, err = save_and_reload()
    if not ok:
        return jsonify({"error": f"Service modifie mais rechargement Caddy echoue : {err}"}), 500
    return jsonify({"ok": True})


@app.route("/api/services/<name>", methods=["DELETE"])
def api_services_delete(name: str):
    data = load_services()
    services = data.get("services", [])
    target = next((s for s in services if s["name"] == name), None)
    if target is None:
        return jsonify({"error": f"'{name}' introuvable"}), 404

    # .remove() plutot qu'une liste reconstruite par comprehension :
    # garde la MEME sequence (et donc les commentaires que ruamel lui a
    # associes) au lieu d'en fabriquer une nouvelle sans historique.
    services.remove(target)
    save_services(data)

    ok, err = save_and_reload()
    if not ok:
        return jsonify({"error": f"Service retire mais rechargement Caddy echoue : {err}"}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5052)
