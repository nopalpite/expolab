"""Tests de la capture d'une carte en image reduite (sd-flasher/capture.py), de l'import
d'une image depuis un lien et du telechargement d'une image stockee.

Le pipeline complet tourne sur une vraie petite image (MBR + FAT + ext4) construite
pendant le test : il faut sfdisk, mkfs.ext4, e2image, e2fsck, resize2fs et zstd
(presents sur le runner de CI ; test saute sinon). Le reste n'a besoin d'aucun outil."""
import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

SRC = Path(__file__).resolve().parent.parent / "sd-flasher"
sys.path.insert(0, str(SRC))
import capture  # noqa: E402

TOOLS = ("sfdisk", "mkfs.ext4", "e2image", "e2fsck", "resize2fs", "zstd")
needs_tools = pytest.mark.skipif(not all(shutil.which(t) for t in TOOLS) or os.name == "nt",
                                 reason="outils de disque absents (sfdisk, e2fsprogs, zstd)")


# --- briques sans outils ------------------------------------------------------------------

def mbr(entries):
    head = bytearray(512)
    for i, (start, size) in enumerate(entries):
        struct.pack_into("<BxxxBxxxII", head, 446 + 16 * i, 0, 0x83, start, size)
    head[510:512] = b"\x55\xaa"
    return head


def test_patch_mbr_changes_only_the_size_of_the_right_entry():
    head = mbr([(8192, 1000), (16384, 50000)])
    before = bytes(head)
    capture.patch_mbr(head, 2, 16384, 1234)
    assert struct.unpack_from("<I", head, 446 + 16 + 12)[0] == 1234
    assert struct.unpack_from("<I", head, 446 + 12)[0] == 1000
    changed = [i for i in range(512) if head[i] != before[i]]
    assert changed and all(446 + 16 + 12 <= i < 446 + 16 + 16 for i in changed)


def test_patch_mbr_refuses_an_unexpected_entry():
    with pytest.raises(capture.CaptureError):
        capture.patch_mbr(mbr([(8192, 1000), (16384, 50000)]), 2, 99999, 1)
    with pytest.raises(capture.CaptureError):
        capture.patch_mbr(bytearray(512), 1, 0, 1)   # pas de signature MBR


def superblock(blocks, free, log_block=2, sixty_four=False):
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 0x04, blocks & 0xFFFFFFFF)
    struct.pack_into("<I", sb, 0x0C, free & 0xFFFFFFFF)
    struct.pack_into("<I", sb, 0x18, log_block)
    struct.pack_into("<H", sb, 0x38, capture.EXT_MAGIC)
    if sixty_four:
        struct.pack_into("<I", sb, 0x60, 0x80)
        struct.pack_into("<I", sb, 0x150, blocks >> 32)
        struct.pack_into("<I", sb, 0x158, free >> 32)
    return sb


def test_ext_superblock_reads_sizes_at_an_offset(tmp_path):
    f = tmp_path / "disk"
    f.write_bytes(b"\0" * 4096 + b"\0" * 1024 + superblock(1000, 400) + b"\0" * 2048)
    assert capture.ext_superblock(str(f), 4096) == {"block_size": 4096, "blocks": 1000, "free_blocks": 400}
    g = tmp_path / "big"
    g.write_bytes(b"\0" * 1024 + superblock((1 << 32) + 5, 3, sixty_four=True))
    assert capture.ext_superblock(str(g))["blocks"] == (1 << 32) + 5
    assert capture.ext_superblock(str(f), 0) is None   # pas de magie ext


def fake_sfdisk(label="dos", partitions=None, rc=0):
    partitions = partitions if partitions is not None else [{"node": "/dev/sda1", "start": 8192, "size": 1000},
                                                             {"node": "/dev/sda2", "start": 16384, "size": 50000}]

    def run(cmd, **kwargs):
        body = json.dumps({"partitiontable": {"label": label, "sectorsize": 512, "partitions": partitions}})
        return subprocess.CompletedProcess(cmd, rc, stdout=body, stderr="erreur" if rc else "")
    return run


def card(tmp_path, root_start=16384, fs=True):
    f = tmp_path / "card"
    with open(f, "wb") as out:
        out.truncate(root_start * 512 + 8 * 1024 * 1024)
        if fs:
            out.seek(root_start * 512 + 1024)
            out.write(superblock(2048, 1536))
    return str(f)


def test_plan_reads_layout_and_used_space(tmp_path):
    plan = capture.plan_capture(card(tmp_path), fake_sfdisk())
    assert plan["root_number"] == 2 and plan["root_start"] == 16384 and plan["head_bytes"] == 16384 * 512
    assert plan["used_bytes"] == 512 * 4096 and plan["fs_bytes"] == 2048 * 4096
    assert capture.space_needed(plan) == 2 * (16384 * 512 + 512 * 4096)


