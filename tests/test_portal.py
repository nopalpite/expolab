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
sys.path.insert(0, str(APP_PATH.parent))  # plans.py, importe par app.py
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
    monkeypatch.setenv("PORTAL_OPERATOR_ENV", str(tmp_path / "operator.env"))
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


# --- compte exploitant : role, acces limite, plans -----------------------------------------------

OP_USER, OP_PASSWORD = "exploitant", "Mdp-exploit-1"


def set_operator(portal, tmp_path, password=OP_PASSWORD):
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    (tmp_path / "operator.env").write_text(f"OPERATOR_USER={OP_USER}\nOPERATOR_HASH={pw_hash}\n")
    portal._operator_cache.update(mtime=None, value=None)


def op_cookie(portal):
    res = login(portal, password=OP_PASSWORD, user=OP_USER, next="https://dockhand.web.expolab.lan/")
    assert res.status_code == 303 and res.headers["Location"] == "/"   # jamais vers une page d'administration
    return {"headers": {"Cookie": session_cookie(res)}}


def verify(portal, auth, host, uri="/", accept="application/json"):
    headers = {**auth["headers"], "X-Forwarded-Host": host, "X-Forwarded-Uri": uri, "X-Forwarded-Proto": "https", "Accept": accept}
    return portal.client.get("/auth/verify", headers=headers)


def test_no_operator_account_means_no_operator_login(portal):
    assert login(portal, password=OP_PASSWORD, user=OP_USER).status_code == 401


def test_operator_reaches_only_the_exploitation_pages(portal, tmp_path):
    set_operator(portal, tmp_path)
    auth = op_cookie(portal)
    flasher = "sd-flasher.web.expolab.lan"
    for uri in ("/exploitation", "/exploitation?machine=rpi-test", "/api/exploitation/status", "/api/exploitation/flash", "/static/exploitation.js"):
        assert verify(portal, auth, flasher, uri).status_code == 204, uri
    for host, uri in ((flasher, "/"), (flasher, "/api/flash"), (flasher, "/api/settings"), (flasher, "/api/images/x.img/file"),
                      (flasher, "/exploitation/../api/settings"), ("dockhand.web.expolab.lan", "/exploitation"),
                      ("bastion.web.expolab.lan", "/"), ("dnsmasq.web.expolab.lan", "/api/topology")):
        assert verify(portal, auth, host, uri).status_code == 403, (host, uri)
    page = verify(portal, auth, "dockhand.web.expolab.lan", "/", accept="text/html")
    assert page.status_code == 302 and page.headers["Location"] == "https://dashboard.web.expolab.lan/"
    assert portal.client.get("/api/services", **auth).status_code == 401
    assert portal.client.get("/api/status", **auth).status_code == 401
    home = portal.client.get("/", **auth)
    assert home.status_code == 200 and b"Plans des salles" in home.data and b"https://sd-flasher.web.expolab.lan/exploitation" in home.data
    assert b"drawer" not in home.data   # pas le tiroir des services de l'administrateur


def test_admin_is_unchanged(portal, tmp_path):
    set_operator(portal, tmp_path)
    auth = logged_in(portal)
    assert verify(portal, auth, "dockhand.web.expolab.lan", "/").status_code == 204
    assert verify(portal, auth, "sd-flasher.web.expolab.lan", "/api/settings").status_code == 204
    assert b"drawer" in portal.client.get("/", **auth).data


def test_an_operator_token_cannot_pass_for_an_admin(portal, tmp_path):
    set_operator(portal, tmp_path)
    op = portal.operator_credentials()
    forged = portal.make_token(USER, op, role=portal.ADMIN)   # signe avec la cle de l'exploitant
    headers = {"Cookie": f"expolab_session={forged}", "X-Forwarded-Host": "dockhand.web.expolab.lan", "X-Forwarded-Uri": "/"}
    assert portal.client.get("/auth/verify", headers=headers).status_code == 401
    other = portal.make_token(OP_USER, op, role="superadmin")
    assert portal.read_session(other, admin=portal.credentials(), operator=op) is None


