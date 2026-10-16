"""Tests de l'API /api/credentials de caddy-admin (changement du couple d'administration)."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("ruamel.yaml")
bcrypt = pytest.importorskip("bcrypt")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))  # admin_credentials, dockhand_setup, dockhand_stack (monte sur /server dans l'image)


@pytest.fixture
def admin(tmp_path, monkeypatch):
    (tmp_path / "stacks" / "caddy").mkdir(parents=True)
    (tmp_path / "stacks" / "bastion").mkdir(parents=True)
    (tmp_path / "stacks" / "caddy" / "auth.env").write_text(
        "BASIC_AUTH_USER=admin\nBASIC_AUTH_PASSWORD=ancien-mdp-123\nBASIC_AUTH_HASH=$2a$14$OLDHASH\n"
    )
    (tmp_path / "stacks" / "bastion" / "bastion.env").write_text("BASTION_API_TOKEN=jeton\nBASTION_ADMIN_PASSWORD=ancien\n")

    spec = importlib.util.spec_from_file_location("caddy_admin_app", REPO / "caddy-admin" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["caddy_admin_app"] = module  # Flask retrouve templates/ et static/
    spec.loader.exec_module(module)
    module.app.testing = True
    monkeypatch.setattr(module, "SERVER_DIR", tmp_path)
    monkeypatch.setattr(module, "AUTH_ENV_PATH", tmp_path / "stacks" / "caddy" / "auth.env")
    # bcrypt rapide pour les tests
    monkeypatch.setattr(module.admin_credentials, "BCRYPT_ROUNDS", 4)
    module.events = []
    monkeypatch.setattr(module, "_dockhand_update", lambda old, new, pw: module.events.append(("dockhand", old, new)))
    monkeypatch.setattr(module, "_redeploy_bastion", lambda: module.events.append(("bastion",)))
    module._cred_failures.clear()
    module.root = tmp_path
    module.client = module.app.test_client()
    return module


def post(admin, **overrides):
    body = {"current_password": "ancien-mdp-123", "new_user": "gestion.expo", "new_password": "Nouveau-mdp-456", **overrides}
    return admin.client.post("/api/credentials", json=body)


def test_get_returns_the_user_but_never_a_password(admin):
    res = admin.client.get("/api/credentials")
    assert res.get_json() == {"user": "admin"}
    assert b"ancien-mdp-123" not in res.data and b"OLDHASH" not in res.data


def test_success_applies_everywhere_and_returns_the_steps(admin):
    res = post(admin)
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "steps": ["Dockhand", "Portail et interfaces d'administration", "Bastion"]}
    assert admin.events == [("dockhand", "admin", "gestion.expo"), ("bastion",)]
    auth = (admin.root / "stacks" / "caddy" / "auth.env").read_text()
    assert "BASIC_AUTH_USER=gestion.expo" in auth and "BASIC_AUTH_PASSWORD=Nouveau-mdp-456" in auth
    new_hash = [line for line in auth.splitlines() if line.startswith("BASIC_AUTH_HASH=")][0].partition("=")[2]
    assert bcrypt.checkpw(b"Nouveau-mdp-456", new_hash.encode())


def test_wrong_current_password_is_refused_and_changes_nothing(admin):
    res = post(admin, current_password="mauvais")
    assert res.status_code == 403
    assert admin.events == []
    assert "ancien-mdp-123" in (admin.root / "stacks" / "caddy" / "auth.env").read_text()


def test_empty_current_password_is_refused(admin):
    assert post(admin, current_password="").status_code == 403


def test_repeated_wrong_current_passwords_are_throttled(admin):
    for _ in range(admin.CRED_LIMIT):
        assert post(admin, current_password="mauvais").status_code == 403
    # meme avec le bon mot de passe : verrouille tant que la fenetre n'est pas ecoulee
    assert post(admin).status_code == 429
    assert admin.events == []


def test_invalid_new_values_are_refused_before_any_change(admin):
    assert post(admin, new_user="ab").status_code == 400
    assert post(admin, new_password="court").status_code == 400
    assert post(admin, new_password="avec'apostrophe-123").status_code == 400
    assert admin.events == []


def test_dockhand_refusal_returns_502_and_changes_nothing(admin, monkeypatch):
    def refuse(old, new, pw):
        raise RuntimeError("Dockhand a deja un utilisateur 'gestion.expo'")

    monkeypatch.setattr(admin, "_dockhand_update", refuse)
    res = post(admin)
    assert res.status_code == 502
    assert "deja un utilisateur" in res.get_json()["error"] and res.get_json()["done"] == []
    assert "ancien-mdp-123" in (admin.root / "stacks" / "caddy" / "auth.env").read_text()


def test_concurrent_change_is_refused(admin):
    admin._cred_lock.acquire()
    try:
        assert post(admin).status_code == 409
    finally:
        admin._cred_lock.release()
    assert post(admin).status_code == 200  # le verrou est bien libere ensuite


# --- compte exploitant ----------------------------------------------------------------------

def test_operator_account_needs_the_admin_password(admin):
    body = {"current_password": "faux-mdp", "user": "exploitant", "password": "Mdp-exploit-1"}
    assert admin.client.post("/api/operator", json=body).status_code == 403
    assert admin.client.get("/api/operator").get_json() == {"user": ""}
    body["current_password"] = "ancien-mdp-123"
    res = admin.client.post("/api/operator", json=body)
    assert res.status_code == 200 and res.get_json()["user"] == "exploitant"
    assert admin.client.get("/api/operator").get_json() == {"user": "exploitant"}
    assert admin.events == []   # ni Dockhand ni Bastion : l'exploitant n'y a pas de compte
    assert admin.client.delete("/api/operator", json={"current_password": "faux"}).status_code == 403
    assert admin.client.delete("/api/operator", json={"current_password": "ancien-mdp-123"}).get_json()["user"] == ""


def test_operator_account_same_name_as_admin_refused(admin):
    res = admin.client.post("/api/operator", json={"current_password": "ancien-mdp-123", "user": "admin", "password": "Mdp-exploit-1"})
    assert res.status_code == 400 and "different" in res.get_json()["error"]