@pytest.mark.parametrize("run, message", [
    (fake_sfdisk(label="gpt"), "gpt"),
    (fake_sfdisk(partitions=[]), "Aucune partition"),
    (fake_sfdisk(partitions=[{"node": "/dev/sda5", "start": 16384, "size": 10}]), "primaire"),
    (fake_sfdisk(rc=1), "illisible"),
])
def test_plan_refuses_unsupported_cards(tmp_path, run, message):
    with pytest.raises(capture.CaptureError, match=message):
        capture.plan_capture(card(tmp_path), run)


def test_plan_refuses_a_non_ext_last_partition(tmp_path):
    with pytest.raises(capture.CaptureError, match="ext2/3/4"):
        capture.plan_capture(card(tmp_path, fs=False), fake_sfdisk())


def test_plan_checks_space_before_copying_anything(tmp_path):
    work = tmp_path / "work"
    with pytest.raises(capture.CaptureError, match="Espace insuffisant"):
        capture.run_capture(source=card(tmp_path), work_dir=work, output=tmp_path / "out.img.zst", compression="zst",
                            update=lambda **k: None, run=fake_sfdisk(), free_bytes=1000)
    assert not work.exists() and not (tmp_path / "out.img.zst").exists()


# --- pipeline reel sur une petite image ----------------------------------------------------------

def build_card(tmp_path: Path) -> Path:
    """Image de 96 Mio : MBR, p1 FAT (8 Mio), p2 ext4 (le reste) avec ~6 Mio de donnees."""
    img = tmp_path / "carte.img"
    with open(img, "wb") as f:
        f.truncate(96 * 1024 * 1024)
    subprocess.run(["sfdisk", "-q", str(img)], input="label: dos\n2048,16384,c\n18432,,83\n", text=True, check=True)
    with open(img, "r+b") as f:
        f.seek(2048 * 512)
        f.write(b"BOOT" * 1024)   # contenu reconnaissable dans la "partition de demarrage"
    data = tmp_path / "rootfs"
    (data / "etc").mkdir(parents=True)
    (data / "etc" / "hostname").write_text("rpi-test\n")
    (data / "data.bin").write_bytes(os.urandom(6 * 1024 * 1024))
    size_kib = (96 * 1024 * 1024 - 18432 * 512) // 1024
    subprocess.run(["mkfs.ext4", "-F", "-q", "-d", str(data), "-E", f"offset={18432 * 512}", str(img), f"{size_kib}k"], check=True)
    return img


@needs_tools
@pytest.mark.parametrize("compression", ["zst", "none"])
def test_capture_shrinks_a_real_card_image(tmp_path, compression):
    src = build_card(tmp_path)
    src_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    out = tmp_path / "images" / ("rpi-test" + capture.COMPRESSIONS[compression])
    out.parent.mkdir()
    steps = []
    result = capture.run_capture(source=str(src), work_dir=tmp_path / "images" / ".capture-x", output=out,
                                 compression=compression, update=lambda **k: steps.append(k))
    assert hashlib.sha256(src.read_bytes()).hexdigest() == src_hash   # la "carte" n'a pas bouge
    assert out.exists() and not Path(str(out) + ".part").exists() and not (tmp_path / "images" / ".capture-x").exists()
    assert result["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert [s["step"] for s in steps][-1] == "checksum" and steps[-1]["percent"] == 100
    assert [p["percent"] for p in steps] == sorted(p["percent"] for p in steps)   # la progression ne recule jamais

    raw = out.read_bytes() if compression == "none" else subprocess.run(["zstd", "-dc", str(out)], capture_output=True, check=True).stdout
    assert len(raw) == result["image_bytes"] < src.stat().st_size
    assert raw[:18432 * 512][2048 * 512:2048 * 512 + 4096] == b"BOOT" * 1024   # partition de demarrage intacte
    start, sectors = struct.unpack_from("<II", raw, 446 + 16 + 8)
    assert start == 18432 and (start + sectors) * 512 == len(raw)       # table corrigee = fin de l'image
    part = tmp_path / "part.ext4"
    part.write_bytes(raw[start * 512:])
    sb = capture.ext_superblock(str(part))
    assert sb["blocks"] * sb["block_size"] == sectors * 512
    assert subprocess.run(["e2fsck", "-fn", str(part)], capture_output=True).returncode == 0   # systeme de fichiers sain
    cat = subprocess.run(["debugfs", "-R", "cat /etc/hostname", str(part)], capture_output=True, text=True)
    assert cat.stdout == "rpi-test\n"


@needs_tools
def test_capture_failure_leaves_nothing_behind(tmp_path):
    src = build_card(tmp_path)
    out = tmp_path / "out.img.zst"

    def broken(cmd, **kwargs):
        if cmd[0] == "resize2fs":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="plantage simule")
        return subprocess.run(cmd, **kwargs)

    with pytest.raises(capture.CaptureError, match="plantage simule"):
        capture.run_capture(source=str(src), work_dir=tmp_path / "w", output=out, compression="zst",
                            update=lambda **k: None, run=broken)
    assert not out.exists() and not Path(str(out) + ".part").exists() and not (tmp_path / "w").exists()


