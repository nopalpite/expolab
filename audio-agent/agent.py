#!/usr/bin/env python3
"""Agent audio d'une machine de l'expo (Ubuntu Server, PipeWire) - bibliotheque standard seulement.

Il se connecte LUI-MEME au hub (audio-admin, attente longue) : le hub n'a jamais besoin de joindre la
machine, et une machine qui redemarre retrouve son niveau toute seule. Le hub envoie un niveau
EFFECTIF (gain en dB, mute) deja calcule avec la hierarchie exposition / salle / dispositif : l'agent
ne connait que son propre niveau.

Garanties :
  - le son ne depend jamais du reseau : au demarrage l'agent reprend son DERNIER niveau applique
    (memorise sur la machine), en douceur, avant meme d'avoir contacte le hub ;
  - il attend la carte son (elle n'existe pas tout de suite apres le demarrage) et ne previent systemd
    (Type=notify) qu'ensuite : les applications sonores declarees After= l'agent ne demarrent donc
    jamais a un niveau inconnu ;
  - la sortie est choisie par son NOM (AGENT_SINK_MATCH, par defaut la carte USB), jamais par son
    numero, qui change a chaque demarrage ; elle est signalee au hub, ainsi que l'absence de carte.

Unite commune : gain en dB (0 dB = pleine echelle). PipeWire regle un volume « cubique » :
gain_lineaire = volume ** 3, donc volume = 10 ** (dB / 60).

Configuration : ~/.config/expolab-agent.conf (lignes CLE=valeur) ou variables d'environnement :
  AGENT_HUB          adresse du hub, ex. http://10.42.0.1:5063
  AGENT_TOKEN        jeton partage avec le hub
  AGENT_NAME         nom de la machine (defaut : nom d'hote)
  AGENT_SINK_MATCH   morceau du nom de la sortie, ex. "usb" (defaut) ; "default" = sortie par defaut
"""
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

MIN_DB = -60.0
SMOOTH_STEPS = 4       # petit lissage anti-claquement (pas un « fondu » reglable)
SMOOTH_SECONDS = 0.06
SOFT_START_SECONDS = 1.5
POLL_TIMEOUT = 20      # le hub repond au plus tard apres ~8 s
CONF_FILE = os.path.expanduser("~/.config/expolab-agent.conf")
STATE_FILE = os.path.expanduser("~/.local/state/expolab-agent/state.json")


def load_conf(path: str = CONF_FILE, environ=None) -> dict:
    """Fichier CLE=valeur, surcharge par l'environnement."""
    environ = os.environ if environ is None else environ
    conf = {"AGENT_HUB": "", "AGENT_TOKEN": "", "AGENT_NAME": socket.gethostname(), "AGENT_SINK_MATCH": "usb"}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    conf[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    for key in conf:
        if environ.get(key):
            conf[key] = environ[key]
    return conf


def db_to_volume(db: float) -> float:
    return 0.0 if db <= MIN_DB else 10 ** (db / 60.0)


def volume_to_db(volume: float) -> float:
    return MIN_DB if volume <= 0 else max(MIN_DB, 60.0 * math.log10(volume))


class Sink:
    """La sortie audio : trouvee par son nom, volume et mute via wpctl (PipeWire)."""

    def __init__(self, match: str):
        self.match = match.lower()
        self.id = None      # numero PipeWire, relu quand il disparait
        self.name = ""      # nom stable de la sortie (alsa_output.usb-...)

    def _wp(self, *args):
        return subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=5)

    def _sink_ids(self) -> list[str]:
        out = self._wp("status").stdout
        block = out.split("Sinks:", 1)[-1].split("Sources:", 1)[0]
        return re.findall(r"(\d+)\.\s", block)

    def _node_name(self, sid: str) -> str:
        m = re.search(r'node\.name\s*=\s*"([^"]+)"', self._wp("inspect", sid).stdout)
        return m.group(1) if m else ""

    def resolve(self) -> bool:
        """Trouve la sortie voulue ; sans correspondance, « default » ou une carte absente => False
        sauf pour « default » (sortie par defaut de PipeWire)."""
        try:
            if self.match in ("", "default"):
                out = self._wp("inspect", "@DEFAULT_AUDIO_SINK@").stdout
                m = re.search(r"id\s+(\d+),", out)
                self.id = m.group(1) if m else None
                n = re.search(r'node\.name\s*=\s*"([^"]+)"', out)
                self.name = n.group(1) if n else ""
                return self.id is not None
            for sid in self._sink_ids():
                name = self._node_name(sid)
                if self.match in name.lower():
                    self.id, self.name = sid, name
                    return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.id, self.name = None, ""
        return False

    def ready(self) -> bool:
        return self.id is not None or self.resolve()

    def get(self):
        """(dB, coupe) actuels, ou (None, None) si la sortie est introuvable."""
        if not self.ready():
            return None, None
        try:
            out = self._wp("get-volume", self.id).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        m = re.search(r"Volume:\s*([0-9.]+)", out)
        if not m:
            self.id = None    # le numero a change (carte rebranchee) : on la retrouvera par son nom
            return None, None
        return volume_to_db(float(m.group(1))), "[MUTED]" in out

    def set_db(self, db: float) -> None:
        if self.ready():
            self._wp("set-volume", self.id, f"{db_to_volume(db):.5f}")

    def set_mute(self, mute: bool) -> None:
        if self.ready():
            self._wp("set-mute", self.id, "1" if mute else "0")


