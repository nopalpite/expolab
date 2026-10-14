"""Tests de ntp-admin : analyse de la sortie de chronyc, reglages du serveur de temps,
validation, redemarrage du conteneur (API Docker) - sans chrony ni Docker."""
import importlib.util
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("flask")

APP_PATH = Path(__file__).resolve().parent.parent / "ntp-admin" / "app.py"

# Sorties reelles de `chronyc -n -c ...` (champs separes par des virgules).
TRACKING_SYNCED = "A29FC87B,162.159.200.123,3,1792038898.847645441,-0.000001084,-0.000007688,0.000017590,1.025,0.010,0.065,0.0251,0.000614,64.7,Normal"
TRACKING_LOCAL = "7F7F0101,127.127.1.1,10,1792038898.000000000,0.000000000,0.000000000,0.000000000,0.000,0.000,0.000,0.0,0.0,0.0,Normal"
TRACKING_NONE = "00000000,,0,0.000000000,0.000000000,0.000000000,0.000000000,0.000,0.000,0.000,0.0,0.0,0.0,Not synchronised"
SOURCES = "\n".join([
    "^,*,162.159.200.123,3,6,377,43,-0.000099,-0.000186,0.017",
    "^,+,51.159.140.11,2,6,377,12,0.001500,0.001500,0.021",
    "^,?,203.0.113.9,0,10,0,-,0.000000,0.000000,0.000",
    "^,x,198.51.100.7,2,6,377,30,3.200000,3.200000,0.040",
    "#,*,127.127.1.1,10,4,377,5,0.000000,0.000000,0.000",
])
CLIENTS = "\n".join([
    "127.0.0.1,0,0,-,-,-,12,0,1,0",           # ntp-admin lui-meme : une commande, pas une requete d'heure
    "10.42.0.147,340,2,6,-,12,0,0,-,-",
    "10.42.0.148,12,0,8,-,0,0,0,-,-",         # vu a l'instant : 0 est une vraie valeur
    "10.42.0.150,5,0,-,-,-,0,0,-,-",          # derniere requete inconnue
    "10.42.0.149,80,0,6,-,3600,0,0,-,-",
])


