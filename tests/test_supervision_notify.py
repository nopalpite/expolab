"""Canaux d'alerte de la supervision (supervision-admin/notify.py) : URL Apprise de chaque type, secrets jamais
renvoyes, niveaux par canal, un message par passage, colonne « Prévenus », silence de nuit et recapitulatif."""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("yaml")
pytest.importorskip("flask")

SRC = Path(__file__).resolve().parent.parent / "supervision-admin"
sys.path.insert(0, str(SRC))
import checks as C  # noqa: E402
import notify as N  # noqa: E402
from history import History  # noqa: E402
from supervisor import Event  # noqa: E402

TELEGRAM = {"token": "123456:ABC-def_1", "chat_id": "-100123"}


def at(hour, minute=0, day=27):
    return datetime(2026, 10, day, hour, minute).timestamp()


class Outbox:
    def __init__(self):
        self.sent, self.fail = [], {}

    def __call__(self, url, title, body):
        self.sent.append((url, title, body))
        return (False, self.fail[url]) if url in self.fail else (True, "")


@pytest.fixture
def env(tmp_path):
    now, outbox = [at(14)], Outbox()
    history = History(tmp_path / "s.db")
    return N.Notifier(history, send=outbox, clock=lambda: now[0], background=False), outbox, now, history


def event(history, machine="rpi-test", label="Carte son USB", level=C.ALERT, kind="opened", now=0.0):
    check = C.Check("audio_usb", label, level, "aucune carte son USB branchée")
    incident_id = history.open_incident(machine, check, now)
    return Event(kind, machine, label, level, check.detail, now, incident_id)


# --- URL de chaque type -----------------------------------------------------------------------

def test_each_kind_builds_its_apprise_url():
    assert N.build_url("telegram", TELEGRAM) == "tgram://123456:ABC-def_1/-100123"
    hook = "https://discord.com/api/webhooks/1/abc"
    assert N.build_url("discord", {"webhook": hook}) == hook
    url = N.build_url("mailjet", {"api_key": "K", "secret_key": "S/+", "sender": "alertes@expolab.fr",
                                  "recipients": "regie@musee.fr, astreinte@musee.fr"})
    assert url.startswith("mailtos://expolab.fr?smtp=in-v3.mailjet.com&port=587&mode=starttls&user=K&pass=S%2F%2B")
    assert "to=regie%40musee.fr%2Castreinte%40musee.fr" in url


@pytest.mark.parametrize("kind, fields", [
    ("telegram", {"token": "pas-un-jeton", "chat_id": "1"}),
    ("telegram", {"token": "1:a", "chat_id": "abc"}),
    ("discord", {"webhook": "https://evil.example/api/webhooks/1"}),
    ("slack", {"webhook": "https://discord.com/api/webhooks/1/a"}),
    ("mailjet", {"api_key": "K", "secret_key": "S", "sender": "pas une adresse", "recipients": "a@b.fr"}),
    ("inconnu", {}),
])
def test_bad_fields_are_refused(kind, fields):
    with pytest.raises(N.ChannelError):
        N.build_url(kind, fields)


def test_mailjet_and_other_urls_are_understood_by_apprise():
    apprise = pytest.importorskip("apprise")
    sender = apprise.Apprise()
    assert sender.add(N.build_url("mailjet", {"api_key": "K", "secret_key": "S", "sender": "a@expolab.fr", "recipients": "b@x.fr"}))
    assert sender[0].smtp_host == "in-v3.mailjet.com" and sender[0].port == 587
    assert N.build_url("apprise", {"url": "ntfy://ntfy.sh/expolab"}) == "ntfy://ntfy.sh/expolab"
    with pytest.raises(N.ChannelError):
        N.build_url("apprise", {"url": "zzz://rien"})


# --- canaux -----------------------------------------------------------------------------------

