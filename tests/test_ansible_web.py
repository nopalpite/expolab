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
        self.ready, self.runs = False, {}
        monkeypatch.setattr(aw, "DEPLOY_DIR", tmp_path / "deploy")
        monkeypatch.setattr(aw, "RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(aw, "sync_repo", lambda: (True, ""))
        inventory = type("Inv", (), {"fetch_machines": staticmethod(lambda: machines or [{"name": "rpi-test-02.expolab.lan"}])})
        monkeypatch.setattr(aw, "load_inventory_module", lambda: inventory)
        monkeypatch.setattr(aw, "planned_tasks", lambda limit: planned)
        monkeypatch.setattr(aw, "probe_card", lambda ip, instance: self.ready)
        monkeypatch.setattr(aw, "enqueue_run", self.enqueue_run)
        monkeypatch.setattr(aw, "run_entry", lambda run_id: self.runs.get(run_id))
        (tmp_path / "runs").mkdir(parents=True)
        self.tmp = tmp_path

    def enqueue_run(self, limit, actor=""):
        self.limit, self.actor = limit, actor
        run_id = f"R{len(self.runs) + 1}"
        self.runs[run_id] = {"id": run_id, "status": "queued"}
        return run_id, False

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
    lab.ready = True
    aw.deploy_once(now=1030)
    assert lab.status()["state"] == "running" and lab.limit == "rpi-test-02.expolab.lan" and lab.actor == "exploitant"
    aw.deploy_once(now=1040)
    assert lab.status()["percent"] == 1 and lab.status()["queued"] is True   # en file : elle attend son tour
    lab.runs["R1"]["status"] = "running"
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
    assert lab.status()["state"] == "retrying"           # un nouvel essai automatique...
    aw.deploy_once(now=1030 + deploy.RETRY_DELAY - 1)
    assert lab.status()["state"] == "retrying"           # ...2 min plus tard
    aw.deploy_once(now=1030 + deploy.RETRY_DELAY)
    assert lab.status()["state"] == "running" and lab.status()["run_id"] == "R2" and lab.status()["attempt"] == 2
    lab.runs["R2"]["status"] = "failed"
    aw.deploy_once(now=2000)
    assert lab.status()["state"] == "failed"             # un seul nouvel essai
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


def test_every_subprocess_gets_its_own_stdin():
    # Vecu : la sonde ssh passait le stdin herite (celui du conteneur) en non bloquant, puis
    # ansible-playbook refusait de demarrer (« Ansible requires blocking IO ») pour TOUS les
    # runs jusqu'au redemarrage. Chaque sous-processus doit donc recevoir son propre stdin.
    import ast
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "run" and getattr(n.func.value, "id", "") == "subprocess"]
    assert len(calls) >= 4
    for call in calls:
        assert any(k.arg == "stdin" for k in call.keywords), f"subprocess.run ligne {call.lineno} sans stdin"


def test_cancelled_deploy_run_is_not_retried(monkeypatch, tmp_path):
    lab = DeployLab(monkeypatch, tmp_path)
    lab.request()
    lab.ready = True
    aw.deploy_once(now=1010)
    aw.deploy_once(now=1020)
    lab.runs["R1"]["status"] = "cancelled"               # annule a la main par l'administrateur
    aw.deploy_once(now=1030)
    assert lab.status()["state"] == "failed"


# --- file d'attente des runs ---------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(aw, "RUNS_INDEX", tmp_path / "index.yaml")
    targets = aw.build_targets(MACHINES, group_name, {"desktop"})
    monkeypatch.setattr(aw, "load_targets", lambda: (targets, None, 200))
    aw.RUN_STATE["running"] = False
    aw.app.config["TESTING"] = True
    return aw.app.test_client()


def statuses():
    return [(r["limit"], r["status"]) for r in aw.load_runs()]


