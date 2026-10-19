"""Tests d'ansible-web : cibles (tags + machines) et validation de --limit."""
import importlib.util
import re
from pathlib import Path

import pytest

pytest.importorskip("flask")

APP_PATH = Path(__file__).resolve().parent.parent / "ansible-web" / "app.py"
spec = importlib.util.spec_from_file_location("ansible_web_app", APP_PATH)
aw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aw)


def group_name(tag):
    # meme normalisation que bastion_inventory.group_name (depot bastion-ansible)
    return re.sub(r"[^A-Za-z0-9_]", "_", tag).strip("_") or "sans_tag"


MACHINES = [
    {"name": "pi-02", "host": "pi-02.expolab.lan", "tags": ["gpio", "ecran-tactile"]},
    {"name": "pi-01", "host": "10.42.0.147", "tags": ["desktop"]},
    {"name": "pi-03", "host": "10.42.0.149", "tags": []},
]


def test_build_targets_lists_tags_and_machines():
    targets = aw.build_targets(MACHINES, group_name, {"desktop", "gpio"})
    assert [t["name"] for t in targets["tags"]] == ["desktop", "ecran-tactile", "gpio"]
    by_name = {t["name"]: t for t in targets["tags"]}
    assert by_name["ecran-tactile"]["role"] == "ecran_tactile"
    assert by_name["gpio"]["has_role"] is True
    assert by_name["ecran-tactile"]["has_role"] is False
    # machines triees par nom, tags exprimes en noms de groupe
    assert [m["name"] for m in targets["machines"]] == ["pi-01", "pi-02", "pi-03"]
    assert targets["machines"][1]["tags"] == ["ecran_tactile", "gpio"]


def test_allowed_limits_are_groups_and_hosts_only():
    targets = aw.build_targets(MACHINES, group_name, set())
    allowed = aw.allowed_limits(targets)
    assert {"desktop", "gpio", "ecran_tactile", "pi-01", "pi-03"} <= allowed
    # le tag brut (avec tiret) n'est PAS un groupe Ansible valide
    assert "ecran-tactile" not in allowed
    assert "all" not in allowed
    assert "pi-*" not in allowed


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(aw, "RUNS_INDEX", tmp_path / "index.yaml")
    started = []
    monkeypatch.setattr(aw, "execute_run", lambda run_id, limit: started.append((run_id, limit)))
    targets = aw.build_targets(MACHINES, group_name, {"desktop"})
    monkeypatch.setattr(aw, "load_targets", lambda: (targets, None, 200))
    aw.RUN_STATE["running"] = False
    aw.app.config["TESTING"] = True
    test_client = aw.app.test_client()
    test_client.started = started
    return test_client


def post_run(client, payload):
    return client.post("/api/runs", json=payload)


def test_run_on_unknown_target_is_rejected(client):
    response = post_run(client, {"limit": "n-importe-quoi"})
    assert response.status_code == 400
    assert "Cible inconnue" in response.get_json()["error"]
    assert client.started == []
    assert aw.RUN_STATE["running"] is False  # le verrou n'a pas ete pris


def test_run_with_limit_pattern_is_rejected(client):
    assert post_run(client, {"limit": "pi-*"}).status_code == 400
    assert post_run(client, {"limit": "gpio:&desktop"}).status_code == 400


def test_run_on_machine_and_group_are_accepted(client):
    response = post_run(client, {"limit": "pi-01"})
    assert response.status_code == 201
    aw.RUN_STATE["running"] = False
    assert post_run(client, {"limit": "ecran_tactile"}).status_code == 201


def test_run_without_limit_does_not_query_bastion(client, monkeypatch):
    monkeypatch.setattr(aw, "load_targets", lambda: pytest.fail("ne doit pas etre appele"))
    assert post_run(client, {}).status_code == 201
    assert post_run(client, {"limit": None}).status_code == 409  # run deja en cours


def test_run_propagates_sync_errors(client, monkeypatch):
    monkeypatch.setattr(aw, "load_targets", lambda: (None, "miroir introuvable", 502))
    response = post_run(client, {"limit": "pi-01"})
    assert response.status_code == 502
    assert "miroir introuvable" in response.get_json()["error"]


def test_tag_whose_role_is_missing_from_order_yaml_is_flagged():
    # cas vecu : role cap_sciences_launcher present, mais absent de order.yaml -> aucun play
    machines = [{"name": "pi-04", "host": "10.42.0.150", "tags": ["cap-sciences-launcher", "desktop"]}]
    targets = aw.build_targets(machines, group_name, {"desktop", "cap_sciences_launcher"}, {"desktop"})
    by_name = {t["name"]: t for t in targets["tags"]}
    assert by_name["cap-sciences-launcher"]["role"] == "cap_sciences_launcher"
    assert by_name["cap-sciences-launcher"]["in_order"] is False
    assert by_name["desktop"]["in_order"] is True
    # order.yaml illisible : rien a signaler
    assert all(t["in_order"] for t in aw.build_targets(machines, group_name, set())["tags"])


def test_ordered_roles_reads_order_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "REPO_DIR", tmp_path)
    assert aw.ordered_roles() is None
    (tmp_path / "order.yaml").write_text("# commentaire\norder:\n  - desktop\n  - audio\n")
    assert aw.ordered_roles() == {"desktop", "audio"}


EMPTY_RUN_LOG = """[WARNING]: Could not match supplied host pattern, ignoring: gpio

PLAY [Typologie desktop] *******************************************************
skipping: no hosts matched

PLAY RECAP *********************************************************************

"""
REAL_RUN_LOG = """PLAY [Typologie audio] *********************************************************

TASK [Gathering Facts] *********************************************************
ok: [rpi-test-02.expolab.lan]

PLAY RECAP *********************************************************************
rpi-test-02.expolab.lan    : ok=19   changed=1    unreachable=0    failed=0    skipped=2    rescued=0    ignored=0
"""


def test_run_status_flags_a_run_that_touched_no_machine():
    assert aw.run_status(0, EMPTY_RUN_LOG) == "empty"
    assert aw.run_status(0, "") == "empty"
    assert aw.run_status(0, REAL_RUN_LOG) == "ok"
    assert aw.run_status(2, REAL_RUN_LOG) == "failed"
    assert aw.run_status(4, EMPTY_RUN_LOG) == "failed"