def test_secrets_never_come_back_and_are_kept_when_left_empty(env):
    notifier, outbox, now, history = env
    cid = notifier.save_channel({"kind": "telegram", "name": "Régie", "fields": TELEGRAM, "levels": [C.ALERT]})["id"]

    [view] = notifier.channels_view()
    assert view["fields"] == {"token": True, "chat_id": "-100123"} and "url" not in view
    assert "ABC-def_1" not in repr(view)

    notifier.save_channel({"id": cid, "name": "Régie 2", "fields": {"token": ""}, "levels": [C.ALERT, C.WARNING], "enabled": False})
    [channel] = history.get("channels")
    assert (channel["name"], channel["fields"], channel["enabled"]) == ("Régie 2", TELEGRAM, False)
    assert channel["levels"] == [C.ALERT, C.WARNING]


def test_channel_needs_a_level_and_known_id(env):
    notifier, *_ = env
    with pytest.raises(N.ChannelError):
        notifier.save_channel({"kind": "telegram", "fields": TELEGRAM, "levels": []})
    with pytest.raises(N.ChannelError):
        notifier.save_channel({"id": "nope", "fields": TELEGRAM, "levels": [C.ALERT]})
    with pytest.raises(N.ChannelError):
        notifier.delete_channel("nope")


def test_test_message_records_the_last_send(env):
    notifier, outbox, now, history = env
    cid = notifier.save_channel({"kind": "telegram", "fields": TELEGRAM, "levels": [C.ALERT]})["id"]
    outbox.fail["tgram://123456:ABC-def_1/-100123"] = "Unauthorized"

    assert notifier.test_channel(cid) == {"at": now[0], "ok": False, "error": "Unauthorized"}
    assert notifier.channels_view()[0]["last"]["error"] == "Unauthorized"


def test_events_go_to_channels_by_level_in_one_message_and_are_noted(env):
    notifier, outbox, now, history = env
    notifier.save_channel({"kind": "telegram", "name": "Régie", "fields": TELEGRAM, "levels": [C.ALERT]})
    notifier.save_channel({"kind": "discord", "name": "Équipe", "fields": {"webhook": "https://discord.com/api/webhooks/1/a"},
                           "levels": [C.ALERT, C.WARNING]})
    notifier.save_channel({"kind": "slack", "name": "Coupé", "fields": {"webhook": "https://hooks.slack.com/services/T/B/x"},
                           "levels": [C.ALERT], "enabled": False})
    alert = event(history)
    warn = event(history, "borne", "Température", C.WARNING)

    notifier.after_tick([alert, warn], expo_open=True)

    assert [(url.split(":")[0], title) for url, title, _ in outbox.sent] == [
        ("tgram", "🔴 Alerte · rpi-test"), ("https", "Supervision expolab : 2 changements")]
    assert outbox.sent[1][2] == "🔴 rpi-test · Carte son USB : aucune carte son USB branchée\n🟠 borne · Température : aucune carte son USB branchée"
    notified = {i["machine"]: i["notified"] for i in history.open_incidents()}
    assert notified == {"rpi-test": "Régie, Équipe", "borne": "Équipe"}


def test_failed_send_is_not_noted_as_notified(env):
    notifier, outbox, now, history = env
    notifier.save_channel({"kind": "telegram", "name": "Régie", "fields": TELEGRAM, "levels": [C.ALERT]})
    outbox.fail["tgram://123456:ABC-def_1/-100123"] = "timeout"
    notifier.after_tick([event(history)], expo_open=True)
    assert history.open_incidents()[0]["notified"] == ""


def test_resolved_message():
    title, body = N.message([Event("resolved", "rpi-test", "Carte son USB", C.ALERT, "UGREEN", 0, 1)])
    assert (title, body) == ("✅ Rétabli · rpi-test", "✅ rpi-test · Carte son USB : rétabli")


# --- nuit -------------------------------------------------------------------------------------

