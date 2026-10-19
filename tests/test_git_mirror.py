"""Tests de git-mirror : commit actuel affiche et verification "a jour ?" retenue entre deux
verifications, avec de vrais depots git locaux (aucun reseau)."""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")
if shutil.which("git") is None:
    pytest.skip("git absent", allow_module_level=True)

APP_PATH = Path(__file__).resolve().parent.parent / "git-mirror" / "app.py"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, message: str) -> str:
    (work / "f.txt").write_text(message)
    git("add", "f.txt", cwd=work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


@pytest.fixture
def gm(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("git_mirror_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(module, "REPOS_DIR", tmp_path / "data" / "repos")
    monkeypatch.setattr(module, "CONFIG_PATH", tmp_path / "data" / "mirrors.yaml")
    (tmp_path / "data").mkdir()
    upstream = tmp_path / "upstream.git"
    git("init", "-q", "--bare", "-b", "main", str(upstream))
    work = tmp_path / "work"
    git("clone", "-q", str(upstream), str(work))
    module.upstream, module.work = upstream, work
    module.client = module.app.test_client()
    return module


def mirrors(gm):
    return {m["name"]: m for m in gm.client.get("/api/mirrors").get_json()["mirrors"]}


def test_list_shows_the_current_commit(gm):
    sha = commit(gm.work, "Premier commit")
    assert gm.client.post("/api/mirrors", json={"name": "ansible", "remote_url": str(gm.upstream)}).status_code == 201
    m = mirrors(gm)["ansible"]
    assert m["up_to_date"] is True
    assert m["commit"]["sha"] == sha and m["commit"]["short"] == sha[:7]
    assert m["commit"]["subject"] == "Premier commit"
    assert m["checked_at"]


def test_a_new_remote_commit_shows_up_at_the_next_check_and_sync_catches_up(gm):
    commit(gm.work, "Premier commit")
    gm.client.post("/api/mirrors", json={"name": "ansible", "remote_url": str(gm.upstream)})
    new_sha = commit(gm.work, "Deuxieme commit")
    # La page lit le dernier resultat : rien ne change avant la verification suivante...
    assert mirrors(gm)["ansible"]["up_to_date"] is True
    # ...qui voit le retard et le commit distant.
    gm.check_mirror(gm.find_mirror(gm.load_mirrors(), "ansible"))
    m = mirrors(gm)["ansible"]
    assert m["up_to_date"] is False and m["remote_short"] == new_sha[:7]
    assert m["commit"]["subject"] == "Premier commit"
    # Un sync recupere le commit et reverifie aussitot.
    assert gm.client.post("/api/mirrors/ansible/sync").status_code == 200
    m = mirrors(gm)["ansible"]
    assert m["up_to_date"] is True and m["commit"]["sha"] == new_sha


def test_unreachable_remote_is_unknown_not_late(gm):
    commit(gm.work, "Premier commit")
    gm.client.post("/api/mirrors", json={"name": "ansible", "remote_url": str(gm.upstream)})
    gm.upstream.rename(gm.upstream.with_name("ailleurs.git"))   # depot distant disparu
    gm.check_mirror(gm.find_mirror(gm.load_mirrors(), "ansible"))
    m = mirrors(gm)["ansible"]
    assert m["up_to_date"] is None
    assert m["commit"]["subject"] == "Premier commit"   # le clone local reste lisible


def test_deleted_mirror_forgets_its_status(gm):
    commit(gm.work, "Premier commit")
    gm.client.post("/api/mirrors", json={"name": "ansible", "remote_url": str(gm.upstream)})
    assert gm.client.delete("/api/mirrors/ansible").status_code == 200
    assert "ansible" not in gm._status