def test_runs_wait_their_turn_in_arrival_order(queue):
    first = queue.post("/api/runs", json={"limit": "pi-01"}).get_json()
    second = queue.post("/api/runs", json={"limit": "pi-02"}).get_json()
    assert first["queued"] is False and second["queued"] is True     # le second attend son tour
    assert first["id"] != second["id"]                                # meme seconde : ids distincts
    again = queue.post("/api/runs", json={"limit": "pi-02"}).get_json()
    assert again["id"] == second["id"]                                # deja en attente : pas de doublon
    assert aw.next_queued()["limit"] == "pi-01"
    assert statuses() == [("pi-01", "running"), ("pi-02", "queued")]
    assert queue.get(f"/api/runs/{second['id']}/log").get_json()["log"].startswith("En attente")
    aw.update_run(first["id"], status="ok")
    assert aw.next_queued()["limit"] == "pi-02"
    assert aw.next_queued() is None


def test_only_a_queued_run_can_be_cancelled(queue):
    first = queue.post("/api/runs", json={"limit": "pi-01"}).get_json()["id"]
    second = queue.post("/api/runs", json={"limit": "pi-02"}).get_json()["id"]
    aw.next_queued()                                                   # le premier demarre
    assert queue.post(f"/api/runs/{first}/cancel").status_code == 409  # deja lance : va a son terme
    assert queue.post(f"/api/runs/{second}/cancel").status_code == 200
    assert statuses() == [("pi-01", "running"), ("pi-02", "cancelled")]
    assert aw.next_queued() is None                                    # un run annule ne part jamais
    assert queue.post("/api/runs/inconnu/cancel").status_code == 404


def test_restart_marks_an_interrupted_run_failed_and_keeps_the_queue(queue):
    queue.post("/api/runs", json={"limit": "pi-01"})
    queue.post("/api/runs", json={"limit": "pi-02"})
    aw.next_queued()
    aw.recover_runs()                                                   # ansible-web redemarre
    assert statuses() == [("pi-01", "failed"), ("pi-02", "queued")]
    assert queue.get("/api/runs").get_json()["runs"][0]["limit"] in ("pi-01", "pi-02")


def test_queue_loop_runs_one_at_a_time(queue, monkeypatch):
    seen = []

    def fake_execute(run_id, limit):
        seen.append((limit, aw.RUN_STATE["running"]))
        aw.update_run(run_id, status="ok")
        aw.RUN_STATE["running"] = False
        if len(seen) == 2:
            raise SystemExit   # fin du test
    monkeypatch.setattr(aw, "execute_run", fake_execute)
    queue.post("/api/runs", json={"limit": "pi-01"})
    queue.post("/api/runs", json={"limit": "pi-02"})
    with pytest.raises(SystemExit):
        aw.queue_loop()
    assert seen == [("pi-01", True), ("pi-02", True)]


# --- onglet Applications (apps.py) -------------------------------------------------------------

import apps  # noqa: E402

PROGS = [
    {"name": "godot-template", "releases": [
        {"tag": "v1.1.0", "published_at": "2026-10-24T10:00:00Z",
         "assets": [{"name": "godot_template-1.1.0-linux-arm64.tar.gz", "size": 3, "sha256": "aaa"}]},
        {"tag": "v1.0.0", "published_at": "2026-10-20T10:00:00Z",
         "assets": [{"name": "godot_template-1.0.0-linux-arm64.tar.gz", "arch": "arm64", "size": 3, "sha256": "bbb"},
                    {"name": "godot_template-1.0.0-linux-x86_64.tar.gz", "arch": "x86_64", "size": 3, "sha256": "ccc"}]},
    ]},
]
RECAP = """
TASK [internal_app : Installer le programme] ****
PLAY RECAP *********************************************************************
rpi-test-02.expolab.lan    : ok=12   changed=3    unreachable=0    failed=0    skipped=1    rescued=0    ignored=0
rpi-test.expolab.lan       : ok=4    changed=0    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
"""


def test_programs_only_lists_mirrors_with_releases():
    answers = {"/api/mirrors": {"mirrors": [{"name": "ansible", "releases": []},
                                            {"name": "godot-template", "releases": [{"tag": "v1.1.0"}]}]},
               "/api/mirrors/godot-template/releases": {"releases": PROGS[0]["releases"]}}
    assert [p["name"] for p in apps.programs(get=answers.__getitem__)] == ["godot-template"]


