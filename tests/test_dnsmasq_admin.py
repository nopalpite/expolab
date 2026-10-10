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
