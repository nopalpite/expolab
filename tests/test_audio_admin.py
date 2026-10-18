"""Tests de la console audio (audio-admin) : hierarchie exposition / salle / dispositif, une ou plusieurs
salles, presets, enregistrement des machines, attente longue des agents, conservation sur disque, et
API (en-tete anti-requete-forgee, jeton des agents, vue exploitant en pourcentages)."""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

SRC = Path(__file__).resolve().parent.parent / "audio-admin"
sys.path.insert(0, str(SRC))
import bastion as B  # noqa: E402
import hub as H  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


class FakeBastion:
    """Source des salles : {id: nom} et {nom court de machine: id de salle}, comme le fichier de Bastion."""

    def __init__(self):
        self.rooms, self.members = {}, {}

    def __call__(self):
        return B.RoomMap(self.rooms, self.members)


@pytest.fixture
def hub(tmp_path, clock):
    return H.Hub(str(tmp_path / "state.json"), clock=clock, rooms_source=FakeBastion())


def add(hub, name, room="", db=-24.0, validated=True, free=False):
    """Machine deja vue et validee (equivalent d'un agent qui s'est annonce), rangee dans la salle `room` DE BASTION
    (id = nom ici) ; room="" : aucune salle dans Bastion."""
    hub.agent_report({"name": name, "actual_db": db, "actual_mute": False, "sink": "alsa_output.usb-x"})
    hub.devices[name].update(db=db, validated=validated, free=free)
    if room:
        hub.rooms_source.rooms[room] = room
        hub.rooms_source.members[B.short_name(name)] = room


# --- niveaux -----------------------------------------------------------------------------------------

def test_single_room_general_volume_is_the_only_level(hub):
    add(hub, "a", "Salle A", -24)
    add(hub, "b", "Salle A", -30)
    assert hub.multi() is False
    hub.set_expo(db=-6)
    assert hub.effective("a")["gain_db"] == -30.0 and hub.effective("b")["gain_db"] == -36.0
    hub.set_expo(db=-6)
    hub.rooms["Salle A"] = {"db": -20.0, "mute": True}            # ignore : pas de niveau « salle » avec une seule salle
    assert hub.effective("a") == {"gain_db": -30.0, "mute": False, "managed": True}
    assert hub.state_view()["rooms"] == []


def test_a_machine_without_a_bastion_room_does_not_exist_for_the_console(hub):
    add(hub, "a", "Salle A", -24)
    add(hub, "libre", "", -24)                           # creee dans Bastion, pas encore rangee dans une salle
    view = hub.state_view()
    assert [d["name"] for d in view["devices"]] == ["a"] and view["pending"] == []     # ni affichee, ni « a valider »
    assert view["multi"] is False and view["rooms"] == []
    answer = hub.agent_poll("libre", {}, rev=0, timeout=0)
    assert answer["managed"] is False                    # non pilotee : elle garde son niveau local
    assert hub.agent_poll("a", {}, rev=0, timeout=0)["managed"] is True


def test_a_pending_machine_is_only_proposed_once_bastion_has_placed_it_in_a_room(hub):
    hub.agent_poll("borne-04", {"actual_db": -20}, rev=0, timeout=0)
    assert hub.state_view()["pending"] == []
    hub.rooms_source.rooms["Salle A"] = "Salle A"
    hub.rooms_source.members["borne-04"] = "Salle A"
    assert [p["name"] for p in hub.state_view()["pending"]] == ["borne-04"]


def test_moving_a_machine_in_bastion_moves_it_in_the_console(hub):
    add(hub, "a", "Salle A", -24)
    add(hub, "b", "Salle B", -24)
    hub.set_room("Salle A", db=-10)
    assert hub.effective("a")["gain_db"] == -40.0         # -24 + -10 + -6
    hub.rooms_source.members["a"] = "Salle B"
    assert hub.effective("a")["gain_db"] == -30.0         # il suit maintenant le volume de Salle B (0)
    hub.rooms_source.members.pop("a")                     # retiree de sa salle dans Bastion
    assert hub.effective("a")["managed"] is False and [d["name"] for d in hub.state_view()["devices"]] == ["b"]
    hub.rooms_source.members["a"] = "Salle B"
    assert hub.effective("a")["managed"] is True