@pytest.fixture
def ntp(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ntp_admin_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ntp_admin_app"] = module  # Flask retrouve templates/ et static/
    spec.loader.exec_module(module)
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setattr(module, "CONFIG_DIR", cfg)
    monkeypatch.setattr(module, "LEASES_FILE", tmp_path / "dnsmasq.leases")
    module.calls = []
    module.chronyc = {"tracking": TRACKING_SYNCED, "sources": SOURCES, "clients": CLIENTS}

    def fake_chronyc(*args, csv_output=True, timeout=6):
        module.calls.append(args)
        output = module.chronyc.get(args[0])
        if output is None:
            return False, "506 Cannot talk to daemon"
        return True, output + "\n"

    monkeypatch.setattr(module, "run_chronyc", fake_chronyc)
    module.docker = {"running": True, "restart_status": 204}

    def fake_docker(method, path, timeout=10):
        module.calls.append((method, path))
        if method == "GET":
            return 200, json.dumps({"State": {"Running": module.docker["running"]}}).encode()
        return module.docker["restart_status"], b'{"message": "boom"}'

    monkeypatch.setattr(module, "docker_request", fake_docker)
    module.app.testing = True
    module.client = module.app.test_client()
    module.cfg = cfg
    module.tmp = tmp_path
    return module


# --- analyse de chronyc ---------------------------------------------------------------------

def test_tracking_synced(ntp):
    t = ntp.parse_tracking(TRACKING_SYNCED)
    assert t["state"] == "synced" and t["stratum"] == 3 and t["ref_name"] == "162.159.200.123"
    assert t["offset_ms"] == 0.001 and t["ref_time"] == 1792038898 and t["update_interval_s"] == 64.7


def test_tracking_local_clock_is_not_a_real_synchronisation(ntp):
    # le serveur sert sa propre horloge (directive `local`) : ne pas le presenter comme synchronise
    assert ntp.parse_tracking(TRACKING_LOCAL)["state"] == "local"


def test_tracking_not_synchronised(ntp):
    t = ntp.parse_tracking(TRACKING_NONE)
    assert t["state"] == "none" and t["ref_time"] is None


@pytest.mark.parametrize("text", ["", "\n", "pas,assez,de,champs", "506 Cannot talk to daemon"])
def test_tracking_garbage_is_none(ntp, text):
    assert ntp.parse_tracking(text) is None


def test_sources(ntp):
    sources = ntp.parse_sources(SOURCES)
    assert [s["state"] for s in sources] == ["selected", "combined", "unreachable", "falseticker", "selected"]
    assert sources[0]["address"] == "162.159.200.123" and sources[0]["stratum"] == 3
    assert sources[0]["poll_s"] == 64 and sources[0]["last_rx_s"] == 43 and sources[0]["offset_ms"] == -0.099
    assert sources[2]["last_rx_s"] == 0                      # "-" : jamais de reponse
    assert sources[4]["kind"] == "local" and sources[0]["kind"] == "server"


def test_sources_ignore_malformed_lines(ntp):
    assert ntp.parse_sources("n,importe,quoi\n\n") == []


def test_clients_only_those_who_asked_for_the_time(ntp):
    names = {"10.42.0.147": "bobby", "10.42.0.148": "steph"}
    clients = ntp.parse_clients(CLIENTS, names)
    assert [c["ip"] for c in clients] == ["10.42.0.148", "10.42.0.147", "10.42.0.149", "10.42.0.150"]   # plus recents d'abord
    by_ip = {c["ip"]: c for c in clients}
    assert "127.0.0.1" not in by_ip
    assert by_ip["10.42.0.147"]["name"] == "bobby" and by_ip["10.42.0.147"]["requests"] == 340 and by_ip["10.42.0.147"]["dropped"] == 2
    assert by_ip["10.42.0.148"]["last_s"] == 0               # a l'instant
    assert by_ip["10.42.0.150"]["last_s"] is None and by_ip["10.42.0.150"]["name"] == ""


def test_leases_give_machine_names(ntp):
    (ntp.tmp / "dnsmasq.leases").write_text("1792038898 dc:a6:32:00:00:01 10.42.0.147 bobby 01:dc\n1 aa 10.42.0.9 * 01:aa\n")
    assert ntp.load_names() == {"10.42.0.147": "bobby"}
    assert ntp.load_names(ntp.tmp / "absent") == {}


# --- validation -------------------------------------------------------------------------------

@pytest.mark.parametrize("values", [["0.pool.ntp.org", "ntp.ubuntu.com", "192.168.1.1"], ["  a.fr  ", "", "b-c.fr"]])
def test_valid_upstream(ntp, values):
    clean, error = ntp.check_upstream(values)
    assert error is None and all(" " not in v for v in clean)


@pytest.mark.parametrize("bad", ["mauvais nom", "evil;rm", "$(x)", "-debut.fr", "fin-", "http://x.fr", "a/b", "é.fr", "x" * 300])
def test_invalid_upstream(ntp, bad):
    clean, error = ntp.check_upstream(["ok.fr", bad])
    assert clean is None and error


def test_upstream_is_deduplicated_and_limited(ntp):
    assert ntp.check_upstream(["a.fr", "a.fr"])[0] == ["a.fr"]
    assert ntp.check_upstream([f"h{i}.fr" for i in range(11)])[1]
    assert ntp.check_upstream("pas une liste")[1]


@pytest.mark.parametrize("value, expected", [("10.42.0.0/24", "10.42.0.0/24"), ("10.42.0.7", "10.42.0.7/32"), ("10.42.0.5/24", "10.42.0.0/24"), ("10.0.0.0/8", "10.0.0.0/8")])
def test_valid_allow(ntp, value, expected):
    assert ntp.check_allow([value]) == ([expected], None)


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "10.0.0.0/7", "128.0.0.0/1", "300.1.1.1", "10.0.0.0/33", "n'importe quoi", "fd00::/8", "10.0.0"])
def test_invalid_or_too_wide_allow(ntp, bad):
    # un /0 ouvrirait le serveur de temps a tout le reseau physique
    clean, error = ntp.check_allow([bad])
    assert clean is None and error


