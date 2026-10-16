"""Tests de sd-flasher : detection des cartes (garde-fous), lecture/ecriture/verification
de l'image, identite de la carte, pipeline complet et API. Aucun vrai disque : des
fichiers ordinaires jouent les peripheriques."""

import gzip
import importlib.util
import json
import lzma
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("flask")

SRC = Path(__file__).resolve().parent.parent / "sd-flasher"
sys.path.insert(0, str(SRC))
import flasher  # noqa: E402

PAYLOAD = os.urandom(300_000) + b"\x00" * 200_000


# --- detection ------------------------------------------------------------------------------

def disk(name, size=32_000_000_000, tran="usb", rm="1", ro="0", mounts=(), children=(), model="SD Reader"):
    return {
        "name": name, "path": f"/dev/{name}", "size": size, "rm": rm, "ro": ro, "tran": tran, "type": "disk",
        "model": model, "vendor": "Generic", "serial": "123", "mountpoints": list(mounts) or [None],
        "children": [{"name": flasher.partition_path(name, i), "path": flasher.partition_path(f"/dev/{name}", i), "type": "part",
                      "mountpoints": [m] if m else [None]} for i, m in enumerate(children, 1)],
    }


MOUNTINFO = (
    "29 1 179:2 / / rw,relatime - ext4 /dev/mmcblk0p2 rw\n"
    "40 29 179:1 / /boot/firmware rw - vfat /dev/mmcblk0p1 rw\n"
    "50 29 0:30 / /proc rw - proc proc rw\n"
)


def classify(*disks, mountinfo=MOUNTINFO, max_bytes=flasher.DEFAULT_MAX_DEVICE_BYTES):
    return flasher.classify_devices({"blockdevices": list(disks)}, mountinfo, max_bytes)


def test_usb_card_reader_is_eligible():
    result = classify(disk("sda"))
    assert [d["path"] for d in result["eligible"]] == ["/dev/sda"]
    assert result["eligible"][0]["model"] == "Generic SD Reader"


def test_the_servers_own_disks_are_never_eligible():
    system = disk("mmcblk0", tran="", rm="0", children=["/boot/firmware", "/"])
    nvme = disk("nvme0n1", tran="nvme", rm="0")
    result = classify(system, nvme, disk("sda"))
    assert [d["path"] for d in result["eligible"]] == ["/dev/sda"]
    assert {d["path"] for d in result["ignored"]} == {"/dev/mmcblk0", "/dev/nvme0n1"}


def test_a_usb_disk_used_by_the_server_is_refused_even_if_lsblk_does_not_know():
    # le conteneur ne voit pas les montages de l'hote : /proc/1/mountinfo est la reference
    mountinfo = MOUNTINFO + "60 29 8:17 / /srv/data rw - ext4 /dev/sdb1 rw\n"
    result = classify(disk("sdb", children=[None]), mountinfo=mountinfo)
    assert result["eligible"] == [] and "serveur" in result["ignored"][0]["reason"]


def test_usb_ssd_mounted_as_root_is_refused():
    result = classify(disk("sdb", mounts=["/"], children=["/boot/firmware", "/"]))
    assert result["eligible"] == []


@pytest.mark.parametrize(
    "kwargs",
    [{"tran": "sata"}, {"tran": ""}, {"ro": "1"}, {"size": 500_000_000}, {"size": 2_000_000_000_000}],
)
def test_other_disks_are_refused(kwargs):
    assert classify(disk("sda", **kwargs))["eligible"] == []


def test_size_cap_is_configurable():
    assert classify(disk("sda", size=400_000_000_000), max_bytes=512 * 10**9)["eligible"]


def test_unreadable_host_mounts_refuse_everything(tmp_path, monkeypatch):
    class Done:
        stdout = json.dumps({"blockdevices": [disk("sda")]})

    monkeypatch.setattr(flasher.subprocess, "run", lambda *a, **k: Done())
    result = flasher.read_devices(mountinfo_path=str(tmp_path / "absent"))
    assert result["eligible"] == [] and "illisibles" in result["ignored"][0]["reason"]


# --- images -----------------------------------------------------------------------------------

@pytest.fixture
def images(tmp_path):
    d = tmp_path / "images"
    d.mkdir()
    (d / "master.img").write_bytes(PAYLOAD)
    with gzip.open(d / "master-gz.img.gz", "wb") as f:
        f.write(PAYLOAD)
    (d / "master-xz.img.xz").write_bytes(lzma.compress(PAYLOAD))
    (d / "notes.txt").write_text("pas une image")
    return d


def test_list_images_ignores_other_files(images):
    assert sorted(i["name"] for i in flasher.list_images(images)) == ["master-gz.img.gz", "master-xz.img.xz", "master.img"]


@pytest.mark.parametrize("name", ["master.img", "master-gz.img.gz", "master-xz.img.xz"])
def test_all_formats_decompress_to_the_same_bytes(images, name):
    seen = []
    data = b"".join(flasher.iter_image(images / name, progress=lambda done, total: seen.append((done, total))))
    assert data == PAYLOAD
    assert seen and seen[-1][0] <= seen[-1][1]


def test_write_then_read_back_matches(tmp_path, images):
    target = tmp_path / "card.bin"
    written, digest = flasher.write_image(flasher.iter_image(images / "master.img"), str(target), create=True, sync_every=1)
    assert written == len(PAYLOAD) and target.read_bytes() == PAYLOAD
    assert flasher.read_back_sha256(str(target), written) == digest


def test_read_back_detects_a_corrupted_card(tmp_path, images):
    target = tmp_path / "card.bin"
    written, digest = flasher.write_image(flasher.iter_image(images / "master.img"), str(target), create=True)
    raw = bytearray(target.read_bytes())
    raw[1000] ^= 0xFF
    target.write_bytes(bytes(raw))
    assert flasher.read_back_sha256(str(target), written) != digest


def test_read_back_fails_on_a_short_card(tmp_path):
    target = tmp_path / "card.bin"
    target.write_bytes(b"abc")
    with pytest.raises(flasher.FlashError, match="plus court"):
        flasher.read_back_sha256(str(target), 1000)


def test_writing_never_creates_a_missing_device(tmp_path):
    # create=False (le cas reel) : un chemin /dev disparu ne doit pas devenir un fichier ordinaire
    missing = tmp_path / "sdz"
    with pytest.raises(flasher.FlashError):
        flasher.write_image(iter([b"x"]), str(missing))
    assert not missing.exists()


# --- reglages et fichier de configuration ----------------------------------------------------------

GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample expolab-automation"


def seed(hostname="module-02", settings=None, now=1_800_000_000):
    files = flasher.render_seed(hostname, settings or {}, now=now)
    assert files["user-data"].startswith("#cloud-config\n")  # cloud-init refuse un user-data sans cet en-tete
    return files, yaml.safe_load(files["user-data"]), yaml.safe_load(files["network-config"])["network"]


def test_seed_sets_hostname_keys_and_fresh_ssh_host_keys():
    keys = [GOOD_KEY, GOOD_KEY.replace("example", "autre")]
    files, user, _net = seed(settings={"authorized_keys": keys})
    assert user["hostname"] == "module-02" and user["preserve_hostname"] is False
    assert user["manage_etc_hosts"] is True and user["ssh_deletekeys"] is True
    assert user["ssh_authorized_keys"] == keys
    assert set(files) == set(flasher.SEED_FILES)


def test_seed_without_keys_or_wifi_is_minimal():
    _files, user, net = seed()
    assert "ssh_authorized_keys" not in user
    assert "wifis" not in net and net["ethernets"]["all-eth"]["dhcp4"] is True


