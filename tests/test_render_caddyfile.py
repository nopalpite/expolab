"""Tests de server/render-caddyfile.py (modes TLS, forward_auth vers le portail,
cadres autorises uniquement depuis le portail, echec ferme)."""
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "server" / "render-caddyfile.py"
PORTAL = {"name": "dashboard", "backend_port": 3001}


def render(tmp_path, services, *args):
    services_file = tmp_path / "services.yaml"
    services_file.write_text(yaml.safe_dump({"services": services}, allow_unicode=True), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(services_file), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def vhost(output: str, name: str, domain: str = "web.expolab.lan") -> str:
    start = output.index(f"{name}.{domain} {{")
    return output[start : output.index("\n}\n", start)]


def test_internal_mode_uses_tls_internal(tmp_path):
    result = render(tmp_path, [PORTAL])
    assert result.returncode == 0
    assert "dashboard.web.expolab.lan {" in result.stdout
    assert "tls internal" in result.stdout
    assert "forward_auth" not in result.stdout


def test_signed_mode_requires_domain(tmp_path):
    result = render(tmp_path, [PORTAL], "signed")
    assert result.returncode == 1
    assert "signed" in result.stderr


def test_signed_mode_references_ovh_env_and_domain(tmp_path):
    result = render(tmp_path, [PORTAL], "signed", "expolab.example.fr")
    assert result.returncode == 0
    assert "dashboard.expolab.example.fr {" in result.stdout
    assert "dns ovh {" in result.stdout
    # jamais de secret ecrit en clair : uniquement des references d'environnement
    assert "{env.OVH_APPLICATION_SECRET}" in result.stdout


def test_auth_true_delegates_to_the_portal(tmp_path):
    result = render(tmp_path, [PORTAL, {"name": "caddy", "auth": True, "backend_port": 5052}])
    assert result.returncode == 0
    block = vhost(result.stdout, "caddy")
    assert "forward_auth localhost:3001 {" in block
    assert "uri /auth/verify" in block
    assert "basic_auth" not in result.stdout
    # la verification passe AVANT le proxy
    assert block.index("forward_auth") < block.index("reverse_proxy")


def test_auth_uses_the_declared_portal_address(tmp_path):
    portal = {"name": "dashboard", "backend_host": "10.0.0.5", "backend_port": 4000}
    result = render(tmp_path, [portal, {"name": "caddy", "auth": True, "backend_port": 5052}])
    assert "forward_auth 10.0.0.5:4000 {" in result.stdout


def test_auth_except_excludes_paths(tmp_path):
    result = render(
        tmp_path,
        [PORTAL, {"name": "git-mirror", "auth": True, "auth_except": ["/git/*"], "backend_port": 5054}],
    )
    assert result.returncode == 0
    assert "not path /git/*" in result.stdout
    assert "forward_auth @protected localhost:3001 {" in result.stdout


def test_auth_without_portal_fails_closed(tmp_path):
    # Sans portail pour verifier la session, un service `auth: true` ne doit JAMAIS finir ouvert.
    result = render(tmp_path, [{"name": "caddy", "auth": True, "backend_port": 5052}])
    assert result.returncode == 1
    assert "refuse de generer" in result.stderr


def test_framed_services_only_allow_the_portal_as_parent(tmp_path):
    result = render(tmp_path, [PORTAL, {"name": "fleet", "auth": True, "backend_port": 5050}])
    block = vhost(result.stdout, "fleet")
    assert 'header +Content-Security-Policy "frame-ancestors https://dashboard.web.expolab.lan"' in block
    # le service ne peut pas imposer son propre refus de cadre
    assert "header_down -X-Frame-Options" in block
    assert "frame-ancestors[^;]*" in block


def test_frame_parent_follows_the_public_domain(tmp_path):
    result = render(tmp_path, [PORTAL, {"name": "fleet", "backend_port": 5050}], "signed", "expolab.example.fr")
    assert 'frame-ancestors https://dashboard.expolab.example.fr"' in result.stdout


def test_portal_itself_is_never_framed_by_caddy_headers(tmp_path):
    result = render(tmp_path, [PORTAL])
    assert "header_down" not in result.stdout
    assert "frame-ancestors" not in result.stdout  # le portail pose lui-meme frame-ancestors 'none'


def test_framed_false_service_is_left_untouched(tmp_path):
    result = render(tmp_path, [PORTAL, {"name": "git-mirror", "framed": False, "backend_port": 5054}])
    block = vhost(result.stdout, "git-mirror")
    assert "header_down" not in block and "frame-ancestors" not in block


def test_extra_routes_come_before_catch_all(tmp_path):
    result = render(
        tmp_path,
        [{"name": "bastion", "backend_port": 5000, "extra_routes": [{"path": "/vnc-ws/*", "backend_port": 6080}]}],
    )
    out = result.stdout
    assert out.index("/vnc-ws/*") < out.index("localhost:5000")


def test_repo_services_yaml_renders_in_both_modes(tmp_path):
    services = REPO / "server" / "services.yaml"
    for args in ([], ["signed", "expolab.example.fr"]):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(services), *args], capture_output=True, text=True, encoding="utf-8"
        )
        assert result.returncode == 0, result.stderr
        assert "ansible-web." in result.stdout
        assert "basic_auth" not in result.stdout
        data = yaml.safe_load(services.read_text(encoding="utf-8"))
        protected = [s["name"] for s in data["services"] if s.get("auth")]
        assert protected, "aucun service protege dans services.yaml ?"
        assert result.stdout.count("forward_auth") == len(protected)
