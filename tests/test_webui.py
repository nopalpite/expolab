"""Tests de la webui : pas de suppression/arret d'une machine en cours de creation."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("yaml")

APP_PATH = Path(__file__).resolve().parent.parent / "webui" / "app.py"
spec = importlib.util.spec_from_file_location("webui_app", APP_PATH)
webui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webui)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "FLEET_DIR", tmp_path)
    monkeypatch.setattr(webui, "INVENTORY_PATH", tmp_path / "inventory.yaml")
    webui.save_inventory({"fleet": [{"name": "bobby", "mac": "dc:a6:32:00:00:01", "role": "sans-ecran"}]})
    started = []
    monkeypatch.setattr(webui, "start_job", lambda *a, **kw: started.append((a, kw)))
    webui._jobs.clear()
    client = webui.app.test_client()
    client.started = started
    yield client
    webui._jobs.clear()


def running_create(name):
    webui._jobs[f"create-{name}-1"] = {"status": "running", "log": "", "kind": "create", "name": name}


def test_delete_refused_while_creating(client):
    running_create("bobby")
    res = client.delete("/api/fleet/bobby")
    assert res.status_code == 409
    assert "en cours de creation" in res.get_json()["error"]
    assert client.started == []
    # l'inventaire n'a pas ete modifie
    assert [pi["name"] for pi in webui.load_inventory()["fleet"]] == ["bobby"]


@pytest.mark.parametrize("action", ["start", "stop"])
def test_start_stop_refused_while_creating(client, action):
    running_create("bobby")
    assert client.post(f"/api/fleet/bobby/{action}").status_code == 409
    assert client.started == []


def test_delete_allowed_once_creation_finished(client):
    webui._jobs["create-bobby-1"] = {"status": "done", "log": "", "kind": "create", "name": "bobby"}
    assert client.delete("/api/fleet/bobby").status_code == 202
    assert webui.load_inventory()["fleet"] == []


def test_creation_of_another_machine_does_not_block(client):
    running_create("autre")
    assert client.delete("/api/fleet/bobby").status_code == 202
