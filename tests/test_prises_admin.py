"""Tests de prises-admin : lecture des prises, codes d'appairage, modules, historique, et API
(ajout, marche/arret, extinction propre, actions groupees, retrait) avec un faux controleur
Matter. L'extinction propre ne coupe JAMAIS une machine qui repond encore."""
import datetime
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("yaml")

SRC = Path(__file__).resolve().parent.parent / "prises-admin"
sys.path.insert(0, str(SRC))
import plugs as P  # noqa: E402


def node(node_id=7, on=True, power_mw=96500, energy_mwh=840000, available=True):
    attrs = {"0/40/1": "Fab", "0/40/3": "Prise X", "0/40/10": "1.2.0", "1/6/0": on}
    if power_mw is not None:
        attrs["2/144/8"] = power_mw
    if energy_mwh is not None:
        attrs["2/145/1"] = {"energy": energy_mwh}
    return {"node_id": node_id, "available": available, "attributes": attrs}


# --- briques -----------------------------------------------------------------------------

def test_read_node():
    p = P.read_node(node())
    assert (p["on"], p["power_w"], p["energy_wh"], p["endpoint"], p["can_switch"], p["measures"]) == (True, 96.5, 840.0, 1, True, True)
    q = P.read_node(node(power_mw=None, energy_mwh=None))
    assert q["measures"] is False and q["power_w"] is None
    legacy = P.read_node({"node_id": 2, "available": True, "attributes": {"1/6/0": False, "1/2820/1291": 12}})
    assert legacy["power_w"] == 12.0 and legacy["on"] is False
    assert P.read_node({"node_id": 1, "available": False, "attributes": {}})["can_switch"] is False


@pytest.mark.parametrize("code, expected", [
    ("3497-011-2332", "34970112332"), ("34970112332", "34970112332"),
    ("mt:y.k9042c00ka0648g00", "MT:Y.K9042C00KA0648G00"),
    ("123", None), ("MT:", None), ("3497-011-2332; rm", None), ("MT:abc def", None),
])
def test_normalize_code(code, expected):
    assert P.normalize_code(code) == expected


def test_load_modules(tmp_path):
    res = tmp_path / "reservations.conf"
    res.write_text("rpi-test,10.42.0.134,12h\naa:bb:cc:dd:ee:ff,10.42.0.50,12h\n# x\n")
    cfg = tmp_path / "machines.yaml"
    cfg.write_text("machines:\n- {name: projo.expolab.lan, host: 10.42.0.151}\n- {name: rpi-test.expolab.lan, host: 10.42.0.9}\n")
    assert P.load_modules(res, cfg) == {"projo": "10.42.0.151", "rpi-test": "10.42.0.134"}   # la reservation l'emporte


def test_history_series_and_energy_today(tmp_path):
    h = P.History(tmp_path / "h.db")
    now = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    h.add([P.read_node(node(power_mw=100000, energy_mwh=1_000_000))], now=now - 3600)
    h.add([P.read_node(node(power_mw=50000, energy_mwh=1_050_000))], now=now - 60)
    h.add([P.read_node(node(node_id=8, available=False))], now=now)          # injoignable : pas de mesure
    series = h.series(7, now=now)
    assert len(series) == 48 and series[-1] == 50.0 and series[-2] == 100.0 and series[0] is None
    assert h.series(8, now=now) == [None] * 48
    assert h.energy_today(7, 1100.0, now=now) == 100.0
    assert h.energy_today(7, None, now=now) is None


# --- API -------------------------------------------------------------------------------------

class FakeMatter:
    def __init__(self):
        self.nodes = {7: node(7), 8: node(8, on=False)}
        self.calls = []

    def call(self, command, args=None, timeout=30):
        self.calls.append((command, args))
        if command is None:
            return {"bluetooth_enabled": True, "wifi_credentials_set": True, "sdk_version": "x"}
        if command == "get_nodes":
            return list(self.nodes.values())
        if command == "set_wifi_credentials":
            return None
        if command == "commission_with_code":
            self.nodes[9] = node(9)
            return self.nodes[9]
        if command == "device_command":
            if args["cluster_id"] == 6:
                self.nodes[args["node_id"]]["attributes"]["1/6/0"] = args["command_name"] == "On"
            return None
        if command == "remove_node":
            self.nodes.pop(args["node_id"], None)
            return None
        raise AssertionError(command)


