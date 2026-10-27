"""Supervision des machines (supervision-admin) : machines lues dans Bastion, metriques de node_exporter, regles par
tag, delais (confirmation, demarrage), exposition fermee, maintenance, historique et API. Aucune machine reelle."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")
pytest.importorskip("flask")

SRC = Path(__file__).resolve().parent.parent / "supervision-admin"
sys.path.insert(0, str(SRC))
import checks as C  # noqa: E402
import fleet as F  # noqa: E402
import scrape as S  # noqa: E402
from history import History  # noqa: E402
from supervisor import Supervisor  # noqa: E402

# Extrait du /metrics reel de rpi-test (tags audio, kiosk), etiquettes dans l'ordre de node_exporter.
HEALTHY = """# HELP node_time_seconds System time.
# TYPE node_time_seconds gauge
node_time_seconds 1.79312845e+09
node_filesystem_avail_bytes{device="/dev/mmcblk0p1",device_error="",fstype="vfat",mountpoint="/boot/firmware"} 4.13761024e+08
node_filesystem_avail_bytes{device="/dev/mmcblk0p2",device_error="",fstype="ext4",mountpoint="/"} 1.0285826048e+10
node_filesystem_size_bytes{device="/dev/mmcblk0p2",device_error="",fstype="ext4",mountpoint="/"} 1.5065268224e+10
node_hwmon_temp_celsius{chip="thermal_thermal_zone0",sensor="temp0"} 51.121
node_thermal_zone_temp{type="cpu-thermal",zone="0"} 52.095
node_memory_MemAvailable_bytes 3.034771456e+09
node_memory_MemTotal_bytes 3.96238848e+09
node_systemd_unit_state{name="kiosk-vnc.service",state="active",type="simple"} 1
node_systemd_unit_state{name="kiosk-vnc.service",state="failed",type="simple"} 0
node_systemd_unit_state{name="kiosk.service",state="active",type="simple"} 1
node_systemd_unit_state{name="kiosk.service",state="failed",type="simple"} 0
pop_inov_display_connected{connector="card1-HDMI-A-1"} 0
pop_inov_display_connected{connector="card1-HDMI-A-2"} 1
pop_inov_probe_timestamp_seconds 1.793128445e+09
pop_inov_throttled 0
pop_inov_undervoltage 0
pop_inov_usb_device{classes="ef,01,03",id="2b89:64ed",name="UGREEN CM721 USB Audio",port="1-1.1"} 1
"""

YAML = """rooms:
- {id: expo, name: Grande salle}
machines:
- {id: a, name: rpi-test.expolab.lan, host: 10.42.0.134, tags: [audio, kiosk, monitoring], room: expo,
   credentials: {username: pi, password: SECRET}}