def test_seed_wifi_round_trips_awkward_characters():
    # guillemets, deux-points, accolades, #, antislash, unicode : le YAML doit les restituer a l'identique
    ssid, psk = 'Expo "wifi": {2026} #1 é', "p@ss: 'word' \\ {x} #y"
    _files, _user, net = seed(settings={"wifi_ssid": ssid, "wifi_psk": psk, "wifi_country": "FR"})
    wlan = net["wifis"]["wlan0"]
    assert wlan["access-points"] == {ssid: {"password": psk}}
    assert wlan["regulatory-domain"] == "FR" and wlan["dhcp4"] is True


def test_every_flash_gets_a_new_instance_id():
    # sinon cloud-init croit avoir deja configure cette carte (copie du master) et ne rejoue rien
    first, _, _ = seed(now=1_800_000_000)
    second, _, _ = seed(now=1_800_000_001)
    assert first["meta-data"] != second["meta-data"]
    meta = yaml.safe_load(first["meta-data"])
    assert meta["instance-id"].startswith("expolab-module-02-") and meta["local-hostname"] == "module-02"


@pytest.mark.parametrize("hostname", ["", "a", "Module-02", "module_02", "-bad", "bad-", "x" * 40, "mod ule", "mod\nule", "../etc"])
def test_invalid_hostnames_are_refused(hostname):
    with pytest.raises(flasher.FlashError):
        flasher.check_hostname(hostname)


@pytest.mark.parametrize(
    "settings",
    [
        {"wifi_ssid": "expo", "wifi_psk": "court"},
        {"wifi_ssid": "ex\npo", "wifi_psk": "motdepasse1"},
        {"wifi_ssid": "expo", "wifi_psk": "motdepasse1", "wifi_country": "france"},
        {"authorized_keys": ["pas une cle"]},
        {"authorized_keys": ["ssh-ed25519 AAAA\nrm -rf /"]},
    ],
)
def test_invalid_settings_are_refused(settings):
    with pytest.raises(flasher.FlashError):
        flasher.check_settings(settings)


# --- identite de la carte ----------------------------------------------------------------------------

