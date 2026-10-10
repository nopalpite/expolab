"""Tests de server/dockhand_setup.py contre un faux Dockhand a etat (comportement
verifie sur une vraie instance : creation de l'utilisateur sans auth tant qu'il
n'y en a pas, 401 une fois l'auth activee, jeton exigeant le mot de passe)."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
import dockhand_setup as ds  # noqa: E402


class FakeDockhand:
    def __init__(self, envs=None, users=None, auth_enabled=False):
        self.envs = list(envs or [])
        self.users = dict(users or {})  # nom -> mot de passe
        self.auth_enabled = auth_enabled
        self.tokens = set()
        self.sessions = set()
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload, code=200, headers=()):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
                cookie = self.headers.get("Cookie", "")
                return bearer in outer.tokens or any(s in cookie for s in outer.sessions)

            def _body(self):
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length)) if length else {}

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if self.path == "/api/auth/settings":
                    return self._send({"authEnabled": outer.auth_enabled})
                if self.path == "/api/environments":
                    if outer.auth_enabled and not self._authed():
                        return self._send({"error": "Unauthorized"}, 401)
                    return self._send(outer.envs)
                self._send({}, 404)

            def do_PUT(self):
                outer.calls.append(("PUT", self.path))
                body = self._body()
                if self.path == "/api/auth/settings":
                    if body.get("authEnabled") and not outer.users:
                        return self._send({"error": "no user"}, 400)
                    outer.auth_enabled = bool(body.get("authEnabled"))
                    return self._send({"authEnabled": outer.auth_enabled})
                self._send({}, 404)

            def do_POST(self):
                outer.calls.append(("POST", self.path))
                body = self._body()
                if self.path == "/api/environments":
                    if outer.auth_enabled and not self._authed():
                        return self._send({"error": "Unauthorized"}, 401)
                    env = {"id": len(outer.envs) + 1, "name": body["name"], "connectionType": body.get("connectionType")}
                    env["socketPath"] = body.get("socketPath")
                    outer.envs.append(env)
                    return self._send(env)
                if self.path == "/api/users":
                    if outer.users and not self._authed():
                        return self._send({"error": "Unauthorized"}, 401)
                    outer.users[body["username"]] = body["password"]
                    return self._send({"id": len(outer.users), "username": body["username"], "isAdmin": True}, 201)
                if self.path == "/api/auth/login":
                    if outer.users.get(body.get("username")) != body.get("password"):
                        return self._send({"error": "Invalid credentials"}, 401)
                    outer.sessions.add("sess1")
                    return self._send({"success": True}, 200, [("Set-Cookie", "session=sess1; Path=/")])
                if self.path == "/api/auth/tokens":
                    if not self._authed():
                        return self._send({"error": "Unauthorized"}, 401)
                    if not body.get("password"):
                        return self._send({"error": "Password is required to create an API token"}, 400)
                    token = f"dh_{len(outer.tokens) + 1:040d}"
                    outer.tokens.add(token)
                    return self._send({"id": 1, "token": token, "tokenPrefix": token[:8]}, 201)
                self._send({}, 404)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def make(monkeypatch):
    servers = []

    def factory(**kwargs):
        server = FakeDockhand(**kwargs)
        servers.append(server)
        monkeypatch.delenv("DOCKHAND_TOKEN", raising=False)
        return server

    yield factory
    for server in servers:
        server.close()


def test_creates_local_socket_environment_when_none(make):
    fake = make()
    env_id = ds.ensure_environment(ds.Client(fake.url))
    assert env_id == 1
    assert fake.envs[0]["connectionType"] == "socket"
    assert fake.envs[0]["socketPath"] == "/var/run/docker.sock"


def test_existing_environment_is_left_alone(make):
    fake = make(envs=[{"id": 7, "name": "autre"}])
    assert ds.ensure_environment(ds.Client(fake.url)) == 7
    assert ("POST", "/api/environments") not in fake.calls


def test_environment_with_auth_enabled_needs_a_token(make):
    fake = make(auth_enabled=True, users={"admin": "x"})
    with pytest.raises(ds.SetupError, match="authentification"):
        ds.ensure_environment(ds.Client(fake.url))


def test_first_setup_creates_user_enables_auth_and_stores_token(make, tmp_path):
    fake = make()
    env_file = tmp_path / "dockhand.env"
    assert ds.ensure_auth(fake.url, "admin", "Mot-de-passe-1", env_file) == "configure"
    assert fake.users == {"admin": "Mot-de-passe-1"}
    assert fake.auth_enabled is True
    token = env_file.read_text().strip().removeprefix("DOCKHAND_TOKEN=")
    assert token in fake.tokens
    if os.name != "nt":  # pas de droits POSIX sous Windows
        assert oct(env_file.stat().st_mode & 0o777) == oct(0o600)
    # le jeton obtenu ouvre bien l'API
    assert ds.ensure_environment(ds.Client(fake.url, token)) == 1


def test_second_run_with_valid_token_does_nothing(make, tmp_path):
    fake = make()
    env_file = tmp_path / "dockhand.env"
    ds.ensure_auth(fake.url, "admin", "Mot-de-passe-1", env_file)
    before = list(fake.calls)
    assert ds.ensure_auth(fake.url, "admin", "Mot-de-passe-1", env_file) == "deja"
    assert [c for c in fake.calls[len(before):] if c[0] != "GET"] == []


def test_lost_token_is_regenerated_with_the_same_credentials(make, tmp_path):
    fake = make(users={"admin": "Mot-de-passe-1"}, auth_enabled=True)
    env_file = tmp_path / "dockhand.env"
    assert ds.ensure_auth(fake.url, "admin", "Mot-de-passe-1", env_file) == "configure"
    assert env_file.read_text().startswith("DOCKHAND_TOKEN=dh_")


def test_other_lines_of_the_env_file_are_kept(make, tmp_path):
    fake = make()
    env_file = tmp_path / "dockhand.env"
    env_file.write_text("AUTRE=1\nDOCKHAND_TOKEN=perime\n")
    ds.ensure_auth(fake.url, "admin", "Mot-de-passe-1", env_file)
    lines = env_file.read_text().splitlines()
    assert "AUTRE=1" in lines
    assert sum(line.startswith("DOCKHAND_TOKEN=") for line in lines) == 1
    assert "DOCKHAND_TOKEN=perime" not in lines


def test_existing_account_with_other_password_gives_a_clear_error(make, tmp_path):
    fake = make(users={"admin": "autre-mot-de-passe"}, auth_enabled=True)
    with pytest.raises(ds.SetupError, match="refusee"):
        ds.ensure_auth(fake.url, "admin", "Mot-de-passe-1", tmp_path / "dockhand.env")
    assert not (tmp_path / "dockhand.env").exists()


def test_unreachable_dockhand_is_reported(tmp_path):
    with pytest.raises(ds.SetupError, match="joindre"):
        ds.ensure_auth("http://127.0.0.1:9", "admin", "Mot-de-passe-1", tmp_path / "dockhand.env")
