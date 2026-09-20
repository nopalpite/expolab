#!/usr/bin/env python3
"""expolab dnsmasq-admin : baux DHCP, reservations et enregistrements DNS.

Les baux (server/stacks/dnsmasq/data/dnsmasq.leases) sont en lecture
seule - dnsmasq les gere lui-meme. Les reservations DHCP et
enregistrements DNS statiques sont ecrits dans deux fichiers separes
(server/stacks/dnsmasq/admin-config/) que dnsmasq relit A CHAUD sur
SIGHUP (--dhcp-hostsfile / --addn-hosts, voir dnsmasq.conf) - pas besoin
de redemarrer le conteneur ni de couper le DHCP/DNS pour le reste de la
flotte a chaque modification.
"""
import ipaddress
import re
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

LEASES_PATH = Path("/dnsmasq-data/dnsmasq.leases")
RESERVATIONS_PATH = Path("/dnsmasq-admin-config/reservations.conf")
DNS_RECORDS_PATH = Path("/dnsmasq-admin-config/dns-records.conf")

EXPO_LAN = ipaddress.ip_network("10.42.0.0/24")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")


def reload_dnsmasq() -> tuple[bool, str]:
    result = subprocess.run(
        ["docker", "kill", "--signal=HUP", "expolab-dnsmasq"],
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
                "expires_in_min": max(0, remaining // 60),
            }
        )
    leases.sort(key=lambda l: l["ip"])
    return leases


def load_reservations() -> list[dict]:
    if not RESERVATIONS_PATH.exists():
        return []
    reservations = []
    for line in RESERVATIONS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        mac, ip = parts[0], parts[1]
        hostname = parts[2] if len(parts) > 2 else ""
        reservations.append({"mac": mac, "ip": ip, "hostname": hostname})
    reservations.sort(key=lambda r: r["ip"])
    return reservations


def save_reservations(reservations: list[dict]) -> None:
    RESERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{r['mac']},{r['ip']},{r['hostname']}" if r["hostname"] else f"{r['mac']},{r['ip']}" for r in reservations]
    RESERVATIONS_PATH.write_text("\n".join(lines) + ("\n" if lines else ""))


def load_dns_records() -> list[dict]:
    if not DNS_RECORDS_PATH.exists():
        return []
    records = []
    for line in DNS_RECORDS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        records.append({"ip": parts[0], "hostname": parts[1]})
    records.sort(key=lambda r: r["hostname"])
    return records


def save_dns_records(records: list[dict]) -> None:
    DNS_RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{r['ip']} {r['hostname']}" for r in records]
    DNS_RECORDS_PATH.write_text("\n".join(lines) + ("\n" if lines else ""))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/leases")
def api_leases():
    return jsonify({"leases": load_leases()})


@app.route("/api/reservations", methods=["GET"])
def api_reservations_list():
    return jsonify({"reservations": load_reservations()})


@app.route("/api/reservations", methods=["POST"])
def api_reservations_create():
    body = request.get_json(force=True, silent=True) or {}
    mac = (body.get("mac") or "").strip().lower()
    ip = (body.get("ip") or "").strip()
    hostname = (body.get("hostname") or "").strip().lower()

    if not MAC_RE.match(mac):
        return jsonify({"error": "Adresse MAC invalide (format aa:bb:cc:dd:ee:ff)"}), 400
    if not valid_ip_in_expo_lan(ip):
        return jsonify({"error": "IP invalide (doit etre dans 10.42.0.0/24)"}), 400
    if hostname and not HOSTNAME_RE.match(hostname):
        return jsonify({"error": "Hostname invalide (minuscules/chiffres/tirets)"}), 400

    reservations = load_reservations()
    if any(r["mac"] == mac for r in reservations):
        return jsonify({"error": f"Une reservation existe deja pour {mac}"}), 409
    if any(r["ip"] == ip for r in reservations):
        return jsonify({"error": f"L'IP {ip} est deja reservee"}), 409

    reservations.append({"mac": mac, "ip": ip, "hostname": hostname})
    save_reservations(reservations)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation ajoutee mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True}), 201


@app.route("/api/reservations/<mac>", methods=["DELETE"])
def api_reservations_delete(mac: str):
    mac = mac.lower()
    reservations = load_reservations()
    new_reservations = [r for r in reservations if r["mac"] != mac]
    if len(new_reservations) == len(reservations):
        return jsonify({"error": "introuvable"}), 404
    save_reservations(new_reservations)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation retiree mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True})


@app.route("/api/dns-records", methods=["GET"])
def api_dns_records_list():
    return jsonify({"records": load_dns_records()})


@app.route("/api/dns-records", methods=["POST"])
def api_dns_records_create():
    body = request.get_json(force=True, silent=True) or {}
    hostname = (body.get("hostname") or "").strip().lower()
    ip = (body.get("ip") or "").strip()

    if not HOSTNAME_RE.match(hostname):
        return jsonify({"error": "Hostname invalide (minuscules/chiffres/tirets)"}), 400
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return jsonify({"error": "IP invalide"}), 400

    records = load_dns_records()
    if any(r["hostname"] == hostname for r in records):
        return jsonify({"error": f"'{hostname}' existe deja"}), 409

    records.append({"ip": ip, "hostname": hostname})
    save_dns_records(records)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Enregistrement ajoute mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True}), 201


@app.route("/api/dns-records/<hostname>", methods=["DELETE"])
def api_dns_records_delete(hostname: str):
    hostname = hostname.lower()
    records = load_dns_records()
    new_records = [r for r in records if r["hostname"] != hostname]
    if len(new_records) == len(records):
        return jsonify({"error": "introuvable"}), 404
    save_dns_records(new_records)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Enregistrement retire mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5051)
