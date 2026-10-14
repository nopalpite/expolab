#!/usr/bin/env python3
"""expolab ntp-admin : etat et reglages du serveur de temps (chrony, stack ntp).

Aucune base de donnees : tout vient de fichiers et de sockets montes dans le conteneur.
  /config           (lecture-ecriture) stacks/ntp/config : mode, serveurs amont, reseaux
                    autorises, repli sur l'horloge locale. Relus par le conteneur ntp a son
                    demarrage : on les modifie puis on le redemarre.
  /run/chrony       socket de commande de chronyd, partagee avec le conteneur ntp : `chronyc`
                    en lit l'etat (synchronisation, sources, machines clientes).
  /leases           (lecture seule) baux dnsmasq, pour afficher le NOM des machines.
  docker.sock       uniquement pour redemarrer le conteneur ntp (API Docker, sans client docker).
"""
import csv
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CONFIG_DIR = Path(os.environ.get("NTP_CONFIG_DIR", "/config"))
CHRONY_SOCKET = os.environ.get("NTP_CHRONY_SOCKET", "/run/chrony/chronyd.sock")
LEASES_FILE = Path(os.environ.get("NTP_LEASES_FILE", "/leases/dnsmasq.leases"))
DOCKER_SOCKET = os.environ.get("NTP_DOCKER_SOCKET", "/var/run/docker.sock")
CONTAINER = os.environ.get("NTP_CONTAINER", "expolab-ntp")

MODES = ("online", "offline")
MAX_UPSTREAM = 10
MAX_ALLOW = 10
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
DEFAULT_UPSTREAM = ["0.pool.ntp.org", "1.pool.ntp.org", "2.pool.ntp.org", "3.pool.ntp.org"]
DEFAULT_ALLOW = ["10.42.0.0/24"]
LOCAL_REF_ID = "7F7F0101"   # horloge locale (directive `local`) : le serveur ne s'appuie sur rien d'externe
SOURCE_STATES = {
    "*": "selected",      # source retenue
    "+": "combined",      # utilisee en complement
    "-": "excluded",      # exclue de la combinaison
    "?": "unreachable",   # injoignable ou inutilisable
    "x": "falseticker",   # donne une heure fausse
    "~": "unstable",      # trop variable
}


# --- chronyc ---------------------------------------------------------------------

def run_chronyc(*args: str, csv_output: bool = True, timeout: int = 6) -> tuple[bool, str]:
    """(ok, sortie). -n : adresses numeriques (pas de DNS pendant qu'on regarde l'etat)."""
    cmd = ["chronyc", "-h", CHRONY_SOCKET, "-n"] + (["-c"] if csv_output else []) + list(args)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stdout + done.stderr).strip() or "chronyc a echoue"
    return True, done.stdout


def _rows(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text)) if row]


