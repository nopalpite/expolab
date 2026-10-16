"""Logique des prises sans reseau ni Flask : lecture des attributs Matter d'une prise,
historique de consommation (SQLite), liste des modules de l'expo. Teste sans controleur.

Attributs dans le format de python-matter-server : node["attributes"]["endpoint/cluster/attribut"].
"""
import datetime
import sqlite3
import time
from pathlib import Path

import yaml

ONOFF, IDENTIFY = 6, 3
POWER_CLUSTER, POWER_ATTR = 0x0090, 8          # Electrical Power Measurement : ActivePower (mW)
ENERGY_CLUSTER, ENERGY_ATTR = 0x0091, 1        # Electrical Energy Measurement : CumulativeEnergyImported
LEGACY_CLUSTER, LEGACY_ATTR = 0x0B04, 0x050B   # ancien Electrical Measurement : ActivePower (W)


def find(attributes: dict, cluster: int, attribute: int):
    """(endpoint, valeur) du premier endpoint qui expose cluster/attribut."""
    for key, value in attributes.items():
        parts = key.split("/")
        if len(parts) == 3 and parts[1] == str(cluster) and parts[2] == str(attribute):
            return int(parts[0]), value
    return None, None


def read_node(node: dict) -> dict:
    """Ce que la page affiche d'une prise."""
    attrs = node.get("attributes") or {}
    ep, on = find(attrs, ONOFF, 0)
    _ep, power_mw = find(attrs, POWER_CLUSTER, POWER_ATTR)
    _ep, energy = find(attrs, ENERGY_CLUSTER, ENERGY_ATTR)
    _ep, legacy_w = find(attrs, LEGACY_CLUSTER, LEGACY_ATTR)
    if isinstance(energy, dict):
        energy = energy.get("energy", energy.get("0"))
    power = round(power_mw / 1000, 1) if isinstance(power_mw, (int, float)) else (
        float(legacy_w) if isinstance(legacy_w, (int, float)) else None)
    return {
        "node_id": node.get("node_id"),
        "available": bool(node.get("available")),
        "vendor": attrs.get("0/40/1") or "",
        "product": attrs.get("0/40/3") or "",
        "version": attrs.get("0/40/10") or "",
        "endpoint": ep if ep is not None else 1,
        "can_switch": ep is not None,
        "on": bool(on) if on is not None else None,
        "power_w": power,
        "energy_wh": round(energy / 1000, 1) if isinstance(energy, (int, float)) else None,
        "measures": power is not None,
    }


def normalize_code(code: str) -> str | None:
    """Code d'appairage Matter : texte de QR code "MT:..." ou code manuel a 11 (ou 21) chiffres."""
    code = (code or "").strip()
    if code.upper().startswith("MT:") and len(code) > 4 and all(c.isalnum() or c in ".-:" for c in code):
        return "MT:" + code[3:].upper()
    digits = "".join(c for c in code if c.isdigit())
    if len(digits) in (11, 21) and all(c.isdigit() or c in " -" for c in code):
        return digits
    return None


# --- modules de l'expo (a qui appartient chaque prise) -------------------------------------

def load_modules(reservations: Path, bastion_config: Path) -> dict:
    """{nom: ip} : reservations DHCP par nom, puis machines de Bastion (nom court, adresse IP)."""
    modules = {}
    try:
        for line in reservations.read_text().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] and not line.lstrip().startswith("#") and ":" not in parts[0]:
                modules[parts[0]] = parts[1]
    except OSError:
        pass
    try:
        data = yaml.safe_load(bastion_config.read_text()) or {}
        for m in data.get("machines") or []:
            if isinstance(m, dict) and m.get("name"):
                name = str(m["name"]).split(".")[0].lower()
                host = str(m.get("host") or "")
                modules.setdefault(name, host if host.replace(".", "").isdigit() else "")
    except (OSError, yaml.YAMLError):
        pass
    return dict(sorted(modules.items()))


# --- historique (une mesure par minute et par prise, 48 h gardees) ---------------------------

class History:
    def __init__(self, path: Path):
        self.path = path
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS samples (ts INTEGER, node INTEGER, power REAL, energy REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS samples_node_ts ON samples (node, ts)")

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, plugs: list[dict], now: float | None = None) -> None:
        now = int(time.time() if now is None else now)
        rows = [(now, p["node_id"], p["power_w"], p["energy_wh"]) for p in plugs if p["available"] and p["measures"]]
        with self._db() as db:
            db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?)", rows)
            db.execute("DELETE FROM samples WHERE ts < ?", (now - 48 * 3600,))

    def series(self, node: int, now: float | None = None, hours: int = 24, buckets: int = 48) -> list:
        """Puissance moyenne par tranche (None = pas de mesure), des plus anciennes aux recentes."""
        now = time.time() if now is None else now
        start, width = now - hours * 3600, hours * 3600 / buckets
        with self._db() as db:
            rows = db.execute("SELECT ts, power FROM samples WHERE node = ? AND ts >= ? AND power IS NOT NULL",
                              (node, int(start))).fetchall()
        sums, counts = [0.0] * buckets, [0] * buckets
        for ts, power in rows:
            i = min(buckets - 1, int((ts - start) // width))
            sums[i] += power
            counts[i] += 1
        return [round(s / c, 1) if c else None for s, c in zip(sums, counts)]

    def energy_today(self, node: int, current_wh: float | None, now: float | None = None) -> float | None:
        """Wh consommes depuis minuit (heure locale), d'apres le compteur cumule de la prise."""
        if current_wh is None:
            return None
        now = time.time() if now is None else now
        midnight = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._db() as db:
            row = db.execute("SELECT energy FROM samples WHERE node = ? AND ts >= ? AND energy IS NOT NULL ORDER BY ts LIMIT 1",
                             (node, int(midnight))).fetchone()
        return round(max(0.0, current_wh - row[0]), 1) if row else 0.0
