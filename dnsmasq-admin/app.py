#!/usr/bin/env python3
"""expolab dnsmasq-admin : baux DHCP et reservations.

Les baux (server/stacks/dnsmasq/data/dnsmasq.leases) sont en lecture
seule - dnsmasq les gere lui-meme. Les reservations DHCP sont ecrites
dans server/stacks/dnsmasq/admin-config/reservations.conf, que dnsmasq
relit A CHAUD sur SIGHUP (--dhcp-hostsfile, voir dnsmasq.conf) - pas
besoin de redemarrer le conteneur ni de couper le DHCP/DNS pour le reste
de la flotte a chaque modification.

Pas d'enregistrements DNS statiques independants du DHCP ici : tous les
services du serveur d'expo vivent sous *.web.expolab.lan, un unique
wildcard dans dnsmasq.conf (le tri par service se fait ensuite cote
Caddy via le Host: HTTP, pas via DNS) - une liste d'enregistrements
individuels par nom aurait ete purement redondante.
"""
import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

LEASES_PATH = Path("/dnsmasq-data/dnsmasq.leases")
RESERVATIONS_PATH = Path("/dnsmasq-admin-config/reservations.conf")

DNSMASQ_CONTAINER = "expolab-dnsmasq"
EXPO_LAN_INTERFACE = "expo-lan"  # interface= de dnsmasq.conf, requise par dhcp_release
EXPO_LAN = ipaddress.ip_network("10.42.0.0/24")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


