"""Tests d'ansible-web : cibles (tags + machines) et validation de --limit."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")

APP_PATH = Path(__file__).resolve().parent.parent / "ansible-web" / "app.py"
sys.path.insert(0, str(APP_PATH.parent))   # app.py importe deploy.py, a cote de lui
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


# --- installation d'une machine apres un flash exploitant (deploy.py) ---------------------------

import deploy  # noqa: E402

# Sortie reelle de `--list-hosts --list-tasks --limit rpi-test.expolab.lan` (abregee) : les
# plays qui ne visent pas la machine (`hosts (0):`) listent quand meme leurs taches.
LIST_TASKS = """
playbook: site.yml

  play #1 (wifi): Typologie wifi	TAGS: []
    pattern: ['wifi']
    hosts (1):
      rpi-test.expolab.lan
    tasks:
      wifi : Installer iw	TAGS: []
      wifi : Poser la regle udev	TAGS: []

  play #2 (desktop): Typologie desktop	TAGS: []
    pattern: ['desktop']
    hosts (0):
    tasks:
      desktop : Installer sway	TAGS: []
      desktop : Deployer la config sway	TAGS: []

  play #3 (audio): Typologie audio	TAGS: []
    pattern: ['audio']
    hosts (1):
      rpi-test.expolab.lan
    tasks:
      audio : Installer PipeWire	TAGS: []
"""


def test_deploy_helpers():
    assert deploy.count_planned_tasks(LIST_TASKS) == 3
    assert deploy.count_planned_tasks(LIST_TASKS.replace("hosts (1):", "hosts (0):")) == 0   # aucun play ne la vise
    log = "TASK [Gathering Facts] ***\nok\nTASK [wifi : Installer iw] ***\nTASK [wifi : Poser la regle udev] ***\n"
    assert deploy.count_done_tasks(log) == 2
    assert deploy.progress(2, 3) == 67 and deploy.progress(9, 3) == 99 and deploy.progress(0, 3) == 1 and deploy.progress(1, 0) == 0
    machines = [{"name": "rpi-test-02.expolab.lan"}, {"name": "rpi-test"}]
    assert deploy.inventory_name(machines, "rpi-test-02") == "rpi-test-02.expolab.lan"
    assert deploy.inventory_name(machines, "rpi-test") == "rpi-test" and deploy.inventory_name(machines, "rpi") is None
    # Reconnaitre LA carte flashee, premier demarrage termine.
    assert deploy.card_ready("expolab-x-1\nstatus: done\n", "expolab-x-1")
    assert deploy.card_ready("expolab-x-1\nstatus: error\n", "expolab-x-1")   # cloud-init en erreur : on installe quand meme
    assert not deploy.card_ready("expolab-x-1\nstatus: running\n", "expolab-x-1")
    assert not deploy.card_ready("expolab-x-0\nstatus: done\n", "expolab-x-1")   # ancienne carte encore allumee
    assert not deploy.card_ready("", "expolab-x-1")
    assert not deploy.valid_request({"hostname": "../etc", "requested_at": 1, "instance_id": "i"})


class DeployLab:
    """ansible-web sans reseau : Bastion, clone, sonde SSH et runs simules."""

    def __init__(self, monkeypatch, tmp_path, planned=3, machines=None):
        self.ready, self.runs, self.busy = False, {}, False
        monkeypatch.setattr(aw, "DEPLOY_DIR", tmp_path / "deploy")
        monkeypatch.setattr(aw, "RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(aw, "sync_repo", lambda: (True, ""))
        inventory = type("Inv", (), {"fetch_machines": staticmethod(lambda: machines or [{"name": "rpi-test-02.expolab.lan"}])})
        monkeypatch.setattr(aw, "load_inventory_module", lambda: inventory)
        monkeypatch.setattr(aw, "planned_tasks", lambda limit: planned)
        monkeypatch.setattr(aw, "probe_card", lambda ip, instance: self.ready)
        monkeypatch.setattr(aw, "start_run", self.start_run)
        monkeypatch.setattr(aw, "run_entry", lambda run_id: self.runs.get(run_id))
        (tmp_path / "runs").mkdir(parents=True)
        self.tmp = tmp_path

    def start_run(self, limit, actor=""):
        if self.busy:
            return None
        self.limit, self.actor = limit, actor
        self.runs["R1"] = {"id": "R1", "status": "running"}
        return "R1"

    def request(self, hostname="rpi-test-02", requested_at=1000):
        path = self.tmp / "deploy" / "requests" / f"{hostname}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hostname": hostname, "ip": "10.42.0.107", "instance_id": "expolab-rpi-test-02-1",
                                    "requested_at": requested_at}))

    def status(self, hostname="rpi-test-02"):
        return deploy.read_json(self.tmp / "deploy" / "status" / f"{hostname}.json")


def test_deploy_waits_for_the_card_then_runs_and_reports_progress(monkeypatch, tmp_path):
    lab = DeployLab(monkeypatch, tmp_path)
    lab.request()
    aw.deploy_once(now=1010)
    assert lab.status()["state"] == "waiting" and lab.status()["limit"] == "rpi-test-02.expolab.lan"
    aw.deploy_once(now=1020)
    assert lab.status()["state"] == "waiting"           # la carte n'a pas encore demarre
    lab.ready, lab.busy = True, True
    aw.deploy_once(now=1030)
    assert lab.status()["state"] == "waiting"           # un autre run tourne : elle attend son tour
    lab.busy = False
    aw.deploy_once(now=1040)
    assert lab.status()["state"] == "running" and lab.limit == "rpi-test-02.expolab.lan" and lab.actor == "exploitant"
    (tmp_path / "runs" / "R1.log").write_text("TASK [Gathering Facts] *\nTASK [wifi : Installer iw] *\nTASK [wifi : Poser la regle udev] *\n")
    aw.deploy_once(now=1050)
    assert lab.status()["percent"] == 67
    lab.runs["R1"]["status"] = "ok"
    aw.deploy_once(now=1060)
    assert lab.status()["state"] == "done" and lab.status()["percent"] == 100


def test_failed_run_and_machine_without_role(monkeypatch, tmp_path):
    lab = DeployLab(monkeypatch, tmp_path)
    lab.request()
    lab.ready = True
    aw.deploy_once(now=1010)
    aw.deploy_once(now=1020)
    lab.runs["R1"]["status"] = "failed"
    aw.deploy_once(now=1030)
    assert lab.status()["state"] == "failed"
    lab2 = DeployLab(monkeypatch, tmp_path / "b", planned=0)
    lab2.request()
    aw.deploy_once(now=1010)
    assert lab2.status()["state"] == "none"             # aucun role prevu : rien a installer
    lab3 = DeployLab(monkeypatch, tmp_path / "c", machines=[{"name": "autre.expolab.lan"}])
    lab3.request()
    aw.deploy_once(now=1010)
    assert lab3.status()["state"] == "none"             # inconnue de Bastion : rien a installer


def test_deploy_times_out_and_restarts_on_a_new_request(monkeypatch, tmp_path):
    lab = DeployLab(monkeypatch, tmp_path)
    lab.request(requested_at=1000)
    aw.deploy_once(now=1010)
    aw.deploy_once(now=1000 + deploy.WAIT_SECONDS + 1)
    assert lab.status()["state"] == "timeout"
    lab.request(requested_at=5000)                      # « J'ai verifie, attendre encore »
    aw.deploy_once(now=5010)
    assert lab.status()["state"] == "waiting"
    (tmp_path / "deploy" / "requests" / "rpi-test-02.json").unlink()   # « Abandonner »
    aw.deploy_once(now=5020)
    assert lab.status() is None