def test_night_wraps_midnight_and_is_validated(env):
    notifier, *_ = env
    night = notifier.night()
    assert [notifier.is_night(datetime.fromtimestamp(at(h, m)), night) for h, m in ((18, 59), (19, 0), (2, 0), (7, 59), (8, 0))] == \
        [False, True, True, True, False]
    assert not notifier.is_night(datetime.fromtimestamp(at(2)), {**night, "enabled": False})
    assert notifier.set_night({"start": "22:30"})["start"] == "22:30"
    for bad in ({"start": "25:00"}, {"end": "8h"}, {"enabled": "oui"}, {"start": "08:00"}):
        with pytest.raises(N.ChannelError):
            notifier.set_night(bad)


def test_night_is_silent_then_one_recap_in_the_morning(env):
    notifier, outbox, now, history = env
    notifier.save_channel({"kind": "telegram", "name": "Régie", "fields": TELEGRAM, "levels": [C.ALERT]})
    now[0] = at(23)
    notifier.after_tick([event(history, now=at(23))], expo_open=True)
    gone = event(history, "borne", "Température", C.WARNING, now=at(1, day=28))
    history.close_incident(gone.incident_id, at(2, day=28), "rétabli")
    now[0] = at(7, 59, day=28)
    notifier.after_tick([], expo_open=True)
    assert outbox.sent == [] and history.open_incidents()[0]["notified"] == "silence de nuit"

    now[0] = at(8, 0, day=28)
    notifier.after_tick([], expo_open=True)
    now[0] = at(8, 30, day=28)
    notifier.after_tick([], expo_open=True)                          # une seule fois par jour

    [(_, title, body)] = outbox.sent
    assert title == "Supervision expolab : récapitulatif de la nuit"
    assert body.splitlines() == ["Cette nuit : 2 problème(s) apparu(s), 1 rétabli(s).", "En cours :",
                                 "🔴 rpi-test · Carte son USB : aucune carte son USB branchée"]


def test_no_recap_when_the_expo_is_closed_or_late(env):
    notifier, outbox, now, history = env
    notifier.save_channel({"kind": "telegram", "fields": TELEGRAM, "levels": [C.ALERT]})
    now[0] = at(8, 5, day=28)
    notifier.after_tick([], expo_open=False)
    now[0] = at(10, 0, day=29)
    notifier.after_tick([], expo_open=True)
    assert outbox.sent == []

    now[0] = at(8, 1, day=30)
    notifier.after_tick([], expo_open=True)
    assert outbox.sent[0][2].splitlines() == ["Cette nuit : rien à signaler.", "Aucun problème en cours."]


# --- API --------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    spec = importlib.util.spec_from_file_location("supervision_app_notify", SRC / "app.py")
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    outbox = Outbox()
    app_module.create(tmp_path, machines_source=lambda: [], fetch=lambda host: None, send=outbox, background=False)
    return app_module.app.test_client(), outbox


H = {"X-Requested-With": "expolab"}


def test_api_channels_and_night(client):
    client, outbox = client
    assert client.get("/").status_code == 200
    kinds = client.get("/api/channels").get_json()["kinds"]
    assert [k["id"] for k in kinds] == ["telegram", "discord", "slack", "mailjet", "apprise"]

    assert client.post("/api/channels", json={"kind": "telegram", "fields": TELEGRAM, "levels": [C.ALERT]}).status_code == 403
    cid = client.post("/api/channels", json={"kind": "telegram", "fields": TELEGRAM, "levels": [C.ALERT]}, headers=H).get_json()["id"]
    assert client.post("/api/channels", json={"kind": "telegram", "fields": {}, "levels": [C.ALERT]}, headers=H).status_code == 400
    assert client.post(f"/api/channels/{cid}/test", headers=H).get_json()["last"]["ok"] is True
    assert outbox.sent[0][1] == "Supervision expolab : test"
    assert "ABC-def_1" not in client.get("/api/channels").get_data(as_text=True)

    assert client.post("/api/night", json={"start": "20:00"}, headers=H).get_json()["night"]["start"] == "20:00"
    assert client.post("/api/night", json={"start": "nuit"}, headers=H).status_code == 400
    assert client.delete(f"/api/channels/{cid}", headers=H).status_code == 200
    assert client.get("/api/channels").get_json()["channels"] == []
