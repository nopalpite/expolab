"""Etat et regles de la console audio (aucune dependance web : testable seul).

Trois niveaux, tous en dB (0 dB = pleine echelle, -60 dB = silence) :
    exposition  ->  salle (seulement s'il y a PLUSIEURS salles)  ->  machine

    niveau envoye a une machine = son fader + volume de sa salle + volume de l'exposition
    (une machine « hors exposition » ignore le volume de l'exposition, pas son mute)

Les salles et l'appartenance des machines viennent de BASTION (bastion.py) : une machine qui n'est dans aucune salle
n'existe pas pour la console (ni affichee, ni pilotee : elle garde son niveau local). L'agent de chaque machine ne
recoit que son niveau EFFECTIF (gain en dB + mute) : la hierarchie reste ici. S'il n'y a qu'une salle, le volume
« general » EST le volume de cette salle : pas de niveau « salle ». L'etat est conserve sur disque : un redemarrage du
serveur ne change aucun niveau.
"""
import json
import os
import re
import threading
import time

from bastion import RoomMap

MIN_DB = -60.0
MAX_DB = 0.0
ONLINE_SECONDS = 12.0     # une machine sans nouvelle depuis ce delai est « hors ligne »
POLL_SECONDS = 8.0        # duree maximale d'une attente longue : le hub entend une machine au moins toutes les ~8 s
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
PRESET_RE = re.compile(r"^[^\x00-\x1f]{1,40}$")


class HubError(ValueError):
    """Requete invalide : le message est destine a l'utilisateur."""


def clamp_db(value) -> float:
    try:
        db = float(value)
    except (TypeError, ValueError):
        raise HubError("Niveau invalide.") from None
    if db != db:   # NaN
        raise HubError("Niveau invalide.")
    return max(MIN_DB, min(MAX_DB, db))


def pct_to_db(pct) -> float:
    """Echelle de l'exploitant : 0 a 100 % repartis lineairement en dB entre -60 et 0."""
    try:
        p = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        raise HubError("Niveau invalide.") from None
    return round(MIN_DB + p * (MAX_DB - MIN_DB) / 100.0, 1)


def db_to_pct(db: float) -> int:
    return int(round((db - MIN_DB) * 100.0 / (MAX_DB - MIN_DB)))


