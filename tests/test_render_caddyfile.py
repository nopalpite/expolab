"""Tests de server/render-caddyfile.py (modes TLS, basic_auth, echec ferme)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "server" / "render-caddyfile.py"
HASH = "$2a$14$FAKEHASHFORTESTSONLY"


def render(tmp_path, services, *args, auth=True):
    services_file = tmp_path / "services.yaml"
    services_file.write_text(yaml.safe_dump({"services": services}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASIC_AUTH")}
    if auth:
        env.update(BASIC_AUTH_USER="admin", BASIC_AUTH_HASH=HASH)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(services_file), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_internal_mode_uses_tls_internal(tmp_path):
    result = render(tmp_path, [{"name": "dashboard", "backend_port": 3001}])
    assert result.returncode == 0
    assert "dashboard.web.expolab.lan {" in result.stdout
    assert "tls internal" in result.stdout
    assert "basic_auth" not in result.stdout


def test_signed_mode_requires_domain(tmp_path):
    result = render(tmp_path, [{"name": "dashboard", "backend_port": 3001}], "signed")
    assert result.returncode == 1
    assert "signed" in result.stderr


def test_signed_mode_references_ovh_env_and_domain(tmp_path):
    result = render(tmp_path, [{"name": "dashboard", "backend_port": 3001}], "signed", "expolab.example.fr")
    assert result.returncode == 0
    assert "dashboard.expolab.example.fr {" in result.stdout
    assert "dns ovh {" in result.stdout
    # jamais de secret ecrit en clair : uniquement des references d'environnement
    assert "{env.OVH_APPLICATION_SECRET}" in result.stdout


def test_auth_true_emits_basic_auth(tmp_path):
    result = render(tmp_path, [{"name": "caddy", "auth": True, "backend_port": 5052}])
    assert result.returncode == 0
    assert f"admin {HASH}" in result.stdout


def test_auth_except_excludes_paths(tmp_path):
    result = render(
        tmp_path,
        [{"name": "git-mirror", "auth": True, "auth_except": ["/git/*"], "backend_port": 5054}],
    )
    assert result.returncode == 0
    assert "not path /git/*" in result.stdout
    assert "basic_auth @protected" in result.stdout


def test_auth_without_credentials_fails_closed(tmp_path):
    # Sans identifiants, un service `auth: true` ne doit JAMAIS finir ouvert.
    # Le vrai auth.env (present sur un poste deja deploye) fausserait le test.
    if (REPO / "server" / "stacks" / "caddy" / "auth.env").exists():
        pytest.skip("auth.env local present")
    result = render(tmp_path, [{"name": "caddy", "auth": True, "backend_port": 5052}], auth=False)
    assert result.returncode == 1
    assert "refuse de generer" in result.stderr


def test_extra_routes_come_before_catch_all(tmp_path):
    result = render(
        tmp_path,
        [{"name": "bastion", "backend_port": 5000, "extra_routes": [{"path": "/vnc-ws/*", "backend_port": 6080}]}],
    )
    out = result.stdout
    assert out.index("/vnc-ws/*") < out.index("localhost:5000")


def test_repo_services_yaml_renders_in_both_modes(tmp_path):
    services = REPO / "server" / "services.yaml"
    env = {**os.environ, "BASIC_AUTH_USER": "admin", "BASIC_AUTH_HASH": HASH}
    for args in ([], ["signed", "expolab.example.fr"]):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(services), *args], capture_output=True, text=True, env=env
        )
        assert result.returncode == 0, result.stderr
        assert "ansible-web." in result.stdout