def test_changing_the_operator_password_closes_only_operator_sessions(portal, tmp_path):
    set_operator(portal, tmp_path)
    op_auth, admin_auth = op_cookie(portal), logged_in(portal)
    set_operator(portal, tmp_path, password="Autre-mdp-2")
    assert portal.client.get("/", **op_auth).status_code == 302           # renvoye a la connexion
    assert verify(portal, admin_auth, "dockhand.web.expolab.lan").status_code == 204


def test_old_admin_tokens_without_role_stay_valid(portal):
    creds = portal.credentials()
    payload = base64.urlsafe_b64encode(b'{"u":"%s","exp":%d}' % (USER.encode(), int(time.time()) + 600)).rstrip(b"=").decode()
    import hashlib
    import hmac as _hmac
    sig = _hmac.new(portal._signing_key(creds), payload.encode(), hashlib.sha256).hexdigest()
    assert portal.read_token(f"{payload}.{sig}", creds) == USER


def test_plans_api_and_image_whitelist(portal, tmp_path, monkeypatch):
    set_operator(portal, tmp_path)
    maps = tmp_path / "maps"
    maps.mkdir()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1001).to_bytes(4, "big") + (563).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    (maps / "expo.png").write_bytes(png)
    (maps / "secret.txt").write_text("non")
    cfg = tmp_path / "machines.yaml"
    cfg.write_text("""rooms:
- {id: expo, name: Expo, map_image: expo.png}
machines:
- id: a
  name: rpi-test.expolab.lan
  host: 10.42.0.134
  room: expo
  position: {x: 57.04, y: 76.47}
  credentials: {password: SECRET-DE-BASTION}
- {id: b, name: projo, host: projo.expolab.lan, room: expo, position: {x: 40, y: 46}}
- {id: c, name: borne, host: 10.42.0.200, room: expo}
""")
    monkeypatch.setattr(portal.plans, "BASTION_CONFIG", cfg)
    monkeypatch.setattr(portal.plans, "BASTION_MAPS", maps)
    topology = {"presence_ok": True, "now": 1000, "machines": [
        {"hostname": "rpi-test", "ip": "10.42.0.134", "state": "online", "since": 900},
        {"hostname": "projo", "ip": "10.42.0.150", "state": "offline", "since": 960},
    ]}
    monkeypatch.setattr(portal.plans, "fetch_presence", lambda: topology)
    auth = op_cookie(portal)
    data = portal.client.get("/api/plans", **auth).get_json()
    room = data["rooms"][0]
    assert room["ratio"] == [1001, 563] and room["ratio_known"] and room["unplaced"] == 1
    assert [(m["label"], m["x"], m["y"], m["state"]) for m in room["machines"]] == [("rpi-test", 57.04, 76.47, "up"), ("projo", 40.0, 46.0, "down")]
    assert b"SECRET-DE-BASTION" not in portal.client.get("/api/plans", **auth).data
    assert portal.client.get("/plans/image/expo.png", **auth).data == png
    assert portal.client.get("/plans/image/secret.txt", **auth).status_code == 404
    assert portal.client.get("/plans/image/..%2Fmachines.yaml", **auth).status_code == 404
    assert portal.client.get("/api/plans").status_code == 401


sys.path.insert(0, str(APP_PATH.parent))
import plans as _plans_module  # noqa: E402


def test_plans_unknown_presence_and_jpeg_size(tmp_path):
    jpg = tmp_path / "plan.jpg"
    jpg.write_bytes(b"\xff\xd8" + b"\xff\xe0\x00\x04xx" + b"\xff\xc0\x00\x11\x08" + (300).to_bytes(2, "big") + (640).to_bytes(2, "big") + b"\x00" * 12)
    assert _plans_module.image_size(jpg) == (640, 300)
    assert _plans_module.image_size(tmp_path / "absent.png") is None
    rooms = [{"id": "r", "name": "R", "map_image": "plan.svg"}]
    machines = [{"id": "a", "name": "a", "host": "10.42.0.9", "room": "r", "position": {"x": 1, "y": 2}}]
    data = _plans_module.build_plans(rooms, machines, None, tmp_path)
    assert data["presence_ok"] is False and data["rooms"][0]["machines"][0]["state"] == "unknown"
    assert data["rooms"][0]["ratio"] == list(_plans_module.DEFAULT_RATIO) and not data["rooms"][0]["ratio_known"]