def _float(value: str, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_tracking(text: str) -> dict | None:
    """`chronyc -c tracking` : ref-id, nom, strate, heure de reference, ecart systeme, dernier
    ecart, ecart quadratique, frequence (ppm), freq. residuelle, inclinaison, delai racine,
    dispersion racine, intervalle de mise a jour, etat de seconde intercalaire."""
    rows = _rows(text)
    if not rows or len(rows[0]) < 14:
        return None
    r = rows[0]
    ref_id, stratum = r[0].upper(), int(_float(r[2], 0) or 0)
    leap = r[13]
    if ref_id == LOCAL_REF_ID:
        state = "local"       # sert sa propre horloge : rien de synchronise en amont
    elif stratum == 0 or ref_id in ("", "00000000") or leap.lower().startswith("not"):
        state = "none"
    else:
        state = "synced"
    ref_time = _float(r[3], 0.0) or 0.0
    return {
        "state": state,
        "ref_id": ref_id,
        "ref_name": r[1] if r[1] and r[1] != "." else "",
        "stratum": stratum,
        "ref_time": int(ref_time) if ref_time > 0 else None,        # derniere mise a l'heure (epoch)
        "offset_ms": round(abs(_float(r[4], 0.0) or 0.0) * 1000, 3),  # ecart de l'horloge du serveur a l'heure de reference
        "last_offset_ms": round((_float(r[5], 0.0) or 0.0) * 1000, 3),
        "rms_offset_ms": round((_float(r[6], 0.0) or 0.0) * 1000, 3),
        "frequency_ppm": _float(r[7]),
        "skew_ppm": _float(r[9]),
        "root_delay_ms": round((_float(r[10], 0.0) or 0.0) * 1000, 3),
        "update_interval_s": _float(r[12]),
        "leap": leap,
    }


def parse_sources(text: str) -> list[dict]:
    """`chronyc -c sources` : mode, etat, adresse, strate, intervalle (log2), portee (octal),
    derniere reponse (s), dernier ecart, ..."""
    sources = []
    for r in _rows(text):
        if len(r) < 8:
            continue
        sources.append(
            {
                "kind": "local" if r[0] == "#" else "server",
                "state": SOURCE_STATES.get(r[1], "unreachable"),
                "address": r[2],
                "stratum": int(_float(r[3], 0) or 0),
                "poll_s": 2 ** int(_float(r[4], 0) or 0),
                "reach": r[5],
                "last_rx_s": int(_float(r[6], 0) or 0),
                "offset_ms": round((_float(r[7], 0.0) or 0.0) * 1000, 3),
            }
        )
    return sources


def parse_clients(text: str, names: dict[str, str]) -> list[dict]:
    """`chronyc -c clients` : adresse, paquets NTP, abandons, intervalle, intervalle limite,
    derniere requete NTP (s), paquets de commande... Seuls ceux qui ont demande l'heure comptent
    (l'acces local de ntp-admin est une commande, pas une requete NTP)."""
    clients = []
    for r in _rows(text):
        if len(r) < 6:
            continue
        packets = int(_float(r[1], 0) or 0)
        if packets <= 0:
            continue
        last = _float(r[5])
        clients.append(
            {
                "ip": r[0],
                "name": names.get(r[0], ""),
                "requests": packets,
                "dropped": int(_float(r[2], 0) or 0),
                # None : inconnu ("-") ou valeur sentinelle de chrony ; 0 est une vraie valeur ("a l'instant")
                "last_s": int(last) if last is not None and 0 <= last < 4_000_000_000 else None,
            }
        )
    clients.sort(key=lambda c: (c["last_s"] is None, c["last_s"] if c["last_s"] is not None else 0))
    return clients


def load_names(path: Path = None) -> dict[str, str]:
    """ip -> nom de machine, d'apres les baux dnsmasq (vide si indisponible)."""
    path = path or LEASES_FILE
    names = {}
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] != "*":
                names[parts[2]] = parts[3]
    except OSError:
        pass
    return names


# --- reglages ---------------------------------------------------------------------

def _read_lines(name: str, default: list[str]) -> list[str]:
    try:
        lines = [ln.strip() for ln in (CONFIG_DIR / name).read_text().splitlines()]
    except OSError:
        return list(default)
    return [ln for ln in lines if ln and not ln.startswith("#")]


def read_config() -> dict:
    try:
        mode = (CONFIG_DIR / "mode").read_text().strip()
    except OSError:
        mode = "online"
    try:
        fallback = (CONFIG_DIR / "local-fallback").read_text().strip() != "off"
    except OSError:
        fallback = True
    return {
        "mode": mode if mode in MODES else "online",
        "upstream": _read_lines("upstream.conf", DEFAULT_UPSTREAM),
        "allow": _read_lines("allow.conf", DEFAULT_ALLOW),
        "local_fallback": fallback,
    }


def check_upstream(values) -> tuple[list[str] | None, str | None]:
    if not isinstance(values, list) or len(values) > MAX_UPSTREAM:
        return None, f"Liste de serveurs invalide ({MAX_UPSTREAM} au maximum)."
    clean = []
    for value in values:
        host = str(value).strip()
        if not host:
            continue
        if not HOST_RE.match(host) or len(host) > 253:
            return None, f"Serveur invalide : {host[:60]} (nom ou adresse, sans espace ni caractere special)."
        if host not in clean:
            clean.append(host)
    return clean, None


def check_allow(values) -> tuple[list[str] | None, str | None]:
    if not isinstance(values, list) or len(values) > MAX_ALLOW:
        return None, f"Liste de reseaux invalide ({MAX_ALLOW} au maximum)."
    clean = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            return None, f"Reseau invalide : {text[:60]} (attendu : 10.42.0.0/24)."
        if net.version != 4:
            return None, f"Reseau invalide : {text[:60]} (IPv4 uniquement)."
        if net.prefixlen < 8:
            # 0.0.0.0/0 ouvrirait le serveur de temps a tout le reseau physique (et a internet si le port est expose)
            return None, f"Reseau trop large : {net} (au moins /8)."
        text = str(net)
        if text not in clean:
            clean.append(text)
    return clean, None