@pytest.fixture
def rootfs(tmp_path):
    root = tmp_path / "root"
    (root / "etc" / "ssh").mkdir(parents=True)
    (root / "var" / "lib" / "dbus").mkdir(parents=True)
    (root / "etc" / "hostname").write_text("master\n")
    (root / "etc" / "hosts").write_text("127.0.0.1\tlocalhost\n127.0.1.1\tmaster\n")
    (root / "etc" / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (root / "var" / "lib" / "dbus" / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    for key in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub", "ssh_host_rsa_key"):
        (root / "etc" / "ssh" / key).write_text("secret")
    (root / "etc" / "ssh" / "sshd_config").write_text("Port 22\n")
    return root


def test_reset_identity_makes_the_copy_unique(rootfs):
    flasher.reset_identity(rootfs, "module-02")
    assert (rootfs / "etc" / "hostname").read_text() == "module-02\n"
    hosts = (rootfs / "etc" / "hosts").read_text()
    assert "127.0.1.1\tmodule-02" in hosts and "master" not in hosts and "localhost" in hosts
    assert (rootfs / "etc" / "machine-id").read_text() == ""
    assert not (rootfs / "var" / "lib" / "dbus" / "machine-id").exists()
    assert sorted(p.name for p in (rootfs / "etc" / "ssh").iterdir()) == ["sshd_config"]  # la config SSH reste


def test_reset_identity_never_writes_through_a_symlink(rootfs, tmp_path):
    outside = tmp_path / "server-file"
    outside.write_text("fichier du SERVEUR")
    (rootfs / "etc" / "hostname").unlink()
    try:
        (rootfs / "etc" / "hostname").symlink_to(outside)
    except OSError:
        pytest.skip("liens symboliques indisponibles")
    flasher.reset_identity(rootfs, "module-02")
    assert outside.read_text() == "fichier du SERVEUR"
    assert (rootfs / "etc" / "hostname").read_text() == "module-02\n"


def test_reset_identity_refuses_something_that_is_not_a_linux_root(tmp_path):
    with pytest.raises(flasher.FlashError, match="Linux"):
        flasher.reset_identity(tmp_path, "module-02")


# --- pipeline -------------------------------------------------------------------------------------------

def run(tmp_path, images, **overrides):
    steps = []
    kwargs = dict(
        image_path=images / "master-gz.img.gz", target=str(tmp_path / "card.bin"), hostname="module-02",
        settings={"wifi_ssid": "expo", "wifi_psk": "motdepasse1", "authorized_keys": [GOOD_KEY]},
        dry_run=True, dry_run_dir=tmp_path / "sim", update=lambda **f: steps.append(f),
    )
    kwargs.update(overrides)
    return flasher.run_flash(**kwargs), steps


def test_dry_run_goes_through_all_four_steps(tmp_path, images):
    result, steps = run(tmp_path, images)
    assert (tmp_path / "card.bin").read_bytes() == PAYLOAD
    assert [s["step"] for s in steps if "step" in s][0] == "write"
    assert {s["step"] for s in steps} == set(flasher.STEPS)
    assert steps[-1]["percent"] == 100
    percents = [s["percent"] for s in steps]
    assert percents == sorted(percents)  # la progression ne recule jamais
    boot = tmp_path / "sim" / "module-02-boot"
    assert {p.name for p in boot.iterdir()} == set(flasher.SEED_FILES)
    assert yaml.safe_load((boot / "user-data").read_text())["hostname"] == "module-02"
    assert "expo" in yaml.safe_load((boot / "network-config").read_text())["network"]["wifis"]["wlan0"]["access-points"]
    assert (tmp_path / "sim" / "module-02-root" / "etc" / "hostname").read_text() == "module-02\n"
    assert result["dry_run"] and result["bytes"] == len(PAYLOAD)


def test_invalid_settings_are_caught_before_any_write(tmp_path, images):
    with pytest.raises(flasher.FlashError):
        run(tmp_path, images, settings={"wifi_ssid": "expo", "wifi_psk": "x"})
    assert not (tmp_path / "card.bin").exists()


def test_failed_verification_stops_before_customizing(tmp_path, images, monkeypatch):
    monkeypatch.setattr(flasher, "read_back_sha256", lambda *a, **k: "0" * 64)
    with pytest.raises(flasher.FlashError, match="verification"):
        run(tmp_path, images)
    assert not (tmp_path / "sim" / "module-02-boot").exists()  # carte douteuse : on ne la prepare pas


def test_real_mode_unmounts_first_and_ejects_last(tmp_path, images):
    calls = []

    class Mount:
        def __init__(self, name, path):
            self.name, self.path = name, path

        def __enter__(self):
            calls.append(f"mount-{self.name}")
            return self.path

        def __exit__(self, *exc):
            calls.append(f"umount-{self.name}")

    boot, root = tmp_path / "boot", tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    boot.mkdir()
    (tmp_path / "card.bin").write_bytes(b"")  # un vrai peripherique existe deja : l'ecriture n'en cree jamais
    run(tmp_path, images, dry_run=False,
        unmount=lambda t: calls.append("unmount"), do_eject=lambda t: calls.append("eject"),
        boot_mount=lambda t: Mount("boot", boot), root_mount=lambda t: Mount("root", root))
    assert calls == ["unmount", "mount-boot", "umount-boot", "mount-root", "umount-root", "eject"]


# --- API --------------------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, images, monkeypatch):
    spec = importlib.util.spec_from_file_location("sd_flasher_app", SRC / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sd_flasher_app"] = module  # Flask retrouve templates/ et static/
    spec.loader.exec_module(module)
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "reservations.conf").write_text(
        "module-02,10.42.0.148\nmodule-01,10.42.0.147\naa:bb:cc:dd:ee:ff,10.42.0.50,par-mac\n# commentaire\n"
    )
    (tmp_path / "pub").write_text(GOOD_KEY + "\n")
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps({"eligible": [dict(flasher.parse_lsblk({"blockdevices": [disk("sda")]})[0], reason="")], "ignored": []}))
    monkeypatch.setattr(module, "IMAGES_DIR", images)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(module, "RESERVATIONS_FILE", tmp_path / "res" / "reservations.conf")
    monkeypatch.setattr(module, "AUTOMATION_PUB", tmp_path / "pub")
    monkeypatch.setattr(module, "DEVICES_FILE", str(devices))
    monkeypatch.setattr(module, "DRY_RUN", True)
    monkeypatch.setattr(module, "DRY_RUN_DIR", tmp_path / "sim")
    module._job = {"state": "idle"}
    module.app.testing = True
    module.client = module.app.test_client()
    module.tmp = tmp_path
    return module


def wait_done(api, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = api.client.get("/api/status").get_json()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("flash trop long")


def flash(api, **overrides):
    body = {"device": "/dev/sda", "hostname": "module-02", "confirm": "module-02"}
    body.update(overrides)
    return api.client.post("/api/flash", json=body)


def test_status_lists_machines_by_name_only(api):
    data = api.client.get("/api/status").get_json()
    assert data["machines"] == [{"hostname": "module-01", "ip": "10.42.0.147"}, {"hostname": "module-02", "ip": "10.42.0.148"}]
    assert data["devices"]["eligible"][0]["path"] == "/dev/sda" and data["dry_run"] is True


def test_machines_are_read_with_the_lease_time_written_by_dnsmasq_admin(api):
    api.RESERVATIONS_FILE.write_text("module-02,10.42.0.148,12h\naa:bb:cc:dd:ee:ff,10.42.0.50,par-mac,12h\naa:bb:cc:dd:ee:00,10.42.0.51,12h\n")
    assert api.client.get("/api/status").get_json()["machines"] == [{"hostname": "module-02", "ip": "10.42.0.148"}]


def test_full_flash_through_the_api(api):
    assert flash(api).status_code == 202
    job = wait_done(api)
    assert job["state"] == "done" and job["ip"] == "10.42.0.148" and job["percent"] == 100
    sim = api.tmp / "sim"
    assert (sim / "sda").stat().st_size > 0
    user = yaml.safe_load((sim / "module-02-boot" / "user-data").read_text())
    assert user["ssh_authorized_keys"] == [GOOD_KEY]  # cle d'automatisation injectee par defaut
    events = [e["event"] for e in api.client.get("/api/audit").get_json()]
    assert events[:2] == ["flash-done", "flash-start"]


def test_confirmation_must_match_the_hostname(api):
    res = flash(api, confirm="module-03")
    assert res.status_code == 400 and "confirmation" in res.get_json()["error"].lower()
    assert api.client.get("/api/status").get_json()["job"]["state"] == "idle"


def test_a_device_outside_the_eligible_list_is_refused(api):
    for device in ("/dev/mmcblk0", "/dev/sda1", "../../dev/sda", "/dev/sdb", ""):
        assert flash(api, device=device).status_code == 409, device


def test_bad_hostname_is_refused(api):
    assert flash(api, hostname="Bad Name", confirm="Bad Name").status_code == 400


def test_unknown_image_name_is_refused(api):
    assert flash(api, image="../../etc/passwd").status_code == 409


def test_only_one_flash_at_a_time(api, monkeypatch):
    release = {"go": False}
    original = api.flasher.run_flash

    def slow(**kwargs):
        while not release["go"]:
            time.sleep(0.02)
        return original(**kwargs)

    monkeypatch.setattr(api.flasher, "run_flash", slow)
    assert flash(api).status_code == 202
    second = flash(api)
    assert second.status_code == 409 and "en cours" in second.get_json()["error"]
    assert api.client.post("/api/job/dismiss").status_code == 409
    release["go"] = True
    assert wait_done(api)["state"] == "done"


def test_a_failure_is_reported_and_audited(api, monkeypatch):
    def boom(**kwargs):
        raise flasher.FlashError("carte defectueuse")

    monkeypatch.setattr(api.flasher, "run_flash", boom)
    flash(api)
    job = wait_done(api)
    assert job["state"] == "error" and "defectueuse" in job["error"]
    assert api.client.get("/api/audit").get_json()[0]["event"] == "flash-error"
    assert api.client.post("/api/job/dismiss").get_json() == {"state": "idle"}


def test_wifi_password_is_never_sent_back(api):
    res = api.client.put("/api/settings", json={"wifi_ssid": "expo", "wifi_psk": "motdepasse1", "wifi_country": "fr"})
    data = res.get_json()
    assert res.status_code == 200 and data["has_wifi_psk"] and data["wifi_country"] == "FR"
    assert "motdepasse1" not in json.dumps(data) and "motdepasse1" not in api.client.get("/api/settings").get_data(as_text=True)
    assert "motdepasse1" not in api.client.get("/api/preview?hostname=module-02").get_data(as_text=True)
    # champ vide = on garde l'actuel
    api.client.put("/api/settings", json={"wifi_ssid": "expo2", "wifi_psk": ""})
    assert json.loads((api.DATA_DIR / "settings.json").read_text())["wifi_psk"] == "motdepasse1"


def test_settings_file_is_private(api):
    api.client.put("/api/settings", json={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"})
    if os.name != "nt":
        assert (api.DATA_DIR / "settings.json").stat().st_mode & 0o777 == 0o600


def test_invalid_settings_are_rejected(api):
    assert api.client.put("/api/settings", json={"wifi_ssid": "expo", "wifi_psk": "court"}).status_code == 400
    assert api.client.put("/api/settings", json={"extra_keys": ["pas une cle"]}).status_code == 400


def test_automation_key_can_be_switched_off(api):
    assert api.effective_settings()["authorized_keys"] == [GOOD_KEY]
    api.client.put("/api/settings", json={"use_automation_key": False})
    assert api.effective_settings()["authorized_keys"] == []


def test_preview_summarises_without_secrets(api):
    api.client.put("/api/settings", json={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"})
    data = api.client.get("/api/preview?hostname=module-02").get_json()
    assert data["wifi"] == "expo" and data["keys"] == 1
    assert api.client.get("/api/preview?hostname=Bad").status_code == 400


# --- garde-fous ajoutes a la relecture ----------------------------------------------------------------

def test_failed_unmount_never_deletes_the_cards_files(tmp_path, monkeypatch):
    # Un umount qui echoue laisse le dossier de montage = la carte : rmtree y effacerait tout
    # et le flash s'afficherait "reussi". On doit lever une erreur et ne RIEN supprimer.
    mnt = tmp_path / "mnt"
    mnt.mkdir()
    (mnt / "expolab.conf").write_text("HOSTNAME=module-02")
    monkeypatch.setattr(flasher, "reread_partitions", lambda device, number, wait_seconds=15: "/dev/fake1")
    monkeypatch.setattr(flasher.tempfile, "mkdtemp", lambda prefix="": str(mnt))

    class Done:
        def __init__(self, code, err=""):
            self.returncode, self.stderr, self.stdout = code, err, ""

    def fake_run(cmd, **kwargs):
        return Done(1, "target is busy") if cmd[0] == "umount" else Done(0)

    monkeypatch.setattr(flasher.subprocess, "run", fake_run)
    with pytest.raises(flasher.FlashError, match="demonter"):
        with flasher.mount_partition("/dev/fake", 1, "vfat"):
            pass
    assert (mnt / "expolab.conf").read_text() == "HOSTNAME=module-02"


def test_image_larger_than_the_card_is_refused_upfront(tmp_path):
    image = tmp_path / "big.img"
    image.write_bytes(b"x" * 5000)
    with pytest.raises(flasher.FlashError, match="plus grande"):
        flasher.check_fits(image, 4000)
    flasher.check_fits(image, 5000)  # juste assez : accepte
    gz = tmp_path / "big.img.gz"
    gz.write_bytes(b"x" * 5000)
    flasher.check_fits(gz, 100)  # compressee : taille inconnue d'avance, pas de refus ici


def test_a_swapped_disk_is_never_written(api, tmp_path, monkeypatch):
    # entre la verification et l'ecriture, /dev/sda devient un AUTRE disque (autre numero de serie)
    swapped = json.loads((tmp_path / "devices.json").read_text())
    swapped["eligible"][0]["serial"] = "AUTRE-DISQUE"
    original = api.read_devices
    calls = {"n": 0}

    def read_devices():
        calls["n"] += 1
        return original() if calls["n"] == 1 else swapped

    monkeypatch.setattr(api, "read_devices", read_devices)
    assert flash(api).status_code == 202
    job = wait_done(api)
    assert job["state"] == "error" and "a change" in job["error"]
    assert not (api.tmp / "sim" / "sda").exists()  # rien ecrit


def test_an_image_too_big_for_the_card_fails_before_writing(api, tmp_path, monkeypatch):
    big = api.IMAGES_DIR / "zzz-huge.img"
    big.write_bytes(b"x" * 10)
    devices = json.loads((tmp_path / "devices.json").read_text())
    devices["eligible"][0]["size"] = 5  # carte "plus petite" que l'image
    (tmp_path / "devices.json").write_text(json.dumps(devices))
    assert flash(api, image="zzz-huge.img").status_code == 202
    job = wait_done(api)
    assert job["state"] == "error" and "plus grande" in job["error"]


# --- envoi et suppression d'images depuis la page ------------------------------------------------------

MBR = b"\x00" * 510 + b"\x55\xaa" + b"\x00" * 2000  # debut d'une image de carte : signature de partitions


def put_image(api, name, body, sha256=None, headers=None):
    url = f"/api/images/{name}" + (f"?sha256={sha256}" if sha256 else "")
    return api.client.put(url, data=body, content_type="application/octet-stream", headers=headers or {})


def leftovers(api):
    return sorted(p.name for p in api.IMAGES_DIR.glob("*.part"))


def test_uploaded_archive_is_stored_and_listed_with_its_hash(api):
    import hashlib

    body = gzip.compress(PAYLOAD)
    res = put_image(api, "nouveau.img.gz", body)
    assert res.status_code == 201, res.get_json()
    digest = hashlib.sha256(body).hexdigest()
    assert (api.IMAGES_DIR / "nouveau.img.gz").read_bytes() == body
    assert res.get_json() == {"name": "nouveau.img.gz", "size": len(body), "sha256": digest}
    listed = {i["name"]: i for i in api.client.get("/api/status").get_json()["images"]}
    assert listed["nouveau.img.gz"]["sha256"] == digest and listed["master.img"]["sha256"] == ""  # copiee a la main : inconnue
    assert api.client.get("/api/audit").get_json()[0]["event"] == "image-upload"
    assert leftovers(api) == []


def test_uploaded_image_can_be_flashed(api):
    put_image(api, "zz-nouveau.img.xz", lzma.compress(PAYLOAD))
    assert flash(api, image="zz-nouveau.img.xz").status_code == 202
    assert wait_done(api)["state"] == "done"
    assert (api.tmp / "sim" / "sda").read_bytes() == PAYLOAD


def test_raw_image_needs_a_partition_table(api):
    assert put_image(api, "brute.img", MBR).status_code == 201
    res = put_image(api, "pas-une-image.img", b"juste du texte" * 100)
    assert res.status_code == 400 and "table de partitions" in res.get_json()["error"]
    assert not (api.IMAGES_DIR / "pas-une-image.img").exists() and leftovers(api) == []


def test_archive_must_match_its_extension(api):
    res = put_image(api, "faux.img.gz", b"ceci n'est pas du gzip" * 50)
    assert res.status_code == 400 and ".gz" in res.get_json()["error"]
    assert not (api.IMAGES_DIR / "faux.img.gz").exists() and leftovers(api) == []


@pytest.mark.parametrize("name", [".cache.img", "a b.img", "x.txt", "sans-extension", "x..img", "é.img", "x.img.exe"])
def test_bad_file_names_are_refused(api, name):
    assert put_image(api, name, MBR).status_code == 400


def test_an_existing_image_is_never_overwritten(api):
    assert put_image(api, "master.img", MBR).status_code == 409
    assert (api.IMAGES_DIR / "master.img").read_bytes() == PAYLOAD


def test_wrong_expected_hash_discards_the_upload(api):
    import hashlib

    res = put_image(api, "corrompue.img", MBR, sha256="0" * 64)
    assert res.status_code == 400 and "SHA-256" in res.get_json()["error"]
    assert not (api.IMAGES_DIR / "corrompue.img").exists() and leftovers(api) == []
    assert put_image(api, "correcte.img", MBR, sha256=hashlib.sha256(MBR).hexdigest().upper()).status_code == 201
    assert put_image(api, "invalide.img", MBR, sha256="pas-un-hash").status_code == 400


def test_upload_is_refused_when_there_is_no_room(api, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(api.shutil, "disk_usage", lambda path: usage(10**12, 10**12 - 5_000_000, 5_000_000))
    res = put_image(api, "grosse.img", MBR)
    assert res.status_code == 507 and "insuffisant" in res.get_json()["error"]
    assert not (api.IMAGES_DIR / "grosse.img").exists()


def test_empty_or_truncated_uploads_leave_nothing_behind(api):
    assert put_image(api, "vide.img", b"").status_code == 411
    # le navigateur annonce plus d'octets qu'il n'en envoie (connexion coupee) : on force CONTENT_LENGTH au niveau WSGI
    res = api.client.put("/api/images/coupee.img", data=MBR, content_type="application/octet-stream",
                         environ_overrides={"CONTENT_LENGTH": str(len(MBR) + 5000)})
    assert res.status_code == 400
    assert not (api.IMAGES_DIR / "coupee.img").exists() and leftovers(api) == []


def test_only_one_upload_at_a_time(api):
    assert api._upload_lock.acquire(blocking=False)
    try:
        res = put_image(api, "deux.img", MBR)
        assert res.status_code == 409 and "en cours" in res.get_json()["error"]
    finally:
        api._upload_lock.release()
    assert put_image(api, "deux.img", MBR).status_code == 201


def test_stale_partial_uploads_are_cleaned_at_startup(api):
    (api.IMAGES_DIR / "interrompue.img.part").write_bytes(b"x" * 100)
    assert "interrompue.img.part" not in {i["name"] for i in api.client.get("/api/status").get_json()["images"]}
    api.remove_stale_parts()
    assert leftovers(api) == []


def test_delete_removes_the_image_and_its_hash(api):
    put_image(api, "temporaire.img", MBR)
    assert (api.IMAGES_DIR / "temporaire.img.sha256").exists()
    assert api.client.delete("/api/images/temporaire.img").status_code == 200
    assert not (api.IMAGES_DIR / "temporaire.img").exists() and not (api.IMAGES_DIR / "temporaire.img.sha256").exists()
    assert api.client.get("/api/audit").get_json()[0]["event"] == "image-delete"


def test_delete_only_touches_listed_images(api):
    (api.IMAGES_DIR / "notes.txt").write_text("pas une image")
    assert api.client.delete("/api/images/notes.txt").status_code == 404
    assert api.client.delete("/api/images/inexistante.img").status_code == 404
    assert (api.IMAGES_DIR / "notes.txt").exists()


def test_image_being_written_cannot_be_deleted(api):
    api._job = {"state": "running", "image": "master.img"}
    assert api.client.delete("/api/images/master.img").status_code == 409
    assert (api.IMAGES_DIR / "master.img").exists()
    assert api.client.delete("/api/images/master-gz.img.gz").status_code == 200  # une autre image : ok


def test_status_reports_free_space(api):
    assert api.client.get("/api/status").get_json()["images_free"] > 0


def test_a_truncated_archive_gives_a_clear_error(tmp_path):
    full = gzip.compress(PAYLOAD)
    (tmp_path / "coupee.img.gz").write_bytes(full[: len(full) // 2])
    with pytest.raises(flasher.FlashError, match="corrompue"):
        b"".join(flasher.iter_image(tmp_path / "coupee.img.gz"))
    (tmp_path / "abimee.img.xz").write_bytes(lzma.compress(PAYLOAD)[:200])
    with pytest.raises(flasher.FlashError, match="corrompue"):
        b"".join(flasher.iter_image(tmp_path / "abimee.img.xz"))


@pytest.mark.parametrize(
    "name",
    [
        "ubuntu-24.04.3-preinstalled-server-arm64+raspi.img.xz",  # nom officiel d'Ubuntu : contient un "+"
        "master_v2=final@jeudi,1.img.xz",
        "Master.IMG.xz".replace("IMG", "img"),
    ],
)
def test_names_with_plus_and_similar_are_accepted(api, name):
    res = put_image(api, name, lzma.compress(PAYLOAD))
    assert res.status_code == 201, res.get_json()
    assert name in {i["name"] for i in api.client.get("/api/status").get_json()["images"]}
    assert api.client.delete(f"/api/images/{name.replace('+', '%2B')}").status_code == 200  # le "+" survit a l'URL


@pytest.mark.parametrize("name", ["+debut.img", "-debut.img", "=x.img", "a/b.img", "a\\b.img", "x*.img", "x?.img", "x%.img", "x:y.img"])
def test_unsafe_names_stay_refused(api, name):
    res = put_image(api, name.replace("/", "%2F"), MBR)
    assert res.status_code in (400, 404)
    assert not [p for p in api.IMAGES_DIR.iterdir() if p.name not in {"master.img", "master-gz.img.gz", "master-xz.img.xz", "notes.txt"}]


# --- compte utilisateur -----------------------------------------------------------------------------------


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl absent")
FAKE_HASH = "$6$sel$" + "a" * 86


def fake_hasher(password):
    return FAKE_HASH


def account_seed(**overrides):
    settings = {"user_name": "expo", "user_password": "motdepasse-secret", "authorized_keys": [GOOD_KEY]}
    settings.update(overrides)
    files = flasher.render_seed("module-02", settings, now=1_800_000_000, hasher=fake_hasher)
    return files, yaml.safe_load(files["user-data"])


def test_account_is_created_with_hashed_password_sudo_and_keys():
    _files, user = account_seed()
    entry = user["users"][0]
    assert entry["name"] == "expo" and entry["shell"] == "/bin/bash"
    assert entry["passwd"] == FAKE_HASH and entry["lock_passwd"] is False
    assert entry["sudo"] == "ALL=(ALL) NOPASSWD:ALL" and "sudo" in entry["groups"]
    assert entry["ssh_authorized_keys"] == [GOOD_KEY]
    assert "ssh_authorized_keys" not in user           # les cles sont rattachees au compte, pas au compte par defaut
    assert user["ssh_pwauth"] is True and user["chpasswd"] == {"expire": False}
    assert len(user["users"]) == 1                      # pas de "default" : le compte "ubuntu" n'est pas cree en plus


def test_the_plain_password_is_never_written_to_the_card():
    files, _user = account_seed()
    assert all("motdepasse-secret" not in content for content in files.values())


def test_account_without_password_is_key_only():
    _files, user = account_seed(user_password="")
    entry = user["users"][0]
    assert entry["lock_passwd"] is True and "passwd" not in entry
    assert user["ssh_pwauth"] is False


def test_password_ssh_login_can_be_switched_off():
    _files, user = account_seed(user_pwauth=False)
    assert user["users"][0]["passwd"] == FAKE_HASH and user["ssh_pwauth"] is False  # console seulement


def test_sudo_can_be_switched_off():
    _files, user = account_seed(user_sudo=False)
    assert "sudo" not in user["users"][0] and "groups" not in user["users"][0]


def test_without_an_account_the_images_default_user_is_kept():
    files = flasher.render_seed("module-02", {"authorized_keys": [GOOD_KEY]}, now=1_800_000_000, hasher=fake_hasher)
    user = yaml.safe_load(files["user-data"])
    assert "users" not in user and "ssh_pwauth" not in user and user["ssh_authorized_keys"] == [GOOD_KEY]


@pytest.mark.parametrize("name", ["root", "Root", "1abc", "a b", "x" * 40, "é", "a;b", "-x", "a/b"])
def test_invalid_account_names_are_refused(name):
    with pytest.raises(flasher.FlashError, match="utilisateur"):
        flasher.check_settings({"user_name": name})


@pytest.mark.parametrize("settings", [{"user_password": "motdepasse1"}, {"user_name": "expo", "user_password": "court"},
                                     {"user_name": "expo", "user_password": "a\nb" + "c" * 10}])
def test_invalid_account_passwords_are_refused(settings):
    with pytest.raises(flasher.FlashError):
        flasher.check_settings(settings)


def test_the_password_goes_through_stdin_not_the_command_line():
    seen = {}

    class Done:
        returncode, stdout = 0, FAKE_HASH + "\n"

    def run(cmd, **kwargs):
        seen["cmd"], seen["input"] = cmd, kwargs.get("input")
        return Done()

    assert flasher.hash_password("motdepasse-secret", run=run) == FAKE_HASH
    assert "motdepasse-secret" not in " ".join(seen["cmd"]) and "motdepasse-secret" in seen["input"]


def test_hashing_failures_are_clear_errors():
    class Failed:
        returncode, stdout = 1, ""

    with pytest.raises(flasher.FlashError, match="hacher"):
        flasher.hash_password("motdepasse1", run=lambda *a, **k: Failed())

    def missing(*a, **k):
        raise FileNotFoundError("openssl")

    with pytest.raises(flasher.FlashError, match="openssl"):
        flasher.hash_password("motdepasse1", run=missing)


@needs_openssl
def test_openssl_hash_is_salted_and_verifiable():
    import subprocess

    first, second = flasher.hash_password("motdepasse1"), flasher.hash_password("motdepasse1")
    assert first.startswith("$6$") and first != second  # sel aleatoire
    salt = first.split("$")[2]
    again = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input="motdepasse1\n", capture_output=True, text=True)
    assert again.stdout.strip() == first                 # le hache correspond bien au mot de passe


# --- compte utilisateur : API ---------------------------------------------------------------------------

def test_account_settings_never_leak_the_password(api):
    res = api.client.put("/api/settings", json={"user_name": "expo", "user_password": "motdepasse-secret"})
    data = res.get_json()
    assert res.status_code == 200 and data["user_name"] == "expo" and data["has_user_password"] is True
    for url in ("/api/settings", "/api/preview?hostname=module-02", "/api/status", "/api/audit"):
        assert "motdepasse-secret" not in api.client.get(url).get_data(as_text=True), url
    if os.name != "nt":
        assert (api.DATA_DIR / "settings.json").stat().st_mode & 0o777 == 0o600
    api.client.put("/api/settings", json={"user_name": "expo", "user_password": ""})   # champ vide : on garde
    assert json.loads((api.DATA_DIR / "settings.json").read_text())["user_password"] == "motdepasse-secret"


def test_account_password_can_be_removed_and_follows_the_account(api):
    api.client.put("/api/settings", json={"user_name": "expo", "user_password": "motdepasse-secret"})
    assert api.client.put("/api/settings", json={"clear_user_password": True}).get_json()["has_user_password"] is False
    api.client.put("/api/settings", json={"user_password": "motdepasse-secret"})
    assert api.client.put("/api/settings", json={"user_name": ""}).get_json()["has_user_password"] is False  # plus de compte : plus de mot de passe


def test_invalid_account_settings_are_rejected(api):
    assert api.client.put("/api/settings", json={"user_name": "root"}).status_code == 400
    assert api.client.put("/api/settings", json={"user_name": "expo", "user_password": "court"}).status_code == 400
    assert api.client.get("/api/settings").get_json()["user_name"] == ""   # rien d'enregistre


def test_preview_warns_when_nobody_could_log_in(api):
    api.client.put("/api/settings", json={"use_automation_key": False})
    data = api.client.get("/api/preview?hostname=module-02").get_json()
    assert data["user"] is None and any("Aucun compte" in w for w in data["warnings"])
    api.client.put("/api/settings", json={"user_name": "expo"})
    assert any("Ni mot de passe" in w for w in api.client.get("/api/preview?hostname=module-02").get_json()["warnings"])
    api.client.put("/api/settings", json={"user_password": "motdepasse-secret"})
    data = api.client.get("/api/preview?hostname=module-02").get_json()
    assert data["warnings"] == [] and data["user"] == "expo" and data["password"] is True and data["sudo"] is True


@needs_openssl
def test_a_flash_writes_the_account_to_the_card(api):
    api.client.put("/api/settings", json={"user_name": "expo", "user_password": "motdepasse-secret"})
    assert flash(api).status_code == 202
    assert wait_done(api)["state"] == "done"
    text = (api.tmp / "sim" / "module-02-boot" / "user-data").read_text()
    entry = yaml.safe_load(text)["users"][0]
    assert entry["name"] == "expo" and entry["passwd"].startswith("$6$")
    assert "motdepasse-secret" not in text


# --- Wi-Fi : redemarrage unique au premier demarrage --------------------------------------------------

def test_wifi_seed_reboots_once_after_the_first_configuration():
    # le Wi-Fi n'est pas active au premier demarrage : cloud-init redemarre la machine (power_state, une fois par instance)
    _files, user, _net = seed(settings={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"})
    assert user["power_state"]["mode"] == "reboot" and user["power_state"]["condition"] is True


def test_seed_without_wifi_never_reboots():
    _files, user, _net = seed()
    assert "power_state" not in user


def test_the_reboot_is_per_instance_so_it_cannot_loop():
    # power_state ne s'applique qu'une fois par instance : l'instance-id change a chaque flash (donc un
    # redemarrage par flash) et reste identique aux demarrages suivants (donc plus de redemarrage)
    first, _, _ = seed(settings={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"}, now=1_800_000_000)
    again, _, _ = seed(settings={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"}, now=1_800_000_000)
    assert first["meta-data"] == again["meta-data"]


def test_preview_announces_the_reboot_only_with_wifi(api):
    assert api.client.get("/api/preview?hostname=module-02").get_json()["reboot"] is False
    api.client.put("/api/settings", json={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"})
    assert api.client.get("/api/preview?hostname=module-02").get_json()["reboot"] is True


def test_a_flash_with_wifi_writes_the_reboot_to_the_card(api):
    api.client.put("/api/settings", json={"wifi_ssid": "expo", "wifi_psk": "motdepasse1"})
    assert flash(api).status_code == 202
    assert wait_done(api)["state"] == "done"
    user = yaml.safe_load((api.tmp / "sim" / "module-02-boot" / "user-data").read_text())
    assert user["power_state"]["mode"] == "reboot"


# --- pas de mises a jour automatiques ---------------------------------------------------------------------

def test_automatic_upgrades_are_switched_off_from_the_first_boot():
    # sinon unattended-upgrades garde le verrou dpkg pendant des minutes (apt et Ansible attendent)
    _files, user, _net = seed()
    conf = next(f for f in user["write_files"] if f["path"].startswith("/etc/apt/apt.conf.d/"))
    assert 'APT::Periodic::Unattended-Upgrade "0";' in conf["content"]
    assert 'APT::Periodic::Update-Package-Lists "0";' in conf["content"]
    assert conf["permissions"] == "0644"
    assert conf["path"] > "/etc/apt/apt.conf.d/20auto-upgrades"   # lu apres celui d'Ubuntu, donc prioritaire
    assert user["runcmd"][0][:3] == ["systemctl", "disable", "--now"]
    assert {"apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service"} <= set(user["runcmd"][0])


def test_apt_config_survives_the_yaml_round_trip():
    _files, user, _net = seed()
    content = user["write_files"][0]["content"]
    assert content.count("\n") == 5 and content.startswith("//")   # 1 commentaire + 4 reglages, un par ligne



# --- serveur de temps et proxy apt -----------------------------------------------------------------------

def files_by_path(user):
    return {f["path"]: f for f in user["write_files"]}


def test_time_server_is_added_to_chrony_not_substituted():
    # Ubuntu 26.04 utilise chrony : on AJOUTE le serveur du lab (prefere), les sources d'Ubuntu restent
    _files, user, _net = seed(settings={"time_server": "10.42.0.1"})
    src = files_by_path(user)["/etc/chrony/sources.d/expolab.sources"]
    assert "server 10.42.0.1 iburst prefer" in src["content"] and src["permissions"] == "0644"
    assert ["systemctl", "restart", "chrony"] in user["runcmd"]


def test_chrony_accepts_the_unauthenticated_lab_server_and_steps():
    # Sans authselectmode ignore, chrony attend des sources NTS injoignables en expo (constate sur materiel)
    _files, user, _net = seed(settings={"time_server": "10.42.0.1"})
    conf = files_by_path(user)["/etc/chrony/conf.d/expolab.conf"]["content"]
    assert "authselectmode ignore\n" in conf and "makestep 1 -1\n" in conf


def test_no_time_server_means_no_chrony_file():
    _files, user, _net = seed()
    assert "/etc/chrony/sources.d/expolab.sources" not in files_by_path(user)
    assert "/etc/chrony/conf.d/expolab.conf" not in files_by_path(user)
    assert ["systemctl", "restart", "chrony"] not in user["runcmd"]


def test_apt_proxy_is_written_only_when_configured():
    _files, user, _net = seed(settings={"apt_proxy": "http://10.42.0.1:3142"})
    assert files_by_path(user)["/etc/apt/apt.conf.d/01expolab-proxy"]["content"] == 'Acquire::http::Proxy "http://10.42.0.1:3142";\n'
    _files, user, _net = seed()
    assert "/etc/apt/apt.conf.d/01expolab-proxy" not in files_by_path(user)   # injoignable hors expo : casserait apt


def test_the_other_files_are_kept_when_adding_the_expo_servers():
    _files, user, _net = seed(settings={"time_server": "10.42.0.1", "apt_proxy": "http://10.42.0.1:3142"})
    assert "/etc/apt/apt.conf.d/99-expolab-no-auto-upgrade" in files_by_path(user)
    assert len(user["write_files"]) == 4


@pytest.mark.parametrize("value", ["10.42.0.1", "ntp.interne", "time.example.org", "a"])
def test_valid_time_servers(value):
    flasher.check_settings({"time_server": value})


@pytest.mark.parametrize("value", ["mauvais nom", "evil;rm -rf /", "10.42.0.1\nserver evil", "-x.fr", "x.fr-", "http://10.42.0.1", "é.fr", "a" * 300])
def test_invalid_time_servers_are_refused(value):
    # le nom finit dans un fichier de configuration de chrony : aucune injection de directive possible
    with pytest.raises(flasher.FlashError, match="temps"):
        flasher.check_settings({"time_server": value})


@pytest.mark.parametrize("value", ["http://10.42.0.1:3142", "http://cache.interne", "http://10.42.0.1", "http://a.b:80"])
def test_valid_apt_proxies(value):
    flasher.check_settings({"apt_proxy": value})


@pytest.mark.parametrize("value", ["10.42.0.1:3142", "https://10.42.0.1:3142", "http://10.42.0.1:3142/", 'http://x"; evil "', "http://", "ftp://x", "http://x y", "http://10.42.0.1:99999999", "http://a:b"])
def test_invalid_apt_proxies_are_refused(value):
    with pytest.raises(flasher.FlashError, match="Proxy apt"):
        flasher.check_settings({"apt_proxy": value})


def test_keyboard_and_locale_go_to_cloud_init():
    _files, user, _net = seed(settings={"keyboard_layout": "fr", "locale": "fr_FR.UTF-8"})
    assert user["keyboard"] == {"layout": "fr", "model": "pc105"}
    assert user["locale"] == "fr_FR.UTF-8"
    _files, user, _net = seed()
    assert "keyboard" not in user and "locale" not in user and "timezone" not in user   # vide : reglages de l'image
    _files, user, _net = seed(settings={"timezone": "Europe/Paris"})
    assert user["timezone"] == "Europe/Paris"


@pytest.mark.parametrize("settings", [{"keyboard_layout": "FR"}, {"keyboard_layout": "fr; rm"}, {"locale": "fr_FR"},
                                      {"locale": "fr_FR.UTF-8\nruncmd"}, {"timezone": "paris"},
                                      {"timezone": "Europe/Paris; rm"}, {"timezone": "../../etc/passwd"}])
def test_invalid_keyboard_or_locale_is_refused(settings):
    with pytest.raises(flasher.FlashError):
        flasher.check_settings(settings)


def test_api_defaults_to_french_keyboard_and_locale(api):
    s = api.client.get("/api/settings").get_json()
    assert s["keyboard_layout"] == "fr" and s["locale"] == "fr_FR.UTF-8"
    p = api.client.get("/api/preview?hostname=module-02").get_json()
    assert p["keyboard_layout"] == "fr" and p["locale"] == "fr_FR.UTF-8" and p["timezone"] == "Europe/Paris"
    assert s["timezone"] == "Europe/Paris"
    res = api.client.put("/api/settings", json={"keyboard_layout": "BE", "locale": ""})
    assert res.status_code == 200 and res.get_json()["keyboard_layout"] == "be" and res.get_json()["locale"] == ""
    assert api.client.put("/api/settings", json={"locale": "francais"}).status_code == 400


def test_api_defaults_to_the_lab_time_server_and_no_proxy(api):
    s = api.client.get("/api/settings").get_json()
    assert s["time_server"] == "10.42.0.1" and s["apt_proxy"] == ""
    p = api.client.get("/api/preview?hostname=module-02").get_json()
    assert p["time_server"] == "10.42.0.1" and p["apt_proxy"] is None


def test_api_saves_and_clears_both(api):
    res = api.client.put("/api/settings", json={"time_server": "ntp.interne", "apt_proxy": "http://10.42.0.1:3142"})
    assert res.status_code == 200 and res.get_json()["apt_proxy"] == "http://10.42.0.1:3142"
    assert api.client.get("/api/preview?hostname=module-02").get_json()["apt_proxy"] == "http://10.42.0.1:3142"
    res = api.client.put("/api/settings", json={"time_server": "", "apt_proxy": ""})
    assert res.get_json()["time_server"] == "" and res.get_json()["apt_proxy"] == ""
    assert api.client.get("/api/preview?hostname=module-02").get_json()["time_server"] is None


def test_api_refuses_invalid_values_and_keeps_the_previous_ones(api):
    assert api.client.put("/api/settings", json={"time_server": "mauvais nom"}).status_code == 400
    assert api.client.put("/api/settings", json={"apt_proxy": "10.42.0.1:3142"}).status_code == 400
    s = api.client.get("/api/settings").get_json()
    assert s["time_server"] == "10.42.0.1" and s["apt_proxy"] == ""


@needs_openssl
def test_a_flash_writes_the_expo_servers_to_the_card(api):
    api.client.put("/api/settings", json={"apt_proxy": "http://10.42.0.1:3142"})
    assert flash(api).status_code == 202
    assert wait_done(api)["state"] == "done"
    user = yaml.safe_load((api.tmp / "sim" / "module-02-boot" / "user-data").read_text())
    paths = {f["path"] for f in user["write_files"]}
    assert {"/etc/chrony/sources.d/expolab.sources", "/etc/apt/apt.conf.d/01expolab-proxy"} <= paths


def test_flash_removes_files_of_settings_left_empty(tmp_path):
    root = tmp_path / "racine"
    for rel in (flasher.APT_PROXY_FILE, *flasher.TIME_SERVER_FILES):
        (root / rel.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (root / rel.lstrip("/")).write_text("ancienne valeur de l'image")
    done = flasher.remove_unset_files(root, {"apt_proxy": "", "time_server": "10.42.0.1"})
    assert done == [f"{flasher.APT_PROXY_FILE} retire (reglage vide)"]
    assert not (root / flasher.APT_PROXY_FILE.lstrip("/")).exists()
    assert all((root / rel.lstrip("/")).exists() for rel in flasher.TIME_SERVER_FILES)   # reglage rempli : reecrit par cloud-init
    assert len(flasher.remove_unset_files(root, {})) == 2 and flasher.remove_unset_files(root, {}) == []


def test_seed_writes_the_files_that_remove_unset_files_knows():
    _files, user, _net = seed(settings={"apt_proxy": "http://10.42.0.1:3142", "time_server": "10.42.0.1"})
    paths = {f["path"] for f in user["write_files"]}
    assert {flasher.APT_PROXY_FILE, *flasher.TIME_SERVER_FILES} <= paths


# --- paquets installes au demarrage (via le cache apt) ------------------------------------------

PROXY = "http://10.42.0.1:3142"


def test_seed_installs_packages_through_a_retrying_service():
    _files, user, _net = seed(settings={"apt_proxy": PROXY, "packages": ["avahi-daemon", "htop"]})
    files = files_by_path(user)
    script = files[flasher.PACKAGES_SCRIPT]
    assert script["permissions"] == "0755" and script["content"].startswith("#!/bin/sh\n")
    assert "install -y -q --no-install-recommends avahi-daemon htop" in script["content"]
    unit = files[flasher.PACKAGES_UNIT]["content"]
    assert "After=network-online.target\n" in unit and f"ConditionPathExists=!{flasher.PACKAGES_DONE}" in unit
    # Jamais rattache au demarrage ni ordonne apres cloud-final : boucle avec multi-user.target, que
    # systemd cassait en annulant le service (constate sur une vraie carte).
    assert "cloud-final" not in unit and "[Install]" not in unit and "multi-user" not in unit
    timer = files[flasher.PACKAGES_TIMER]["content"]
    for line in ("OnBootSec=1min", "OnUnitInactiveSec=2min", "Unit=expolab-packages.service", "WantedBy=timers.target"):
        assert line in timer
    assert "DPkg::Lock::Timeout=300" in script["content"] and "systemctl disable --now expolab-packages.timer" in script["content"]
    assert ["systemctl", "enable", "--now", "expolab-packages.timer"] in user["runcmd"]
    assert "packages" not in user   # jamais le module cloud-init, qui ne reessaie pas


def test_no_packages_no_service():
    _files, user, _net = seed(settings={"apt_proxy": PROXY})
    assert flasher.PACKAGES_UNIT not in files_by_path(user)
    assert not any("expolab-packages.service" in " ".join(c) for c in user["runcmd"])


@pytest.mark.parametrize("settings, message", [
    ({"packages": ["htop"]}, "proxy apt"),
    ({"apt_proxy": PROXY, "packages": ["htop; rm -rf /"]}, "invalide"),
    ({"apt_proxy": PROXY, "packages": ["Htop"]}, "invalide"),
    ({"apt_proxy": PROXY, "packages": ["-y"]}, "invalide"),
    ({"apt_proxy": PROXY, "packages": [f"p{i:03d}" for i in range(101)]}, "Trop"),
])
def test_package_settings_are_checked(settings, message):
    with pytest.raises(flasher.FlashError, match=message):
        flasher.check_settings(settings)


def test_api_packages_from_text_deduplicated_and_shown_in_preview(api):
    res = api.client.put("/api/settings", json={"apt_proxy": PROXY, "packages": "avahi-daemon\nHTOP, htop  avahi-daemon\n"})
    assert res.status_code == 200 and res.get_json()["packages"] == ["avahi-daemon", "htop"]
    assert api.client.get("/api/preview?hostname=module-02").get_json()["packages"] == ["avahi-daemon", "htop"]
    assert api.client.put("/api/settings", json={"apt_proxy": "", "packages": "htop"}).status_code == 400
    assert api.client.put("/api/settings", json={"apt_proxy": PROXY, "packages": ""}).get_json()["packages"] == []


def test_flash_cleans_package_service_and_stamp_of_a_captured_image(tmp_path):
    root = tmp_path / "racine"
    for rel in (flasher.PACKAGES_SCRIPT, flasher.PACKAGES_UNIT, flasher.PACKAGES_WANTS, flasher.PACKAGES_DONE,
                flasher.PACKAGES_TIMER, flasher.PACKAGES_TIMER_WANTS):
        (root / rel.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (root / rel.lstrip("/")).write_text("de l'image")
    # Paquets demandes : seule la marque "deja fait" part (sinon l'installation ne se ferait jamais).
    flasher.remove_unset_files(root, {"apt_proxy": PROXY, "time_server": "x", "packages": ["htop"]})
    assert not (root / flasher.PACKAGES_DONE.lstrip("/")).exists()
    assert not (root / flasher.PACKAGES_WANTS.lstrip("/")).exists()   # ancien lien (boucle) : toujours retire
    assert (root / flasher.PACKAGES_UNIT.lstrip("/")).exists() and (root / flasher.PACKAGES_TIMER.lstrip("/")).exists()
    # Aucun paquet demande : le service de l'image part aussi.
    flasher.remove_unset_files(root, {"apt_proxy": PROXY, "time_server": "x"})
    for rel in (flasher.PACKAGES_SCRIPT, flasher.PACKAGES_UNIT, flasher.PACKAGES_TIMER, flasher.PACKAGES_TIMER_WANTS):
        assert not (root / rel.lstrip("/")).exists()


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="sh Linux requis")
def test_install_script_retries_until_success_then_stops(tmp_path):
    files = {f["path"]: f["content"] for f in flasher.packages_files(["avahi-daemon", "htop"])}
    stamp = tmp_path / "expolab" / "packages.done"
    script = tmp_path / "install.sh"
    script.write_text(files[flasher.PACKAGES_SCRIPT].replace(flasher.PACKAGES_DONE, str(stamp)))
    fakes = tmp_path / "bin"
    fakes.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("apt-get", "dpkg", "systemctl"):
        (fakes / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\ncase " $* " in *" install "*) [ -f "{tmp_path}/fail" ] && exit 100;; esac\nexit 0\n')
        (fakes / tool).chmod(0o755)
    env = dict(os.environ, PATH=f"{fakes}:{os.environ['PATH']}")
    (tmp_path / "fail").write_text("")   # cache injoignable : l'installation echoue
    assert subprocess.run(["sh", str(script)], env=env).returncode != 0 and not stamp.exists()
    (tmp_path / "fail").unlink()
    assert subprocess.run(["sh", str(script)], env=env).returncode == 0 and stamp.exists()
    assert "apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends avahi-daemon htop" in log.read_text()
    assert "systemctl disable --now expolab-packages.timer" in log.read_text()   # le minuteur s'arrete
    calls = log.read_text().count("\n")
    assert subprocess.run(["sh", str(script)], env=env).returncode == 0
    assert log.read_text().count("\n") == calls   # deja fait : apt n'est plus jamais appele


# --- interface d'exploitation ------------------------------------------------------------------

def test_exploitation_needs_an_image_chosen_by_the_admin(api):
    st = api.client.get("/api/exploitation/status").get_json()
    assert st["image_ready"] is False and st["job"] == {"state": "idle"}
    assert {m["hostname"] for m in st["machines"]} == {"module-01", "module-02"}
    assert api.client.post("/api/exploitation/flash", json={"device": "/dev/sda", "hostname": "module-02"}).status_code == 409
    assert api.client.put("/api/settings", json={"operator_image": "inconnue.img"}).status_code == 400
    assert api.client.put("/api/settings", json={"operator_image": "master.img"}).get_json()["operator_image"] == "master.img"
    assert api.client.get("/api/exploitation/status").get_json()["image_ready"] is True


def test_exploitation_flashes_only_a_reserved_machine_with_the_admin_image(api):
    api.client.put("/api/settings", json={"operator_image": "master-xz.img.xz"})
    post = lambda body: api.client.post("/api/exploitation/flash", json=body)  # noqa: E731
    assert post({"device": "/dev/sda", "hostname": "pas-reservee"}).status_code == 400
    assert post({"device": "/dev/sdz", "hostname": "module-02"}).status_code == 409
    res = post({"device": "/dev/sda", "hostname": "module-02", "image": "master.img"})   # l'image demandee est ignoree
    assert res.status_code == 202 and res.get_json()["kind"] == "flash"
    job = wait_done(api)
    assert job["state"] == "done" and job["image"] == "master-xz.img.xz"
    seen = api.client.get("/api/exploitation/status").get_json()["job"]
    assert seen["state"] == "done" and seen["hostname"] == "module-02" and "image" not in seen and "result" not in seen
    audit = api.client.get("/api/audit").get_json()
    assert any(a["event"] == "flash-start" and a.get("actor") == "exploitant" for a in audit)
    assert api.client.post("/api/exploitation/dismiss").get_json() == {"state": "idle"}


def test_exploitation_hides_an_admin_capture(api):
    api._job = {"kind": "capture", "state": "running", "image": "secret.img.zst", "device": "/dev/sda"}
    assert api.client.get("/api/exploitation/status").get_json()["job"] == {"state": "busy"}
    assert api.client.post("/api/exploitation/dismiss").get_json() == {"state": "busy"}   # jamais annulee par l'exploitant
    api._job = {"state": "idle"}


def test_exploitation_page_is_served(api):
    res = api.client.get("/exploitation")
    assert res.status_code == 200 and b"exploitation.js" in res.data
