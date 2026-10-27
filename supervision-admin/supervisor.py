"""Coeur de la supervision : un passage (tick) toutes les 30 s sur les machines taguees `monitoring` dans Bastion.

Etats qui suspendent la surveillance, sans jamais rien signaler :
  - exposition FERMEE : aucune machine interrogee ; les incidents en cours sont clos (raison « exposition fermée »).
    A l'ouverture, un delai de demarrage (5 min par defaut) laisse aux machines le temps de s'allumer.
    Bascule a la main dans la page, ou par la future gestion d'allumage de l'expo (POST /api/expo en local) ;
  - machine EN MAINTENANCE : eteinte volontairement, hors surveillance ; a la sortie, meme delai de demarrage.
Un probleme n'est signale (incident ouvert dans l'historique + evenement « opened ») qu'apres avoir dure
`confirm_seconds` ; pendant le delai de demarrage il est suivi mais pas signale. Les evenements renvoyes par tick()
sont ceux qu'on notifie (« opened », « raised » quand un avertissement devient une alerte, « resolved »).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import checks as C

PURGE_EVERY = 3600


@dataclass(frozen=True)
class Event:
    kind: str           # opened | raised | resolved
    machine: str
    label: str
    level: str
    detail: str
    at: float


class Supervisor:
    def __init__(self, history, machines_source, fetch, clock=time.time, workers=8):
        self.history, self.machines_source, self.fetch, self.clock = history, machines_source, fetch, clock
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.RLock()
        self._checks = {}           # {machine: [Check]} du dernier passage
        self._polled = {}           # {machine: heure du dernier passage}
        self._failing = {}          # {(machine, check_id): depuis quand le probleme dure}
        self._resumed = {}          # {machine: sortie de maintenance}
        self._purged = -PURGE_EVERY
        if self.history.get("expo") is None:
            self.history.put("expo", {"open": True, "since": self.clock(), "source": "premier démarrage"})

    # --- reglages et etats ----------------------------------------------------------------------

    def settings(self) -> dict:
        return C.validate({}, {**C.DEFAULTS, **(self.history.get("settings") or {})})

    def update_settings(self, changes: dict) -> dict:
        with self._lock:
            merged = C.validate(changes, self.settings())
            self.history.put("settings", merged)
            return merged

    def expo(self) -> dict:
        return self.history.get("expo")

    def set_expo(self, is_open: bool, source="manuel"):
        with self._lock:
            if bool(is_open) == self.expo()["open"]:
                return
            now = self.clock()
            self.history.put("expo", {"open": bool(is_open), "since": now, "source": str(source)[:60]})
            if not is_open:
                for incident in self.history.open_incidents():
                    self.history.close_incident(incident["id"], now, "exposition fermée")
                self._checks, self._polled, self._failing = {}, {}, {}

    def set_maintenance(self, machine: str, on: bool, note=""):
        with self._lock:
            now = self.clock()
            if on:
                self.history.set_maintenance(machine, str(note or "").strip()[:200], now)
                self._forget(machine, now, "maintenance")
            elif self.history.end_maintenance(machine):
                self._resumed[machine] = now

    def _forget(self, machine, now, reason):
        for incident in self.history.open_incidents():
            if incident["machine"] == machine:
                self.history.close_incident(incident["id"], now, reason)
        self._checks.pop(machine, None)
        self._polled.pop(machine, None)
        self._failing = {k: v for k, v in self._failing.items() if k[0] != machine}

    def grace_until(self, machine: str, settings=None) -> float:
        settings = settings or self.settings()
        start = max(self.expo()["since"], self._resumed.get(machine, 0))
        return start + 60 * settings["startup_grace_minutes"]

    # --- passage --------------------------------------------------------------------------------

    def tick(self) -> list[Event]:
        if not self.expo()["open"]:
            return []
        machines = self.machines_source()
        maintenance = self.history.maintenance()
        watched = [m for m in machines if m.name not in maintenance]
        results = list(self.pool.map(lambda m: self.fetch(m.host), watched))
        with self._lock:
            if not self.expo()["open"]:            # fermee pendant les requetes
                return []
            now, settings = self.clock(), self.settings()
            known, maintenance = {m.name for m in watched}, self.history.maintenance()
            for name in list(self._checks) + [i["machine"] for i in self.history.open_incidents()]:
                if name not in known and name not in maintenance:
                    self._forget(name, now, "retirée de la supervision")
            events = []
            for machine, samples in zip(watched, results):
                if machine.name in maintenance:     # mise en maintenance pendant les requetes
                    continue
                events += self._judge(machine, C.evaluate(machine.tags, samples, settings), now, settings)
            if now - self._purged >= PURGE_EVERY:
                self.history.purge(now - 86400 * settings["retention_days"])
                self._purged = now
            return events

    def _judge(self, machine, results, now, settings) -> list[Event]:
        name, events = machine.name, []
        self._checks[name], self._polled[name] = results, now
        opened = {i["check_id"]: i for i in self.history.open_incidents() if i["machine"] == name}
        in_grace = now < self.grace_until(name, settings)
        seen = set()
        for check in results:
            seen.add(check.id)
            key, incident = (name, check.id), opened.get(check.id)
            if check.level is None:
                self._failing.pop(key, None)
                if incident:
                    self.history.close_incident(incident["id"], now, "rétabli")
                    events.append(Event("resolved", name, check.label, incident["level"], check.detail, now))
                continue
            since = self._failing.setdefault(key, now)
            if incident:
                if C.SEVERITY[check.level] > C.SEVERITY[incident["level"]]:
                    self.history.raise_level(incident["id"], check.level, check.detail)
                    events.append(Event("raised", name, check.label, check.level, check.detail, now))
            elif not in_grace and now - since >= settings["confirm_seconds"]:
                self.history.open_incident(name, check, now)
                events.append(Event("opened", name, check.label, check.level, check.detail, now))
        for check_id, incident in opened.items():
            if check_id not in seen:       # plus verifie (tag retire) ou masque par « injoignable »
                reason = "machine injoignable" if results[0].level else "plus vérifié"
                self.history.close_incident(incident["id"], now, reason)
                self._failing.pop((name, check_id), None)
        return events

    # --- vue pour la page -----------------------------------------------------------------------

    def view(self) -> dict:
        with self._lock:
            now, settings, expo = self.clock(), self.settings(), self.expo()
            maintenance = self.history.maintenance()
            opened = {}
            for incident in self.history.open_incidents():
                opened.setdefault(incident["machine"], []).append(incident)
            machines = []
            for m in self.machines_source():
                incidents = opened.get(m.name, [])
                grace = self.grace_until(m.name, settings)
                if m.name in maintenance:
                    status = "maintenance"
                elif not expo["open"]:
                    status = "fermée"
                elif incidents:
                    status = max((i["level"] for i in incidents), key=C.SEVERITY.get)
                elif m.name not in self._polled:
                    status = "en attente"
                elif now < grace:
                    status = "démarrage"
                else:
                    status = "ok"
                machines.append({
                    "name": m.name, "room": m.room, "tags": sorted(m.tags - {"monitoring"}), "status": status,
                    "maintenance": maintenance.get(m.name), "polled_at": self._polled.get(m.name),
                    "grace_until": grace if now < grace else None,
                    "checks": [{"id": c.id, "label": c.label, "level": c.level, "detail": c.detail,
                                "failing_since": self._failing.get((m.name, c.id))} for c in self._checks.get(m.name, [])],
                })
            return {"now": now, "expo": expo, "settings": settings, "machines": machines,
                    "bastion_error": getattr(self.machines_source, "error", "")}
