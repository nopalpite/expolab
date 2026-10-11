"""Tests de server/admin_credentials.py : validation, verification du mot de passe
actuel, reecriture de auth.env / bastion.env, ordre et echecs partiels."""
import os
import sys
from pathlib import Path

import pytest

bcrypt = pytest.importorskip("bcrypt")

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
import admin_credentials as ac  # noqa: E402

NEW_PASSWORD = 'Nouveau $mdp #1 "ok"'


@pytest.fixture
def server(tmp_path):
    (tmp_path / "stacks" / "caddy").mkdir(parents=True)
    (tmp_path / "stacks" / "bastion").mkdir(parents=True)
    (tmp_path / "stacks" / "caddy" / "auth.env").write_text(
        "BASIC_AUTH_USER=admin\nBASIC_AUTH_PASSWORD=ancien-mdp-123\nBASIC_AUTH_HASH=$2a$14$OLDHASH\n"
    )
    (tmp_path / "stacks" / "bastion" / "bastion.env").write_text(
        "BASTION_SECRET_KEY=cle-de-session\nBASTION_API_TOKEN=jeton-api\nBASTION_ADMIN_PASSWORD=ancien-bastion\n"
    )
    return tmp_path


class Calls:
    def __init__(self, fail_dockhand=None, fail_bastion=None):
        self.events = []
        self.fail_dockhand = fail_dockhand
        self.fail_bastion = fail_bastion

    def dockhand(self, old, new, password):
        if self.fail_dockhand:
            raise RuntimeError(self.fail_dockhand)
        self.events.append(("dockhand", old, new, password))

    def bastion(self):
        if self.fail_bastion:
            raise RuntimeError(self.fail_bastion)
        self.events.append(("bastion",))


def run(server, calls, user="gestion.expo", password=NEW_PASSWORD):
    return ac.apply_credentials(
        server, user, password, dockhand_update=calls.dockhand, redeploy_bastion=calls.bastion, rounds=4
    )


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("user", ["ab", "a" * 33, "avec espace", "accenté", "", "x;y"])
def test_invalid_users_are_refused(user):
    assert ac.check_user(user)


@pytest.mark.parametrize("user", ["admin", "gestion.expo", "a_b-c", "A1b"])
def test_valid_users_are_accepted(user):
    assert ac.check_user(user) is None


@pytest.mark.parametrize("password", ["court", "avec'apostrophe-123", "retour\nligne-123", ""])
def test_invalid_passwords_are_refused(password):
    assert ac.check_password(password)


def test_special_characters_are_fine_in_a_password():
    assert ac.check_password(NEW_PASSWORD) is None


# --- mot de passe actuel ----------------------------------------------------

def test_current_password_is_verified_against_auth_env(server):
    assert ac.verify_current(server, "ancien-mdp-123")
    assert not ac.verify_current(server, "autre")
    assert not ac.verify_current(server, "")


def test_current_password_never_matches_without_auth_env(tmp_path):
    assert not ac.verify_current(tmp_path, "")
    assert not ac.verify_current(tmp_path, "n-importe-quoi")


# --- application ------------------------------------------------------------

def test_apply_updates_dockhand_then_portal_then_bastion(server):
    calls = Calls()
    done = run(server, calls)
    assert done == ["Dockhand", "Portail et interfaces d'administration", "Bastion"]
    assert calls.events == [("dockhand", "admin", "gestion.expo", NEW_PASSWORD), ("bastion",)]


def test_auth_env_is_rewritten_in_place_with_a_verifiable_hash(server):
    auth_env = server / "stacks" / "caddy" / "auth.env"
    inode = auth_env.stat().st_ino
    run(server, Calls())
    if os.name != "nt":  # pas d'inode comparable sous Windows
        assert auth_env.stat().st_ino == inode
    assert ac.env_value(auth_env, "BASIC_AUTH_USER") == "gestion.expo"
    assert ac.env_value(auth_env, "BASIC_AUTH_PASSWORD") == NEW_PASSWORD
    assert bcrypt.checkpw(NEW_PASSWORD.encode(), ac.env_value(auth_env, "BASIC_AUTH_HASH").encode())
    assert "OLDHASH" not in auth_env.read_text()


def test_bastion_env_keeps_other_secrets_and_quotes_special_values(server):
    run(server, Calls())
    text = (server / "stacks" / "bastion" / "bastion.env").read_text()
    lines = text.splitlines()
    assert "BASTION_SECRET_KEY=cle-de-session" in lines and "BASTION_API_TOKEN=jeton-api" in lines
    assert "BASTION_ADMIN_USER=gestion.expo" in lines
    assert "BASTION_ADMIN_PASSWORD='Nouveau $mdp #1 \"ok\"'" in lines  # litteral pour Docker Compose
    assert sum(line.startswith("BASTION_ADMIN_USER=") for line in lines) == 1
    assert sum(line.startswith("BASTION_ADMIN_PASSWORD=") for line in lines) == 1
    assert "ancien-bastion" not in text


def test_simple_values_are_not_quoted(server):
    run(server, Calls(), password="Mot-de-passe-simple-1")
    assert "BASTION_ADMIN_PASSWORD=Mot-de-passe-simple-1" in (server / "stacks" / "bastion" / "bastion.env").read_text()


def test_dockhand_refusal_changes_nothing(server):
    before_auth = (server / "stacks" / "caddy" / "auth.env").read_text()
    before_bastion = (server / "stacks" / "bastion" / "bastion.env").read_text()
    calls = Calls(fail_dockhand="identifiant deja pris")
    with pytest.raises(ac.CredentialsError, match="rien n'a ete modifie") as err:
        run(server, calls)
    assert err.value.done == []
    assert (server / "stacks" / "caddy" / "auth.env").read_text() == before_auth
    assert (server / "stacks" / "bastion" / "bastion.env").read_text() == before_bastion
    assert calls.events == []


def test_bastion_failure_reports_what_was_already_applied(server):
    with pytest.raises(ac.CredentialsError, match="Bastion") as err:
        run(server, Calls(fail_bastion="Dockhand injoignable"))
    assert err.value.done == ["Dockhand", "Portail et interfaces d'administration"]


def test_without_bastion_installed_nothing_is_redeployed(server):
    (server / "stacks" / "bastion" / "bastion.env").unlink()
    calls = Calls()
    assert run(server, calls) == ["Dockhand", "Portail et interfaces d'administration"]
    assert ("bastion",) not in calls.events
    assert not (server / "stacks" / "bastion" / "bastion.env").exists()
