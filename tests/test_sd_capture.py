"""Tests de la capture d'une carte en image reduite (sd-flasher/capture.py), de l'import
d'une image depuis un lien et du telechargement d'une image stockee.

Le pipeline complet tourne sur une vraie petite image (MBR + FAT + ext4) construite
pendant le test : il faut sfdisk, mkfs.ext4, e2image, e2fsck, resize2fs et zstd
(presents sur le runner de CI ; test saute sinon). Le reste n'a besoin d'aucun outil."""
import contextlib
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
    # Copie brute : la preparation master monte la copie (root + loop), testee a part.
    assert api.client.post("/api/capture", json={"device": "/dev/sda", "name": "rpi-test", "prepare": False}).status_code == 202
    assert api.client.post("/api/capture", json={"device": "/dev/sda", "name": "autre"}).status_code in (202, 409)
    wait(lambda: api._job["state"] != "running", timeout=60)
    assert api._job["state"] == "done", api._job.get("error")
    names = [i["name"] for i in api.client.get("/api/status").get_json()["images"]]
    assert "rpi-test.img.zst" in names
    assert (api.IMAGES_DIR / "rpi-test.img.zst.sha256").read_text().strip() == api._job["result"]["sha256"]
    assert api._job["result"]["prepared"] is False


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
    # l'etat final n'est visible qu'APRES le nettoyage : le verrou est libre, un nouvel envoi est possible tout de suite
    assert not api._upload_lock.locked()


# --- espace libre de la partition de demarrage (FAT) -----------------------------------------------------------
# Un volume FAT minimal mais conforme au calcul de la specification (le type se deduit du nombre de
# clusters) : on y met des clusters VIVANTS (a conserver) et des clusters LIBRES pleins de restes (a vider).

STALE = b"RESTE-DE-SECRET-"
LIVE = b"DONNEE-VIVANTE-"