def test_rooms_come_with_their_bastion_names_and_ids(hub):
    hub.rooms_source.rooms.update({"salle-a": "Grande salle", "salle-b": "Atelier"})
    add(hub, "a", "", -24)
    add(hub, "b", "", -24)
    hub.rooms_source.members.update({"a": "salle-a", "b": "salle-b"})
    rooms = hub.state_view()["rooms"]
    assert [(r["id"], r["name"]) for r in rooms] == [("salle-b", "Atelier"), ("salle-a", "Grande salle")]   # triees par nom
    hub.set_room("salle-a", db=-4)
    with pytest.raises(H.HubError):
        hub.set_room("Atelier", db=-4)                    # on adresse une salle par son id de Bastion


def test_several_rooms_add_a_room_level_between_the_expo_and_the_machines(hub):
    add(hub, "a", "Salle A", -24)
    add(hub, "b", "Salle B", -24)
    assert hub.multi() is True
    hub.set_expo(db=-6)
    hub.set_room("Salle A", db=-3)
    assert hub.effective("a")["gain_db"] == -33.0      # -24 + -3 + -6
    assert hub.effective("b")["gain_db"] == -30.0      # -24 + 0 + -6
    assert [r["name"] for r in hub.state_view()["rooms"]] == ["Salle A", "Salle B"]


def test_mutes_cascade_and_a_free_machine_only_ignores_the_expo_volume(hub):
    add(hub, "a", "Salle A")
    add(hub, "b", "Salle B", free=True)
    hub.set_expo(db=-20)
    assert hub.effective("b")["gain_db"] == -24.0               # hors exposition : pas de -20
    hub.set_room("Salle A", mute=True)
    assert hub.effective("a")["mute"] is True and hub.effective("b")["mute"] is False
    hub.set_expo(mute=True)                                      # « Tout couper » coupe aussi les machines libres
    assert hub.effective("b")["mute"] is True
    hub.set_expo(mute=False)
    hub.set_device("a", mute=True)
    assert hub.effective("a")["mute"] is True


def test_levels_are_clamped_and_invalid_values_refused(hub):
    add(hub, "a", "S")
    hub.set_expo(db=-5)
    hub.set_device("a", db=12)
    assert hub.devices["a"]["db"] == 0.0
    hub.set_device("a", db=-500)
    assert hub.devices["a"]["db"] == H.MIN_DB
    for bad in ("x", None, float("nan")):
        with pytest.raises(H.HubError):
            H.clamp_db(bad)
    # la somme est bornee elle aussi : jamais au-dessus de la pleine echelle
    hub.set_expo(db=0)
    hub.set_device("a", db=0)
    assert hub.effective("a")["gain_db"] == 0.0


def test_operator_percent_scale_round_trips():
    assert H.pct_to_db(0) == -60.0 and H.pct_to_db(100) == 0.0 and H.pct_to_db(50) == -30.0
    assert H.pct_to_db(-5) == -60.0 and H.pct_to_db(250) == 0.0
    for pct in (0, 25, 65, 100):
        assert H.db_to_pct(H.pct_to_db(pct)) == pct
    with pytest.raises(H.HubError):
        H.pct_to_db("abc")


# --- machines --------------------------------------------------------------------------------------------

