"""Tests de dnsmasq-admin : reservations par MAC ET par nom, liberation de bail."""
import importlib.util
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

APP_PATH = Path(__file__).resolve().parent.parent / "dnsmasq-admin" / "app.py"
spec = importlib.util.spec_from_file_location("dnsmasq_admin_app", APP_PATH)
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)

MAC_A = "dc:a6:32:00:99:01"


def test_parse_mac_reservation_with_and_without_hostname():
    assert da.parse_reservation_line(f"{MAC_A},10.42.0.60,pi-03") == {"mac": MAC_A, "ip": "10.42.0.60", "hostname": "pi-03"}
    assert da.parse_reservation_line(f"{MAC_A},10.42.0.60") == {"mac": MAC_A, "ip": "10.42.0.60", "hostname": ""}


def test_parse_name_only_reservation():
    assert da.parse_reservation_line("pi-03,10.42.0.60") == {"mac": "", "ip": "10.42.0.60", "hostname": "pi-03"}


def test_parse_ignores_comments_blank_and_malformed_lines():
    for line in ("", "   ", "# commentaire", "pas-d-ip"):
        assert da.parse_reservation_line(line) is None


def test_format_roundtrip():
    for line in (f"{MAC_A},10.42.0.60,pi-03", f"{MAC_A},10.42.0.60", "pi-03,10.42.0.60"):
        assert da.format_reservation_line(da.parse_reservation_line(line)) == line


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "RESERVATIONS_PATH", tmp_path / "reservations.conf")
    monkeypatch.setattr(da, "LEASES_PATH", tmp_path / "dnsmasq.leases")
    reloads = []
    monkeypatch.setattr(da, "reload_dnsmasq", lambda: (reloads.append(1) or (True, "")))
    da.app.config["TESTING"] = True
    test_client = da.app.test_client()
    test_client.reloads = reloads
    test_client.tmp = tmp_path
    return test_client


def write_leases(client, *lines):
    (client.tmp / "dnsmasq.leases").write_text("\n".join(lines) + "\n")


def future():
    return int(time.time()) + 3600


def test_create_name_only_reservation(client):
    response = client.post("/api/reservations", json={"ip": "10.42.0.60", "hostname": "pi-03"})
    assert response.status_code == 201
    assert (client.tmp / "reservations.conf").read_text() == "pi-03,10.42.0.60\n"
    assert client.reloads == [1]


def test_create_requires_mac_or_hostname(client):
    response = client.post("/api/reservations", json={"ip": "10.42.0.60"})
    assert response.status_code == 400
    assert "MAC ou un hostname" in response.get_json()["error"]


def test_hostname_and_ip_must_be_unique(client):
    client.post("/api/reservations", json={"ip": "10.42.0.60", "hostname": "pi-03"})
    assert client.post("/api/reservations", json={"ip": "10.42.0.61", "hostname": "pi-03"}).status_code == 409
    assert client.post("/api/reservations", json={"ip": "10.42.0.60", "hostname": "pi-04"}).status_code == 409
    # une reservation par MAC ne peut pas non plus reprendre un nom deja reserve
    response = client.post("/api/reservations", json={"mac": MAC_A, "ip": "10.42.0.62", "hostname": "pi-03"})
    assert response.status_code == 409


def test_update_and_delete_name_only_reservation(client):
    client.post("/api/reservations", json={"ip": "10.42.0.60", "hostname": "pi-03"})
    assert client.put("/api/reservations/pi-03", json={"ip": "10.42.0.70", "hostname": "autre"}).status_code == 200
    # le nom est la cle d'une reservation par nom : il ne change pas
    assert (client.tmp / "reservations.conf").read_text() == "pi-03,10.42.0.70\n"
    assert client.delete("/api/reservations/pi-03").status_code == 200
    assert (client.tmp / "reservations.conf").read_text() == ""
    assert client.delete("/api/reservations/pi-03").status_code == 404