# --- reglages -------------------------------------------------------------------------------------

def test_defaults_when_nothing_is_configured(ntp):
    assert ntp.read_config() == {"mode": "online", "upstream": ntp.DEFAULT_UPSTREAM, "allow": ["10.42.0.0/24"], "local_fallback": True}


def test_saving_writes_one_file_per_setting(ntp):
    res = ntp.client.put("/api/config", json={"mode": "online", "upstream": ["ntp.ubuntu.com", "0.pool.ntp.org"], "allow": ["10.42.0.0/24", "10.66.66.0/24"], "local_fallback": False})
    assert res.status_code == 200
    assert (ntp.cfg / "mode").read_text() == "online\n"
    assert (ntp.cfg / "upstream.conf").read_text() == "ntp.ubuntu.com\n0.pool.ntp.org\n"
    assert (ntp.cfg / "allow.conf").read_text() == "10.42.0.0/24\n10.66.66.0/24\n"
    assert (ntp.cfg / "local-fallback").read_text() == "off\n"
    assert not list(ntp.cfg.glob(".*.tmp"))                       # ecriture atomique : aucun reste
    assert ntp.client.get("/api/config").get_json()["upstream"] == ["ntp.ubuntu.com", "0.pool.ntp.org"]
    assert not any(c[0] == "POST" for c in ntp.calls)               # enregistrer seul ne redemarre rien


def test_apply_restarts_the_container(ntp):
    res = ntp.client.put("/api/config", json={"mode": "offline", "apply": True})
    assert res.status_code == 200 and res.get_json()["applied"] is True
    assert ("POST", "/containers/expolab-ntp/restart?t=10") in ntp.calls
    assert (ntp.cfg / "mode").read_text() == "offline\n"


def test_failed_restart_is_reported_but_settings_are_kept(ntp):
    ntp.docker["restart_status"] = 500
    res = ntp.client.put("/api/config", json={"mode": "offline", "apply": True})
    assert res.status_code == 502 and "redemarrage a echoue" in res.get_json()["error"]
    assert (ntp.cfg / "mode").read_text() == "offline\n"


def test_offline_mode_does_not_need_upstream_servers(ntp):
    assert ntp.client.put("/api/config", json={"mode": "offline", "upstream": []}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "bizarre"},
        {"mode": "online", "upstream": []},                         # en ligne sans serveur : incoherent
        {"upstream": ["mauvais nom"]},
        {"allow": []},
        {"allow": ["0.0.0.0/0"]},
        {"allow": "10.42.0.0/24"},
    ],
)
def test_invalid_settings_are_refused_and_change_nothing(ntp, body):
    before = ntp.read_config()
    res = ntp.client.put("/api/config", json=body)
    assert res.status_code == 400 and res.get_json()["error"]
    assert ntp.read_config() == before and not list(ntp.cfg.iterdir())


def test_unknown_mode_in_file_falls_back_to_online(ntp):
    (ntp.cfg / "mode").write_text("bizarre\n")
    assert ntp.read_config()["mode"] == "online"


def test_comments_and_blank_lines_in_files_are_ignored(ntp):
    (ntp.cfg / "upstream.conf").write_text("# amont\n\nntp.ubuntu.com\n")
    assert ntp.read_config()["upstream"] == ["ntp.ubuntu.com"]


# --- etat --------------------------------------------------------------------------------------------

def test_status_assembles_everything(ntp):
    (ntp.tmp / "dnsmasq.leases").write_text("1 dc:a6 10.42.0.147 bobby 01:dc\n")
    data = ntp.client.get("/api/status").get_json()
    assert data["tracking"]["state"] == "synced" and data["container"] == {"running": True}
    assert len(data["sources"]) == 5 and data["error"] is None
    assert data["clients"][1]["name"] == "bobby"
    assert data["config"]["mode"] == "online" and data["now"] > 1_700_000_000


def test_status_when_chronyd_is_unreachable(ntp):
    ntp.chronyc.clear()
    ntp.docker["running"] = False
    data = ntp.client.get("/api/status").get_json()
    assert data["tracking"] is None and data["sources"] == [] and data["clients"] == []
    assert data["container"] == {"running": False} and "Cannot talk" in data["error"]


