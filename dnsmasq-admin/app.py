#!/usr/bin/env python3
"""expolab dnsmasq-admin : vue des baux DHCP actifs sur expo-lan.

dnsmasq.conf change rarement une fois le lab deploye (adresses/domaines
fixes) - la vraie donnee vivante et utile a surveiller au quotidien, ce
sont les baux DHCP en cours. Lecture seule : le fichier de baux
(server/stacks/dnsmasq/data/dnsmasq.leases) est monte en lecture seule,
rien n'est ecrit depuis cette page.
"""
import time
from pathlib import Path

from flask import Flask, jsonify, render_template

app = Flask(__name__)

LEASES_PATH = Path("/dnsmasq-data/dnsmasq.leases")


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/leases")
def api_leases():
    return jsonify({"leases": load_leases()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5051)
