#!/usr/bin/env python3
"""expolab vpn-admin : creer/supprimer des pairs WireGuard depuis un navigateur.

Ne reimplemente RIEN de la logique WireGuard elle-meme - se contente
d'appeler vpn/add-peer.sh et vpn/remove-peer.sh (montes en volume, voir
server/stacks/vpn-admin/docker-compose.yml), exactement comme le ferait
un humain en ligne de commande. Objectif : eviter de dupliquer une
logique deja ecrite et deja prouvee fonctionnelle sur ce hote (contexte :
wg-easy, la solution tierce initialement envisagee, s'est averee cassee
sur le noyau Raspberry Pi 5 6.18+ - regression connue, non corrigee en
amont - d'ou ce choix de construire une page minimale par-dessus nos
propres scripts plutot que d'adopter un gros projet externe non fiable
sur ce materiel).
"""
import io
import re
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)

VPN_DIR = Path("/vpn")
WG_DIR = VPN_DIR / "wireguard" / "config"
WG0_CONF = WG_DIR / "wg0.conf"
PEERS_DIR = WG_DIR / "peers"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

PEER_BLOCK_RE = re.compile(
    r"# peer: (?P<name>\S+)\n\[Peer\]\n"
    r"PublicKey = (?P<pubkey>\S+)\n"
    r"PresharedKey = \S+\n"
    r"AllowedIPs = (?P<ip>[\d.]+)/32",
)


def wg_dump() -> dict[str, dict]:
    """pubkey -> {latest_handshake, transfer_rx, transfer_tx} via `wg show wg0 dump`."""
    try:
        out = subprocess.run(
            ["docker", "exec", "wireguard", "wg", "show", "wg0", "dump"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except Exception:
        return {}
    stats = {}
    lines = out.stdout.strip().splitlines()
    for line in lines[1:]:  # 1ere ligne = l'interface elle-meme, pas un pair
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        pubkey, _psk, _endpoint, _allowed_ips, handshake, rx, tx = parts[:7]
        stats[pubkey] = {
            "latest_handshake": int(handshake),
            "transfer_rx": int(rx),
            "transfer_tx": int(tx),
        }
    return stats


def list_peers() -> list[dict]:
    if not WG0_CONF.exists():
        return []
    content = WG0_CONF.read_text()
    stats = wg_dump()
    now = int(time.time())
    peers = []
    for m in PEER_BLOCK_RE.finditer(content):
        pubkey = m.group("pubkey")
        stat = stats.get(pubkey, {})
        handshake = stat.get("latest_handshake", 0)
        peers.append(
            {
                "name": m.group("name"),
                "vpn_ip": m.group("ip"),
                "connected": bool(handshake) and (now - handshake) < 180,
                "last_handshake_min": ((now - handshake) // 60) if handshake else None,
                "transfer_rx": stat.get("transfer_rx", 0),
                "transfer_tx": stat.get("transfer_tx", 0),
            }
        )
    return peers


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/peers", methods=["GET"])
def api_peers_list():
    return jsonify({"peers": list_peers()})


@app.route("/api/peers", methods=["POST"])
def api_peers_create():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        return jsonify({"error": "Nom invalide (minuscules/chiffres/tirets, 3-32 caracteres, doit commencer par une lettre)"}), 400

    result = subprocess.run(
        ["bash", str(VPN_DIR / "add-peer.sh"), name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or result.stdout.strip()}), 400
    return jsonify({"ok": True}), 201


@app.route("/api/peers/<name>", methods=["DELETE"])
def api_peers_delete(name: str):
    result = subprocess.run(
        ["bash", str(VPN_DIR / "remove-peer.sh"), name],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or result.stdout.strip()}), 400
    return jsonify({"ok": True})


@app.route("/api/peers/<name>/conf")
def api_peers_conf(name: str):
    if not NAME_RE.match(name):
        return jsonify({"error": "nom invalide"}), 400
    conf_path = PEERS_DIR / f"{name}.conf"
    if not conf_path.exists():
        return jsonify({"error": "introuvable"}), 404
    return send_file(
        io.BytesIO(conf_path.read_bytes()),
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"{name}.conf",
    )


@app.route("/api/peers/<name>/qr.png")
def api_peers_qr(name: str):
    if not NAME_RE.match(name):
        return jsonify({"error": "nom invalide"}), 400
    conf_path = PEERS_DIR / f"{name}.conf"
    if not conf_path.exists():
        return jsonify({"error": "introuvable"}), 404

    import qrcode

    img = qrcode.make(conf_path.read_text())
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5053)