def test_mac_reservations_keep_working(client):
    assert client.post("/api/reservations", json={"mac": MAC_A, "ip": "10.42.0.60", "hostname": "pi-03"}).status_code == 201
    assert client.post("/api/reservations", json={"mac": MAC_A, "ip": "10.42.0.61"}).status_code == 409
    assert client.put(f"/api/reservations/{MAC_A}", json={"ip": "10.42.0.65", "hostname": "pi-03"}).status_code == 200
    listing = client.get("/api/reservations").get_json()["reservations"]
    assert listing[0]["id"] == MAC_A and listing[0]["ip"] == "10.42.0.65"


def test_pending_renewal_matches_name_only_reservation_by_hostname(client):
    client.post("/api/reservations", json={"ip": "10.42.0.60", "hostname": "pi-03"})
    write_leases(client, f"{future()} dc:a6:32:00:99:02 10.42.0.127 pi-03 01:dc:a6:32:00:99:02")
    reservation = client.get("/api/reservations").get_json()["reservations"][0]
    assert reservation["pending_renewal"] is True


def test_release_passes_client_id_to_dhcp_release(client, monkeypatch):
    write_leases(client, f"{future()} {MAC_A} 10.42.0.60 pi-03 01:{MAC_A}")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(da.subprocess, "run", fake_run)
    response = client.post(f"/api/leases/{MAC_A}/release")
    assert response.status_code == 200
    assert response.get_json()["ip"] == "10.42.0.60"
    assert calls == [["docker", "exec", "expolab-dnsmasq", "dhcp_release", "expo-lan", "10.42.0.60", MAC_A, f"01:{MAC_A}"]]


def test_release_without_client_id_and_errors(client, monkeypatch):
    write_leases(client, f"{future()} {MAC_A} 10.42.0.60 pi-03 *")
    calls = []
    monkeypatch.setattr(
        da.subprocess, "run", lambda cmd, **kw: (calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, "", "boom"))
    )
    response = client.post(f"/api/leases/{MAC_A}/release")
    assert response.status_code == 502 and "boom" in response.get_json()["error"]
    assert calls[0][-1] == MAC_A  # pas d'identifiant client quand le bail n'en a pas

    assert client.post("/api/leases/dc:a6:32:00:99:09/release").status_code == 404
    assert client.post("/api/leases/pas-une-mac/release").status_code == 400


# --- liberation automatique des baux orphelins (reconcile_once) -------------

MAC_OLD = "dc:a6:32:00:99:0a"
MAC_NEW = "dc:a6:32:00:99:0b"


def lease(mac, ip, hostname):
    return {"mac": mac, "ip": ip, "hostname": hostname, "client_id": "", "expires_in_min": 600}


class Lab:
    """Etat simule : reservations, baux, machines qui repondent en ARP."""

    def __init__(self, monkeypatch, reservations, leases, alive):
        self.leases = leases
        self.alive = alive  # ip -> True/False/None
        self.released = []
        monkeypatch.setattr(da, "load_reservations", lambda: reservations)
        monkeypatch.setattr(da, "load_leases", lambda: self.leases)
        monkeypatch.setattr(da, "host_alive", lambda ip: self.alive.get(ip))
        monkeypatch.setattr(da, "release_lease", self.release)
        da._events.clear()

    def release(self, lease):
        self.released.append(lease["mac"])
        self.leases = [l for l in self.leases if l["mac"] != lease["mac"]]
        return True, ""


NAME_RESERVATION = [{"mac": "", "ip": "10.42.0.60", "hostname": "pi-03"}]


def test_dead_holder_released_after_three_failed_checks(monkeypatch):
    lab = Lab(monkeypatch, NAME_RESERVATION, [lease(MAC_OLD, "10.42.0.60", "pi-03")], {"10.42.0.60": False})
    misses = {}
    da.reconcile_once(misses)
    da.reconcile_once(misses)
    assert lab.released == []
    da.reconcile_once(misses)
    assert lab.released == [MAC_OLD]