def test_resolve_and_run_vars():
    assert apps.resolve(PROGS[0], "latest")["tag"] == "v1.1.0"
    assert apps.resolve(PROGS[0], "v1.0.0")["tag"] == "v1.0.0"
    assert apps.resolve(PROGS[0], "v9.9.9") is None and apps.resolve(None, "latest") is None
    store = {"rpi-test-02.expolab.lan": {"app": "godot-template", "version": "v1.0.0"},
             "rpi-test.expolab.lan": {"app": "godot-template", "version": "latest"},
             "gerard.expolab.lan": {"installed": {"app": "x", "version": "v1"}},          # plus de programme
             "module-09.expolab.lan": {"app": "godot-template", "version": "v0.1.0"}}     # release disparue
    out = apps.run_vars(store, PROGS)["expolab_apps"]
    assert set(out) == {"rpi-test-02.expolab.lan", "rpi-test.expolab.lan"}
    assert out["rpi-test.expolab.lan"]["version"] == "v1.1.0"                            # « derniere » resolue
    base = "http://127.0.0.1:5054/releases/godot-template/v1.0.0/"
    assert out["rpi-test-02.expolab.lan"] == {"name": "godot-template", "version": "v1.0.0", "archives": {
        "arm64": {"archive": "godot_template-1.0.0-linux-arm64.tar.gz", "sha256": "bbb",
                  "url": base + "godot_template-1.0.0-linux-arm64.tar.gz"},
        "x86_64": {"archive": "godot_template-1.0.0-linux-x86_64.tar.gz", "sha256": "ccc",
                   "url": base + "godot_template-1.0.0-linux-x86_64.tar.gz"}}}
    # Index plus ancien (sans « arch ») : l'architecture est deduite du nom.
    assert set(out["rpi-test.expolab.lan"]["archives"]) == {"arm64"}
    assert apps.release_arches(PROGS[0]["releases"][1]) == ["arm64", "x86_64"]


def test_installed_version_comes_from_the_play_recap():
    assert apps.hosts_ok(RECAP) == {"rpi-test-02.expolab.lan"}
    snapshot = {"rpi-test-02.expolab.lan": {"name": "godot-template", "version": "v1.1.0"},
                "rpi-test.expolab.lan": {"name": "godot-template", "version": "v1.1.0"}}
    store = {"rpi-test-02.expolab.lan": {"app": "godot-template", "run_id": "R1"}}
    assert apps.record_installed(store, snapshot, RECAP, now="T")
    assert store["rpi-test-02.expolab.lan"]["installed"] == {"app": "godot-template", "version": "v1.1.0", "at": "T"}
    assert "run_id" not in store["rpi-test-02.expolab.lan"]
    assert "rpi-test.expolab.lan" not in store                                           # en echec : rien d'installe
    # Un depot Ansible sans role `app` ne pose rien : on ne pretend pas l'avoir installe.
    assert not apps.record_installed({}, snapshot, RECAP.replace("TASK [internal_app : ", "TASK [kiosk : "))


def test_row_state():
    wanted = {"tag": "v1.1.0"}
    entry = {"app": "godot-template", "installed": {"app": "godot-template", "version": "v1.1.0"}}
    assert apps.row_state({}, None, None) == "none"
    assert apps.row_state(entry, wanted, None) == "uptodate"
    assert apps.row_state(entry, {"tag": "v1.2.0"}, None) == "outdated"
    assert apps.row_state({"app": "godot-template"}, None, None) == "missing"
    assert apps.row_state(entry, wanted, {"status": "queued"}) == "queued"
    assert apps.row_state(entry, wanted, {"status": "failed"}) == "failed"