@pytest.fixture
def app(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("prises_app", SRC / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = FakeMatter()
    monkeypatch.setattr(module.matter_client, "call", fake.call)
    (tmp_path / "res.conf").write_text("projo,10.42.0.151,12h\nrpi-test,10.42.0.134,12h\n")
    (tmp_path / "flasher.json").write_text(json.dumps({"wifi_ssid": "expolab", "wifi_psk": "secret-wifi"}))
    for name, value in (("DATA_DIR", tmp_path / "data"), ("RESERVATIONS", tmp_path / "res.conf"),
                        ("BASTION_CONFIG", tmp_path / "absent.yaml"), ("FLASHER_SETTINGS", tmp_path / "flasher.json"),
                        ("SHUTDOWN_WAIT", 1), ("SHUTDOWN_GRACE", 0)):
        monkeypatch.setattr(module, name, value)
    module.DATA_DIR.mkdir()
    module._history = module.P.History(module.DATA_DIR / "h.db")
    module.refresh()
    module.client = module.app.test_client()
    module.fake = fake
    return module


def wait(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("delai depasse")


def test_state_and_summary(app):
    st = app.client.get("/api/state").get_json()
    assert st["modules"] == ["projo", "rpi-test"] and st["controller"]["bluetooth"] is True
    assert sorted(p["node_id"] for p in st["plugs"]) == [7, 8]
    assert st["summary"]["on"] == 1 and st["summary"]["total"] == 2 and st["summary"]["power_w"] == 193.0


def test_add_plug_sends_the_wifi_then_commissions(app):
    assert app.client.post("/api/plugs", json={"code": "123"}).status_code == 400
    assert app.client.post("/api/plugs", json={"code": "34970112332", "module": "inconnu"}).status_code == 400
    assert app.client.post("/api/plugs", json={"code": "3497-011-2332", "module": "projo"}).status_code == 202
    wait(lambda: app._job["state"] != "running")
    assert app._job["state"] == "done" and app._job["node_id"] == 9
    commands = [c for c, _a in app.fake.calls if c in ("set_wifi_credentials", "commission_with_code")]
    assert commands == ["set_wifi_credentials", "commission_with_code"]
    assert ("commission_with_code", {"code": "34970112332", "network_only": False}) in app.fake.calls
    assert app.load_meta()[9] == {"name": "prise-projo", "module": "projo"}
    assert "secret-wifi" not in app.client.get("/api/state").get_data(as_text=True)   # jamais renvoye a la page


def test_add_plug_without_wifi_settings_fails_cleanly(app, tmp_path):
    (tmp_path / "flasher.json").write_text("{}")
    app.client.post("/api/plugs", json={"code": "34970112332"})
    wait(lambda: app._job["state"] != "running")
    assert app._job["state"] == "error" and "Wi-Fi" in app._job["error"]
    assert not any(c == "commission_with_code" for c, _a in app.fake.calls)


def test_switch_on_and_immediate_off(app):
    assert app.client.post("/api/plugs/8/power", json={"on": True}).status_code == 200
    assert app.fake.nodes[8]["attributes"]["1/6/0"] is True
    assert app.client.post("/api/plugs/7/power", json={"on": False, "clean": False}).status_code == 200
    assert app.fake.nodes[7]["attributes"]["1/6/0"] is False


def test_clean_off_shuts_the_machine_down_before_cutting(app, monkeypatch):
    app.save_meta({7: {"name": "p", "module": "projo"}})
    ssh_calls, up = [], {"v": True}
    monkeypatch.setattr(app.subprocess, "run", lambda cmd, **kw: (ssh_calls.append(cmd), up.update(v=False)))
    monkeypatch.setattr(app, "ssh_up", lambda ip: up["v"])
    assert app.client.post("/api/plugs/7/power", json={"on": False}).status_code == 202
    wait(lambda: 7 not in app._busy)
    assert ssh_calls and ssh_calls[0][-2:] == ["pi@10.42.0.151", "sudo -n systemctl poweroff"]
    assert app.fake.nodes[7]["attributes"]["1/6/0"] is False


def test_clean_off_never_cuts_a_machine_still_running(app, monkeypatch):
    app.save_meta({7: {"name": "p", "module": "projo"}})
    monkeypatch.setattr(app.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(app, "ssh_up", lambda ip: True)          # la machine ne s'eteint pas
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    app.client.post("/api/plugs/7/power", json={"on": False})
    wait(lambda: 7 not in app._busy)
    assert app.fake.nodes[7]["attributes"]["1/6/0"] is True     # prise laissee allumee
    st = app.client.get("/api/state").get_json()
    assert "ne s'est pas eteinte" in next(p for p in st["plugs"] if p["node_id"] == 7)["notice"]


def test_all_on(app):
    assert app.client.post("/api/all", json={"action": "on"}).get_json()["nodes"] == [8]
    assert app.client.post("/api/all", json={"action": "x"}).status_code == 400


def test_update_and_remove(app):
    app.save_meta({7: {"name": "p", "module": "projo"}})
    assert app.client.put("/api/plugs/7", json={"name": "projo-gauche", "module": "rpi-test"}).get_json()["module"] == "rpi-test"
    assert app.client.put("/api/plugs/7", json={"module": "inconnu"}).status_code == 400
    assert app.client.delete("/api/plugs/7").status_code == 200
    assert 7 not in app.fake.nodes and 7 not in app.load_meta()
    assert app.client.delete("/api/plugs/42").status_code == 404
