"""Plans des salles pour l'interface d'exploitation : lecture SEULE des donnees de Bastion
(salles, image du plan, position de chaque machine) et presence en direct (onglet Topologie
de dnsmasq-admin). Rien n'est jamais ecrit, aucun identifiant de Bastion n'est lu ni renvoye :
seuls quelques champs sont repris (liste blanche).

Format de Bastion (config/machines.yaml) :
  rooms:    [{id, name, map_image}]
  machines: [{id, name, host, room, position: {x, y}}]   # x, y : % de la largeur/hauteur du plan
"""
import ipaddress
import json
import os
import struct
import urllib.request
from pathlib import Path

import yaml

BASTION_CONFIG = Path(os.environ.get("PORTAL_BASTION_CONFIG", "/bastion-config/machines.yaml"))
BASTION_MAPS = Path(os.environ.get("PORTAL_BASTION_MAPS", "/bastion-maps"))
TOPOLOGY_URL = os.environ.get("PORTAL_TOPOLOGY_URL", "http://127.0.0.1:5051/api/topology")
DEFAULT_RATIO = (16, 10)   # comme Bastion, le temps que le navigateur lise l'image (SVG...)
STATES = {"online": "up", "offline": "down"}   # le reste : "unknown" (jamais vue, presence inconnue)


def image_size(path: Path) -> tuple[int, int] | None:
    """(largeur, hauteur) d'une image PNG, GIF ou JPEG lue dans son en-tete ; None sinon
    (SVG, WebP...) : le navigateur corrige alors le rapport une fois l'image chargee."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if head.startswith(b"\xff\xd8"):
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    length = struct.unpack(">H", f.read(2))[0]
                    if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                        h, w = struct.unpack(">HH", f.read(5)[1:5])
                        return (w, h) if w and h else None
                    f.seek(length - 2, os.SEEK_CUR)
    except (OSError, struct.error):
        return None
    return None


def load_bastion(path: Path | None = None) -> tuple[list[dict], list[dict]]:
    """(salles, machines) - champs en liste blanche seulement."""
    try:
        data = yaml.safe_load((path or BASTION_CONFIG).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return [], []
    rooms = [{"id": str(r["id"]), "name": str(r.get("name") or r["id"]), "map_image": r.get("map_image") or ""}
             for r in data.get("rooms") or [] if isinstance(r, dict) and r.get("id")]
    machines = []
    for m in data.get("machines") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        pos = m.get("position") if isinstance(m.get("position"), dict) else None
        try:
            position = {"x": float(pos["x"]), "y": float(pos["y"])} if pos else None
        except (KeyError, TypeError, ValueError):
            position = None
        machines.append({"id": str(m["id"]), "name": str(m.get("name") or m["id"]), "host": str(m.get("host") or ""),
                         "room": str(m.get("room") or ""), "position": position})
    return rooms, machines


def fetch_presence(url: str | None = None, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url or TOPOLOGY_URL, timeout=timeout) as resp:
            return json.load(resp)
    except (OSError, ValueError):
        return None


def short_name(value: str) -> str:
    """rpi-test.expolab.lan -> rpi-test (nom tel que la machine l'annonce au DHCP)."""
    return value.split(".")[0].lower() if value else ""


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def build_plans(rooms: list[dict], machines: list[dict], topology: dict | None, maps_dir: Path | None = None) -> dict:
    """Ce qu'affiche la page Plans : salles (avec le rapport de leur image) et machines
    placees, chacune avec son etat (up / down / unknown)."""
    by_ip, by_name = {}, {}
    for t in (topology or {}).get("machines", []):
        if t.get("state") == "absent":
            continue   # reservation sans bail : on la retrouvera par son nom plus bas, inconnue
        by_ip[t.get("ip")] = t
        by_name[short_name(t.get("hostname", ""))] = t
    presence_ok = bool(topology) and topology.get("presence_ok", True)
    maps_dir = maps_dir or BASTION_MAPS

    out_rooms = []
    for room in rooms:
        image = room["map_image"]
        size = image_size(maps_dir / image) if image and "/" not in image and not image.startswith(".") else None
        placed = []
        for m in machines:
            if m["room"] != room["id"] or not m["position"]:
                continue
            t = by_ip.get(m["host"]) if is_ip(m["host"]) else None
            t = t or by_name.get(short_name(m["host"])) or by_name.get(short_name(m["name"]))
            state = STATES.get(t["state"], "unknown") if (t and presence_ok) else "unknown"
            placed.append({"id": m["id"], "name": m["name"], "label": short_name(m["name"]) or m["name"],
                           "x": m["position"]["x"], "y": m["position"]["y"], "state": state,
                           "ip": (t or {}).get("ip") or (m["host"] if is_ip(m["host"]) else ""),
                           "since": (t or {}).get("since"), "last_seen": (t or {}).get("last_seen")})
        out_rooms.append({"id": room["id"], "name": room["name"], "image": image or None,
                          "ratio": list(size or DEFAULT_RATIO), "ratio_known": bool(size), "machines": placed,
                          "unplaced": sum(1 for m in machines if m["room"] == room["id"] and not m["position"])})
    return {"rooms": out_rooms, "presence_ok": presence_ok, "now": (topology or {}).get("now")}


def map_image_allowed(name: str, rooms: list[dict]) -> bool:
    """Seules les images referencees par une salle sont servies (jamais un chemin)."""
    return any(r["map_image"] == name for r in rooms) and "/" not in name and not name.startswith(".")