def test_an_unknown_agent_is_registered_pending_and_gets_no_orders(hub):
    hub.rooms_source.rooms["Salle A"] = "Salle A"
    hub.rooms_source.members["borne-04"] = "Salle A"
    answer = hub.agent_poll("borne-04", {"actual_db": -20, "sink": "usb"}, rev=0, timeout=0)
    assert answer["managed"] is False and "borne-04" in hub.devices
    view = hub.state_view()
    assert [p["name"] for p in view["pending"]] == ["borne-04"] and view["devices"] == []
    hub.validate("borne-04")
    assert hub.agent_poll("borne-04", {}, rev=0, timeout=0)["managed"] is True
    assert [d["name"] for d in hub.state_view()["devices"]] == ["borne-04"]


def test_forget_removes_a_machine_and_invalid_names_are_refused(hub):
    add(hub, "a", "S")
    hub.forget("a")
    assert "a" not in hub.devices
    with pytest.raises(H.HubError):
        hub.forget("a")
    for bad in ("", "../x", "a b", "x" * 80):
        with pytest.raises(H.HubError):
            hub.agent_poll(bad, {}, rev=0, timeout=0)


def test_presence_follows_the_heartbeat(hub, clock):
    add(hub, "a", "S")
    assert hub.online("a") is True
    clock.t += H.ONLINE_SECONDS + 1
    assert hub.online("a") is False and hub.state_view()["devices"][0]["online"] is False
    hub.agent_report({"name": "a", "actual_db": -20})
    assert hub.online("a") is True


def test_a_poll_returns_at_once_when_the_level_changed_and_waits_otherwise(hub):
    add(hub, "a", "S")
    rev = hub.agent_poll("a", {}, rev=0, timeout=0)["rev"]
    t0 = time.time()
    answer = {}
    thread = threading.Thread(target=lambda: answer.update(hub.agent_poll("a", {}, rev=rev, timeout=5)))
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()                        # rien n'a change : l'agent attend
    hub.set_device("a", db=-10)
    thread.join(3)
    assert not thread.is_alive() and time.time() - t0 < 3
    assert answer["gain_db"] == -10.0 - 6.0 and answer["rev"] > rev      # niveau de l'exposition (-6) inclus


def test_the_agent_hears_the_effective_level_not_the_hierarchy(hub):
    add(hub, "a", "Salle A", -24)
    add(hub, "b", "Salle B", -24)
    hub.set_expo(db=-6)
    hub.set_room("Salle B", db=-9)
    assert hub.agent_poll("b", {}, rev=0, timeout=0) == {"rev": hub.rev, "gain_db": -39.0, "mute": False, "managed": True}


# --- presets ------------------------------------------------------------------------------------------------

def test_presets_restore_every_level_and_track_modifications(hub):
    add(hub, "a", "Salle A", -24)
    add(hub, "b", "Salle B", -30)
    hub.set_expo(db=-3)
    hub.set_room("Salle A", db=-6)
    hub.set_device("b", mute=True)
    hub.save_preset("Scolaire")
    assert hub.current == "Scolaire" and hub.modified is False
    hub.set_expo(db=-40)
    hub.set_room("Salle A", db=-20)
    hub.set_device("a", db=-50)
    hub.set_device("b", mute=False)
    assert hub.modified is True
    hub.recall_preset("Scolaire")
    assert hub.expo == {"db": -3.0, "mute": False} and hub.rooms["Salle A"]["db"] == -6.0
    assert hub.devices["a"]["db"] == -24.0 and hub.devices["b"] == {**hub.devices["b"], "db": -30.0, "mute": True}
    assert hub.modified is False and hub.state_view()["current"] == "Scolaire"


def test_recall_ignores_machines_that_no_longer_exist_and_keeps_new_ones(hub):
    add(hub, "a", "S", -24)
    add(hub, "gone", "S", -24)
    hub.save_preset("P")
    hub.forget("gone")
    add(hub, "new", "S", -10)
    hub.set_device("a", db=-50)
    hub.recall_preset("P")
    assert hub.devices["a"]["db"] == -24.0 and hub.devices["new"]["db"] == -10.0