def test_alive_holder_is_never_released_and_counter_resets(monkeypatch):
    lab = Lab(monkeypatch, NAME_RESERVATION, [lease(MAC_OLD, "10.42.0.60", "pi-03")], {"10.42.0.60": False})
    misses = {}
    da.reconcile_once(misses)
    da.reconcile_once(misses)
    lab.alive["10.42.0.60"] = True  # la machine repond a nouveau
    for _ in range(5):
        da.reconcile_once(misses)
    assert lab.released == []
    assert "10.42.0.60" not in misses


def test_unknown_liveness_never_releases(monkeypatch):
    lab = Lab(monkeypatch, NAME_RESERVATION, [lease(MAC_OLD, "10.42.0.60", "pi-03")], {"10.42.0.60": None})
    misses = {}
    for _ in range(6):
        da.reconcile_once(misses)
    assert lab.released == []


def test_new_board_on_fallback_ip_releases_dead_and_new_leases(monkeypatch):
    # carte neuve branchee avant la liberation : nom reserve, mais IP de repli
    leases = [lease(MAC_OLD, "10.42.0.60", ""), lease(MAC_NEW, "10.42.0.130", "pi-03")]
    lab = Lab(monkeypatch, NAME_RESERVATION, leases, {"10.42.0.60": False})
    da.reconcile_once({})
    assert sorted(lab.released) == sorted([MAC_OLD, MAC_NEW])
    assert [e["kind"] for e in da._events] == ["liberation", "liberation"]


def test_new_board_with_live_holder_is_a_conflict_and_nothing_is_released(monkeypatch):
    leases = [lease(MAC_OLD, "10.42.0.60", ""), lease(MAC_NEW, "10.42.0.130", "pi-03")]
    lab = Lab(monkeypatch, NAME_RESERVATION, leases, {"10.42.0.60": True})
    misses = {}
    da.reconcile_once(misses)
    da.reconcile_once(misses)
    assert lab.released == []
    assert [e["kind"] for e in da._events] == ["conflit"]  # signale une seule fois


def test_new_board_on_fallback_ip_with_free_reserved_ip_releases_only_its_own(monkeypatch):
    lab = Lab(monkeypatch, NAME_RESERVATION, [lease(MAC_NEW, "10.42.0.130", "pi-03")], {})
    da.reconcile_once({})
    assert lab.released == [MAC_NEW]


def test_machine_on_its_reserved_ip_is_left_alone(monkeypatch):
    lab = Lab(monkeypatch, NAME_RESERVATION, [lease(MAC_NEW, "10.42.0.60", "pi-03")], {"10.42.0.60": True})
    da.reconcile_once({})
    assert lab.released == []


def test_mac_reservations_are_not_touched(monkeypatch):
    reservations = [{"mac": MAC_OLD, "ip": "10.42.0.60", "hostname": "pi-03"}]
    lab = Lab(monkeypatch, reservations, [lease(MAC_OLD, "10.42.0.60", "pi-03")], {"10.42.0.60": False})
    misses = {}
    for _ in range(5):
        da.reconcile_once(misses)
    assert lab.released == []


def test_host_alive_distinguishes_dead_host_from_missing_tool(monkeypatch):
    def fake_run(returncode, stderr=""):
        return lambda *a, **kw: subprocess.CompletedProcess(a, returncode, "", stderr)

    monkeypatch.setattr(da.subprocess, "run", fake_run(0))
    assert da.host_alive("10.42.0.60") is True
    monkeypatch.setattr(da.subprocess, "run", fake_run(1))
    assert da.host_alive("10.42.0.60") is False
    monkeypatch.setattr(da.subprocess, "run", fake_run(127, "executable file not found"))
    assert da.host_alive("10.42.0.60") is None
