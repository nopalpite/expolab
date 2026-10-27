"""Coeur de la supervision : un passage (tick) toutes les 30 s sur les machines taguees `monitoring` dans Bastion.

Etats qui suspendent la surveillance, sans jamais rien signaler :
  - exposition FERMEE : aucune machine interrogee ; les incidents en cours sont clos (raison « exposition fermée »).
    A l'ouverture, un delai de demarrage (5 min par defaut) laisse aux machines le temps de s'allumer.
    Bascule a la main dans la page, ou par la future gestion d'allumage de l'expo (POST /api/expo en local) ;
  - machine EN MAINTENANCE : eteinte volontairement, hors surveillance ; a la sortie, meme delai de demarrage.
Un probleme n'est signale (incident ouvert dans l'historique + evenement « opened ») qu'apres avoir dure
`confirm_seconds` ; pendant le delai de demarrage il est suivi mais pas signale. Les evenements renvoyes par tick()
sont ceux qu'on notifie (« opened », « raised » quand un avertissement devient une alerte, « resolved »).

A chaque nouvelle minute, le passage enregistre aussi un point par machine joignable pour les courbes (temperature,
processeur, memoire, disque) ; le processeur se calcule entre deux passages. Le programme du kiosque vu par la sonde est
rapproche de celui prevu dans ansible-web (programme.py) pour la page.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import checks as C
import programme as P

PURGE_EVERY = 3600


@dataclass(frozen=True)
class Event:
    kind: str           # opened | raised | resolved
    machine: str
    label: str
    level: str
    detail: str
    at: float
    incident_id: int


class Supervisor:
    def __init__(self, history, machines_source, fetch, clock=time.time, workers=8, planned=None):
        self.history, self.machines_source, self.fetch, self.clock = history, machines_source, fetch, clock
        self.planned = planned or (lambda: {})
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.RLock()
        self._checks = {}           # {machine: [Check]} du dernier passage
        self._polled = {}           # {machine: heure du dernier passage}
        self._failing = {}          # {(machine, check_id): depuis quand le probleme dure}
        self._resumed = {}          # {machine: sortie de maintenance}
        self._cpu = {}              # {machine: (secondes CPU totales, inactives)} du passage precedent
        self._restarts = {}         # {machine: [(heure, relances de kiosk.service)]} sur C.RESTART_WINDOW
        self._program = {}          # {machine: programme vu par la sonde}
        self._point_minute = None   # derniere minute enregistree pour les courbes
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
            now, source = self.clock(), str(source)[:60]
            self.history.put("expo", {"open": bool(is_open), "since": now, "source": source})
            if is_open:
                grace = self.settings()["startup_grace_minutes"]
                self.history.journal("", "expo", "Exposition ouverte", f"{source} · démarrage {grace} min", now)
                return
            closed = self.history.open_incidents()
            for incident in closed:
                self.history.close_incident(incident["id"], now, "exposition fermée")
            self._checks, self._polled, self._failing, self._program, self._restarts = {}, {}, {}, {}, {}
            detail = f"{source} · {len(closed)} problème(s) clos sans envoi" if closed else source
            self.history.journal("", "expo", "Exposition fermée", detail, now)

    def set_maintenance(self, machine: str, on: bool, note=""):
        with self._lock:
            now, note = self.clock(), str(note or "").strip()[:200]
            if on:
                self.history.set_maintenance(machine, note, now)
                self._forget(machine, now, "maintenance")
                self.history.journal(machine, "maintenance", "En maintenance", note, now)
            elif self.history.end_maintenance(machine):
                self._resumed[machine] = now
                self.history.journal(machine, "maintenance", "Fin de maintenance", "", now)

    def _forget(self, machine, now, reason):
        for incident in self.history.open_incidents():
            if incident["machine"] == machine:
                self.history.close_incident(incident["id"], now, reason)
        for state in (self._checks, self._polled, self._cpu, self._restarts, self._program):
            state.pop(machine, None)
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
            events, points = [], {}
            for machine, samples in zip(watched, results):
                if machine.name in maintenance:     # mise en maintenance pendant les requetes
                    continue
                restarts = 0
                if samples is None:
                    self._cpu.pop(machine.name, None)
                    self._program.pop(machine.name, None)
                else:
                    points[machine.name] = self._measure(machine.name, samples)
                    restarts = self._count_restarts(machine.name, samples, now)
                    self._program[machine.name] = C.program_info(samples)
                events += self._judge(machine, C.evaluate(machine.tags, samples, settings, restarts), now, settings)
            minute = int(now // 60) * 60
            if points and minute != self._point_minute:
                self.history.add_points(minute, points)
                self._point_minute = minute
            if now - self._purged >= PURGE_EVERY:
                self.history.purge(now - 86400 * settings["retention_days"])
                self.history.purge_points(now - 86400 * settings["graph_days"])
                self._purged = now
            return events

    def _measure(self, name, samples) -> dict:
        cpu, previous = None, self._cpu.get(name)
        current = self._cpu[name] = C.cpu_seconds(samples)
        if current and previous and current[0] > previous[0]:
            cpu = max(0.0, min(100.0, 100 * (1 - (current[1] - previous[1]) / (current[0] - previous[0]))))
        return {"temp": C.temperature(samples), "cpu": cpu, "mem": C.memory_used(samples), "disk": C.disk_used(samples)}

    def _count_restarts(self, name, samples, now) -> int:
        """Relances de kiosk.service sur les C.RESTART_WINDOW dernieres secondes (compteur remis a zero au demarrage)."""
        total = samples.value("pop_inov_kiosk_restarts")
        if total is None:
            self._restarts.pop(name, None)
            return 0
        seen = [(t, n) for t, n in self._restarts.get(name, []) if now - t <= C.RESTART_WINDOW] + [(now, total)]
        self._restarts[name] = seen
        return int(sum(b - a if b >= a else b for (_, a), (_, b) in zip(seen, seen[1:])))

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
                    events.append(Event("resolved", name, check.label, incident["level"], check.detail, now, incident["id"]))
                continue
            since = self._failing.setdefault(key, now)
            if incident:
                if C.SEVERITY[check.level] > C.SEVERITY[incident["level"]]:
                    self.history.raise_level(incident["id"], check.level, check.detail)
                    events.append(Event("raised", name, check.label, check.level, check.detail, now, incident["id"]))
            elif not in_grace and now - since >= C.confirm_seconds(check.id, settings):
                incident_id = self.history.open_incident(name, check, now)
                events.append(Event("opened", name, check.label, check.level, check.detail, now, incident_id))
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
            maintenance, planned = self.history.maintenance(), self.planned()
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
                    "program": self._program_view(m.name, planned.get(m.name)),
                    "checks": [{"id": c.id, "label": c.label, "level": c.level, "detail": c.detail,
                                "failing_since": self._failing.get((m.name, c.id))} for c in self._checks.get(m.name, [])],
                })
            return {"now": now, "expo": expo, "settings": settings, "machines": machines,
                    "bastion_error": getattr(self.machines_source, "error", ""),
                    "planned_error": getattr(self.planned, "error", "")}

    def _program_view(self, name, planned) -> dict | None:
        if name not in self._polled:          # pas encore vu (ou expo fermee, maintenance) : seulement le prevu
            return {"installed": None, "planned": planned, "status": "", "restarts": None} if planned else None
        installed = self._program.get(name)
        status = P.compare(planned, installed)
        if not status:
            return None
        restarts = self._restarts.get(name, [])
        return {"installed": installed, "planned": planned, "status": status,
                "restarts": int(restarts[-1][1]) if restarts else None}
