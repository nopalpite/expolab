"""Tests de l'agent audio (audio-agent/agent.py) : conversion dB <-> volume PipeWire, choix de la
sortie par son NOM, application d'un niveau (mute / reprise en douceur), reprise au demarrage sans
reseau, configuration. Aucun PipeWire reel : une fausse sortie et un faux hub."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parent.parent / "audio-agent" / "agent.py"
spec = importlib.util.spec_from_file_location("audio_agent", SRC)
agent = importlib.util.module_from_spec(spec)
sys.modules["audio_agent"] = agent
spec.loader.exec_module(agent)


STATUS = """PipeWire 'pipewire-0' [1.6.2]
Audio
 ├─ Devices:
 │      48. Built-in Audio                      [alsa]
 │      49. USB Audio Device                    [alsa]
 ├─ Sinks:
 │  *   55. Built-in Audio Stereo               [vol: 0.63]
 │      56. Built-in Audio Digital Stereo (HDMI) [vol: 0.40]
 │      57. USB Audio Device Analog Stereo      [vol: 1.00]
 ├─ Sources:
 │      61. Mic                                 [vol: 1.00]
"""
NAMES = {"55": "alsa_output.platform-fe00b840.mailbox.stereo-fallback",
         "56": "alsa_output.platform-fef00700.hdmi.hdmi-stereo",
         "57": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo"}


class FakeWp:
    """Remplace Sink._wp : une petite machine a etats (volume lineaire, coupe)."""

    def __init__(self, status=STATUS, volume=0.63, muted=False):
        self.status, self.volume, self.muted, self.calls = status, volume, muted, []

    def __call__(self, *args):
        self.calls.append(args)
        out = ""
        if args[0] == "status":
            out = self.status
        elif args[0] == "inspect":
            sid = args[1]
            if sid == "@DEFAULT_AUDIO_SINK@":
                out = f'id 55, type PipeWire:Interface:Node\n  node.name = "{NAMES["55"]}"\n'
            else:
                out = f'id {sid}, type PipeWire:Interface:Node\n  node.name = "{NAMES.get(sid, "")}"\n'
        elif args[0] == "get-volume":
            out = f"Volume: {self.volume:.2f}" + (" [MUTED]" if self.muted else "") + "\n"
        elif args[0] == "set-volume":
            self.volume = float(args[2])
        elif args[0] == "set-mute":
            self.muted = args[2] == "1"
        return SimpleNamespace(stdout=out, returncode=0)


def make_agent(monkeypatch, tmp_path, match="usb", wp=None):
    monkeypatch.setattr(agent, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    conf = {"AGENT_HUB": "http://hub", "AGENT_TOKEN": "t", "AGENT_NAME": "gerard", "AGENT_SINK_MATCH": match}
    ag = agent.Agent(conf)
    ag.sink._wp = wp or FakeWp()
    return ag


def test_db_and_volume_conversions_follow_the_cubic_law():
    assert agent.db_to_volume(0) == pytest.approx(1.0)
    assert agent.db_to_volume(-60) == 0.0
    assert agent.db_to_volume(-30) == pytest.approx(0.3162, abs=1e-3)
    assert agent.volume_to_db(0.63) == pytest.approx(-12.0, abs=0.1)
    assert agent.volume_to_db(0.0) == agent.MIN_DB
    for db in (-45, -24, -6):
        assert agent.volume_to_db(agent.db_to_volume(db)) == pytest.approx(db, abs=1e-6)


def test_the_output_is_found_by_name_not_by_number(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path, "usb")
    assert ag.sink.resolve() and ag.sink.id == "57" and "usb" in ag.sink.name
    # les numeros ont change apres un redemarrage : la carte est retrouvee par son nom
    ag.sink._wp.status = STATUS.replace("57.", "63.")
    NAMES["63"] = NAMES["57"]
    ag.sink.id = None
    assert ag.sink.resolve() and ag.sink.id == "63"


def test_no_usb_card_means_no_output_and_is_reported(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path, "usb", FakeWp(status=STATUS.replace("USB Audio Device Analog Stereo", "Other")))
    NAMES["57"] = "alsa_output.platform-xyz"
    try:
        assert ag.sink.resolve() is False
        st = ag.status()
        assert st["actual_db"] is None and "pas de sortie audio" in st["error"]
        ag.apply(-20, False)            # sans sortie : rien n'est applique, aucune erreur
        assert ag.applied == (None, None)
    finally:
        NAMES["57"] = "alsa_output.usb-Generic_USB_Audio-00.analog-stereo"


def test_default_match_uses_the_default_sink(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path, "default")
    assert ag.sink.resolve() and ag.sink.id == "55"


def test_apply_sets_the_level_in_the_cubic_scale(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path)
    ag.sink.resolve()
    ag.apply(-30, False)
    assert ag.sink._wp.volume == pytest.approx(agent.db_to_volume(-30), abs=1e-4)
    assert ag.applied == (-30, False) and json.loads(Path(agent.STATE_FILE).read_text()) == {"gain_db": -30, "mute": False}


def test_apply_clamps_to_the_range(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path)
    ag.sink.resolve()
    ag.apply(12, False)
    assert ag.sink._wp.volume == pytest.approx(1.0, abs=1e-4)      # jamais au-dessus de 0 dB
    ag.apply(-200, False)
    assert ag.sink._wp.volume == 0.0


def test_mute_ramps_down_then_mutes_and_keeps_the_target_for_the_return(monkeypatch, tmp_path):
    wp = FakeWp(volume=agent.db_to_volume(-12))
    ag = make_agent(monkeypatch, tmp_path, wp=wp)
    ag.sink.resolve()
    ag.apply(-12, True)
    sequence = [c for c in wp.calls if c[0] in ("set-volume", "set-mute")]
    assert sequence[-2][0] == "set-mute" and sequence[-2][2] == "1"       # coupe apres la descente...
    assert wp.muted and wp.volume == pytest.approx(agent.db_to_volume(-12), abs=1e-4)   # ...volume cible garde
    wp.calls.clear()
    ag.apply(-12, False)                                                  # reprise : sortie du mute depuis le silence
    first_calls = [c for c in wp.calls if c[0] in ("set-volume", "set-mute")][:2]
    assert first_calls[0][0] == "set-volume" and float(first_calls[0][2]) == 0.0
    assert first_calls[1] == ("set-mute", ag.sink.id, "0")
    assert not wp.muted and wp.volume == pytest.approx(agent.db_to_volume(-12), abs=1e-4)


def test_soft_start_restores_the_last_level_without_any_network(monkeypatch, tmp_path):
    Path(tmp_path / "state.json").write_text(json.dumps({"gain_db": -24, "mute": False}))
    wp = FakeWp(volume=1.0)                          # la machine redemarre avec un volume « par defaut » fort
    ag = make_agent(monkeypatch, tmp_path, wp=wp)
    ag.sink.resolve()
    ag.soft_start()
    assert wp.volume == pytest.approx(agent.db_to_volume(-24), abs=1e-4) and not wp.muted
    assert ag.applied == (-24, False)
    # la montee est progressive : le niveau part du silence
    volumes = [float(c[2]) for c in wp.calls if c[0] == "set-volume"]
    assert volumes[0] == 0.0 and volumes == sorted(volumes)


def test_soft_start_keeps_a_muted_machine_muted(monkeypatch, tmp_path):
    Path(tmp_path / "state.json").write_text(json.dumps({"gain_db": -20, "mute": True}))
    ag = make_agent(monkeypatch, tmp_path)
    ag.sink.resolve()
    ag.soft_start()
    assert ag.sink._wp.muted and ag.applied == (-20, True)


def test_soft_start_without_memory_changes_nothing(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path)
    ag.sink.resolve()
    ag.soft_start()
    assert not [c for c in ag.sink._wp.calls if c[0].startswith("set-")]


def test_poll_sends_status_and_stores_the_desired_level(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path)
    ag.sink.resolve()
    sent = []

    def fake_http(path, data=None, timeout=10):
        sent.append((path, data))
        return {"rev": 7, "gain_db": -18.0, "mute": False, "managed": True}

    monkeypatch.setattr(ag, "http", fake_http)
    ag.poll_once()
    path, body = sent[0]
    assert path == "/agent/poll" and body["name"] == "gerard" and body["rev"] == 0 and "usb" in body["sink"]
    assert ag.rev == 7 and ag.desired["gain_db"] == -18.0 and ag.wake.is_set()


def test_worker_ignores_an_unvalidated_machine_and_skips_a_level_already_applied(monkeypatch, tmp_path):
    ag = make_agent(monkeypatch, tmp_path)
    ag.sink.resolve()
    ag.sink._wp.calls.clear()
    applied = []
    monkeypatch.setattr(ag, "apply", lambda db, mute: (applied.append((db, mute)), setattr(ag, "applied", (db, mute))))
    monkeypatch.setattr(ag, "report", lambda: None)

    def one_pass(desired):
        ag.desired = desired
        ag.wake.set()
        monkeypatch.setattr(ag.wake, "wait", lambda: (_ for _ in ()).throw(StopIteration))
        # une seule passe de la boucle : on reproduit son corps
        d = dict(ag.desired)
        if d.get("managed") and (d["gain_db"], d["mute"]) != ag.applied:
            ag.apply(d["gain_db"], d["mute"])
            ag.report()

    one_pass({"managed": False, "gain_db": -10.0, "mute": False})
    assert applied == []                                  # en attente de validation : rien d'applique
    one_pass({"managed": True, "gain_db": -10.0, "mute": False})
    one_pass({"managed": True, "gain_db": -10.0, "mute": False})
    assert applied == [(-10.0, False)]                    # idempotent


def test_load_conf_file_and_environment(tmp_path):
    f = tmp_path / "agent.conf"
    f.write_text('# commentaire\nAGENT_HUB=http://10.42.0.1:5063\nAGENT_TOKEN="secret"\nAGENT_SINK_MATCH=default\n')
    conf = agent.load_conf(str(f), environ={})
    assert conf["AGENT_HUB"] == "http://10.42.0.1:5063" and conf["AGENT_TOKEN"] == "secret" and conf["AGENT_SINK_MATCH"] == "default"
    assert agent.load_conf(str(f), environ={"AGENT_NAME": "autre"})["AGENT_NAME"] == "autre"
    assert agent.load_conf(str(tmp_path / "absent"), environ={})["AGENT_SINK_MATCH"] == "usb"