# --- API -----------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sd_flasher_app_capture", SRC / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sd_flasher_app_capture"] = module
    spec.loader.exec_module(module)
    images = tmp_path / "images"
    images.mkdir()
    (images / "master.img").write_bytes(b"\0" * 510 + b"\x55\xaa" + os.urandom(100_000))
    eligible = {"path": "/dev/sda", "name": "sda", "size": 32_000_000_000, "removable": True, "readonly": False,
                "transport": "usb", "model": "SD Reader", "serial": "123", "partitions": ["/dev/sda1"], "mountpoints": [], "reason": ""}
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({"eligible": [eligible], "ignored": []}))
    monkeypatch.setattr(module, "IMAGES_DIR", images)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(module, "DEVICES_FILE", str(devices))
    monkeypatch.setattr(module, "DRY_RUN", True)
    monkeypatch.setattr(module, "DRY_RUN_DIR", tmp_path / "sim")
    monkeypatch.setattr(module, "UPLOAD_RESERVE", 0)
    module._job = {"state": "idle"}
    module._fetch = {"state": "idle"}
    module.app.testing = True
    module.client = module.app.test_client()
    module.tmp = tmp_path
    module.devices = devices
    return module


def wait(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("delai depasse")


def test_download_serves_the_file_with_resume(api):
    full = (api.IMAGES_DIR / "master.img").read_bytes()
    res = api.client.get("/api/images/master.img/file")
    assert res.status_code == 200 and res.data == full
    assert "attachment" in res.headers["Content-Disposition"] and "master.img" in res.headers["Content-Disposition"]
    part = api.client.get("/api/images/master.img/file", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.data == full[100:200]
    assert api.client.get("/api/images/..%2Fdata%2Fsettings.json/file").status_code == 404
    assert api.client.get("/api/images/inconnue.img/file").status_code == 404


@pytest.mark.parametrize("name, ok", [("rpi-test", "rpi-test.img.zst"), ("rpi-test.img.zst", "rpi-test.img.zst"),
                                      ("rpi-test.img.xz", "rpi-test.img.zst")])
def test_capture_name_adds_the_compression_suffix_once(api, name, ok):
    assert api.capture_name(name, "zst") == (ok, None)


def test_capture_name_refusals(api):
    assert api.capture_name("x", "rar")[1]
    assert api.capture_name("../evil", "zst")[1]
    assert api.capture_name("", "zst")[1]


def test_capture_api_guards(api):
    post = lambda body: api.client.post("/api/capture", json=body)  # noqa: E731
    assert post({"device": "/dev/sda", "name": "nom avec espace"}).status_code == 400
    assert post({"device": "/dev/sdz", "name": "x"}).status_code == 409            # pas dans la liste eligible
    assert post({"device": "/dev/sda", "name": "master", "compression": "none"}).status_code == 409   # existe deja
    data = json.loads(api.devices.read_text())
    data["eligible"][0]["mountpoints"] = ["/media/carte"]
    api.devices.write_text(json.dumps(data))
    assert "montee" in post({"device": "/dev/sda", "name": "x"}).get_json()["error"]


def test_capture_api_dry_run_without_simulated_card_reports_an_error(api):
    res = api.client.post("/api/capture", json={"device": "/dev/sda", "name": "rpi-test"})
    assert res.status_code == 202 and res.get_json()["kind"] == "capture"
    wait(lambda: api._job["state"] != "running")
    assert api._job["state"] == "error" and "Simulation" in api._job["error"]
    assert api.client.post("/api/flash", json={}).status_code == 400   # le flash reste utilisable ensuite


@needs_tools
def test_capture_api_end_to_end_in_dry_run(api):
    (api.tmp / "sim").mkdir()
    shutil.copy(build_card(api.tmp), api.tmp / "sim" / "sda")
    assert api.client.post("/api/capture", json={"device": "/dev/sda", "name": "rpi-test"}).status_code == 202
    assert api.client.post("/api/capture", json={"device": "/dev/sda", "name": "autre"}).status_code in (202, 409)
    wait(lambda: api._job["state"] != "running", timeout=60)
    assert api._job["state"] == "done", api._job.get("error")
    names = [i["name"] for i in api.client.get("/api/status").get_json()["images"]]
    assert "rpi-test.img.zst" in names
    assert (api.IMAGES_DIR / "rpi-test.img.zst.sha256").read_text().strip() == api._job["result"]["sha256"]


# --- import depuis un lien -----------------------------------------------------------------------

@pytest.fixture
def web(tmp_path):
    root = tmp_path / "www"
    root.mkdir()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_fetch_downloads_into_the_image_list(api, web):
    root, base = web
    payload = b"\x28\xb5\x2f\xfd" + os.urandom(3_000_000)
    (root / "os.img.zst").write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    res = api.client.post("/api/images/fetch", json={"url": f"{base}/os.img.zst", "name": "os.img.zst", "sha256": sha})
    assert res.status_code == 202
    wait(lambda: api._fetch["state"] != "running")
    assert api._fetch["state"] == "done" and api._fetch["received"] == len(payload)
    assert (api.IMAGES_DIR / "os.img.zst").read_bytes() == payload
    assert (api.IMAGES_DIR / "os.img.zst.sha256").read_text().strip() == sha
    assert not (api.IMAGES_DIR / "os.img.zst.part").exists()
    assert api.client.post("/api/images/fetch", json={"url": f"{base}/os.img.zst", "name": "os.img.zst"}).status_code == 409


@pytest.mark.parametrize("served, name, sha, message", [
    (b"pas une archive", "faux.img.zst", "", "archive .zst"),
    (b"\x28\xb5\x2f\xfd" + b"x" * 1000, "bon.img.zst", "0" * 64, "SHA-256"),
])
def test_fetch_rejects_bad_files_and_leaves_nothing(api, web, served, name, sha, message):
    root, base = web
    (root / "f").write_bytes(served)
    assert api.client.post("/api/images/fetch", json={"url": f"{base}/f", "name": name, "sha256": sha}).status_code == 202
    wait(lambda: api._fetch["state"] != "running")
    assert api._fetch["state"] == "error" and message in api._fetch["error"]
    assert not (api.IMAGES_DIR / name).exists() and not (api.IMAGES_DIR / (name + ".part")).exists()


def test_fetch_http_error_and_upload_lock_released(api, web):
    _root, base = web
    assert api.client.post("/api/images/fetch", json={"url": f"{base}/absent.img.xz", "name": "a.img.xz"}).status_code == 202
    wait(lambda: api._fetch["state"] != "running")
    assert api._fetch["state"] == "error" and "404" in api._fetch["error"]
    assert api._upload_lock.acquire(blocking=False)   # le verrou d'envoi a bien ete rendu
    api._upload_lock.release()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://exemple.fr/a.img", "javascript:alert(1)", "http://", ""])
def test_fetch_refuses_non_http_links(api, url):
    assert api.client.post("/api/images/fetch", json={"url": url, "name": "a.img"}).status_code == 400


def test_fetch_never_follows_a_redirect_to_file(api):
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/x"
        assert api.client.post("/api/images/fetch", json={"url": url, "name": "a.img"}).status_code == 202
        wait(lambda: api._fetch["state"] != "running")
        assert api._fetch["state"] == "error" and not (api.IMAGES_DIR / "a.img").exists()
    finally:
        server.shutdown()


def test_fetch_can_be_cancelled(api, monkeypatch):
    class Slow:
        headers = {"Content-Length": "100000000"}
        reads = 0

        def read(self, n):
            Slow.reads += 1
            time.sleep(0.05)
            return (b"\0" * 510 + b"\x55\xaa") if Slow.reads == 1 else b"\0" * 1000

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, *a, **k):
            return Slow()

    monkeypatch.setattr(api, "_opener", lambda: Opener())
    monkeypatch.setattr(api, "free_for_images", lambda: 10**12)
    assert api.client.post("/api/images/fetch", json={"url": "http://exemple.fr/a.img", "name": "a.img"}).status_code == 202
    wait(lambda: api._fetch.get("received", 0) > 0)
    assert api.client.delete("/api/images/fetch").get_json() == {"cancelling": True}
    wait(lambda: api._fetch["state"] != "running")
    assert api._fetch["state"] == "error" and "annule" in api._fetch["error"]
    assert not (api.IMAGES_DIR / "a.img.part").exists()