def test_makestep_and_restart_endpoints(ntp):
    ntp.chronyc["makestep"] = "200 OK"
    assert ntp.client.post("/api/makestep").get_json() == {"done": True, "output": "200 OK"}
    assert ntp.client.post("/api/restart").get_json() == {"restarted": True}
    ntp.chronyc.pop("makestep")
    assert ntp.client.post("/api/makestep").status_code == 502
    ntp.docker["restart_status"] = 500
    assert ntp.client.post("/api/restart").status_code == 502


def test_container_state_helpers(ntp, monkeypatch):
    assert ntp.container_running() is True
    ntp.docker["running"] = False
    assert ntp.container_running() is False
    monkeypatch.setattr(ntp, "docker_request", lambda *a, **k: (404, b""))
    assert ntp.container_running() is False                       # conteneur absent
    monkeypatch.setattr(ntp, "docker_request", lambda *a, **k: (500, b""))
    assert ntp.container_running() is None

    def boom(*a, **k):
        raise FileNotFoundError("docker.sock")

    monkeypatch.setattr(ntp, "docker_request", boom)
    assert ntp.container_running() is None and ntp.restart_container()[0] is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt", reason="sockets Unix indisponibles")
def test_docker_api_over_a_unix_socket(ntp, tmp_path, monkeypatch):
    # le vrai chemin de code : requete HTTP a travers une socket Unix, comme avec /var/run/docker.sock
    path = str(tmp_path / "docker.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    server.settimeout(5)   # un echec de connexion ne doit jamais bloquer le test
    seen = {}

    def serve():
        conn, _ = server.accept()
        seen["request"] = conn.recv(4096).decode().splitlines()[0]
        conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    # La fixture a remplace docker_request par un faux : on charge une copie NEUVE du module (la vraie
    # fonction) et c'est SA socket qu'on redirige. Sans cela le test viserait le vrai /var/run/docker.sock
    # de la machine (present sur un runner CI : reponse 404 d'un vrai Docker).
    real = load_real_module()
    real.DOCKER_SOCKET = path
    status, _body = real.docker_request("POST", "/containers/expolab-ntp/restart?t=10")
    thread.join(5)
    server.close()
    assert status == 204 and seen["request"] == "POST /containers/expolab-ntp/restart?t=10 HTTP/1.1"
    # et le chemin complet : un 204 vaut "redemarre" pour restart_container
    assert real.DOCKER_SOCKET == path


def load_real_module():
    spec = importlib.util.spec_from_file_location("ntp_admin_real", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ntp_admin_real"] = module
    spec.loader.exec_module(module)
    return module


# --- sortie reelle de chrony 4.x releve sur le Pi (reference IPv6 : des deux-points dans les adresses) ---------

REAL_TRACKING = "65A54582,2a01:cb00:129e:a803::101,3,1792011770.620822358,-0.000054924,-0.000083982,0.000360748,7.492,-0.008,0.374,0.016245848,0.001578484,1026.2,Normal"
REAL_SOURCES = "\n".join([
    "^,-,2602:f590::23:161:104:133,2,10,377,768,0.006499388,0.006499388,0.023963086",
    "^,*,2a01:cb00:129e:a803::101,2,10,377,791,-0.000806448,-0.000890429,0.008138250",
    "^,+,2603:c020:8017:3e00:123:123:123:123,2,10,377,419,0.000087811,0.000087811,0.012054270",
])


def test_real_chrony_output(ntp):
    t = ntp.parse_tracking(REAL_TRACKING)
    assert t["state"] == "synced" and t["ref_name"] == "2a01:cb00:129e:a803::101" and t["stratum"] == 3
    assert t["offset_ms"] == 0.055 and t["rms_offset_ms"] == 0.361 and t["update_interval_s"] == 1026.2
    sources = ntp.parse_sources(REAL_SOURCES)
    assert [s["state"] for s in sources] == ["excluded", "selected", "combined"]
    assert sources[1]["address"] == "2a01:cb00:129e:a803::101" and sources[1]["poll_s"] == 1024
    assert ntp.parse_clients("", {}) == []     # aucun client n'a encore demande l'heure : sortie vide