class Hub:
    def __init__(self, path: str, clock=time.time, rooms_source=None):
        self.path = path
        self.clock = clock
        self.rooms_source = rooms_source or (lambda: RoomMap())     # Bastion : {salles, appartenance}
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.expo = {"db": -6.0, "mute": False}
        self.rooms: dict[str, dict] = {}        # niveau de chaque salle, par id de salle de Bastion
        self.devices: dict[str, dict] = {}
        self.presets: dict[str, dict] = {}
        self.current: str | None = None
        self.modified = False
        self.live: dict[str, dict] = {}         # etat vu par l'agent, jamais conserve
        self.rev = 1
        self.load()

    # --- conservation -----------------------------------------------------------------------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        try:
            self.expo = {"db": clamp_db(data["expo"]["db"]), "mute": bool(data["expo"]["mute"])}
            self.rooms = {str(k): {"db": clamp_db(v["db"]), "mute": bool(v["mute"])} for k, v in data.get("rooms", {}).items()}
            self.devices = {str(k): {"label": str(v.get("label", "")), "db": clamp_db(v["db"]), "mute": bool(v["mute"]),
                                     "free": bool(v.get("free", False)), "validated": bool(v.get("validated", True))}
                            for k, v in data.get("devices", {}).items() if NAME_RE.match(str(k))}
            self.presets = data.get("presets", {})
            self.current = data.get("current")
            self.modified = bool(data.get("modified", False))
        except (KeyError, TypeError, HubError):
            pass

    def save(self) -> None:
        data = {"expo": self.expo, "rooms": self.rooms, "devices": self.devices, "presets": self.presets,
                "current": self.current, "modified": self.modified}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def changed(self, modified: bool = True) -> None:
        """A appeler (verrou pris) apres tout changement de niveau : reveille les agents."""
        self.rev += 1
        if modified and self.current:
            self.modified = True
        self.save()
        self.cond.notify_all()

    # --- salles (Bastion) et hierarchie ------------------------------------------------------------

    def room_of(self, name: str) -> str | None:
        """Id de la salle de Bastion de cette machine, ou None : elle n'existe alors pas pour la console."""
        return self.rooms_source().room_of(name)

    def shown(self, name: str) -> bool:
        return name in self.devices and self.room_of(name) is not None

    def room_ids(self) -> list[str]:
        """Salles ou une machine validee est rangee, triees par nom."""
        rmap = self.rooms_source()
        ids = {self.room_of(n) for n, d in self.devices.items() if d["validated"]} - {None}
        return sorted(ids, key=lambda r: (rmap.room_name(r).lower(), r))

    def multi(self) -> bool:
        return len(self.room_ids()) > 1

    def _room(self, room_id: str) -> dict:
        return self.rooms.setdefault(room_id, {"db": 0.0, "mute": False})

    def effective(self, name: str) -> dict:
        d, room_id = self.devices[name], self.room_of(name)
        room = self._room(room_id) if (self.multi() and room_id is not None) else {"db": 0.0, "mute": False}
        muted = self.expo["mute"] or d["mute"] or room["mute"]
        db = d["db"] + room["db"] + (0.0 if d["free"] else self.expo["db"])
        return {"gain_db": round(max(MIN_DB, min(MAX_DB, db)), 1), "mute": muted,
                "managed": d["validated"] and room_id is not None}

    def online(self, name: str) -> bool:
        live = self.live.get(name)
        return bool(live) and self.clock() - live["last_seen"] < ONLINE_SECONDS

    # --- commandes de l'interface ------------------------------------------------------------------

    def set_expo(self, db=None, mute=None) -> None:
        with self.lock:
            if db is not None:
                self.expo["db"] = clamp_db(db)
            if mute is not None:
                self.expo["mute"] = bool(mute)
            self.changed()

    def set_room(self, room_id: str, db=None, mute=None) -> None:
        with self.lock:
            if room_id not in self.room_ids() or not self.multi():
                raise HubError("Salle inconnue.")
            room = self._room(room_id)
            if db is not None:
                room["db"] = clamp_db(db)
            if mute is not None:
                room["mute"] = bool(mute)
            self.changed()

    def set_device(self, name: str, db=None, mute=None, free=None, label=None) -> None:
        with self.lock:
            if name not in self.devices:
                raise HubError("Machine inconnue.")
            d = self.devices[name]
            if db is not None:
                d["db"] = clamp_db(db)
            if mute is not None:
                d["mute"] = bool(mute)
            if free is not None:
                d["free"] = bool(free)
            if label is not None:
                d["label"] = str(label).strip()[:40]
            self.changed(modified=db is not None or mute is not None or free is not None)

    def validate(self, name: str) -> None:
        with self.lock:
            if name not in self.devices:
                raise HubError("Machine inconnue.")
            self.devices[name]["validated"] = True
            self.changed(modified=False)

    def forget(self, name: str) -> None:
        with self.lock:
            if self.devices.pop(name, None) is None:
                raise HubError("Machine inconnue.")
            self.live.pop(name, None)
            self.changed(modified=False)

    def kill(self) -> None:
        self.set_expo(mute=True)

    # --- presets -------------------------------------------------------------------------------------

    def save_preset(self, name: str) -> None:
        name = (name or "").strip()
        if not PRESET_RE.match(name):
            raise HubError("Donnez un nom au preset (40 caracteres au plus).")
        with self.lock:
            self.presets[name] = {"expo": self.expo["db"],
                                  "rooms": {k: v["db"] for k, v in self.rooms.items()},
                                  "devices": {k: {"db": v["db"], "mute": v["mute"]} for k, v in self.devices.items() if v["validated"]}}
            self.current, self.modified = name, False
            self.save()

    def recall_preset(self, name: str) -> None:
        with self.lock:
            p = self.presets.get(name)
            if p is None:
                raise HubError("Preset inconnu.")
            self.expo = {"db": clamp_db(p["expo"]), "mute": False}
            for room, db in p.get("rooms", {}).items():
                self.rooms[room] = {"db": clamp_db(db), "mute": False}
            for dev, v in p.get("devices", {}).items():
                if dev in self.devices:
                    self.devices[dev]["db"], self.devices[dev]["mute"] = clamp_db(v["db"]), bool(v["mute"])
            self.current, self.modified = name, False
            self.changed(modified=False)

    def delete_preset(self, name: str) -> None:
        with self.lock:
            if self.presets.pop(name, None) is None:
                raise HubError("Preset inconnu.")
            if self.current == name:
                self.current, self.modified = None, False
            self.save()

    # --- ce que voient les agents -----------------------------------------------------------------------

    def _seen(self, name: str, body: dict) -> None:
        """Releve l'etat annonce par un agent (et enregistre une machine inconnue, « a valider »)."""
        if name not in self.devices:
            self.devices[name] = {"label": "", "db": -30.0, "mute": False, "free": False, "validated": False}
            self.save()
        self.live[name] = {"last_seen": self.clock(), "actual_db": body.get("actual_db"), "actual_mute": body.get("actual_mute"),
                           "sink": str(body.get("sink") or ""), "error": str(body.get("error") or "")[:120]}

    def agent_poll(self, name: str, body: dict, rev: int, timeout: float = POLL_SECONDS) -> dict:
        """Attente longue : rend l'etat voulu des que quelque chose change (ou apres `timeout`)."""
        if not NAME_RE.match(name or ""):
            raise HubError("Nom de machine invalide.")
        deadline = self.clock() + timeout
        with self.lock:
            self._seen(name, body)
            while self.rev == rev and name in self.devices:
                left = deadline - self.clock()
                if left <= 0:
                    break
                self.cond.wait(min(left, 1.0))
            self._seen(name, body)
            eff = self.effective(name) if name in self.devices else {"gain_db": MIN_DB, "mute": True, "managed": False}
            return {"rev": self.rev, **eff}

    def agent_report(self, body: dict) -> None:
        name = body.get("name", "")
        if not NAME_RE.match(name or ""):
            raise HubError("Nom de machine invalide.")
        with self.lock:
            self._seen(name, body)

    # --- vues pour l'interface ---------------------------------------------------------------------------

    def device_view(self, name: str) -> dict:
        d, live, rmap = self.devices[name], self.live.get(name, {}), self.rooms_source()
        room_id = self.room_of(name)
        eff = self.effective(name)
        return {"name": name, "label": d["label"] or name, "room": room_id, "room_name": rmap.room_name(room_id) if room_id else "",
                "db": d["db"], "mute": d["mute"], "free": d["free"], "validated": d["validated"], "online": self.online(name),
                "effective_db": eff["gain_db"], "effective_mute": eff["mute"],
                "actual_db": live.get("actual_db"), "actual_mute": live.get("actual_mute"), "sink": live.get("sink", ""),
                "error": live.get("error", ""), "seen_ago": None if "last_seen" not in live else round(self.clock() - live["last_seen"])}

    def state_view(self) -> dict:
        with self.lock:
            rmap = self.rooms_source()
            shown = [self.device_view(n) for n in sorted(self.devices) if self.shown(n)]     # sans salle dans Bastion : invisible
            multi = self.multi()
            rooms = [{"id": r, "name": rmap.room_name(r), "db": self._room(r)["db"], "mute": self._room(r)["mute"]}
                     for r in self.room_ids()] if multi else []
            return {"expo": dict(self.expo), "multi": multi, "rooms": rooms,
                    "devices": [d for d in shown if d["validated"]], "pending": [d for d in shown if not d["validated"]],
                    "presets": sorted(self.presets), "current": self.current, "modified": self.modified,
                    "min_db": MIN_DB, "max_db": MAX_DB}

    def operator_view(self) -> dict:
        """Ce que voit l'exploitant : en pourcentage, sans le detail des machines."""
        with self.lock:
            full = self.state_view()
            online = sum(1 for d in full["devices"] if d["online"])
            return {"expo": {"pct": db_to_pct(self.expo["db"]), "mute": self.expo["mute"]}, "multi": full["multi"],
                    "rooms": [{"id": r["id"], "name": r["name"], "pct": db_to_pct(r["db"]), "mute": r["mute"]} for r in full["rooms"]],
                    "presets": full["presets"], "current": full["current"], "modified": full["modified"],
                    "online": online, "total": len(full["devices"])}