def make_fat(kind, total_sectors, bps=512, spc=1, live_chains=((3, 4, 5),)):
    """Retourne (octets du volume, infos). Les chaines `live_chains` sont allouees ; tous les autres
    clusters de donnees sont LIBRES mais contiennent STALE. Le cluster 2 est le repertoire racine (FAT32)."""
    fat32 = kind == "fat32"
    reserved, nfats = (32, 2) if fat32 else (1, 2)
    root_entries = 0 if fat32 else 512
    root_sectors = (root_entries * 32 + bps - 1) // bps
    entry = 4 if fat32 else 2
    fat_sectors = 1
    for _ in range(50):                       # la taille de la FAT depend du nombre de clusters, qui depend d'elle
        clusters = (total_sectors - reserved - nfats * fat_sectors - root_sectors) // spc
        needed = -(-(clusters + 2) * entry // bps)
        if needed == fat_sectors:
            break
        fat_sectors = needed
    data_start = reserved + nfats * fat_sectors + root_sectors
    assert (clusters >= 65525) == fat32 and clusters >= 4085, f"{kind} : {clusters} clusters ne donne pas ce type"
    image = bytearray(total_sectors * bps)
    struct.pack_into("<HBHB", image, 11, bps, spc, reserved, nfats)
    struct.pack_into("<H", image, 17, root_entries)
    struct.pack_into("<H", image, 19, total_sectors if total_sectors < 65536 else 0)
    struct.pack_into("<I", image, 32, 0 if total_sectors < 65536 else total_sectors)
    struct.pack_into("<H", image, 22, 0 if fat32 else fat_sectors)
    struct.pack_into("<I", image, 36, fat_sectors if fat32 else 0)
    image[510:512] = b"\x55\xaa"
    fmt = "<I" if fat32 else "<H"
    eoc = 0x0FFFFFFF if fat32 else 0xFFFF
    table = reserved * bps
    struct.pack_into(fmt, image, table, 0x0FFFFFF8 if fat32 else 0xFFF8)
    struct.pack_into(fmt, image, table + entry, eoc)
    live = []
    if fat32:
        struct.pack_into(fmt, image, table + 2 * entry, eoc)           # repertoire racine
        live.append(2)
    for chain in live_chains:
        for here, following in zip(chain, chain[1:] + (None,)):
            struct.pack_into(fmt, image, table + here * entry, eoc if following is None else following)
            live.append(here)
    cluster_bytes = spc * bps
    for n in range(2, clusters + 2):
        offset = (data_start + (n - 2) * spc) * bps
        pattern = LIVE if n in live else STALE
        image[offset:offset + cluster_bytes] = (pattern * (cluster_bytes // len(pattern) + 1))[:cluster_bytes]
    return bytes(image), {"data_start": data_start, "clusters": clusters, "live": sorted(live), "bps": bps, "spc": spc}


def cluster_range(info, n):
    start = (info["data_start"] + (n - 2) * info["spc"]) * info["bps"]
    return start, start + info["spc"] * info["bps"]


@pytest.mark.parametrize("kind, sectors", [("fat32", 67_500), ("fat16", 16_384)])
def test_wipe_zeroes_free_clusters_and_never_touches_allocated_ones(kind, sectors):
    image, info = make_fat(kind, sectors)
    prefix = b"MBR" * 170 + b"\0" * 2                   # 512 octets avant la partition : elle n'est pas au debut de la copie
    offset = len(prefix)
    head = bytearray(prefix) + bytearray(image) + bytearray(b"SUITE-DE-LA-COPIE" * 10)
    before = bytes(head)
    assert capture.wipe_fat_free_space(head, offset, len(image)) is None
    assert STALE not in head                            # plus aucun reste
    for n in info["live"]:
        a, b = cluster_range(info, n)
        assert head[offset + a:offset + b] == before[offset + a:offset + b]      # cluster alloue : intact
        assert LIVE in head[offset + a:offset + b]
    zeroed = sum(1 for n in range(2, info["clusters"] + 2) if n not in info["live"]
                 and not any(head[offset + cluster_range(info, n)[0]:offset + cluster_range(info, n)[1]]))
    assert zeroed == info["clusters"] - len(info["live"])                          # tous les autres sont a zero
    data_start = offset + info["data_start"] * info["bps"]
    assert head[:data_start] == before[:data_start]                                # secteur de demarrage, FAT, racine : intacts
    assert head[offset + len(image):] == before[offset + len(image):]              # rien en dehors de la partition


def test_wipe_is_idempotent():
    image, _ = make_fat("fat16", 16_384)
    head = bytearray(image)
    assert capture.wipe_fat_free_space(head, 0, len(head)) is None
    once = bytes(head)
    assert capture.wipe_fat_free_space(head, 0, len(head)) is None and bytes(head) == once


@pytest.mark.parametrize(
    "damage, reason",
    [
        (lambda b: b.__setitem__(slice(510, 512), b"\0\0"), "secteur de demarrage"),
        (lambda b: struct.pack_into("<H", b, 11, 513), "incoherents"),            # octets par secteur invalides
        (lambda b: b.__setitem__(13, 3), "incoherents"),                         # secteurs par cluster : pas une puissance de 2
        (lambda b: b.__setitem__(13, 0), "incoherents"),
        (lambda b: struct.pack_into("<H", b, 22, 0), "incoherents"),             # taille de FAT nulle
        (lambda b: struct.pack_into("<H", b, 19, 60_000), "plus de secteurs"),   # annonce plus que la partition
    ],
)
def test_wipe_refuses_inconsistent_volumes_and_leaves_the_copy_alone(damage, reason):
    image, _ = make_fat("fat16", 16_384)
    head = bytearray(image)
    damage(head)
    before = bytes(head)
    note = capture.wipe_fat_free_space(head, 0, len(head))
    assert note and reason in note
    assert bytes(head) == before                                                  # en cas de doute : on ne touche a rien


def test_wipe_reports_what_is_not_a_fat_volume():
    head = bytearray(b"BOOT" * 1024)                                              # comme dans les tests de pipeline : pas un FAT
    assert "FAT" in capture.wipe_fat_free_space(head, 0, len(head))
    assert capture.wipe_fat_free_space(bytearray(100), 0, 100)                    # partition trop courte pour un secteur de demarrage


def test_wipe_does_not_handle_fat12():
    image, _ = make_fat("fat16", 16_384)
    head = bytearray(image)
    head[13] = 8                                                                  # 8 secteurs par cluster : ~2000 clusters, donc FAT12 selon la specification
    before = bytes(head)
    assert "FAT12" in capture.wipe_fat_free_space(head, 0, len(head))
    assert bytes(head) == before


def test_a_partition_cut_short_by_the_copy_is_refused():
    image, _ = make_fat("fat16", 16_384)
    head = bytearray(image[:len(image) // 2])                                     # la copie s'arrete avant la fin de la partition
    before = bytes(head)
    assert "plus de secteurs" in capture.wipe_fat_free_space(head, 0, len(image))
    assert bytes(head) == before


def test_plan_exposes_the_boot_partition(tmp_path):
    plan = capture.plan_capture(card(tmp_path), fake_sfdisk())
    assert plan["boot_start"] == 8192 and plan["boot_sectors"] == 1000
    only_root = fake_sfdisk(partitions=[{"node": "/dev/sda1", "start": 16384, "size": 50000}])
    plan = capture.plan_capture(card(tmp_path), only_root)
    assert plan["boot_start"] is None and plan["boot_sectors"] is None            # une seule partition : pas de demarrage a effacer


# --- verification de l'archive --------------------------------------------------------------------------------------

def test_verify_image_for_a_plain_image(tmp_path):
    data = os.urandom(300_000) + b"\0" * 100_000
    path = tmp_path / "x.img"
    path.write_bytes(data)
    capture.verify_image(path, "none", hashlib.sha256(data).hexdigest())
    with pytest.raises(capture.CaptureError, match="ne correspond pas"):
        capture.verify_image(path, "none", "0" * 64)


@needs_tools
@pytest.mark.parametrize("compression", ["zst", "xz"])
def test_verify_image_detects_corrupt_and_truncated_archives(tmp_path, compression):
    if compression == "xz" and not shutil.which("xz"):
        pytest.skip("xz absent")
    data = os.urandom(200_000) + bytes(300_000)
    cmd = ["zstd", "-q", "-c"] if compression == "zst" else ["xz", "-c"]
    path = tmp_path / ("a.img." + compression)
    path.write_bytes(subprocess.run(cmd, input=data, capture_output=True, check=True).stdout)
    expected = hashlib.sha256(data).hexdigest()
    capture.verify_image(path, compression, expected)
    with pytest.raises(capture.CaptureError, match="ne correspond pas"):
        capture.verify_image(path, compression, "0" * 64)
    whole = path.read_bytes()
    path.write_bytes(whole[:len(whole) // 2])                                      # tronquee
    with pytest.raises(capture.CaptureError):
        capture.verify_image(path, compression, expected)


# --- pipeline reel : plus aucun secret dans l'image ---------------------------------------------------------------------

def build_card_with_boot(tmp_path):
    """Comme build_card, mais la partition de demarrage est un vrai volume FAT16 contenant un fichier vivant
    et, dans ses clusters LIBRES, des restes de secrets (la graine du flasheur avant son remplacement)."""
    img = build_card(tmp_path)
    fat, info = make_fat("fat16", 16_384)
    stale = (b"MOT-DE-PASSE-WIFI-EN-CLAIR" + b"--" + b"HACHE-DU-MOT-DE-PASSE-").ljust(512, b"#")
    fat = bytearray(fat)
    for n in range(2, info["clusters"] + 2):
        if n not in info["live"]:
            a, b = cluster_range(info, n)
            fat[a:b] = stale[:b - a]
    with open(img, "r+b") as f:
        f.seek(2048 * 512)
        f.write(fat)
    return img, info


@needs_tools
def test_capture_leaves_no_secret_in_the_boot_partition(tmp_path):
    src, info = build_card_with_boot(tmp_path)
    raw_before = src.read_bytes()
    assert b"MOT-DE-PASSE-WIFI-EN-CLAIR" in raw_before                            # le scenario contient bien le secret
    out = tmp_path / "images" / "master.img.zst"
    out.parent.mkdir()
    result = capture.run_capture(source=str(src), work_dir=tmp_path / "images" / ".w", output=out, compression="zst", update=lambda **k: None)
    assert src.read_bytes() == raw_before                                          # la carte source n'a PAS ete modifiee
    assert result["boot_wiped"] is True and result["boot_wipe_note"] is None
    raw = subprocess.run(["zstd", "-dc", str(out)], capture_output=True, check=True).stdout
    for secret in (b"MOT-DE-PASSE-WIFI-EN-CLAIR", b"HACHE-DU-MOT-DE-PASSE-", STALE):
        assert secret not in raw, secret
    boot = raw[2048 * 512:2048 * 512 + 16_384 * 512]
    for n in info["live"]:
        a, b = cluster_range(info, n)
        assert LIVE in boot[a:b]                                                   # les fichiers vivants sont conserves
    assert raw[:512][510:512] == b"\x55\xaa"


@needs_tools
def test_capture_still_works_when_the_boot_partition_is_not_fat(tmp_path):
    src = build_card(tmp_path)                                                     # partition de demarrage factice
    out = tmp_path / "images" / "master.img.zst"
    out.parent.mkdir()
    result = capture.run_capture(source=str(src), work_dir=tmp_path / "images" / ".w", output=out, compression="zst", update=lambda **k: None)
    assert result["boot_wiped"] is False and "FAT" in result["boot_wipe_note"]     # signale, sans bloquer
    assert out.exists()


@needs_tools
def test_a_tampered_archive_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    src = build_card(tmp_path)
    out = tmp_path / "images" / "master.img.zst"
    out.parent.mkdir()
    real = capture.verify_image

    def corrupt_then_verify(path, compression, expected, on_read=None):
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF                                              # un octet abime dans l'archive
        path.write_bytes(bytes(data))
        return real(path, compression, expected, on_read)

    monkeypatch.setattr(capture, "verify_image", corrupt_then_verify)
    with pytest.raises(capture.CaptureError):
        capture.run_capture(source=str(src), work_dir=tmp_path / "images" / ".w", output=out, compression="zst", update=lambda **k: None)
    assert not out.exists() and not Path(str(out) + ".part").exists()


# --- preparation master de la copie ------------------------------------------------------------

PREPARE_SCRIPT = Path(__file__).resolve().parent.parent / "image" / "prepare-master.sh"
needs_bash = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt",
                                reason="bash Linux requis (sous Windows, subprocess trouve celui de WSL)")


def fake_system(root: Path, boot: Path) -> None:
    """Arborescence d'une carte deja demarree : compte pi avec mot de passe et cles, graine du
    flasheur avec le Wi-Fi en clair, fichiers poses par le flasheur."""
    for d in ("etc/ssh", "etc/sudoers.d", "etc/netplan", "etc/apt/apt.conf.d", "etc/chrony/sources.d",
              "home/pi/.ssh", "var/lib/cloud/instances/x", "opt/app"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "etc/passwd").write_text("root:x:0:0::/root:/bin/bash\npi:x:1000:1000::/home/pi:/bin/bash\n")
    (root / "etc/shadow").write_text("root:*:1::::::\npi:$6$sel$HACHE-PI:1::::::\n")
    (root / "etc/group").write_text("root:x:0:\nsudo:x:27:pi\npi:x:1000:\n")
    (root / "home/pi/.ssh/authorized_keys").write_text("ssh-ed25519 AAAA cle-de-rpi-test\n")
    (root / "etc/sudoers.d/90-cloud-init-users").write_text("pi ALL=(ALL) NOPASSWD:ALL\n")
    (root / "etc/netplan/50-cloud-init.yaml").write_text("password: WIFI-EN-CLAIR\n")
    (root / "etc/apt/apt.conf.d/01expolab-proxy").write_text('Acquire::http::Proxy "http://10.42.0.1:3142";\n')
    (root / "etc/chrony/sources.d/expolab.sources").write_text("server 10.42.0.1 iburst prefer\n")
    (root / "etc/machine-id").write_text("0123456789abcdef\n")
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("cle")
    (root / "opt/app/programme").write_text("installe dans le master")
    boot.mkdir(parents=True, exist_ok=True)
    (boot / "user-data").write_text("#cloud-config\npasswd: $6$sel$HACHE-PI\n")
    (boot / "network-config").write_text("password: WIFI-EN-CLAIR\n")
    (boot / "meta-data").write_text("instance-id: expolab-rpi-test-1\n")


@needs_bash
def test_prepare_copy_runs_prepare_master_on_the_mounted_copy(tmp_path):
    root, boot = tmp_path / "racine", tmp_path / "demarrage"
    fake_system(root, boot)
    mounted = []

    @contextlib.contextmanager
    def fake_mount(path, fstype):   # pas de vrai montage : les dossiers jouent les partitions de la copie
        mounted.append(fstype)
        yield root if fstype == "ext4" else boot

    head = bytearray(b"\0" * (2048 * 512) + b"FAT!" * 128 + b"\0" * (512 * 1024 - 512))
    plan = {"boot_start": 2048, "boot_sectors": 1024, "sector": 512}
    log = capture.prepare_copy(head=head, plan=plan, part=tmp_path / "systeme.ext", work_dir=tmp_path,
                               script=PREPARE_SCRIPT, mount=fake_mount)
    assert mounted == ["ext4", "vfat"]
    assert any("Comptes ordinaires supprimes : pi" in line for line in log)
    assert "pi:" not in (root / "etc/passwd").read_text() and not (root / "home/pi").exists()
    assert not (root / "etc/sudoers.d/90-cloud-init-users").exists()
    assert not (root / "etc/netplan/50-cloud-init.yaml").exists() and not (root / "var/lib/cloud/instances").exists()
    assert not (root / "etc/apt/apt.conf.d/01expolab-proxy").exists() and not (root / "etc/chrony/sources.d/expolab.sources").exists()
    assert (root / "etc/machine-id").read_text() == "" and not (root / "etc/ssh/ssh_host_ed25519_key").exists()
    assert (root / "opt/app/programme").read_text() == "installe dans le master"   # ce qui est installe reste
    assert (boot / "user-data").read_text().strip() == "#cloud-config" and not (boot / "meta-data").exists()
    for f in list(root.rglob("*")) + list(boot.rglob("*")):
        if f.is_file():
            assert b"HACHE-PI" not in f.read_bytes() and b"WIFI-EN-CLAIR" not in f.read_bytes(), f
    assert head[2048 * 512:2048 * 512 + 4] == b"FAT!"   # le fichier "partition" relu tel quel (faux montage)


def test_prepare_copy_reports_a_failing_script(tmp_path):
    def broken_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="refus simule")

    @contextlib.contextmanager
    def fake_mount(path, fstype):
        yield tmp_path

    script = tmp_path / "prepare.sh"
    script.write_text("exit 0")
    plan = {"boot_start": None, "boot_sectors": None, "sector": 512}
    with pytest.raises(capture.CaptureError, match="refus simule"):
        capture.prepare_copy(head=bytearray(512), plan=plan, part=tmp_path / "p", work_dir=tmp_path,
                             script=script, run=broken_run, mount=fake_mount)
    with pytest.raises(capture.CaptureError, match="introuvable"):
        capture.prepare_copy(head=bytearray(512), plan=plan, part=tmp_path / "p", work_dir=tmp_path,
                             script=tmp_path / "absent.sh", mount=fake_mount)


def test_capture_api_prepares_by_default(api, monkeypatch):
    seen = {}

    def fake_capture(**kwargs):
        seen.update(kwargs)
        raise capture.CaptureError("arret volontaire")

    monkeypatch.setattr(api.capture, "run_capture", fake_capture)
    (api.tmp / "sim").mkdir()
    (api.tmp / "sim" / "sda").write_bytes(b"x")
    for body, expected in (({}, api.PREPARE_SCRIPT), ({"prepare": False}, None)):
        assert api.client.post("/api/capture", json={"device": "/dev/sda", "name": "x", **body}).status_code == 202
        wait(lambda: api._job["state"] != "running")
        assert seen["prepare_script"] == expected
        api._job = {"state": "idle"}


needs_root_loop = pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() != 0
    or not all(shutil.which(t) for t in TOOLS + ("mkfs.vfat", "mount", "losetup")),
    reason="capture preparee : root, montage en boucle et dosfstools requis")


@needs_root_loop
def test_real_prepared_capture_has_no_account_or_secret_left(tmp_path):
    """Pipeline complet avec preparation master, sur une vraie image (FAT + ext4, montages en boucle)."""
    img = tmp_path / "carte.img"
    with open(img, "wb") as f:
        f.truncate(96 * 1024 * 1024)
    subprocess.run(["sfdisk", "-q", str(img)], input="label: dos\n2048,16384,c\n18432,,83\n", text=True, check=True)
    root, boot = tmp_path / "rootfs", tmp_path / "boot"
    fake_system(root, boot)
    fat = tmp_path / "boot.fat"
    with open(fat, "wb") as f:
        f.truncate(16384 * 512)
    subprocess.run(["mkfs.vfat", "-F", "16", "-s", "1", str(fat)], check=True, capture_output=True)
    with capture.loop_mount(fat, "vfat") as mnt:
        for f in boot.iterdir():
            shutil.copy(f, mnt / f.name)
    with open(img, "r+b") as f:
        f.seek(2048 * 512)
        f.write(fat.read_bytes())
    size_kib = (96 * 1024 * 1024 - 18432 * 512) // 1024
    subprocess.run(["mkfs.ext4", "-F", "-q", "-d", str(root), "-E", f"offset={18432 * 512}", str(img), f"{size_kib}k"], check=True)
    src_hash = hashlib.sha256(img.read_bytes()).hexdigest()

    out = tmp_path / "images" / "master.img"
    out.parent.mkdir()
    result = capture.run_capture(source=str(img), work_dir=tmp_path / "images" / ".capture-x", output=out,
                                 compression="none", update=lambda **k: None, prepare_script=PREPARE_SCRIPT)
    assert hashlib.sha256(img.read_bytes()).hexdigest() == src_hash   # la carte n'a pas bouge
    assert result["prepared"] and any("pi" in line for line in result["prepare_log"])
    raw = out.read_bytes()
    for secret in (b"HACHE-PI", b"WIFI-EN-CLAIR", b"cle-de-rpi-test"):
        assert secret not in raw, secret                                # ni fichier vivant, ni reste
    start, sectors = struct.unpack_from("<II", raw, 446 + 16 + 8)
    part = tmp_path / "part.ext4"
    part.write_bytes(raw[start * 512:(start + sectors) * 512])
    assert subprocess.run(["e2fsck", "-fn", str(part)], capture_output=True).returncode == 0
    passwd = subprocess.run(["debugfs", "-R", "cat /etc/passwd", str(part)], capture_output=True, text=True).stdout
    assert "pi:" not in passwd and "root:" in passwd
    app = subprocess.run(["debugfs", "-R", "cat /opt/app/programme", str(part)], capture_output=True, text=True).stdout
    assert app == "installe dans le master"
    bootpart = tmp_path / "boot-out.fat"
    bootpart.write_bytes(raw[2048 * 512:(2048 + 16384) * 512])
    assert subprocess.run(["fsck.vfat", "-n", str(bootpart)], capture_output=True).returncode == 0
