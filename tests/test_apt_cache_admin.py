"""Tests d'apt-cache-admin : analyse du journal, statistiques, miroirs HTTPS, mode, redemarrage."""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

APP_PATH = Path(__file__).resolve().parent.parent / "apt-cache-admin" / "app.py"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    config, logs, cache, leases = (tmp_path / n for n in ("config", "logs", "cache", "leases"))
    for d in (config, logs, cache, leases):
        d.mkdir()
    (config / "mode").write_text("online\n")
    (config / "https-mirrors.conf").write_text("# commentaire\nhttps://download.docker.com/linux/debian\n")
    (leases / "dnsmasq.leases").write_text("1791692370 dc:a6:32:00:00:01 10.42.0.148 bobby 01:dc:a6:32:00:00:01\n1791692371 dc:a6:32:00:00:02 10.42.0.149 * *\n")
    monkeypatch.setenv("APT_CACHE_CONFIG_DIR", str(config))
    monkeypatch.setenv("APT_CACHE_LOG_FILE", str(logs / "apt-cacher.log"))
    monkeypatch.setenv("APT_CACHE_DATA_DIR", str(cache))
    monkeypatch.setenv("APT_CACHE_LEASES_FILE", str(leases / "dnsmasq.leases"))
    spec = importlib.util.spec_from_file_location("apt_cache_admin_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["apt_cache_admin_app"] = module  # Flask retrouve templates/ et static/
    spec.loader.exec_module(module)
    module.app.testing = True
    module.restarts = []
    module.restart_ok = (True, "")
    monkeypatch.setattr(module, "restart_cache", lambda: (module.restarts.append(1), module.restart_ok)[1])
    monkeypatch.setattr(module, "container_running", lambda: True)
    module.config, module.logs, module.cache = config, logs, cache
    module.client = module.app.test_client()
    return module


def write_log(admin, lines):
    (admin.logs / "apt-cacher.log").write_text("\n".join(lines) + "\n")


LOG = [
    # figlet : telecharge (I) puis servi (O) = un echec de cache
    "1791656426|I|135796|10.42.0.148|debrep/pool/main/f/figlet/figlet_2.2.5-3_arm64.deb",
    "1791656426|O|134537|10.42.0.148|debrep/pool/main/f/figlet/figlet_2.2.5-3_arm64.deb",
    # meme paquet re-servi plus tard : O seul = servi par le cache
    "1791656447|O|134537|10.42.0.148|debrep/pool/main/f/figlet/figlet_2.2.5-3_arm64.deb",
    "1791656500|O|134537|10.42.0.149|debrep/pool/main/f/figlet/figlet_2.2.5-3_arm64.deb",
    # index : ignores des statistiques de paquets
    "1791656424|I|760|10.42.0.148|debrep/dists/bookworm/InRelease",
    "1791656424|O|102|10.42.0.148|debrep/dists/bookworm/InRelease",
    "ligne invalide",
    "abc|I|1|x|y",
]


# --- journal -------------------------------------------------------------------

def test_parse_line_and_invalid_lines(admin):
    assert admin.parse_line(LOG[0]) == {
        "time": 1791656426, "dir": "I", "bytes": 135796, "client": "10.42.0.148",
        "path": "debrep/pool/main/f/figlet/figlet_2.2.5-3_arm64.deb",
    }
    assert admin.parse_line("ligne invalide") is None
    assert admin.parse_line("abc|I|1|x|y") is None
    assert admin.parse_line("1|X|1|x|y") is None


def test_stats_count_hits_downloads_and_clients(admin):
    write_log(admin, LOG)
    entries, _ = admin.read_entries()
    stats = admin.compute_stats(entries, admin.load_names())
    assert (stats["requests"], stats["downloads"], stats["hits"]) == (3, 1, 2)
    assert stats["hit_ratio"] == pytest.approx(0.667, abs=0.001)
    assert stats["saved_bytes"] == 134537 * 3 - 135796
    by_client = {c["client"]: c for c in stats["clients"]}
    assert by_client["10.42.0.148"]["name"] == "bobby" and by_client["10.42.0.148"]["requests"] == 2
    assert by_client["10.42.0.149"]["name"] == "" and by_client["10.42.0.149"]["requests"] == 1  # bail sans nom


def test_stats_on_an_empty_log(admin):
    entries, size = admin.read_entries()
    stats = admin.compute_stats(entries, {})
    assert (entries, size) == ([], 0)
    assert stats["hit_ratio"] is None and stats["since"] is None and stats["clients"] == []


def test_only_the_tail_of_a_big_log_is_read_and_the_cut_line_is_dropped(admin):
    lines = [f"{1791650000 + i}|O|10|10.42.0.148|debrep/pool/main/p/p{i}.deb" for i in range(2000)]
    write_log(admin, lines)
    entries, size = admin.read_entries(max_bytes=5000)
    assert size > 5000 and 0 < len(entries) < 2000
    assert entries[-1]["path"].endswith("p1999.deb")
    assert all(e["bytes"] == 10 for e in entries)  # aucune ligne tronquee n'a ete interpretee


def test_recent_entries_filters_and_orders_newest_first(admin):
    write_log(admin, LOG)
    entries, _ = admin.read_entries()
    names = admin.load_names()
    recent = admin.recent_entries(entries, names, 100, "", "", only_packages=True)
    assert [e["time"] for e in recent] == sorted((e["time"] for e in recent), reverse=True)
    assert all(e["path"].endswith(".deb") for e in recent)
    assert {e["kind"] for e in recent} == {"hit", "miss"}
    assert len(recent) == 3  # un acces "miss" (I puis O regroupes) + deux "hit"
    assert len(admin.recent_entries(entries, names, 100, "", "", only_packages=False)) == 4
    assert {e["client"] for e in admin.recent_entries(entries, names, 100, "bobby", "", True)} == {"10.42.0.148"}  # par NOM
    assert {e["client"] for e in admin.recent_entries(entries, names, 100, "149", "", True)} == {"10.42.0.149"}
    assert len(admin.recent_entries(entries, names, 100, "", "FIGLET", True)) == 3
    assert admin.recent_entries(entries, names, 100, "", "inexistant", True) == []
    assert len(admin.recent_entries(entries, names, 2, "", "", True)) == 2


# --- cache et disque -------------------------------------------------------------

def test_cache_usage_counts_packages_and_bytes(admin):
    (admin.cache / "debrep").mkdir()
    (admin.cache / "debrep" / "a.deb").write_bytes(b"x" * 100)
    (admin.cache / "debrep" / "b.deb").write_bytes(b"x" * 50)
    (admin.cache / "debrep" / "InRelease").write_bytes(b"x" * 10)
    usage = admin.cache_usage(now=time.time() + 1000)
    assert usage == {"packages": 2, "bytes": 160}
    (admin.cache / "debrep" / "c.deb").write_bytes(b"x")
    assert admin.cache_usage(now=time.time() + 1001)["packages"] == 2  # resultat memorise


# --- miroirs HTTPS ---------------------------------------------------------------

def test_mirror_validation(admin):
    assert admin.check_mirrors(["https://download.docker.com/linux/debian/", " https://x.example.org "]) == (
        ["https://download.docker.com/linux/debian", "https://x.example.org"], None)
    for bad in ("http://pas-https.example.org/", "https://a b.example.org", "https://x.example.org/$(rm)", "ftp://x", "https://"):
        assert admin.check_mirrors([bad])[1], bad
    assert admin.check_mirrors(["https://a.example.org", "https://a.example.org/"])[1]  # doublon
    assert admin.check_mirrors("pas une liste")[1]
    assert admin.check_mirrors(["https://h%d.example.org" % i for i in range(admin.MAX_MIRRORS + 1)])[1]


def test_get_mirrors_ignores_comments(admin):
    assert admin.client.get("/api/mirrors").get_json() == {"mirrors": ["https://download.docker.com/linux/debian"]}


def test_save_mirrors_without_apply_does_not_restart(admin):
    res = admin.client.put("/api/mirrors", json={"mirrors": ["https://packages.example.org/apt/"]})
    assert res.status_code == 200 and res.get_json()["applied"] is False
    assert admin.restarts == []
    text = (admin.config / "https-mirrors.conf").read_text()
    assert "https://packages.example.org/apt\n" in text and "docker" not in text.split("#")[-1]
    assert text.startswith("# Depots apt en HTTPS")  # en-tete explicatif conserve
    assert admin.read_mirrors() == ["https://packages.example.org/apt"]


def test_save_mirrors_with_apply_restarts_the_cache(admin):
    res = admin.client.put("/api/mirrors", json={"mirrors": ["https://packages.example.org/apt"], "apply": True})
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert admin.restarts == [1]


def test_invalid_mirror_is_refused_and_nothing_is_written(admin):
    before = (admin.config / "https-mirrors.conf").read_text()
    res = admin.client.put("/api/mirrors", json={"mirrors": ["http://pas-https.example.org"], "apply": True})
    assert res.status_code == 400
    assert (admin.config / "https-mirrors.conf").read_text() == before and admin.restarts == []


# --- mode et redemarrage -----------------------------------------------------------

def test_mode_switch_writes_the_file_and_restarts(admin):
    res = admin.client.put("/api/mode", json={"mode": "offline"})
    assert res.status_code == 200
    assert (admin.config / "mode").read_text().strip() == "offline" and admin.restarts == [1]
    assert admin.read_mode() == "offline"


def test_invalid_mode_is_refused(admin):
    assert admin.client.put("/api/mode", json={"mode": "n-importe-quoi"}).status_code == 400
    assert (admin.config / "mode").read_text().strip() == "online" and admin.restarts == []


def test_failed_restart_is_reported_as_502(admin):
    admin.restart_ok = (False, "Cannot connect to the Docker daemon")
    res = admin.client.put("/api/mode", json={"mode": "offline"})
    assert res.status_code == 502 and "redemarrage" in res.get_json()["error"]
    assert "Docker daemon" in res.get_json()["error"]


def test_concurrent_operation_is_refused(admin):
    admin._lock.acquire()
    try:
        assert admin.client.post("/api/restart").status_code == 409
        assert admin.client.put("/api/mode", json={"mode": "offline"}).status_code == 409
    finally:
        admin._lock.release()
    assert admin.client.post("/api/restart").status_code == 200


# --- statut et pages -----------------------------------------------------------------

def test_status_endpoint_aggregates_everything(admin):
    write_log(admin, LOG)
    data = admin.client.get("/api/status").get_json()
    assert data["mode"] == "online" and data["container"] == {"running": True}
    assert data["efficiency"]["hits"] == 2 and data["efficiency"]["log_bytes"] > 0
    assert {"total", "used", "free"} <= set(data["disk"]) and "packages" in data["cache"]
    assert [c["client"] for c in data["clients"]] == ["10.42.0.149", "10.42.0.148"]  # plus recent d'abord


def test_log_endpoint_filters_and_validates_limit(admin):
    write_log(admin, LOG)
    assert len(admin.client.get("/api/log?limit=1").get_json()["entries"]) == 1
    assert len(admin.client.get("/api/log?limit=abc").get_json()["entries"]) == 3
    assert len(admin.client.get("/api/log?all=1").get_json()["entries"]) == 4
    assert admin.client.get("/api/log?client=bobby&q=figlet").get_json()["entries"][0]["name"] == "bobby"


def test_page_is_served(admin):
    res = admin.client.get("/")
    assert res.status_code == 200 and b"Cache apt" in res.data


# --- regroupement des acces du journal ------------------------------------------------

def test_a_download_followed_by_a_serve_is_one_miss_for_that_machine(admin):
    entries = [admin.parse_line(l) for l in LOG[:2]]
    events = admin.group_events(entries)
    assert len(events) == 1
    assert events[0]["kind"] == "miss" and events[0]["client"] == "10.42.0.148"
    assert events[0]["bytes"] == 134537  # taille servie a la machine, pas celle du telechargement


def test_a_serve_alone_is_a_hit_and_a_download_alone_is_a_cache_fetch(admin):
    hit = admin.group_events([admin.parse_line(LOG[2])])
    assert [e["kind"] for e in hit] == ["hit"]
    fetch = admin.group_events([admin.parse_line("1791656424|I|760|10.42.0.148|debrep/dists/bookworm/InRelease")])
    assert [e["kind"] for e in fetch] == ["fetch"]


def test_a_download_is_not_paired_with_a_serve_for_another_machine_or_much_later(admin):
    other_machine = [
        admin.parse_line("100|I|10|10.42.0.148|debrep/pool/main/a/a.deb"),
        admin.parse_line("100|O|10|10.42.0.149|debrep/pool/main/a/a.deb"),
    ]
    assert sorted(e["kind"] for e in admin.group_events(other_machine)) == ["fetch", "hit"]
    too_late = [
        admin.parse_line("100|I|10|10.42.0.148|debrep/pool/main/a/a.deb"),
        admin.parse_line(f"{100 + admin.PAIR_WINDOW + 1}|O|10|10.42.0.148|debrep/pool/main/a/a.deb"),
    ]
    assert sorted(e["kind"] for e in admin.group_events(too_late)) == ["fetch", "hit"]


# --- echecs : le cache repond par une page d'erreur (ex. 503 hors ligne) ----------------

OFFLINE_FAILURE = [
    # cache hors ligne, paquet absent : apt reessaie, le cache repond sa page d'erreur (688 octets)
    "1791742838|O|688|10.42.0.147|debrep/pool/main/s/sl/sl_5.02-1_arm64.deb",
    "1791742841|O|688|10.42.0.147|debrep/pool/main/s/sl/sl_5.02-1_arm64.deb",
    "1791742845|O|688|10.42.0.147|debrep/pool/main/s/sl/sl_5.02-1_arm64.deb",
]


def parsed(admin, lines):
    return [admin.parse_line(line) for line in lines]


def test_a_small_serve_for_a_package_absent_from_the_cache_is_a_failure(admin):
    events = admin.group_events(parsed(admin, OFFLINE_FAILURE), cached=set())
    assert [e["kind"] for e in events] == ["error"] * 3


def test_a_small_serve_for_a_package_present_in_the_cache_is_a_hit(admin):
    # un vrai petit paquet deja en cache n'est pas une erreur
    events = admin.group_events(parsed(admin, OFFLINE_FAILURE[:1]), cached={"sl_5.02-1_arm64.deb"})
    assert [e["kind"] for e in events] == ["hit"]


def test_a_big_serve_is_never_a_failure_even_if_the_file_left_the_cache(admin):
    big = ["1791742838|O|900000|10.42.0.147|debrep/pool/main/s/sl/sl_5.02-1_arm64.deb"]
    assert [e["kind"] for e in admin.group_events(parsed(admin, big), cached=set())] == ["hit"]


def test_without_cache_listing_nothing_is_flagged_as_a_failure(admin):
    assert [e["kind"] for e in admin.group_events(parsed(admin, OFFLINE_FAILURE), cached=None)] == ["hit"] * 3


def test_failures_do_not_count_as_served_packages_or_hits(admin):
    entries = parsed(admin, LOG[:2] + OFFLINE_FAILURE)  # un vrai acces (miss) + 3 echecs
    stats = admin.compute_stats(entries, {"10.42.0.147": "bobby"}, cached={"figlet_2.2.5-3_arm64.deb"})
    assert (stats["requests"], stats["hits"], stats["errors"]) == (1, 0, 3)
    assert stats["hit_ratio"] == 0
    bobby = [c for c in stats["clients"] if c["client"] == "10.42.0.147"][0]
    assert bobby["errors"] == 3 and bobby["requests"] == 0 and bobby["bytes"] == 0


def test_status_and_log_endpoints_expose_failures(admin):
    (admin.cache / "debrep").mkdir()
    (admin.cache / "debrep" / "figlet_2.2.5-3_arm64.deb").write_bytes(b"x" * 10)
    write_log(admin, LOG[:2] + OFFLINE_FAILURE)
    status = admin.client.get("/api/status").get_json()
    assert status["efficiency"]["errors"] == 3 and status["efficiency"]["requests"] == 1
    kinds = [e["kind"] for e in admin.client.get("/api/log").get_json()["entries"]]
    assert kinds.count("error") == 3 and kinds.count("miss") == 1

