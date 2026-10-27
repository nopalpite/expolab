"""Ce qui est attendu d'une machine, juge par le SERVEUR a partir de ses tags Bastion.

La machine ne publie que des faits (node_exporter + sonde du role `monitoring`) ; c'est ici qu'on decide :
  - toute machine : joignable, sonde a jour, temperature, disque, memoire, alimentation ;
  - tag `kiosk` : kiosk.service et kiosk-vnc.service actifs, au moins un ecran branche ;
  - tag `audio` : au moins un appareil USB de classe audio (01) ;
  - si la sonde publie un /health d'application : application qui repond ;
  - si la sonde publie le programme du kiosque (pose par ansible-web) : programme en marche, sans relances en boucle.
Deux niveaux : ALERTE (l'expo est touchee) et AVERTISSEMENT (a surveiller). Les seuils sont des reglages (SETTINGS).
"""
from dataclasses import dataclass

from scrape import Samples

ALERT, WARNING = "alerte", "avertissement"
SEVERITY = {None: 0, WARNING: 1, ALERT: 2}
KIOSK_UNITS = ("kiosk.service", "kiosk-vnc.service")
PROBE_STALE_SECONDS = 120          # la sonde passe toutes les 15 s

# Reglages modifiables dans la page : {cle: (defaut, minimum, maximum, libelle)}.
SETTINGS = {
    "temp_warn": (75, 40, 110, "Température : avertissement (°C)"),
    "temp_alert": (85, 40, 110, "Température : alerte (°C)"),
    "disk_warn": (85, 10, 100, "Disque utilisé : avertissement (%)"),
    "disk_alert": (95, 10, 100, "Disque utilisé : alerte (%)"),
    "mem_warn": (90, 10, 100, "Mémoire utilisée : avertissement (%)"),
    "mem_alert": (95, 10, 100, "Mémoire utilisée : alerte (%)"),
    "unreachable_minutes": (2, 0, 120, "Injoignable depuis (min)"),
    "service_minutes": (1, 0, 120, "Service arrêté depuis (min)"),
    "startup_grace_minutes": (5, 0, 60, "Démarrage : surveiller après (min)"),
    "retention_days": (90, 1, 3650, "Conservation de l'historique (jours)"),
    "graph_days": (7, 1, 90, "Conservation des courbes (jours)"),
}
DEFAULTS = {key: spec[0] for key, spec in SETTINGS.items()}
PAIRS = (("temp_warn", "temp_alert", "température"), ("disk_warn", "disk_alert", "disque"), ("mem_warn", "mem_alert", "mémoire"))
CONFIRM_SECONDS = 60       # les autres problemes : signales apres 1 min
RESTART_WINDOW = 600       # programme qui plante en boucle : RESTART_LOOP relances de kiosk.service en 10 min
RESTART_LOOP = 3


def confirm_seconds(check_id: str, settings: dict) -> int:
    """Duree pendant laquelle un probleme doit durer avant d'etre signale."""
    if check_id == "reachable":
        return 60 * settings["unreachable_minutes"]
    if check_id.startswith("unit:") or check_id == "program":
        return 60 * settings["service_minutes"]
    return CONFIRM_SECONDS


class SettingsError(ValueError):
    pass


def validate(changes: dict, current: dict) -> dict:
    """Reglages fusionnes et verifies (entiers dans leurs bornes, seuils d'alerte au-dela des avertissements)."""
    merged = dict(current)
    for key, raw in (changes or {}).items():
        if key not in SETTINGS:
            raise SettingsError(f"Réglage inconnu : {key}")
        _, low, high, label = SETTINGS[key]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise SettingsError(f"{label} : nombre entier attendu") from None
        if isinstance(raw, bool) or not low <= value <= high:
            raise SettingsError(f"{label} : entre {low} et {high}")
        merged[key] = value
    for warn, alert, what in PAIRS:
        if merged[alert] < merged[warn]:
            raise SettingsError(f"Le seuil d'alerte ({what}) doit être au moins celui d'avertissement.")
    return merged


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    level: str | None          # None : tout va bien
    detail: str = ""


def _ok(cid, label, detail=""):
    return Check(cid, label, None, detail)


def evaluate(tags, samples: Samples | None, settings: dict, restarts: int = 0) -> list[Check]:
    """`restarts` : relances de kiosk.service sur les RESTART_WINDOW dernieres secondes (suivies par le superviseur)."""
    if samples is None:
        return [Check("reachable", "Joignable", ALERT, "ne répond pas (éteinte, hors réseau ou node_exporter arrêté)")]
    checks = [_ok("reachable", "Joignable")]
    checks.append(_probe(samples))
    checks += [c for c in (_temperature(samples, settings), _disk(samples, settings), _memory(samples, settings)) if c]
    checks.append(_power(samples))
    if "kiosk" in tags:
        checks += [_unit(samples, unit) for unit in KIOSK_UNITS]
        checks.append(_display(samples))
    if "audio" in tags:
        checks.append(_audio_usb(samples))
    if samples.value("pop_inov_app_up") is not None:
        checks.append(_app(samples))
    program = _program(samples, restarts)
    if program:
        checks.append(program)
    return checks


# --- mesures (aussi enregistrees pour les courbes) ---------------------------------------------------

def temperature(s: Samples):
    temps = [v for _, v in s.all("node_thermal_zone_temp") + s.all("node_hwmon_temp_celsius") if 0 < v < 150]
    return max(temps) if temps else None