@pytest.fixture
def appsweb(tmp_path, monkeypatch):
    monkeypatch.setattr(aw, "APPS_STORE", tmp_path / "apps" / "apps.json")
    monkeypatch.setattr(aw, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(aw, "RUNS_INDEX", tmp_path / "runs" / "index.yaml")
    (tmp_path / "runs").mkdir()
    machines = [{"name": "rpi-test-02.expolab.lan", "site": "expo", "tags": ["kiosk", "audio"]},
                {"name": "gerard.expolab.lan", "site": None, "tags": ["audio"]}]
    monkeypatch.setattr(aw, "bastion_machines", lambda: machines)
    monkeypatch.setattr(apps, "programs", lambda get=None: PROGS)
    aw.app.config["TESTING"] = True
    client = aw.app.test_client()
    client.tmp = tmp_path
    return client


def rows(client):
    return {r["short"]: r for r in client.get("/api/apps").get_json()["machines"]}


def test_apps_page_assigns_validates_and_installs(appsweb):
    r = rows(appsweb)
    assert r["rpi-test-02"]["state"] == "none" and r["rpi-test-02"]["kiosk"] is True
    assert r["gerard"]["kiosk"] is False
    put = lambda m, body: appsweb.put(f"/api/apps/{m}", json=body)  # noqa: E731
    assert put("inconnue.expolab.lan", {"app": "godot-template"}).status_code == 404
    assert put("rpi-test-02.expolab.lan", {"app": "inexistant"}).status_code == 400
    assert put("rpi-test-02.expolab.lan", {"app": "godot-template", "version": "v9"}).status_code == 400
    assert put("rpi-test-02.expolab.lan", {"app": "godot-template", "version": "v1.0.0"}).status_code == 200
    r = rows(appsweb)["rpi-test-02"]
    assert (r["app"], r["version"], r["wanted"], r["state"]) == ("godot-template", "v1.0.0", "v1.0.0", "outdated")
    res = appsweb.post("/api/apps/rpi-test-02.expolab.lan/install").get_json()
    assert res["ok"] is True
    assert aw.load_runs()[-1]["limit"] == "rpi-test-02.expolab.lan" and aw.load_runs()[-1]["actor"] == "applications"
    assert rows(appsweb)["rpi-test-02"]["state"] == "queued"
    assert appsweb.post("/api/apps/gerard.expolab.lan/install").status_code == 400       # aucun programme
    assert put("rpi-test-02.expolab.lan", {"app": ""}).status_code == 200                # « — aucun — »
    assert rows(appsweb)["rpi-test-02"]["state"] == "none"


def test_orphan_machine_is_listed_and_can_be_removed(appsweb):
    apps.save(aw.APPS_STORE, {"ancien.expolab.lan": {"app": "godot-template", "version": "v1.0.0"}})
    view = appsweb.get("/api/apps").get_json()
    assert [o["name"] for o in view["orphans"]] == ["ancien.expolab.lan"]
    assert view["programs"][0]["machines"] == 1
    assert appsweb.delete("/api/apps/ancien.expolab.lan").status_code == 200
    assert appsweb.get("/api/apps").get_json()["orphans"] == []


def test_every_run_gets_the_programs_and_records_what_was_installed(appsweb, monkeypatch):
    apps.save(aw.APPS_STORE, {"rpi-test-02.expolab.lan": {"app": "godot-template", "version": "latest"}})
    monkeypatch.setattr(aw, "sync_repo", lambda: (True, ""))
    repo = appsweb.tmp / "repo"
    repo.mkdir()
    (repo / "apps.yml").write_text("---")
    monkeypatch.setattr(aw, "REPO_DIR", repo)
    seen = {}

    def fake_run(cmd, stdout, **kw):
        seen["cmd"] = cmd
        stdout.write(RECAP)
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(aw.subprocess, "run", fake_run)
    aw.save_runs([{"id": "R9", "limit": None, "status": "running"}])
    aw.execute_run("R9", None)
    vars_file = appsweb.tmp / "runs" / "R9.apps.json"
    assert seen["cmd"][-2:] == ["-e", f"@{vars_file}"]
    assert seen["cmd"][1] == str(repo / "apps.yml")                     # programme pose AVANT site.yml
    assert json.loads(vars_file.read_text())["expolab_apps"]["rpi-test-02.expolab.lan"]["version"] == "v1.1.0"
    assert rows(appsweb)["rpi-test-02"]["installed"]["version"] == "v1.1.0"
    assert rows(appsweb)["rpi-test-02"]["state"] == "uptodate"