def reload_dnsmasq() -> tuple[bool, str]:
    result = subprocess.run(
        ["docker", "kill", "--signal=HUP", DNSMASQ_CONTAINER],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def valid_ip_in_expo_lan(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in EXPO_LAN
    except ValueError:
        return False


def load_leases() -> list[dict]:
    if not LEASES_PATH.exists():
        return []
    leases = []
    for line in LEASES_PATH.read_text().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        expiry_epoch, mac, ip, hostname = parts[0], parts[1], parts[2], parts[3]
        client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else ""
        try:
            expiry = int(expiry_epoch)
        except ValueError:
            continue
        remaining = expiry - int(time.time())
        leases.append(
            {
                "hostname": hostname if hostname != "*" else "(inconnu)",
                "ip": ip,
                "mac": mac,
                "client_id": client_id,
                "expires_in_min": max(0, remaining // 60),
            }
        )
    leases.sort(key=lambda l: l["ip"])
    return leases


def parse_reservation_line(line: str) -> dict | None:
    """Une ligne de dhcp-hostsfile : `mac,ip[,hostname]` (reservation par
    MAC) ou `hostname,ip` (par NOM, sans MAC - dnsmasq l'applique a toute
    machine qui s'annonce sous ce nom, ce qui permet de remplacer une carte
    sans toucher a la reservation)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None
    if MAC_RE.match(parts[0].lower()):
        return {"mac": parts[0], "ip": parts[1], "hostname": parts[2] if len(parts) > 2 else ""}
    return {"mac": "", "ip": parts[1], "hostname": parts[0]}


def format_reservation_line(r: dict) -> str:
    if not r["mac"]:
        return f"{r['hostname']},{r['ip']}"
    return f"{r['mac']},{r['ip']},{r['hostname']}" if r["hostname"] else f"{r['mac']},{r['ip']}"


def reservation_id(r: dict) -> str:
    """Identifiant stable d'une reservation : sa MAC, ou son nom si elle n'en a pas."""
    return r["mac"] or r["hostname"]


def find_reservation(reservations: list[dict], ident: str) -> dict | None:
    ident = ident.lower()
    return next((r for r in reservations if reservation_id(r).lower() == ident), None)


def load_reservations() -> list[dict]:
    if not RESERVATIONS_PATH.exists():
        return []
    reservations = [r for r in map(parse_reservation_line, RESERVATIONS_PATH.read_text().splitlines()) if r]
    reservations.sort(key=lambda r: r["ip"])
    return reservations


def save_reservations(reservations: list[dict]) -> None:
    RESERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [format_reservation_line(r) for r in reservations]
    content = "\n".join(lines) + ("\n" if lines else "")
    # Ecriture atomique (fichier temporaire + rename) : evite toute
    # fenetre ou une lecture/un rechargement dnsmasq concurrent verrait
    # un fichier partiellement ecrit ou vide, et garantit qu'un crash en
    # cours d'ecriture ne laisse jamais un fichier tronque a la place des
    # reservations existantes.
    tmp_path = RESERVATIONS_PATH.with_suffix(".tmp")
    tmp_path.write_text(content)
    os.replace(tmp_path, RESERVATIONS_PATH)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/leases")
def api_leases():
    return jsonify({"leases": load_leases()})


@app.route("/api/reservations", methods=["GET"])
def api_reservations_list():
    reservations = load_reservations()
    leases = load_leases()
    leases_by_mac = {l["mac"]: l for l in leases}
    leases_by_name = {l["hostname"]: l for l in leases}
    for r in reservations:
        # dnsmasq applique la reservation immediatement pour toute
        # NOUVELLE negociation DHCP, mais un appareil qui a deja un bail
        # actif garde son IP jusqu'a son prochain renouvellement naturel
        # (ou un redemarrage) - signale ce cas plutot que de le
        # redemarrer nous-memes (voir discussion).
        lease = leases_by_mac.get(r["mac"]) if r["mac"] else leases_by_name.get(r["hostname"])
        r["id"] = reservation_id(r)
        r["pending_renewal"] = lease is not None and lease["ip"] != r["ip"]
    return jsonify({"reservations": reservations})


@app.route("/api/reservations", methods=["POST"])
def api_reservations_create():
    body = request.get_json(force=True, silent=True) or {}
    mac = (body.get("mac") or "").strip().lower()
    ip = (body.get("ip") or "").strip()
    hostname = (body.get("hostname") or "").strip().lower()

    if mac and not MAC_RE.match(mac):
        return jsonify({"error": "Adresse MAC invalide (format aa:bb:cc:dd:ee:ff)"}), 400
    if not mac and not hostname:
        return jsonify({"error": "Une adresse MAC ou un hostname est requis (sans MAC, la reservation suit le NOM de la machine)"}), 400
    if not valid_ip_in_expo_lan(ip):
        return jsonify({"error": "IP invalide (doit etre dans 10.42.0.0/24)"}), 400
    if hostname and not HOSTNAME_RE.match(hostname):
        return jsonify({"error": "Hostname invalide (minuscules/chiffres/tirets)"}), 400

    reservations = load_reservations()
    if mac and any(r["mac"] == mac for r in reservations):
        return jsonify({"error": f"Une reservation existe deja pour {mac}"}), 409
    if hostname and any(r["hostname"] == hostname for r in reservations):
        return jsonify({"error": f"Le hostname {hostname} est deja reserve"}), 409
    if any(r["ip"] == ip for r in reservations):
        return jsonify({"error": f"L'IP {ip} est deja reservee"}), 409

    reservations.append({"mac": mac, "ip": ip, "hostname": hostname})
    save_reservations(reservations)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation ajoutee mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True}), 201


@app.route("/api/reservations/<ident>", methods=["PUT"])
def api_reservations_update(ident: str):
    body = request.get_json(force=True, silent=True) or {}
    ip = (body.get("ip") or "").strip()
    hostname = (body.get("hostname") or "").strip().lower()

    if not valid_ip_in_expo_lan(ip):
        return jsonify({"error": "IP invalide (doit etre dans 10.42.0.0/24)"}), 400
    if hostname and not HOSTNAME_RE.match(hostname):
        return jsonify({"error": "Hostname invalide (minuscules/chiffres/tirets)"}), 400

    reservations = load_reservations()
    target = find_reservation(reservations, ident)
    if target is None:
        return jsonify({"error": "introuvable"}), 404
    if any(r["ip"] == ip and r is not target for r in reservations):
        return jsonify({"error": f"L'IP {ip} est deja reservee par une autre entree"}), 409

    if target["mac"]:
        if hostname and any(r["hostname"] == hostname and r is not target for r in reservations):
            return jsonify({"error": f"Le hostname {hostname} est deja reserve"}), 409
        target["hostname"] = hostname
    # Une reservation par nom ne change jamais de nom (c'est sa cle) :
    # seule son IP est modifiable, supprimer/recreer pour la renommer.
    target["ip"] = ip
    save_reservations(reservations)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation modifiee mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True})


@app.route("/api/reservations/<ident>", methods=["DELETE"])
def api_reservations_delete(ident: str):
    reservations = load_reservations()
    target = find_reservation(reservations, ident)
    if target is None:
        return jsonify({"error": "introuvable"}), 404
    save_reservations([r for r in reservations if r is not target])

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation retiree mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True})


@app.route("/api/leases/<mac>/release", methods=["POST"])
def api_leases_release(mac: str):
    """Libere le bail d'une machine (typiquement une carte morte, que sa
    remplacante - meme nom, autre MAC - doit pouvoir remplacer). Sans ca, le
    bail de la morte (12 h) retient son IP et la remplacante en recoit une
    autre, meme avec une reservation par nom. dhcp_release (paquet
    dnsmasq-utils, dans l'image dnsmasq) parle au dnsmasq en cours : aucun
    redemarrage, les autres machines ne sont pas touchees."""
    mac = mac.lower()
    if not MAC_RE.match(mac):
        return jsonify({"error": "Adresse MAC invalide"}), 400
    lease = next((l for l in load_leases() if l["mac"].lower() == mac), None)
    if lease is None:
        return jsonify({"error": "Aucun bail actif pour cette MAC"}), 404

    cmd = ["docker", "exec", DNSMASQ_CONTAINER, "dhcp_release", EXPO_LAN_INTERFACE, lease["ip"], lease["mac"]]
    if lease["client_id"]:
        # Un bail cree avec un identifiant client ne se libere que si on le
        # redonne (constate : sans lui, le bail reste en place).
        cmd.append(lease["client_id"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "dhcp_release a echoue"}), 502
    return jsonify({"ok": True, "ip": lease["ip"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5051)