def disk_used(s: Samples):
    # "/" seulement : avec la racine en overlay (tag overlay_on), node_exporter ne la publie pas et rien n'est verifie.
    size, avail = s.value("node_filesystem_size_bytes", mountpoint="/"), s.value("node_filesystem_avail_bytes", mountpoint="/")
    return 100 - 100 * avail / size if size and avail is not None else None


def memory_used(s: Samples):
    total, avail = s.value("node_memory_MemTotal_bytes"), s.value("node_memory_MemAvailable_bytes")
    return 100 - 100 * avail / total if total and avail is not None else None


def cpu_seconds(s: Samples):
    """(total, inactif) en secondes CPU cumulees : le % d'occupation se calcule entre deux passages."""
    series = s.all("node_cpu_seconds_total")
    if not series:
        return None
    return sum(v for _, v in series), sum(v for labels, v in series if labels.get("mode") == "idle")


def program_info(s: Samples) -> dict | None:
    """Programme du kiosque d'apres la sonde : {name, version, running, started}, ou None."""
    info = s.all("pop_inov_app_info")
    if not info:
        return None
    labels = info[0][0]
    return {"name": labels.get("name", ""), "version": labels.get("version", ""),
            "running": s.value("pop_inov_app_running") == 1, "started": s.value("pop_inov_app_start_time_seconds")}


def _probe(s):
    stamp, now = s.value("pop_inov_probe_timestamp_seconds"), s.value("node_time_seconds")
    if stamp is None:
        return Check("probe", "Sonde", WARNING, "absente : relancer le rôle monitoring")
    age = (now - stamp) if now is not None else 0       # heure de la machine : insensible a un decalage d'horloge
    if age > PROBE_STALE_SECONDS:
        return Check("probe", "Sonde", WARNING, f"arrêtée depuis {int(age // 60)} min")
    return _ok("probe", "Sonde")


def _temperature(s, cfg):
    t = temperature(s)
    if t is None:
        return None
    level = ALERT if t >= cfg["temp_alert"] else WARNING if t >= cfg["temp_warn"] else None
    return Check("temperature", "Température", level, f"{t:.0f} °C")


def _disk(s, cfg):
    pct = disk_used(s)
    return None if pct is None else _used("disk", "Disque", pct, cfg["disk_warn"], cfg["disk_alert"])


def _memory(s, cfg):
    pct = memory_used(s)
    return None if pct is None else _used("memory", "Mémoire", pct, cfg["mem_warn"], cfg["mem_alert"])


def _used(cid, label, pct, warn, alert):
    level = ALERT if pct >= alert else WARNING if pct >= warn else None
    return Check(cid, label, level, f"{pct:.0f} % utilisé")


def _power(s):
    if s.value("pop_inov_undervoltage") == 1:
        return Check("power", "Alimentation", WARNING, "sous-tension : changer l'alimentation ou le câble")
    if s.value("pop_inov_throttled") == 1:
        return Check("power", "Alimentation", WARNING, "processeur bridé (chaleur ou alimentation)")
    return _ok("power", "Alimentation")


def _unit(s, unit):
    states = {labels.get("state"): v for labels, v in s.all("node_systemd_unit_state") if labels.get("name") == unit}
    label = f"Service {unit.removesuffix('.service')}"
    if not states:
        return Check(f"unit:{unit}", label, ALERT, "inconnu (rôle monitoring à relancer ?)")
    if states.get("active") == 1:
        return _ok(f"unit:{unit}", label, "actif")
    current = next((state for state, v in states.items() if v == 1), "inconnu")
    return Check(f"unit:{unit}", label, ALERT, current)


def _display(s):
    connected = sorted(labels.get("connector", "") for labels, v in s.all("pop_inov_display_connected") if v == 1)
    if connected:
        return _ok("display", "Écran", ", ".join(c.split("-", 1)[-1] for c in connected))
    return Check("display", "Écran", ALERT, "aucun écran branché")


def _audio_usb(s):
    cards = [labels.get("name") or labels.get("id", "") for labels, v in s.all("pop_inov_usb_device")
             if v == 1 and "01" in labels.get("classes", "").split(",")]
    if cards:
        return _ok("audio_usb", "Carte son USB", ", ".join(cards))
    return Check("audio_usb", "Carte son USB", ALERT, "aucune carte son USB branchée")


def _app(s):
    if s.value("pop_inov_app_up") == 1:
        seconds = s.value("pop_inov_app_response_seconds")
        return _ok("app", "Application", f"{seconds * 1000:.0f} ms" if seconds is not None else "")
    return Check("app", "Application", ALERT, "ne répond pas")


def _program(s, restarts):
    info = program_info(s)
    if info is None:
        return None
    states = {labels.get("state"): v for labels, v in s.all("node_systemd_unit_state") if labels.get("name") == "kiosk.service"}
    if states and states.get("active") != 1:
        return None        # kiosque arrete : deja signale par son service
    what = " ".join(filter(None, (info["name"], info["version"])))
    if restarts >= RESTART_LOOP:
        return Check("program", "Programme", ALERT, f"{what} plante en boucle ({restarts} relances en {RESTART_WINDOW // 60} min)")
    if not info["running"]:
        return Check("program", "Programme", ALERT, f"{what} ne tourne pas")
    return _ok("program", "Programme", what)
