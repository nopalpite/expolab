"""Salles et appartenance des machines : lues dans Bastion (config/machines.yaml, lecture SEULE).

La console audio ne cree ni ne range aucune salle : une machine n'existe dans la console que si Bastion la
range dans une salle (`room` de la machine). Une machine creee dans Bastion n'a pas de salle par defaut : elle
n'apparait donc pas dans la console tant qu'on ne l'a pas placee dans Bastion. Le rapprochement se fait par le
NOM COURT de la machine (rpi-test.expolab.lan -> rpi-test, comme dans le DHCP et le plan de l'exploitant).
Seuls les champs utiles sont lus : aucun identifiant de Bastion n'est jamais repris.
"""
import os
import time
from pathlib import Path

import yaml


def short_name(value) -> str:
    return str(value or "").split(".")[0].strip().lower()


class RoomMap:
    """{id de salle: nom} et {nom court de machine: id de salle}."""

    def __init__(self, rooms: dict | None = None, members: dict | None = None):
        self.rooms = dict(rooms or {})
        self.members = dict(members or {})

    def room_of(self, name: str) -> str | None:
        return self.members.get(short_name(name))

    def room_name(self, room_id: str) -> str:
        return self.rooms.get(room_id, room_id)


def parse(data) -> RoomMap:
    data = data if isinstance(data, dict) else {}
    rooms = {str(r["id"]): str(r.get("name") or r["id"]) for r in data.get("rooms") or [] if isinstance(r, dict) and r.get("id")}
    members = {}
    for m in data.get("machines") or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        room = str(m.get("room") or "")
        if room and room in rooms:
            members[short_name(m["name"])] = room
    return RoomMap(rooms, members)


class BastionRooms:
    """Lecture de machines.yaml, relue seulement si le fichier a change (et au plus toutes les 2 s)."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._map, self._mtime, self._checked = RoomMap(), None, -10.0

    def __call__(self) -> RoomMap:
        now = self.clock()
        if now - self._checked < 2.0:
            return self._map
        self._checked = now
        try:
            mtime = os.stat(self.path).st_mtime
            if mtime != self._mtime:
                with open(self.path, encoding="utf-8") as f:
                    self._map, self._mtime = parse(yaml.safe_load(f)), mtime
        except (OSError, yaml.YAMLError):
            self._map, self._mtime = RoomMap(), None      # Bastion illisible : aucune machine affichee, rien de pilote
        return self._map
