"""Tests de server/dockhand_stack.py contre un faux serveur Dockhand local."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))
import dockhand_stack  # noqa: E402

COMPOSE = "services:\n  app:\n    build: ${REPO_ROOT}/app\n    volumes:\n      - $REPO_ROOT/data:/data\n      - ${OTHER}:/x\n"


class FakeDockhand:
    """Serveur HTTP minimal imitant les endpoints Dockhand utilises."""

    def __init__(self, environments=None, existing=(), job_statuses=("running", "done")):
        self.environments = [{"id": 1}] if environments is None else environments
        self.existing = set(existing)
        self.job_statuses = list(job_statuses)
        self.calls = []
        self.posted = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if self.path == "/api/environments":
                    self._send(outer.environments)
                elif self.path.startswith("/api/stacks"):
                    self._send([{"name": n} for n in outer.existing])
                elif self.path.startswith("/api/jobs/"):
                    status = outer.job_statuses.pop(0) if len(outer.job_statuses) > 1 else outer.job_statuses[0]
                    self._send({"id": "j1", "status": status})
                else:
                    self._send({}, 404)

            def do_DELETE(self):
                outer.calls.append(("DELETE", self.path))
                self._send({"success": True})

            def do_POST(self):
                outer.calls.append(("POST", self.path))
                length = int(self.headers.get("Content-Length", 0))
                outer.posted = json.loads(self.rfile.read(length))
                self._send({"jobId": "j1"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake(monkeypatch):
    servers = []

    def make(**kwargs):
        server = FakeDockhand(**kwargs)
        servers.append(server)
        monkeypatch.setenv("DOCKHAND_URL", server.url)
        monkeypatch.delenv("DOCKHAND_TOKEN", raising=False)
        monkeypatch.setattr(dockhand_stack, "ENV_FILE", Path("/nonexistent/dockhand.env"))
        monkeypatch.setattr(dockhand_stack.time, "sleep", lambda _: None)
        return server

    yield make
    for server in servers:
        server.close()


@pytest.fixture
def compose_file(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(COMPOSE)
    return path


def test_substitutes_only_repo_root():
    out = dockhand_stack.substitute_repo_root(COMPOSE, "/home/pi/expolab")
    assert "/home/pi/expolab/app" in out
    assert "/home/pi/expolab/data" in out
    assert "${OTHER}" in out  # autres variables laissees intactes
    assert "REPO_ROOT" not in out


def test_creates_stack_when_absent(fake, compose_file):
    server = fake()
    status = dockhand_stack.upsert_stack("app", compose_file, "/r", rebuild_images=False)
    assert status == "done"
    assert not any(method == "DELETE" for method, _ in server.calls)
    assert server.posted["name"] == "app"
    assert server.posted["envId"] == 1
    assert server.posted["deploy"] is True
    assert "/r/app" in server.posted["compose"]


def test_deletes_existing_stack_before_recreating(fake, compose_file):
    server = fake(existing=["app"])
    dockhand_stack.upsert_stack("app", compose_file, "/r", rebuild_images=False)
    methods = [m for m, _ in server.calls]
    assert methods.index("DELETE") < methods.index("POST")


def test_waits_for_job_to_finish(fake, compose_file):
    server = fake(job_statuses=["running", "running", "done"])
    status = dockhand_stack.upsert_stack("app", compose_file, "/r", rebuild_images=False)
    assert status == "done"
    assert sum(1 for m, p in server.calls if m == "GET" and p.startswith("/api/jobs/")) == 3


def test_no_wait_returns_immediately(fake, compose_file):
    server = fake()
    status = dockhand_stack.upsert_stack("app", compose_file, "/r", rebuild_images=False, wait=False)
    assert status == ""
    assert not any(p.startswith("/api/jobs/") for _, p in server.calls)


def test_no_environment_raises(fake, compose_file):
    fake(environments=[])
    with pytest.raises(dockhand_stack.NoEnvironment):
        dockhand_stack.upsert_stack("app", compose_file, "/r", rebuild_images=False)


def test_cli_exit_codes(fake, compose_file):
    fake(environments=[])
    env = {**os.environ, "REPO_ROOT": "/r", "DOCKHAND_URL": os.environ["DOCKHAND_URL"]}
    result = subprocess.run(
        [sys.executable, str(SERVER_DIR / "dockhand_stack.py"), "upsert", "app", str(compose_file), "--no-rebuild"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == dockhand_stack.EXIT_NO_ENV

    result = subprocess.run(
        [sys.executable, str(SERVER_DIR / "dockhand_stack.py"), "upsert", "app", str(compose_file)],
        env={k: v for k, v in env.items() if k != "REPO_ROOT"}, capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "REPO_ROOT" in result.stderr


def test_unreachable_dockhand_is_an_error(monkeypatch, compose_file):
    monkeypatch.setenv("DOCKHAND_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(dockhand_stack, "ENV_FILE", Path("/nonexistent/dockhand.env"))
    with pytest.raises(dockhand_stack.DockhandError):
        dockhand_stack.upsert_stack("app", compose_file, "/r", rebuild_images=False)


def test_rebuild_tolerates_missing_docker(monkeypatch):
    monkeypatch.setenv("PATH", "")
    dockhand_stack.remove_cached_images("app")  # ne doit pas lever
