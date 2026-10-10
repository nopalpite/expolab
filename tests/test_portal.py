"""Tests du portail (portal/app.py) : connexion, cookie de session partage,
verification pour Caddy (forward_auth), basic auth, limitation des tentatives."""
import base64
import importlib.util
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("flask")
bcrypt = pytest.importorskip("bcrypt")
pytest.importorskip("yaml")

APP_PATH = Path(__file__).resolve().parent.parent / "portal" / "app.py"
USER = "gestion.expo"
PASSWORD = "Mdp-de-test-123"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
    auth_env = tmp_path / "auth.env"
    auth_env.write_text(f"BASIC_AUTH_USER={USER}\nBASIC_AUTH_PASSWORD=x\nBASIC_AUTH_HASH={pw_hash}\n")
    services = tmp_path / "services.yaml"
    services.write_text(
        "services:\n"
        "  - {name: dashboard, backend_port: 3001}\n"
        "  - {name: fleet, auth: true, backend_port: 5050, title: Flotte, group: Infrastructure, icon: server, description: Faux Pi}\n"
        "  - {name: dnsmasq, auth: true, backend_port: 5051, title: 'DHCP et DNS', group: Réseau}\n"
        "  - {name: git-mirror, auth: true, backend_port: 5054, framed: false}\n"
        "  - {name: cache, backend_port: 3142, portal: false}\n",
        encoding="utf-8",
    )
    ca = tmp_path / "root.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("PORTAL_SECRET", "s" * 32)
    monkeypatch.setenv("PORTAL_AUTH_ENV", str(auth_env))
    monkeypatch.setenv("PORTAL_SERVICES_FILE", str(services))
    monkeypatch.setenv("PORTAL_CA_FILE", str(ca))
    monkeypatch.setenv("PORTAL_DOMAIN", "web.expolab.lan")
    monkeypatch.setenv("PORTAL_TLS_MODE", "internal")
    spec = importlib.util.spec_from_file_location("portal_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["portal_app"] = module  # Flask retrouve templates/ et static/ via le module
    spec.loader.exec_module(module)
    module.app.testing = True
    module.auth_env = auth_env
    module.client = module.app.test_client(use_cookies=False)  # on gere le cookie a la main
    return module


def basic(user=USER, password=PASSWORD):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def login(portal, password=PASSWORD, user=USER, **extra):
    return portal.client.post("/login", data={"username": user, "password": password, **extra})


def session_cookie(response) -> str:
    cookies = [c for c in response.headers.getlist("Set-Cookie") if c.startswith("expolab_session=")]
    assert cookies, "pas de cookie de session"
    return cookies[0].split(";")[0]


def logged_in(portal):
    """Arguments d'une requete authentifiee par le cookie de session."""
    return {"headers": {"Cookie": session_cookie(login(portal))}}


# --- connexion et cookie ----------------------------------------------------

def test_login_success_sets_a_shared_httponly_cookie(portal):
    res = login(portal)
    assert res.status_code == 303
    cookie = [c for c in res.headers.getlist("Set-Cookie") if c.startswith("expolab_session=")][0]
    assert "Domain=web.expolab.lan" in cookie  # partage avec tous les sous-domaines
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie


def test_login_wrong_password_or_user_is_refused_without_cookie(portal):
    for kwargs in ({"password": "mauvais"}, {"user": "autre"}):
        res = login(portal, **kwargs)
        assert res.status_code == 401
        assert b"incorrect" in res.data
        assert not [c for c in res.headers.getlist("Set-Cookie") if c.startswith("expolab_session=")]


def test_too_many_failures_lock_even_the_right_password(portal):
    for _ in range(portal.LOGIN_LIMIT):
        assert login(portal, password="mauvais").status_code == 401
    res = login(portal)  # le bon mot de passe, mais trop d'echecs avant
    assert res.status_code == 429


def test_logout_clears_the_cookie(portal):
    res = portal.client.post("/logout")
    assert res.status_code == 303
    assert any(c.startswith("expolab_session=;") for c in res.headers.getlist("Set-Cookie"))


# --- /auth/verify (appele par Caddy) ----------------------------------------

def test_verify_accepts_the_session_cookie(portal):
    res = portal.client.get("/auth/verify", **logged_in(portal))
    assert res.status_code == 204


def test_verify_redirects_browsers_to_login_with_the_original_url(portal):
    res = portal.client.get(
        "/auth/verify",
        headers={"Accept": "text/html", "X-Forwarded-Host": "fleet.web.expolab.lan", "X-Forwarded-Uri": "/api/x?y=1", "X-Forwarded-Proto": "https"},
    )
    assert res.status_code == 302
    target = urlparse(res.headers["Location"])
    assert (target.netloc, target.path) == ("dashboard.web.expolab.lan", "/login")
    assert parse_qs(target.query)["next"] == ["https://fleet.web.expolab.lan/api/x?y=1"]


def test_verify_answers_401_with_basic_challenge_to_non_browsers(portal):
    res = portal.client.get("/auth/verify", headers={"Accept": "*/*"})
    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"].startswith("Basic")


def test_verify_accepts_basic_auth_for_curl_and_git(portal):
    assert portal.client.get("/auth/verify", headers={"Authorization": basic()}).status_code == 204
    assert portal.client.get("/auth/verify", headers={"Authorization": basic(password="x")}).status_code == 401
    assert portal.client.get("/auth/verify", headers={"Authorization": "Basic !!pas-du-base64"}).status_code == 401


def test_verify_does_not_redirect_non_get_requests(portal):
    res = portal.client.get("/auth/verify", headers={"Accept": "text/html", "X-Forwarded-Method": "POST"})
    assert res.status_code == 401


def test_verify_fails_closed_without_credentials(portal):
    portal.auth_env.unlink()
    assert portal.client.get("/auth/verify", headers={"Authorization": basic()}).status_code == 503


def test_tampered_cookie_is_rejected(portal):
    cookie = session_cookie(login(portal))
    name, _, value = cookie.partition("=")
    body, _, sig = value.partition(".")
    for forged in (f"{body}.{'0' * len(sig)}", f"{body}x.{sig}", "n-importe-quoi", ""):
        res = portal.client.get("/auth/verify", headers={"Accept": "*/*", "Cookie": f"{name}={forged}"})
        assert res.status_code == 401, forged


def test_expired_cookie_is_rejected(portal):
    creds = portal.credentials()
    old = portal.make_token(USER, creds, now=time.time() - portal.SESSION_SECONDS - 10)
    res = portal.client.get("/auth/verify", headers={"Accept": "*/*", "Cookie": f"expolab_session={old}"})
    assert res.status_code == 401


def test_changing_the_password_invalidates_existing_sessions(portal):
    session = logged_in(portal)
    assert portal.client.get("/auth/verify", **session).status_code == 204
    new_hash = bcrypt.hashpw(b"Nouveau-mdp-456", bcrypt.gensalt(rounds=4)).decode()
    portal.auth_env.write_text(f"BASIC_AUTH_USER={USER}\nBASIC_AUTH_HASH={new_hash}\n")
    os.utime(portal.auth_env, (time.time() + 5, time.time() + 5))  # mtime change : relu
    assert portal.client.get("/auth/verify", headers={"Accept": "*/*", **session["headers"]}).status_code == 401


# --- redirection apres connexion --------------------------------------------

def test_next_only_allows_the_portal_and_its_subdomains(portal):
    assert portal.safe_next("https://fleet.web.expolab.lan/page") == "https://fleet.web.expolab.lan/page"
    assert portal.safe_next("/#fleet") == "/#fleet"
    for evil in ("https://evil.example.com/", "//evil.example.com", "https://web.expolab.lan.evil.com/", "javascript:alert(1)"):
        assert portal.safe_next(evil) == "/", evil
    res = login(portal, next="https://evil.example.com/")
    assert res.headers["Location"] == "/"


# --- interface et API -------------------------------------------------------

def test_index_requires_login_then_shows_the_user(portal):
    assert portal.client.get("/").headers["Location"].endswith("/login")
    res = portal.client.get("/", **logged_in(portal))
    assert res.status_code == 200
    assert USER.encode() in res.data


def test_api_requires_login(portal):
    assert portal.client.get("/api/services").status_code == 401
    assert portal.client.get("/api/status").status_code == 401


def test_services_listing_for_the_drawer(portal, monkeypatch):
    monkeypatch.setattr(portal, "tcp_up", lambda host, port, timeout=0.4: port != 5051)
    data = portal.client.get("/api/services", **logged_in(portal)).get_json()["services"]
    names = [s["name"] for s in data]
    assert names == ["fleet", "dnsmasq", "git-mirror"]  # ni le portail, ni portal: false
    fleet, dnsmasq, git = data
    assert fleet == {
        "name": "fleet", "title": "Flotte", "group": "Infrastructure", "icon": "server",
        "description": "Faux Pi", "framed": True, "url": "https://fleet.web.expolab.lan/", "up": True,
    }
    assert dnsmasq["group"] == "Réseau" and dnsmasq["up"] is False
    assert git["framed"] is False and git["title"] == "git-mirror" and git["group"] == "Autres"
    assert all("_backend" not in s for s in data)


def test_login_page_is_never_framable_by_other_sites(portal):
    res = portal.client.get("/login")
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]


def test_root_certificate_download(portal):
    res = portal.client.get("/ca.crt")
    assert res.status_code == 200 and b"BEGIN CERTIFICATE" in res.data
    portal.TLS_MODE = "signed"
    assert portal.client.get("/ca.crt").status_code == 404
