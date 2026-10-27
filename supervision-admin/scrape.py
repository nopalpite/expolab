"""Lecture du /metrics de node_exporter (port 9100) d'une machine de l'expo.

Seul le format texte de Prometheus est lu, sans bibliotheque : une ligne par serie,
`nom{etiquette="valeur",...} nombre`. node_exporter range les etiquettes dans l'ordre
alphabetique : on les lit dans un dictionnaire, jamais par position.
"""
import math
import re
import urllib.request

PORT = 9100
TIMEOUT = 5.0
MAX_BYTES = 4 * 1024 * 1024        # une machine en publie ~75 Ko : au-dela, ce n'est pas node_exporter

LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
UNESCAPE = {"\\": "\\", '"': '"', "n": "\n"}


class Samples:
    """{nom de metrique: [(etiquettes, valeur)]}."""

    def __init__(self, series: dict | None = None):
        self.series = series or {}

    def all(self, name: str) -> list[tuple[dict, float]]:
        return self.series.get(name, [])

    def value(self, name: str, default=None, **labels):
        """Valeur de la premiere serie dont les etiquettes contiennent `labels`."""
        for got, value in self.all(name):
            if all(got.get(k) == v for k, v in labels.items()):
                return value
        return default


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: UNESCAPE.get(m.group(1), m.group(0)), text)


def parse(text: str) -> Samples:
    series: dict = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        if math.isnan(value):
            continue
        labels = {k: _unescape(v) for k, v in LABEL.findall(m.group(2) or "")}
        series.setdefault(m.group(1), []).append((labels, value))
    return Samples(series)


def fetch(host: str, timeout: float = TIMEOUT) -> Samples | None:
    """Metriques de la machine, ou None si elle ne repond pas (eteinte, node_exporter arrete, hors reseau)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{PORT}/metrics", timeout=timeout) as resp:
            return parse(resp.read(MAX_BYTES).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
