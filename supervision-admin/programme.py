"""Programme prevu pour chaque machine : lu dans ansible-web (onglet « Applications », apps.json, lecture SEULE).

ansible-web enregistre par machine le programme et la version voulus (un tag de release, ou « latest ») et ce que son
dernier run a installe. La sonde de la machine dit ce qui est vraiment pose et s'il tourne (checks.program_info) ;
compare() rapproche les deux pour la page. Une version differente est seulement affichee, jamais une alerte.
"""
import json
import os
import time
from pathlib import Path

from fleet import short_name

LATEST = "latest"


class PlannedPrograms:
    """Lecture d'apps.json, relue seulement si le fichier a change (et au plus toutes les 2 s)."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._planned, self._mtime, self._checked = {}, None, -10.0
        self.error = ""

    def __call__(self) -> dict:
        now = self.clock()
        if now - self._checked < 2.0:
            return self._planned
        self._checked = now
        try:
            mtime = os.stat(self.path).st_mtime
            if mtime != self._mtime:
                self._planned, self._mtime = parse(json.loads(self.path.read_text(encoding="utf-8"))), mtime
            self.error = ""
        except FileNotFoundError:
            self._planned, self._mtime, self.error = {}, None, ""      # aucun programme choisi pour l'instant
        except (OSError, ValueError) as exc:
            self.error = f"ansible-web illisible : {exc.__class__.__name__}"
        return self._planned


def parse(data) -> dict:
    """{machine courte: {app, version, last_run}} pour les machines qui ont un programme choisi."""
    planned = {}
    for host, entry in (data if isinstance(data, dict) else {}).items():
        if not isinstance(entry, dict) or not entry.get("app"):
            continue
        installed = entry.get("installed") if isinstance(entry.get("installed"), dict) else {}
        planned[short_name(host)] = {
            "app": str(entry["app"]), "version": str(entry.get("version") or LATEST),
            # version posee par le dernier run reussi d'ansible-web (seule reference quand on suit « latest »)
            "last_run": str(installed["version"]) if installed.get("app") == entry["app"] and installed.get("version") else "",
        }
    return planned


def compare(planned: dict | None, installed: dict | None) -> str:
    """« à jour », « différent », « absent » (prevu, rien sur la machine), « non prévu » (rien dans ansible-web),
    « inconnu » (« latest » jamais installe par un run) ou "" (ni l'un ni l'autre)."""
    if not planned:
        return "non prévu" if installed else ""
    if not installed:
        return "absent"
    target = planned["last_run"] if planned["version"] == LATEST else planned["version"]
    if installed["name"] != planned["app"]:
        return "différent"
    if not target:
        return "inconnu"
    return "à jour" if installed["version"] == target else "différent"
