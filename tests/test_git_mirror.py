"""Tests de git-mirror : commit actuel affiche et verification "a jour ?" retenue entre deux
verifications, avec de vrais depots git locaux (aucun reseau)."""
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")
if shutil.which("git") is None:
    pytest.skip("git absent", allow_module_level=True)

APP_PATH = Path(__file__).resolve().parent.parent / "git-mirror" / "app.py"
sys.path.insert(0, str(APP_PATH.parent))   # app.py importe releases.py, a cote de lui
import releases  # noqa: E402


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
    monkeypatch.setattr(module, "RELEASES_DIR", tmp_path / "data" / "releases")
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


# --- releases GitHub (programmes des machines) ------------------------------------------------

class FakeGitHub:
    """GitHub simule : liste des releases et telechargement des archives."""

    def __init__(self, releases_list, blobs):
        self.releases_list, self.blobs, self.calls = releases_list, blobs, []

    def get(self, url, token, accept):
        self.calls.append((url, token, accept))
        if url.endswith("/releases?per_page=30"):
            return io.BytesIO(json.dumps(self.releases_list).encode())
        return io.BytesIO(self.blobs[int(url.rsplit("/", 1)[1])])


def release(tag, published, assets, draft=False):
    return {"tag_name": tag, "published_at": published, "draft": draft, "prerelease": False,
            "assets": [{"id": i, "name": n, "size": len(b)} for i, n, b in assets]}


URL = "https://x-access-token:SECRET@github.com/Cap-Sciences/godot_template.git"


def test_github_repo_and_token_come_from_the_mirror_url():
    assert releases.github_repo(URL) == ("Cap-Sciences/godot_template", "SECRET")
    assert releases.github_repo("https://github.com/o/r") == ("o/r", "")
    assert releases.github_repo("https://gitlab.example/o/r.git") is None   # GitLab : pas encore gere
    assert releases.github_repo("http://127.0.0.1:5054/git/x.git") is None


def test_only_arm64_archives_of_published_releases_are_kept(tmp_path):
    arm, x86 = b"binaire-arm64", b"binaire-x86"
    gh = FakeGitHub([
        release("v1.1.0", "2026-10-24T10:00:00Z", [(11, "app-1.1.0-linux-arm64.tar.gz", arm), (12, "app-1.1.0-linux-x86_64.tar.gz", x86)]),
        release("v1.0.0", "2026-10-20T10:00:00Z", [(21, "app-1.0.0-linux-arm64.tar.gz", arm)]),
        release("v2.0.0-brouillon", "2026-10-25T10:00:00Z", [(31, "app-2.0.0-linux-arm64.tar.gz", arm)], draft=True),
        release("v0.9.0", "2026-10-01T10:00:00Z", [(41, "notes.txt", b"x")]),          # pas d'archive arm64
    ], {11: arm, 12: x86, 21: arm, 31: arm, 41: b"x"})
    index = releases.sync_releases("app", URL, tmp_path, gh.get)
    assert [r["tag"] for r in index] == ["v1.1.0", "v1.0.0"]                      # plus recente d'abord
    assert [a["name"] for a in index[0]["assets"]] == ["app-1.1.0-linux-arm64.tar.gz"]
    assert (tmp_path / "app" / "v1.1.0" / "app-1.1.0-linux-arm64.tar.gz").read_bytes() == arm
    assert not (tmp_path / "app" / "v2.0.0-brouillon").exists()
    assert all(token == "SECRET" for _, token, _ in gh.calls)
    assert "SECRET" not in (tmp_path / "app" / "index.json").read_text()        # jamais ecrit
    # Second sync : rien n'est retelecharge.
    gh.calls.clear()
    releases.sync_releases("app", URL, tmp_path, gh.get)
    assert [u for u, _, _ in gh.calls if "/assets/" in u] == []


def test_old_releases_are_pruned_and_a_truncated_download_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "KEEP", 1)
    gh = FakeGitHub([release("v1.0.0", "2026-10-20T10:00:00Z", [(21, "a-linux-arm64.tar.gz", b"v1")])], {21: b"v1"})
    releases.sync_releases("app", URL, tmp_path, gh.get)
    gh.releases_list.insert(0, release("v1.1.0", "2026-10-24T10:00:00Z", [(11, "b-linux-arm64.tar.gz", b"v11")]))
    gh.blobs[11] = b"v11"
    releases.sync_releases("app", URL, tmp_path, gh.get)
    assert not (tmp_path / "app" / "v1.0.0").exists()                           # n'est plus dans les KEEP
    bad = FakeGitHub([release("v3.0.0", "2026-10-26T10:00:00Z", [(51, "c-linux-arm64.tar.gz", b"complet")])], {51: b"tronq"})
    with pytest.raises(ValueError):
        releases.sync_releases("app2", URL, tmp_path, bad.get)
    assert not list((tmp_path / "app2").rglob("*.tar.gz"))                    # rien de partiel


def test_mirror_sync_fetches_releases_and_serves_only_indexed_archives(gm, monkeypatch):
    commit(gm.work, "Premier commit")
    gh = FakeGitHub([release("v1.0.0", "2026-10-26T13:36:20Z", [(7, "godot_template-1.0.0-linux-arm64.tar.gz", b"ARCHIVE")])],
                    {7: b"ARCHIVE"})
    monkeypatch.setattr(releases, "http_get", gh.get)
    monkeypatch.setattr(releases, "github_repo", lambda url: ("Cap-Sciences/godot_template", "SECRET"))
    assert gm.client.post("/api/mirrors", json={"name": "godot-template", "remote_url": str(gm.upstream)}).status_code == 201
    listed = gm.client.get("/api/mirrors").get_json()["mirrors"][0]
    assert listed["releases"] == [{"tag": "v1.0.0", "published_at": "2026-10-26T13:36:20Z"}] and listed["release_error"] is None
    rel = gm.client.get("/api/mirrors/godot-template/releases").get_json()["releases"]
    assert rel[0]["assets"][0]["sha256"] and rel[0]["assets"][0]["size"] == 7
    res = gm.client.get("/releases/godot-template/v1.0.0/godot_template-1.0.0-linux-arm64.tar.gz")
    assert res.status_code == 200 and res.data == b"ARCHIVE"
    for url in ("/releases/godot-template/v1.0.0/autre.tar.gz", "/releases/godot-template/..%2Findex.json/x",
                "/releases/inconnu/v1.0.0/godot_template-1.0.0-linux-arm64.tar.gz"):
        assert gm.client.get(url).status_code == 404
    # GitHub injoignable : le git est synchronise quand meme, l'erreur est affichee.
    monkeypatch.setattr(releases, "http_get", lambda *a: (_ for _ in ()).throw(OSError("pas de reseau")))
    res = gm.client.post("/api/mirrors/godot-template/sync").get_json()
    assert res["ok"] is True and "Releases non recuperees" in res["release_error"]
    assert gm.client.delete("/api/mirrors/godot-template").status_code == 200
    assert not (gm.RELEASES_DIR / "godot-template").exists()