def test_preset_names_are_validated_and_deletion_works(hub):
    for bad in ("", "   ", "x" * 41, "a\nb"):
        with pytest.raises(H.HubError):
            hub.save_preset(bad)
    hub.save_preset("Événementiel")
    hub.delete_preset("Événementiel")
    assert hub.presets == {} and hub.current is None
    with pytest.raises(H.HubError):
        hub.recall_preset("inconnu")


# --- conservation ---------------------------------------------------------------------------------------------

def test_state_survives_a_restart(tmp_path, clock):
    path = str(tmp_path / "state.json")
    src = FakeBastion()
    h = H.Hub(path, clock=clock, rooms_source=src)
    add(h, "a", "Salle A", -24)
    add(h, "b", "Salle B", -30)
    h.set_expo(db=-12, mute=True)
    h.set_room("Salle A", db=-5)
    h.set_device("b", free=True, label="Borne B")
    h.save_preset("P")
    h.save()
    again = H.Hub(path, clock=clock, rooms_source=src)
    assert again.expo == {"db": -12.0, "mute": True} and again.rooms["Salle A"]["db"] == -5.0
    assert again.devices["b"]["free"] is True and again.devices["b"]["label"] == "Borne B"
    assert again.current == "P" and "P" in again.presets and again.live == {}       # la presence n'est jamais conservee


def test_a_corrupt_state_file_starts_clean(tmp_path, clock):
    path = tmp_path / "state.json"
    path.write_text("{pas du json")
    assert H.Hub(str(path), clock=clock).devices == {}
    path.write_text(json.dumps({"expo": {"db": "x"}}))
    assert H.Hub(str(path), clock=clock).expo == {"db": -6.0, "mute": False}