def write_config(config: dict) -> None:
    """Un fichier par reglage, ecriture atomique (remplacement par renommage)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "mode": config["mode"] + "\n",
        "upstream.conf": "\n".join(config["upstream"]) + ("\n" if config["upstream"] else ""),
        "allow.conf": "\n".join(config["allow"]) + ("\n" if config["allow"] else ""),
        "local-fallback": ("on" if config["local_fallback"] else "off") + "\n",
    }
    for name, content in files.items():
        tmp = CONFIG_DIR / f".{name}.tmp"
        tmp.write_text(content)
        os.replace(tmp, CONFIG_DIR / name)


# --- conteneur ntp (API Docker) -------------------------------------------------------

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 10):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker_request(method: str, path: str, timeout: float = 10) -> tuple[int, bytes]:
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def container_running() -> bool | None:
    try:
        status, body = docker_request("GET", f"/containers/{CONTAINER}/json")
    except (OSError, http.client.HTTPException):
        return None
    if status == 404:
        return False
    if status != 200:
        return None
    try:
        return bool(json.loads(body)["State"]["Running"])
    except (ValueError, KeyError, TypeError):
        return None


def restart_container() -> tuple[bool, str]:
    try:
        status, body = docker_request("POST", f"/containers/{CONTAINER}/restart?t=10", timeout=60)
    except (OSError, http.client.HTTPException) as exc:
        return False, str(exc)
    if status != 204:
        return False, body.decode(errors="replace")[:200] or f"Docker a repondu {status}"
    return True, ""


# --- routes ------------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    ok_t, tracking_text = run_chronyc("tracking")
    tracking = parse_tracking(tracking_text) if ok_t else None
    sources, clients, error = [], [], None
    if tracking is not None:
        ok_s, sources_text = run_chronyc("sources")
        sources = parse_sources(sources_text) if ok_s else []
        ok_c, clients_text = run_chronyc("clients")
        clients = parse_clients(clients_text, load_names()) if ok_c else []
    elif not ok_t:
        error = tracking_text   # chronyd injoignable : conteneur arrete ou demarrage en cours
    return jsonify(
        {
            "now": time.time(),
            "timezone": time.strftime("%Z"),
            "container": {"running": container_running()},
            "tracking": tracking,
            "sources": sources,
            "clients": clients,
            "config": read_config(),
            "error": error,
        }
    )


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(read_config())


@app.route("/api/config", methods=["PUT"])
def api_config_put():
    body = request.get_json(silent=True) or {}
    current = read_config()
    mode = body.get("mode", current["mode"])
    if mode not in MODES:
        return jsonify({"error": "Mode invalide (online ou offline)."}), 400
    upstream, error = check_upstream(body.get("upstream", current["upstream"]))
    if error:
        return jsonify({"error": error}), 400
    allow, error = check_allow(body.get("allow", current["allow"]))
    if error:
        return jsonify({"error": error}), 400
    if mode == "online" and not upstream:
        return jsonify({"error": "Mode en ligne sans serveur amont : ajoute-en un, ou passe en mode hors ligne."}), 400
    if not allow:
        return jsonify({"error": "Aucun reseau autorise : personne ne pourrait demander l'heure."}), 400
    config = {"mode": mode, "upstream": upstream, "allow": allow, "local_fallback": bool(body.get("local_fallback", current["local_fallback"]))}
    try:
        write_config(config)
    except OSError as exc:
        return jsonify({"error": f"Ecriture impossible : {exc}"}), 500
    result = read_config()
    if body.get("apply"):
        ok, message = restart_container()
        if not ok:
            return jsonify({"error": f"Reglages enregistres, mais le redemarrage a echoue : {message}", "config": result}), 502
        result["applied"] = True
    return jsonify(result)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    ok, message = restart_container()
    if not ok:
        return jsonify({"error": message}), 502
    return jsonify({"restarted": True})


@app.route("/api/makestep", methods=["POST"])
def api_makestep():
    """Force la mise a l'heure immediate (saut d'horloge) plutot qu'un rattrapage progressif."""
    ok, output = run_chronyc("makestep", csv_output=False)
    if not ok:
        return jsonify({"error": output}), 502
    return jsonify({"done": True, "output": output.strip()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5058)
