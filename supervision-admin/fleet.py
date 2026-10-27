"""Machines a surveiller : lues dans Bastion (config/machines.yaml, lecture SEULE).

Une machine est surveillee si elle porte le tag `monitoring` dans Bastion (le meme tag installe node_exporter et la
sonde par le role Ansible `monitoring`). Les autres tags disent ce qui est attendu (voir checks.py). Seuls le nom, l'adresse,
les tags et la salle sont repris : aucun identifiant de Bastion n'est jamais lu au-dela.
"""
import os
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

MONITORING_TAG = "monitoring"


def short_name(value) -> str:
    return str(value or "").split(".")[0].strip().lower()


@dataclass(frozen=True)
class Machine:
    name: str                 # nom court (rpi-test.expolab.lan -> rpi-test), comme le DHCP et les plans
    host: str
    tags: frozenset
    room: str = ""            # nom lisible de la salle, vide si aucune


def parse(data) -> list[Machine]:
    data = data if isinstance(data, dict) else {}
    rooms = {str(r["id"]): str(r.get("name") or r["id"]) for r in data.get("rooms") or [] if isinstance(r, dict) and r.get("id")}
    machines = {}
    for m in data.get("machines") or []:
        if not isinstance(m, dict) or not m.get("name") or not m.get("host"):
            continue
        tags = frozenset(str(t) for t in m.get("tags") or [] if t)
        if MONITORING_TAG not in tags:
            continue
        name = short_name(m["name"])
        machines[name] = Machine(name, str(m["host"]).strip(), tags, rooms.get(str(m.get("room") or ""), ""))
    return sorted(machines.values(), key=lambda m: m.name)


class BastionMachines:
    """Lecture de machines.yaml, relue seulement si le fichier a change (et au plus toutes les 2 s)."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._machines, self._mtime, self._checked = [], None, -10.0
        self.error = ""

    def __call__(self) -> list[Machine]:
        now = self.clock()
        if now - self._checked < 2.0:
            return self._machines
        self._checked = now
        try:
            mtime = os.stat(self.path).st_mtime
            if mtime != self._mtime:
                with open(self.path, encoding="utf-8") as f:
                    self._machines, self._mtime = parse(yaml.safe_load(f)), mtime
            self.error = ""
        except (OSError, yaml.YAMLError) as exc:
            # Bastion illisible : on garde la derniere liste connue plutot que de tout taire.
            self.error = f"Bastion illisible : {exc.__class__.__name__}"
        return self._machines