# --- API --------------------------------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, clock, monkeypatch):
    spec = importlib.util.spec_from_file_location("audio_admin_app", SRC / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["audio_admin_app"] = module   # Flask retrouve templates/ et static/ via le module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "hub", H.Hub(str(tmp_path / "state.json"), clock=clock, rooms_source=FakeBastion()))
    module._token_cache.update(mtime=None, value="")
    (tmp_path / "agent.token").write_text("jeton-de-test\n")
    module.client = module.ui.test_client()
    module.agent_client = module.agent_api.test_client()
    return module


XHR = {"X-Requested-With": "expolab"}


def test_ui_mutations_need_the_anti_forgery_header(app):
    assert app.client.post("/api/expo", json={"db": -10}).status_code == 403
    assert app.client.post("/api/kill").status_code == 403
    assert app.client.post("/api/expo", json={"db": -10}, headers=XHR).status_code == 200
    assert app.client.get("/api/state").get_json()["expo"]["db"] == -10.0


def test_ui_api_full_cycle(app):
    add(app.hub, "a", "Salle A", -24)
    add(app.hub, "b", "Salle B", -24)
    assert app.client.post("/api/rooms/Salle%20A", json={"db": -8}, headers=XHR).status_code == 200
    assert app.client.post("/api/rooms/inconnue", json={"db": -8}, headers=XHR).status_code == 400
    assert app.client.post("/api/devices/a", json={"mute": True, "label": "Borne"}, headers=XHR).status_code == 200
    assert app.client.post("/api/devices/zzz", json={"mute": True}, headers=XHR).status_code == 400
    assert app.client.post("/api/expo", json={"db": "x"}, headers=XHR).get_json()["error"] == "Niveau invalide."
    assert app.client.post("/api/presets", json={"name": "Nuit"}, headers=XHR).status_code == 200
    assert app.client.post("/api/presets", json={"name": ""}, headers=XHR).status_code == 400
    state = app.client.get("/api/state").get_json()
    assert state["multi"] and state["presets"] == ["Nuit"] and state["current"] == "Nuit"
    a = next(d for d in state["devices"] if d["name"] == "a")
    assert a["label"] == "Borne" and a["mute"] is True
    assert app.client.post("/api/presets/Nuit/recall", headers=XHR).status_code == 200
    assert app.client.delete("/api/presets/Nuit", headers=XHR).status_code == 200
    assert app.client.delete("/api/devices/b", headers=XHR).status_code == 200


def test_the_console_has_no_way_to_change_a_room(app):
    add(app.hub, "a", "Salle A", -24)
    add(app.hub, "b", "Salle B", -24)
    # la salle vient de Bastion : le champ n'existe plus, une valeur envoyee est ignoree
    assert app.client.post("/api/devices/a", json={"room": "Salle B", "mute": True}, headers=XHR).status_code == 200
    state = app.client.get("/api/state").get_json()
    a = next(d for d in state["devices"] if d["name"] == "a")
    assert a["room"] == "Salle A" and a["room_name"] == "Salle A" and a["mute"] is True


def test_operator_api_uses_percentages_and_exposes_no_machine_detail(app):
    add(app.hub, "borne-zz", "Salle A", -24)
    add(app.hub, "table-yy", "Salle B", -24)
    app.hub.save_preset("Scolaire")
    assert app.client.post("/api/exploitation/expo", json={"pct": 50}, headers=XHR).status_code == 200
    assert app.client.post("/api/exploitation/rooms/Salle%20B", json={"pct": 80, "mute": False}, headers=XHR).status_code == 200
    view = app.client.get("/api/exploitation/state").get_json()
    assert view["expo"] == {"pct": 50, "mute": False} and view["multi"] is True and view["presets"] == ["Scolaire"]
    assert {r["name"]: r["pct"] for r in view["rooms"]} == {"Salle A": 100, "Salle B": 80}
    assert "devices" not in view and "borne-zz" not in json.dumps(view) and "table-yy" not in json.dumps(view)
    assert app.hub.expo["db"] == -30.0
    assert app.client.post("/api/exploitation/presets/Scolaire/recall", headers=XHR).status_code == 200
    assert app.client.post("/api/exploitation/expo", json={"pct": 50}).status_code == 403
    assert app.client.post("/api/exploitation/expo", json={"pct": "x"}, headers=XHR).status_code == 400


def test_single_room_operator_view_has_no_room_cards(app):
    add(app.hub, "a", "Salle A", -24)
    view = app.client.get("/api/exploitation/state").get_json()
    assert view["multi"] is False and view["rooms"] == []


def test_pages_are_served(app):
    assert b"Console audio" in app.client.get("/").data
    assert b"Son de l'exposition" in app.client.get("/exploitation").data


def test_agent_api_requires_the_token(app):
    body = {"name": "gerard", "rev": 0, "actual_db": -20, "sink": "usb"}
    assert app.agent_client.post("/agent/poll", json=body).status_code == 401
    assert app.agent_client.post("/agent/poll", json=body, headers={"X-Agent-Token": "faux"}).status_code == 401
    ok = {"X-Agent-Token": "jeton-de-test"}
    res = app.agent_client.post("/agent/poll", json=body, headers=ok)
    assert res.status_code == 200 and res.get_json()["managed"] is False and "gerard" in app.hub.devices
    assert app.agent_client.post("/agent/report", json={"name": "gerard", "actual_db": -18.5}, headers=ok).status_code == 200
    assert app.hub.live["gerard"]["actual_db"] == -18.5
    assert app.agent_client.post("/agent/poll", json={"name": "../x"}, headers=ok).status_code == 400


def test_agent_api_is_closed_without_a_token_file(app, tmp_path):
    (tmp_path / "agent.token").unlink()
    app._token_cache.update(mtime=None, value="")
    assert app.agent_client.post("/agent/poll", json={"name": "a"}, headers={"X-Agent-Token": ""}).status_code == 503


def test_ui_routes_are_not_served_on_the_agent_surface(app):
    ok = {"X-Agent-Token": "jeton-de-test"}
    for path in ("/", "/api/state", "/api/expo"):
        assert app.agent_client.get(path, headers=ok).status_code in (404, 405)
