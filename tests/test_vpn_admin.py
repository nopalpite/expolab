"""Tests de l'adresse publique du VPN dans vpn-admin (Endpoint des configs clientes)."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")

APP_PATH = Path(__file__).resolve().parent.parent / "vpn-admin" / "app.py"

PEER_CONF = """[Interface]
PrivateKey = CLE-PRIVEE-NE-DOIT-PAS-CHANGER
Address = 10.66.66.2/32
DNS = 10.42.0.1

[Peer]
PublicKey = CLE-PUBLIQUE
PresharedKey = CLE-PREPARTAGEE
Endpoint = 192.168.1.77:51820
AllowedIPs = 10.42.0.0/24, 10.66.66.0/24
PersistentKeepalive = 25
"""


@pytest.fixture
def vpn(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("vpn_admin_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vpn_admin_app"] = module  # Flask retrouve templates/ et static/
    spec.loader.exec_module(module)
    wg = tmp_path / "wireguard" / "config"
    (wg / "peers").mkdir(parents=True)
    (wg / "peers" / "default.conf").write_text(PEER_CONF)
    (wg / "peers" / "mon-laptop.conf").write_text(PEER_CONF.replace("10.66.66.2", "10.66.66.3"))
    monkeypatch.setattr(module, "WG_DIR", wg)
    monkeypatch.setattr(module, "PEERS_DIR", wg / "peers")
    monkeypatch.setattr(module, "ENDPOINT_FILE", wg / "public-endpoint")
    module.app.testing = True
    module.wg = wg
    module.client = module.app.test_client()
    return module


# --- analyse de l'adresse ----------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("82.230.154.153:51820", "82.230.154.153:51820"),
        ("vpn.exemple.fr:51820", "vpn.exemple.fr:51820"),
        ("vpn.exemple.fr", "vpn.exemple.fr:51820"),          # port par defaut
        ("  VPN.Exemple.fr:443 ", "VPN.Exemple.fr:443"),
        ("82.230.154.153", "82.230.154.153:51820"),
        ("a.b-c.example.org:65535", "a.b-c.example.org:65535"),
    ],
)
def test_valid_endpoints_are_normalised(vpn, value, expected):
    assert vpn.parse_endpoint(value) == (expected, None)


@pytest.mark.parametrize(
    "value",
    ["vpn.exemple.fr:0", "vpn.exemple.fr:65536", "vpn.exemple.fr:abc", "vpn.exemple.fr:", "999.1.1.1", "1.2.3",
     "-vpn.fr", "vpn fr", "vpn$(x).fr", "http://vpn.fr", "vpn.fr:51820:1", ":51820", "exemple..fr"],
)
def test_invalid_endpoints_are_refused(vpn, value):
    endpoint, error = vpn.parse_endpoint(value)
    assert endpoint is None and error


def test_local_addresses_are_valid_but_flagged(vpn):
    for local in ("192.168.1.77:51820", "10.0.0.5:51820", "172.20.0.1:51820", "127.0.0.1:51820", "pi.lan:51820", "raspberrypi:51820"):
        assert vpn.endpoint_warning(local), local
    for public in ("82.230.154.153:51820", "vpn.exemple.fr:51820"):
        assert vpn.endpoint_warning(public) is None, public


# --- API ---------------------------------------------------------------------------

def test_default_is_empty_until_configured(vpn):
    assert vpn.client.get("/api/endpoint").get_json() == {"endpoint": "", "warning": None}


def test_saving_writes_the_file_add_peer_reads(vpn):
    res = vpn.client.put("/api/endpoint", json={"endpoint": "vpn.exemple.fr"})
    assert res.status_code == 200 and res.get_json()["endpoint"] == "vpn.exemple.fr:51820"
    assert (vpn.wg / "public-endpoint").read_text() == "vpn.exemple.fr:51820\n"
    assert vpn.client.get("/api/endpoint").get_json()["endpoint"] == "vpn.exemple.fr:51820"


def test_saving_does_not_touch_existing_peers_unless_asked(vpn):
    vpn.client.put("/api/endpoint", json={"endpoint": "vpn.exemple.fr:51820"})
    assert "Endpoint = 192.168.1.77:51820" in (vpn.wg / "peers" / "default.conf").read_text()


def test_updating_existing_peers_only_changes_the_endpoint_line(vpn):
    res = vpn.client.put("/api/endpoint", json={"endpoint": "82.230.154.153:51820", "update_existing": True})
    assert res.get_json()["updated"] == ["default", "mon-laptop"]
    after = (vpn.wg / "peers" / "default.conf").read_text()
    assert "Endpoint = 82.230.154.153:51820" in after and "192.168.1.77" not in after
    # tout le reste de la config (cles, adresse, DNS, routes) est intact
    assert after.replace("82.230.154.153:51820", "192.168.1.77:51820") == PEER_CONF


def test_invalid_endpoint_is_refused_and_changes_nothing(vpn):
    vpn.client.put("/api/endpoint", json={"endpoint": "vpn.exemple.fr"})
    res = vpn.client.put("/api/endpoint", json={"endpoint": "pas valide!", "update_existing": True})
    assert res.status_code == 400 and res.get_json()["error"]
    assert (vpn.wg / "public-endpoint").read_text() == "vpn.exemple.fr:51820\n"
    assert "192.168.1.77:51820" in (vpn.wg / "peers" / "default.conf").read_text()


def test_a_local_address_is_saved_with_a_warning(vpn):
    data = vpn.client.put("/api/endpoint", json={"endpoint": "192.168.1.77:51820"}).get_json()
    assert data["endpoint"] == "192.168.1.77:51820" and "reseau local" in data["warning"]


def test_empty_value_removes_the_setting(vpn):
    vpn.client.put("/api/endpoint", json={"endpoint": "vpn.exemple.fr"})
    res = vpn.client.put("/api/endpoint", json={"endpoint": "  "})
    assert res.status_code == 200 and res.get_json()["endpoint"] == ""
    assert not (vpn.wg / "public-endpoint").exists()
    assert vpn.client.put("/api/endpoint", json={"endpoint": ""}).status_code == 200  # deja absent : pas d'erreur