- {id: b, name: rpi-test-02.expolab.lan, host: 10.42.0.107, tags: [audio, wifi]}
- {id: c, name: Borne, host: 10.42.0.150, tags: [monitoring]}
- {id: d, name: sans-adresse, tags: [monitoring]}
"""


def metrics(text=HEALTHY, drop=(), **replace):
    lines = [line for line in text.splitlines() if not any(line.startswith(d) for d in drop)]
    out = "\n".join(lines)
    for old, new in replace.items():
        assert old in out, old
        out = out.replace(old, new)
    return S.parse(out)


def levels(results):
    return {c.id: c.level for c in results}


# --- Bastion ----------------------------------------------------------------------------------

def test_only_machines_tagged_monitoring_are_watched_without_any_credential():
    machines = F.parse(__import__("yaml").safe_load(YAML))

    assert [(m.name, m.host, m.room) for m in machines] == [("borne", "10.42.0.150", ""), ("rpi-test", "10.42.0.134", "Grande salle")]
    assert machines[1].tags == {"audio", "kiosk", "monitoring"}
    assert "SECRET" not in repr(machines)


def test_unreadable_bastion_keeps_the_last_known_machines(tmp_path):
    path, now = tmp_path / "machines.yaml", [0.0]
    path.write_text(YAML, encoding="utf-8")
    source = F.BastionMachines(path, clock=lambda: now[0])
    assert len(source()) == 2

    path.write_text("machines: [", encoding="utf-8")
    now[0] = 10.0
    assert len(source()) == 2 and source.error.startswith("Bastion illisible")


# --- metriques --------------------------------------------------------------------------------

def test_parse_reads_labels_by_name_and_unescapes_values():
    s = S.parse('# HELP x\nm{b="2",a="1"} 3\nm{a="x\\"y\\\\z"} 4e+00\nn NaN\nbad line\n')

    assert s.value("m", a="1") == 3 and s.value("m", b="2") == 3
    assert s.all("m")[1] == ({"a": 'x"y\\z'}, 4.0)
    assert s.value("n") is None and s.value("absent", 7) == 7


def test_unreachable_machine_gives_none(monkeypatch):
    def refuse(url, timeout):
        assert (url, timeout) == ("http://10.42.0.9:9100/metrics", S.TIMEOUT)
        raise ConnectionRefusedError
    monkeypatch.setattr(S.urllib.request, "urlopen", refuse)
    assert S.fetch("10.42.0.9") is None


# --- regles par tag ---------------------------------------------------------------------------

def test_healthy_kiosk_and_audio_machine_has_no_problem():
    results = C.evaluate({"audio", "kiosk", "monitoring"}, metrics(), C.DEFAULTS)

    assert set(levels(results)) == {"reachable", "probe", "temperature", "disk", "memory", "power",
                                    "unit:kiosk.service", "unit:kiosk-vnc.service", "display", "audio_usb"}
    assert all(level is None for level in levels(results).values())
    details = {c.id: c.detail for c in results}
    assert details["audio_usb"] == "UGREEN CM721 USB Audio" and details["display"] == "HDMI-A-2"
    assert details["temperature"] == "52 °C"


def test_tags_decide_what_is_expected():
    results = levels(C.evaluate({"monitoring"}, metrics(drop=("pop_inov_usb", "pop_inov_display")), C.DEFAULTS))
    assert "audio_usb" not in results and "display" not in results and all(v is None for v in results.values())


def test_unreachable_machine_is_a_single_alert():
    assert [(c.id, c.level) for c in C.evaluate({"audio"}, None, C.DEFAULTS)] == [("reachable", C.ALERT)]


def test_missing_sound_card_service_down_and_no_screen_are_alerts():
    s = metrics(drop=("pop_inov_usb_device",),
                **{'name="kiosk-vnc.service",state="active",type="simple"} 1': 'name="kiosk-vnc.service",state="active",type="simple"} 0',
                   'name="kiosk-vnc.service",state="failed",type="simple"} 0': 'name="kiosk-vnc.service",state="failed",type="simple"} 1',
                   'HDMI-A-2"} 1': 'HDMI-A-2"} 0'})
    results = {c.id: c for c in C.evaluate({"audio", "kiosk"}, s, C.DEFAULTS)}

    assert results["audio_usb"].level == C.ALERT
    assert (results["unit:kiosk-vnc.service"].level, results["unit:kiosk-vnc.service"].detail) == (C.ALERT, "failed")
    assert results["unit:kiosk.service"].level is None
    assert results["display"].level == C.ALERT


def test_usb_device_without_audio_class_is_not_a_sound_card():
    s = metrics(**{'classes="ef,01,03"': 'classes="ef,02,0a"'})
    assert levels(C.evaluate({"audio"}, s, C.DEFAULTS))["audio_usb"] == C.ALERT


def test_temperature_disk_and_memory_follow_the_settings():
    hot = metrics(**{"cpu-thermal\",zone=\"0\"} 52.095": "cpu-thermal\",zone=\"0\"} 80"})
    assert levels(C.evaluate(set(), hot, C.DEFAULTS))["temperature"] == C.WARNING
    assert levels(C.evaluate(set(), hot, {**C.DEFAULTS, "temp_alert": 78}))["temperature"] == C.ALERT
    assert levels(C.evaluate(set(), hot, {**C.DEFAULTS, "temp_warn": 85, "temp_alert": 90}))["temperature"] is None

    full = metrics(**{'mountpoint="/"} 1.0285826048e+10': 'mountpoint="/"} 5e+08'})        # 3 % libre
    assert levels(C.evaluate(set(), full, C.DEFAULTS))["disk"] == C.ALERT
    low_mem = metrics(**{"node_memory_MemAvailable_bytes 3.034771456e+09": "node_memory_MemAvailable_bytes 3.4e+08"})   # 91 % utilise
    assert levels(C.evaluate(set(), low_mem, C.DEFAULTS))["memory"] == C.WARNING


def test_overlay_root_without_a_published_root_is_not_checked():
    s = metrics(drop=('node_filesystem_avail_bytes{device="/dev/mmcblk0p2"', 'node_filesystem_size_bytes{device="/dev/mmcblk0p2"'))
    assert "disk" not in levels(C.evaluate(set(), s, C.DEFAULTS))


def test_probe_age_uses_the_machine_clock_and_power_problems_warn():
    stale = metrics(**{"node_time_seconds 1.79312845e+09": "node_time_seconds 1.7931289e+09"})        # +455 s
    assert levels(C.evaluate(set(), stale, C.DEFAULTS))["probe"] == C.WARNING
    assert levels(C.evaluate(set(), metrics(drop=("pop_inov_probe",)), C.DEFAULTS))["probe"] == C.WARNING
    assert levels(C.evaluate(set(), metrics(**{"pop_inov_undervoltage 0": "pop_inov_undervoltage 1"}), C.DEFAULTS))["power"] == C.WARNING


def test_application_health_is_checked_only_when_published():
    s = metrics(HEALTHY + "pop_inov_app_up 0\npop_inov_app_response_seconds 0\n")
    assert levels(C.evaluate(set(), s, C.DEFAULTS))["app"] == C.ALERT


def test_settings_are_validated():
    assert C.validate({"temp_warn": "70"}, C.DEFAULTS)["temp_warn"] == 70
    for bad in ({"inconnu": 1}, {"temp_warn": 5}, {"temp_warn": "chaud"}, {"retention_days": True},
                {"temp_warn": 90, "temp_alert": 80}, {"disk_warn": 96}, {"mem_alert": 80}):
        with pytest.raises(C.SettingsError):
            C.validate(bad, C.DEFAULTS)


# --- supervision : delais, exposition, maintenance, historique --------------------------------

class Fleet:
    def __init__(self):
        self.machines = [F.Machine("rpi-test", "10.42.0.134", frozenset({"audio", "kiosk", "monitoring"}), "Grande salle"),
                         F.Machine("borne", "10.42.0.150", frozenset({"monitoring"}))]
        self.answers = {"10.42.0.134": metrics(), "10.42.0.150": metrics()}
        self.calls = []
        self.error = ""

    def __call__(self):
        return list(self.machines)

    def fetch(self, host):
        self.calls.append(host)
        return self.answers.get(host)


@pytest.fixture
def env(tmp_path):
    now, fleet = [1_000_000.0], Fleet()
    sup = Supervisor(History(tmp_path / "s.db"), fleet, fleet.fetch, clock=lambda: now[0], workers=2)
    sup.update_settings({"unreachable_minutes": 1})      # 1 min pour tout : des scenarios plus courts
    now[0] += 6 * 60                       # passe le delai de demarrage du premier lancement

    def advance(seconds):
        now[0] += seconds
        return sup.tick()
    return sup, fleet, now, advance


def problems(sup):
    return [i for i in sup.history.incidents(0) if i["level"] != "info"]


def journal(sup):
    return [(i["machine"], i["label"], i["detail"]) for i in reversed(sup.history.incidents(0)) if i["level"] == "info"]


def kinds(events):
    return [(e.kind, e.machine, e.label, e.level) for e in events]


def test_problem_is_reported_once_after_the_confirmation_delay_then_resolved(env):
    sup, fleet, now, advance = env
    assert advance(0) == []
    fleet.answers["10.42.0.134"] = None

    assert advance(30) == []                                         # 0 s : a confirmer
    assert advance(30) == []                                         # 30 s
    assert kinds(advance(30)) == [("opened", "rpi-test", "Joignable", C.ALERT)]
    assert advance(30) == []                                         # deja signale : pas de repetition
    assert [v["status"] for v in sup.view()["machines"]] == [C.ALERT, "ok"]

    fleet.answers["10.42.0.134"] = metrics()
    assert kinds(advance(30)) == [("resolved", "rpi-test", "Joignable", C.ALERT)]
    [incident] = problems(sup)
    assert (incident["close_reason"], incident["closed_at"] - incident["opened_at"]) == ("rétabli", 60)


def test_short_glitch_is_never_reported(env):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.150"] = None
    advance(30)
    fleet.answers["10.42.0.150"] = metrics()
    assert advance(30) == [] and problems(sup) == []


def test_warning_that_becomes_an_alert_is_raised(env):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.150"] = metrics(**{"cpu-thermal\",zone=\"0\"} 52.095": "cpu-thermal\",zone=\"0\"} 80"})
    advance(0), advance(60)
    fleet.answers["10.42.0.150"] = metrics(**{"cpu-thermal\",zone=\"0\"} 52.095": "cpu-thermal\",zone=\"0\"} 90"})
    assert kinds(advance(30)) == [("raised", "borne", "Température", C.ALERT)]
    assert len(sup.history.open_incidents()) == 1


def test_closed_expo_checks_nothing_and_opening_waits_for_startup(env):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.134"] = None
    advance(0), advance(60)
    assert len(sup.history.open_incidents()) == 1

    sup.set_expo(False)
    assert sup.history.open_incidents() == [] and problems(sup)[0]["close_reason"] == "exposition fermée"
    fleet.calls.clear()
    assert advance(3600) == [] and fleet.calls == []
    assert {v["status"] for v in sup.view()["machines"]} == {"fermée"}

    sup.set_expo(True, "allumage")
    assert sup.expo()["source"] == "allumage"
    assert advance(30) == [] and advance(4 * 60) == []                  # demarrage : suivi, pas signale
    assert sup.view()["machines"][0]["status"] == "démarrage"
    assert kinds(advance(60)) == [("opened", "rpi-test", "Joignable", C.ALERT)]
    assert journal(sup) == [("", "Exposition fermée", "manuel · 1 problème(s) clos sans envoi"),
                            ("", "Exposition ouverte", "allumage · démarrage 5 min")]


def test_machine_in_maintenance_is_not_polled_and_gets_a_startup_delay(env):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.134"] = None
    advance(0), advance(60)
    sup.set_maintenance("rpi-test", True, "écran en réparation")
    assert sup.history.open_incidents() == [] and problems(sup)[0]["close_reason"] == "maintenance"

    fleet.calls.clear()
    assert advance(3600) == [] and fleet.calls == ["10.42.0.150"]
    view = sup.view()["machines"][0]
    assert (view["status"], view["maintenance"]["note"]) == ("maintenance", "écran en réparation")

    sup.set_maintenance("rpi-test", False)
    assert advance(30) == [] and advance(4 * 60) == []
    assert kinds(advance(60)) == [("opened", "rpi-test", "Joignable", C.ALERT)]
    assert journal(sup) == [("rpi-test", "En maintenance", "écran en réparation"), ("rpi-test", "Fin de maintenance", "")]


def test_service_down_waits_its_own_delay(env):
    sup, fleet, now, advance = env
    sup.update_settings({"service_minutes": 3})
    fleet.answers["10.42.0.134"] = metrics(**{'name="kiosk.service",state="active",type="simple"} 1':
                                              'name="kiosk.service",state="active",type="simple"} 0'})
    advance(0)
    assert advance(120) == []
    assert kinds(advance(60)) == [("opened", "rpi-test", "Service kiosk", C.ALERT)]


def test_machine_removed_from_monitoring_closes_its_incidents_silently(env):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.150"] = None
    advance(0), advance(60)
    fleet.machines.pop()
    assert advance(30) == [] and problems(sup)[0]["close_reason"] == "retirée de la supervision"


def test_unreachable_machine_closes_its_other_incidents(env):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.134"] = metrics(drop=("pop_inov_usb_device",))
    advance(0)
    assert kinds(advance(60)) == [("opened", "rpi-test", "Carte son USB", C.ALERT)]
    fleet.answers["10.42.0.134"] = None
    advance(30)
    assert problems(sup)[-1]["close_reason"] == "machine injoignable"


def test_incidents_survive_a_restart_and_history_is_purged(env, tmp_path):
    sup, fleet, now, advance = env
    fleet.answers["10.42.0.150"] = None
    advance(0), advance(60)

    again = Supervisor(History(tmp_path / "s.db"), fleet, fleet.fetch, clock=lambda: now[0], workers=1)
    assert again.tick() == [] and len(again.history.open_incidents()) == 1      # pas de doublon apres relance

    fleet.answers["10.42.0.150"] = metrics()
    again.tick()
    again.update_settings({"retention_days": 1})
    now[0] += 2 * 86400
    again.tick()
    assert problems(again) == []


def test_settings_have_defaults_and_are_stored(env):
    sup, *_ = env
    assert sup.settings() == {**C.DEFAULTS, "unreachable_minutes": 1}
    assert C.confirm_seconds("reachable", C.DEFAULTS) == 120 and C.confirm_seconds("unit:kiosk.service", C.DEFAULTS) == 60
    assert C.confirm_seconds("audio_usb", C.DEFAULTS) == 60
    sup.update_settings({"temp_warn": 70})
    assert sup.settings()["temp_warn"] == 70
    with pytest.raises(C.SettingsError):
        sup.update_settings({"temp_warn": 1})


# --- API --------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    spec = importlib.util.spec_from_file_location("supervision_app", SRC / "app.py")
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    fleet = Fleet()
    app_module.create(tmp_path, machines_source=fleet, fetch=fleet.fetch)
    return app_module.app.test_client()


H = {"X-Requested-With": "expolab"}


def test_api_refuses_forged_posts_and_bad_input(client):
    assert client.post("/api/expo", json={"open": False}).status_code == 403
    assert client.post("/api/expo", json={"open": "non"}, headers=H).status_code == 400
    assert client.post("/api/maintenance", json={"machine": "inconnue", "on": True}, headers=H).status_code == 400
    assert client.post("/api/settings", json={"temp_warn": 1}, headers=H).status_code == 400


def test_api_drives_expo_maintenance_and_settings(client):
    assert client.post("/api/expo", json={"open": False, "source": "allumage"}, headers=H).get_json()["expo"]["open"] is False
    assert client.post("/api/maintenance", json={"machine": "rpi-test.expolab.lan", "on": True}, headers=H).status_code == 200
    state = client.get("/api/state")
    assert state.headers["Cache-Control"] == "no-store"
    assert {m["name"]: m["status"] for m in state.get_json()["machines"]} == {"rpi-test": "maintenance", "borne": "fermée"}
    assert client.post("/api/settings", json={"retention_days": 30}, headers=H).get_json()["settings"]["retention_days"] == 30
    fields = client.get("/api/settings").get_json()["fields"]
    assert fields["temp_warn"]["default"] == 75
    history = client.get("/api/history?days=abc").get_json()["incidents"]
    assert [(i["machine"], i["label"], i["level"]) for i in history] == [
        ("rpi-test", "En maintenance", "info"), ("", "Exposition fermée", "info")]