class Agent:
    def __init__(self, conf: dict, sink: Sink | None = None):
        self.conf = conf
        self.sink = sink or Sink(conf["AGENT_SINK_MATCH"])
        self.name = conf["AGENT_NAME"]
        self.desired = None            # {"gain_db", "mute", "managed"} recu du hub
        self.rev = 0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.applied = (None, None)    # (gain_db, mute) deja applique

    # --- hub ---------------------------------------------------------------------------------

    def http(self, path: str, data: dict | None = None, timeout: float = 10):
        req = urllib.request.Request(self.conf["AGENT_HUB"] + path, method="POST" if data is not None else "GET",
                                     data=None if data is None else json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json", "X-Agent-Token": self.conf["AGENT_TOKEN"]})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def status(self) -> dict:
        db, muted = self.sink.get()
        return {"name": self.name, "actual_db": db, "actual_mute": muted, "sink": self.sink.name,
                "error": "" if db is not None else "pas de sortie audio (" + self.conf["AGENT_SINK_MATCH"] + ")"}

    def report(self) -> None:
        try:
            self.http("/agent/report", self.status(), timeout=5)
        except (OSError, ValueError):
            pass

    # --- application du niveau -----------------------------------------------------------------

    def persist(self, db: float, mute: bool) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"gain_db": db, "mute": mute}, f)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    def smooth(self, start: float, end: float, seconds: float = SMOOTH_SECONDS, steps: int = SMOOTH_STEPS) -> None:
        if abs(end - start) < 0.05:
            self.sink.set_db(end)
            return
        for i in range(1, steps + 1):
            self.sink.set_db(start + (end - start) * i / steps)
            if i < steps:
                time.sleep(seconds / steps)

    def apply(self, db: float, mute: bool) -> None:
        """Applique un niveau : mute en douceur (descente rapide puis coupure), reprise en douceur."""
        cur_db, cur_mute = self.sink.get()
        if cur_db is None:
            return
        db = max(MIN_DB, min(0.0, float(db)))
        if mute:
            if not cur_mute:
                self.smooth(cur_db, MIN_DB)
                self.sink.set_mute(True)
            self.sink.set_db(db)            # silencieux tant que c'est coupe
        elif cur_mute:
            self.sink.set_db(MIN_DB)
            self.sink.set_mute(False)
            self.smooth(MIN_DB, db)
        else:
            self.smooth(cur_db, db)
        self.applied = (db, mute)
        self.persist(db, mute)

    def soft_start(self) -> None:
        """Au demarrage, avant meme de joindre le hub : dernier niveau applique, atteint en douceur."""
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            db, mute = float(saved["gain_db"]), bool(saved["mute"])
        except (OSError, ValueError, KeyError):
            return
        self.sink.set_db(MIN_DB)
        self.sink.set_mute(mute)
        if mute:
            self.sink.set_db(db)
        else:
            self.smooth(MIN_DB, db, SOFT_START_SECONDS, 15)
        self.applied = (db, mute)

    def wait_for_sink(self, timeout: float = 30.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.sink.resolve():
                return True
            time.sleep(0.5)
        return False

    # --- boucles ---------------------------------------------------------------------------------

    def worker(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            with self.lock:
                d = dict(self.desired or {})
            if d.get("managed") and (d["gain_db"], d["mute"]) != self.applied:
                self.apply(d["gain_db"], d["mute"])
                self.report()

    def poll_once(self) -> None:
        body = self.status()
        body["rev"] = self.rev
        d = self.http("/agent/poll", body, timeout=POLL_TIMEOUT)
        with self.lock:
            changed = d != self.desired
            self.desired, self.rev = d, d.get("rev", self.rev)
        if changed:
            self.wake.set()

    def run(self) -> None:
        found = self.wait_for_sink()
        print(f"sortie audio {self.sink.name or 'INTROUVABLE'} ({'prete' if found else 'absente'})", flush=True)
        self.soft_start()
        notify_ready()
        threading.Thread(target=self.worker, daemon=True).start()
        while True:
            try:
                self.poll_once()
            except (OSError, ValueError, KeyError):
                time.sleep(2)


def notify_ready() -> None:
    """Dit a systemd (Type=notify) que le niveau de depart est applique."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"READY=1", "\0" + path[1:] if path.startswith("@") else path)
    except OSError:
        pass
    finally:
        sock.close()


def main() -> None:
    conf = load_conf()
    if not conf["AGENT_HUB"] or not conf["AGENT_TOKEN"]:
        raise SystemExit("AGENT_HUB et AGENT_TOKEN sont requis (voir ~/.config/expolab-agent.conf).")
    Agent(conf).run()


if __name__ == "__main__":
    main()
