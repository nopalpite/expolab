#!/usr/bin/env python3
"""expolab caddy-admin : ajouter/retirer un service expose par Caddy.

Edite server/services.yaml (la vraie source de verite, deja utilisee par
server/render-caddyfile.py et server/deploy-server.sh) puis regenere le
Caddyfile et le pousse a Caddy via SON PROPRE admin API
(http://127.0.0.1:2019/load, atteignable car Caddy tourne en
network_mode: host comme ce conteneur) - rechargement a chaud, sans
redemarrer le conteneur Caddy ni repasser par Dockhand.
"""
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

SERVER_DIR = Path("/server")
SERVICES_PATH = SERVER_DIR / "services.yaml"
RENDER_SCRIPT = SERVER_DIR / "render-caddyfile.py"
CADDYFILE_PATH = SERVER_DIR / "stacks" / "caddy" / "Caddyfile"
CADDY_ADMIN_URL = "http://127.0.0.1:2019/load"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


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
        return yaml.safe_load(f) or {"services": []}


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


def save_services(data: dict) -> None:
    # Ecriture atomique (fichier temporaire + rename) : evite toute
    # fenetre ou une lecture concurrente verrait un fichier partiellement
    # ecrit, et garantit qu'un crash en cours d'ecriture ne laisse jamais
    # un services.yaml tronque a la place.
    tmp_path = SERVICES_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp_path, SERVICES_PATH)


def save_and_reload() -> tuple[bool, str]:
    """Regenere le Caddyfile depuis services.yaml et le pousse a Caddy."""
    result = subprocess.run(
        ["python3", str(RENDER_SCRIPT), str(SERVICES_PATH)],
        capture_output=True,
        text=True,
        timeout=15,
    )
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
    new_services = [s for s in services if s["name"] != name]
    if len(new_services) == len(services):
        return jsonify({"error": f"'{name}' introuvable"}), 404

    data["services"] = new_services
    save_services(data)

    ok, err = save_and_reload()
    if not ok:
        return jsonify({"error": f"Service retire mais rechargement Caddy echoue : {err}"}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5052)
