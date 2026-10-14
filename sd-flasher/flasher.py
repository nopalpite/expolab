"""Logique de sd-flasher : detection des cartes, lecture de l'image master,
ecriture, verification, personnalisation de la partition de demarrage, ejection.

Aucune dependance a Flask ni a Docker : tout ce qui touche au materiel (lsblk,
montage, ejection) est isole dans de petites fonctions injectables, pour tester
le pipeline avec de simples fichiers a la place des disques.

SECURITE - ce module ecrit sur des disques bruts. Garde-fous, tous verifies a
chaque flash et jamais seulement a l'affichage :
  - seuls les disques USB (transport "usb"), en ecriture, de taille plausible ;
  - jamais un disque dont une partition sert a l'hote (racine, boot, docker...) ;
  - le chemin du peripherique doit etre dans la liste actuelle des disques
    eligibles (jamais un chemin fourni tel quel) ;
  - un fichier (mode simulation) est cree si besoin, un peripherique JAMAIS :
    ouvrir un chemin /dev inexistant ne doit pas creer un fichier dans /dev.
"""
import gzip
import hashlib
import json
import lzma
import os
import re
import subprocess
import tempfile
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

CHUNK = 4 * 1024 * 1024
SYNC_EVERY = 256 * 1024 * 1024
MIN_DEVICE_BYTES = 1_000_000_000
DEFAULT_MAX_DEVICE_BYTES = 256 * 1000**3
HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
IMAGE_SUFFIXES = (".img", ".img.gz", ".img.xz", ".img.zst")
SYSTEM_MOUNTPOINTS = ("/", "/boot", "/boot/firmware", "/var/lib/docker", "/home", "/var", "/usr", "/etc")
NO_AUTO_UPGRADE_CONF = (
    '// Ecrit par sd-flasher : pas de mises a jour automatiques (voir render_seed).\n'
    'APT::Periodic::Update-Package-Lists "0";\n'
    'APT::Periodic::Download-Upgradeable-Packages "0";\n'
    'APT::Periodic::Unattended-Upgrade "0";\n'
    'APT::Periodic::AutocleanInterval "0";\n'
)
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")  # noms de comptes Linux usuels
RESERVED_USERS = {"root"}
SSH_KEY_RE = re.compile(r"^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp\d+|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) [A-Za-z0-9+/=]+( .*)?$")
SEED_FILES = ("user-data", "network-config", "meta-data")  # graine cloud-init (NoCloud) sur la partition de demarrage


class FlashError(Exception):
    pass


# --- detection des disques --------------------------------------------------------

def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true")


def parse_lsblk(data: dict) -> list[dict]:
    """Sortie JSON de `lsblk -J -b -o NAME,PATH,SIZE,RM,RO,TRAN,TYPE,MODEL,VENDOR,SERIAL,MOUNTPOINTS`
    -> liste de disques avec leurs partitions et points de montage."""
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        parts = dev.get("children") or []
        mounts = [m for m in (dev.get("mountpoints") or []) if m]
        for part in parts:
            mounts += [m for m in (part.get("mountpoints") or []) if m]
        disks.append(
            {
                "path": dev.get("path") or f"/dev/{dev.get('name')}",
                "name": dev.get("name") or "",
                "size": int(dev.get("size") or 0),
                "removable": _truthy(dev.get("rm")),
                "readonly": _truthy(dev.get("ro")),
                "transport": (dev.get("tran") or "").lower(),
                "model": " ".join(filter(None, [(dev.get("vendor") or "").strip(), (dev.get("model") or "").strip()])),
                "serial": (dev.get("serial") or "").strip(),
                "partitions": [p.get("path") or f"/dev/{p.get('name')}" for p in parts],
                "mountpoints": mounts,
            }
        )
    return disks


def host_sources_in_use(mountinfo: str) -> set[str]:
    """Peripheriques montes par l'HOTE (lu dans /proc/1/mountinfo, conteneur en
    pid: host) : source des lignes `... - fstype source options`."""
    sources = set()
    for line in mountinfo.splitlines():
        _left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = right.split()
        if len(fields) >= 2 and fields[1].startswith("/dev/"):
            sources.add(fields[1])
    return sources


def eligibility(disk: dict, in_use: set[str], max_bytes: int = DEFAULT_MAX_DEVICE_BYTES) -> tuple[bool, str]:
    """(eligible, raison du refus). Prudent : le moindre doute refuse."""
    if disk["transport"] != "usb":
        return False, "pas en USB (disque interne ou systeme)"
    if disk["readonly"]:
        return False, "lecture seule"
    if not MIN_DEVICE_BYTES <= disk["size"] <= max_bytes:
        return False, f"taille hors limites ({MIN_DEVICE_BYTES // 10**9} Go a {max_bytes // 10**9} Go)"
    if disk["path"] in in_use or any(p in in_use for p in disk["partitions"]):
        return False, "utilise par le serveur"
    if any(m in SYSTEM_MOUNTPOINTS or m.startswith(("/boot/", "/var/lib/docker/")) for m in disk["mountpoints"]):
        return False, "monte comme partition systeme"
    return True, ""


def classify_devices(lsblk_json: dict, mountinfo: str, max_bytes: int = DEFAULT_MAX_DEVICE_BYTES) -> dict:
    in_use = host_sources_in_use(mountinfo)
    eligible, ignored = [], []
    for disk in parse_lsblk(lsblk_json):
        ok, reason = eligibility(disk, in_use, max_bytes)
        (eligible if ok else ignored).append(dict(disk, reason=reason))
    return {"eligible": eligible, "ignored": ignored}


def read_devices(max_bytes: int = DEFAULT_MAX_DEVICE_BYTES, mountinfo_path: str = "/proc/1/mountinfo") -> dict:
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,RM,RO,TRAN,TYPE,MODEL,VENDOR,SERIAL,MOUNTPOINTS"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    try:
        mountinfo = Path(mountinfo_path).read_text()
    except OSError:
        mountinfo = ""
    if not mountinfo:
        # Sans la vue des montages de l'hote, impossible de garantir qu'un disque
        # n'est pas celui du serveur : on n'autorise rien.
        data = json.loads(out.stdout)
        return {"eligible": [], "ignored": [dict(d, reason="montages de l'hote illisibles") for d in parse_lsblk(data)]}
    return classify_devices(json.loads(out.stdout), mountinfo, max_bytes)


# --- images -------------------------------------------------------------------------

def list_images(images_dir: Path) -> list[dict]:
    images = []
    if images_dir.is_dir():
        for path in sorted(images_dir.iterdir()):
            if path.is_file() and path.name.endswith(IMAGE_SUFFIXES):
                stat = path.stat()
                images.append({"name": path.name, "size": stat.st_size, "mtime": int(stat.st_mtime)})
    return images


def check_fits(image_path: Path, device_size: int) -> None:
    """Refuse d'entree une image brute plus grande que la carte (une image compressee
    n'a pas de taille connue d'avance : l'erreur d'espace viendra a l'ecriture)."""
    if image_path.name.endswith(".img") and image_path.stat().st_size > device_size:
        raise FlashError(f"L'image ({image_path.stat().st_size / 1e9:.1f} Go) est plus grande que la carte ({device_size / 1e9:.1f} Go).")


def iter_image(path: Path, progress: Callable[[int, int], None] | None = None) -> Iterator[bytes]:
    """Octets decompresses de l'image (.img, .gz, .xz, .zst). progress(lus, total)
    compte les octets du FICHIER (compresse) lus : la taille decompressee n'est pas
    connue d'avance."""
    total = path.stat().st_size
    with open(path, "rb") as raw:
        name = path.name
        proc = None
        if name.endswith(".gz"):
            stream = gzip.GzipFile(fileobj=raw)
            position = raw.tell
        elif name.endswith(".xz"):
            stream = lzma.LZMAFile(raw)
            position = raw.tell
        elif name.endswith(".zst"):
            # zstd n'est pas dans la bibliotheque standard : binaire externe, qui lit
            # le meme descripteur que nous (le decalage partage donne la progression).
            proc = subprocess.Popen(["zstd", "-dc", "-q"], stdin=raw, stdout=subprocess.PIPE)
            stream = proc.stdout
            position = lambda: os.lseek(raw.fileno(), 0, os.SEEK_CUR)  # noqa: E731
        else:
            stream = raw
            position = raw.tell
        try:
            while True:
                try:
                    chunk = stream.read(CHUNK)
                except (OSError, EOFError, lzma.LZMAError, zlib.error) as exc:
                    # archive tronquee ou corrompue (envoi interrompu, fichier abime)
                    raise FlashError(f"Image illisible ou corrompue ({path.name}) : {exc}") from exc
                if not chunk:
                    break
                if progress:
                    progress(min(position(), total), total)
                yield chunk
        finally:
            if proc is not None:
                proc.stdout.close()
                if proc.wait() != 0:
                    raise FlashError("zstd a echoue : image corrompue ou illisible")


# --- ecriture et verification ---------------------------------------------------------

def write_image(chunks: Iterator[bytes], target: str, *, create: bool = False,
                on_written: Callable[[int], None] | None = None, sync_every: int = SYNC_EVERY) -> tuple[int, str]:
    """Ecrit les octets sur `target`, retourne (octets ecrits, sha256). `create` : ne
    jamais le mettre a True pour un peripherique (voir l'en-tete du module)."""
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    if create:
        flags |= os.O_CREAT | os.O_TRUNC
    digest = hashlib.sha256()
    written = pending = 0
    try:
        fd = os.open(target, flags, 0o600)
    except OSError as exc:
        raise FlashError(f"Impossible d'ouvrir {target} en ecriture : {exc}") from exc
    try:
        for chunk in chunks:
            view = memoryview(chunk)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            digest.update(chunk)
            written += len(chunk)
            pending += len(chunk)
            if pending >= sync_every:
                os.fsync(fd)  # evite d'accumuler des Go en cache : progression honnete
                pending = 0
            if on_written:
                on_written(written)
        os.fsync(fd)
    except OSError as exc:
        raise FlashError(f"Ecriture impossible sur {target} : {exc}") from exc
    finally:
        os.close(fd)
    return written, digest.hexdigest()


def read_back_sha256(target: str, nbytes: int, on_read: Callable[[int], None] | None = None) -> str:
    """sha256 des `nbytes` premiers octets lus SUR le support (cache de pages
    invalide d'abord : on veut ce qui est vraiment ecrit, pas la memoire)."""
    digest = hashlib.sha256()
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        remaining = nbytes
        done = 0
        while remaining > 0:
            chunk = os.read(fd, min(CHUNK, remaining))
            if not chunk:
                raise FlashError("Lecture de verification : le support est plus court que l'image ecrite")
            digest.update(chunk)
            remaining -= len(chunk)
            done += len(chunk)
            if on_read:
                on_read(done)
    finally:
        os.close(fd)
    return digest.hexdigest()


# --- personnalisation de la carte ---------------------------------------------------------

def check_hostname(hostname: str) -> None:
    if not HOSTNAME_RE.match(hostname or ""):
        raise FlashError("Hostname invalide (minuscules, chiffres et tirets ; 3 a 32 caracteres ; commence par une lettre)")


def check_settings(settings: dict) -> None:
    ssid, psk = settings.get("wifi_ssid") or "", settings.get("wifi_psk") or ""
    if ssid and (len(ssid.encode()) > 32 or "\n" in ssid or "\r" in ssid):
        raise FlashError("SSID Wi-Fi invalide (32 octets maximum, une seule ligne)")
    if ssid and not 8 <= len(psk) <= 63:
        raise FlashError("Mot de passe Wi-Fi invalide (8 a 63 caracteres)")
    if "\n" in psk or "\r" in psk:
        raise FlashError("Mot de passe Wi-Fi invalide (une seule ligne)")
    if settings.get("wifi_country") and not re.fullmatch(r"[A-Z]{2}", settings["wifi_country"]):
        raise FlashError("Pays Wi-Fi invalide (code a 2 lettres, ex. FR)")
    for key in settings.get("authorized_keys") or []:
        if not SSH_KEY_RE.match(key.strip()):
            raise FlashError(f"Cle SSH invalide : {key[:40]}...")
    user, password = settings.get("user_name") or "", settings.get("user_password") or ""
    if user and (not USER_RE.match(user) or user in RESERVED_USERS):
        raise FlashError("Nom d'utilisateur invalide (minuscules, chiffres, - et _ ; commence par une lettre ; 32 caracteres maximum ; pas root).")
    if password and not user:
        raise FlashError("Un mot de passe demande un nom d'utilisateur.")
    if password and (not 8 <= len(password) <= 128 or "\n" in password or "\r" in password):
        raise FlashError("Mot de passe utilisateur invalide (8 a 128 caracteres, une seule ligne).")


def hash_password(password: str, run=subprocess.run) -> str:
    """Hache le mot de passe en SHA-512 crypt ($6$...) avec un sel aleatoire, par
    `openssl passwd -6`. Le mot de passe passe par l'entree standard (jamais en argument,
    visible dans la liste des processus). Seul le hache est ecrit sur la carte."""
    try:
        done = run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n", capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FlashError(f"Impossible de hacher le mot de passe (openssl) : {exc}") from exc
    digest = done.stdout.strip()
    if done.returncode != 0 or not digest.startswith("$6$"):
        raise FlashError("Impossible de hacher le mot de passe (openssl a echoue).")
    return digest


def render_seed(hostname: str, settings: dict, now: int | None = None, hasher=hash_password) -> dict[str, str]:
    """Graine cloud-init (source NoCloud) a poser sur la partition de demarrage
    (`system-boot`, FAT) d'Ubuntu Server : {nom de fichier: contenu}.
      user-data      hostname, compte utilisateur (nom, mot de passe HACHE, sudo), cles SSH
                     autorisees, regeneration des cles d'hote. Remplace le user-data par defaut
                     de l'image Ubuntu, qui definissait le compte et son mot de passe : sans
                     compte ici, personne ne pourrait se connecter.
      network-config  DHCP sur l'Ethernet, Wi-Fi si configure (netplan)
      meta-data       instance-id UNIQUE a chaque flash : cloud-init traite donc la carte
                      comme une nouvelle instance et rejoue ses modules (utilisateur,
                      cles SSH, hostname) meme si le master avait deja demarre une fois
    Les valeurs passent par json.dumps : le JSON est du YAML valide, sans aucun risque de
    guillemet ou de caractere special mal interprete."""
    check_hostname(hostname)
    check_settings(settings)

    def dump(data) -> str:
        return json.dumps(data, indent=2, ensure_ascii=True) + "\n"

    user = {"hostname": hostname, "preserve_hostname": False, "manage_etc_hosts": True, "ssh_deletekeys": True}
    keys = [k.strip() for k in (settings.get("authorized_keys") or []) if k.strip()]
    account = (settings.get("user_name") or "").strip()
    if account:
        # `users` sans "default" : seul ce compte est cree (pas le compte "ubuntu" par defaut)
        entry = {"name": account, "gecos": account, "shell": "/bin/bash", "lock_passwd": True}
        if settings.get("user_sudo", True):
            entry["groups"] = ["sudo"]
            entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"  # Ansible a besoin d'elever ses droits sans mot de passe
        if keys:
            entry["ssh_authorized_keys"] = keys
        password = settings.get("user_password") or ""
        if password:
            entry["passwd"] = hasher(password)
            entry["lock_passwd"] = False
        user["users"] = [entry]
        user["ssh_pwauth"] = bool(password and settings.get("user_pwauth", True))
        user["chpasswd"] = {"expire": False}  # pas de changement force au premier login (machines sans clavier)
    elif keys:
        user["ssh_authorized_keys"] = keys  # pas de compte defini : cles pour le compte par defaut de l'image

    # Pas de mises a jour automatiques : au premier demarrage Ubuntu lance unattended-upgrades, qui
    # garde le verrou dpkg pendant des minutes (apt et Ansible attendent) et, sans internet, echoue.
    # Les mises a jour passent par le cache apt du serveur, au moment choisi. La configuration est
    # ecrite des l'etape d'initialisation (avant que le minuteur d'Ubuntu ne declenche la mise a jour) ;
    # runcmd coupe en plus les minuteurs pour les demarrages suivants.
    user["write_files"] = [{
        "path": "/etc/apt/apt.conf.d/99-expolab-no-auto-upgrade",
        "permissions": "0644",
        "content": NO_AUTO_UPGRADE_CONF,
    }]
    user["runcmd"] = [["systemctl", "disable", "--now", "apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service"]]

    if settings.get("wifi_ssid"):
        # Le Wi-Fi decrit dans network-config est ecrit mais PAS active au premier demarrage
        # (comportement connu de cloud-init/netplan : le plus simple est de redemarrer, ce qui
        # evite de debrancher la machine a la main). power_state : une seule fois par instance,
        # donc un redemarrage par flash et aucun aux demarrages suivants.
        user["power_state"] = {"mode": "reboot", "delay": "now", "timeout": 30, "condition": True,
                               "message": "expolab : redemarrage unique apres la premiere configuration (active le Wi-Fi)"}

    network = {"version": 2, "ethernets": {"all-eth": {"match": {"name": "e*"}, "dhcp4": True, "optional": True}}}
    if settings.get("wifi_ssid"):
        network["wifis"] = {
            "wlan0": {
                "dhcp4": True,
                "optional": True,
                "regulatory-domain": settings.get("wifi_country") or "FR",
                "access-points": {settings["wifi_ssid"]: {"password": settings["wifi_psk"]}},
            }
        }

    instance = f"expolab-{hostname}-{now if now is not None else int(time.time())}"
    return {
        "user-data": "#cloud-config\n" + dump(user),
        "network-config": dump({"network": network}),
        "meta-data": f"instance-id: {instance}\nlocal-hostname: {hostname}\n",
    }


def write_seed(boot_dir: Path, seed: dict[str, str]) -> None:
    for name, content in seed.items():
        _write_text(boot_dir / name, content)


def partition_path(device: str, number: int) -> str:
    return f"{device}p{number}" if device[-1].isdigit() else f"{device}{number}"


def unmount_partitions(device: str) -> None:
    out = subprocess.run(["lsblk", "-nr", "-o", "PATH,MOUNTPOINTS", device], capture_output=True, text=True, timeout=15)
    for line in out.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2:
            for mountpoint in fields[1].split():
                if subprocess.run(["umount", mountpoint], capture_output=True).returncode != 0:
                    raise FlashError(f"Impossible de demonter {mountpoint} avant l'ecriture")


def reread_partitions(device: str, number: int, wait_seconds: int = 15) -> str:
    """Relit la table de partitions ; retourne la partition `number` une fois visible."""
    subprocess.run(["blockdev", "--rereadpt", device], capture_output=True, timeout=30)
    part = partition_path(device, number)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if os.path.exists(part):
            return part
        time.sleep(0.5)
    raise FlashError(f"La partition {part} n'est pas apparue apres l'ecriture (image sans table de partitions ?)")


@contextmanager
def mount_partition(device: str, number: int, fstype: str):
    part = reread_partitions(device, number)
    mountpoint = tempfile.mkdtemp(prefix="expolab-mnt-")
    try:
        # nosuid,nodev : on ne fait confiance a rien de ce qui est sur la carte
        result = subprocess.run(["mount", "-t", fstype, "-o", "rw,nosuid,nodev,sync", part, mountpoint], capture_output=True, text=True)
        if result.returncode != 0:
            raise FlashError(f"Montage de {part} impossible : {result.stderr.strip() or 'erreur inconnue'}")
        try:
            yield Path(mountpoint)
        finally:
            subprocess.run(["sync"], timeout=60)
            unmounted = subprocess.run(["umount", mountpoint], capture_output=True, text=True)
            if unmounted.returncode != 0:
                # Jamais de suppression recursive ici : tant que c'est monte, ce dossier EST la carte.
                raise FlashError(f"Impossible de demonter {part} apres ecriture : {unmounted.stderr.strip() or 'erreur inconnue'}")
    finally:
        try:
            os.rmdir(mountpoint)  # ne retire qu'un dossier VIDE (donc demonte)
        except OSError:
            pass


def mount_boot(device: str):
    return mount_partition(device, 1, "vfat")


def mount_root(device: str):
    return mount_partition(device, 2, "ext4")


def _write_text(path: Path, text: str) -> None:
    """Ecrit `path` en remplacant un eventuel lien symbolique PAR un fichier : la
    racine de la carte est montee chez nous, un lien absolu y pointerait vers le
    systeme du SERVEUR."""
    if path.is_symlink():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def reset_identity(root: Path, hostname: str) -> list[str]:
    """Rend la copie du master unique AVANT son premier demarrage :
      - /etc/hostname et /etc/hosts : le nom doit etre le bon des la premiere
        requete DHCP (les reservations dnsmasq sont par nom) ;
      - /etc/machine-id vide : systeme en genere un neuf. Cloner l'ancien donnerait
        le meme identifiant client DHCP a toutes les machines (baux qui se
        volent la meme adresse) ;
      - cles SSH d'hote supprimees : sinon toutes les machines partagent la meme
        identite SSH. cloud-init les regenere au premier demarrage (ssh_deletekeys).
    Retourne la liste de ce qui a ete fait (pour le journal)."""
    check_hostname(hostname)
    etc = root / "etc"
    if etc.is_symlink() or not etc.is_dir():
        raise FlashError("La partition racine ne ressemble pas a un systeme Linux (pas de /etc) : mauvaise image ?")
    done = []
    _write_text(etc / "hostname", hostname + "\n")
    hosts_path = etc / "hosts"
    lines = hosts_path.read_text().splitlines() if hosts_path.is_file() and not hosts_path.is_symlink() else ["127.0.0.1\tlocalhost"]
    lines = [ln for ln in lines if not ln.startswith("127.0.1.1")] + [f"127.0.1.1\t{hostname}"]
    _write_text(hosts_path, "\n".join(lines) + "\n")
    done.append(f"hostname {hostname}")
    _write_text(etc / "machine-id", "")
    dbus_id = root / "var" / "lib" / "dbus" / "machine-id"
    if dbus_id.is_file() and not dbus_id.is_symlink():
        dbus_id.unlink()
    done.append("machine-id vide")
    removed = 0
    ssh_dir = etc / "ssh"
    if ssh_dir.is_dir() and not ssh_dir.is_symlink():
        for key in ssh_dir.glob("ssh_host_*"):
            key.unlink()
            removed += 1
    done.append(f"{removed} fichier(s) de cle SSH d'hote supprime(s)")
    return done


def eject(device: str) -> None:
    """Retire proprement le disque (apres sync) : equivalent d'un 'ejecter' - le
    peripherique disparait du systeme, la carte peut etre retiree."""
    subprocess.run(["sync"], timeout=60)
    subprocess.run(["blockdev", "--flushbufs", device], capture_output=True, timeout=30)
    delete = Path("/sys/block") / os.path.basename(device) / "device" / "delete"
    try:
        delete.write_text("1")
    except OSError:
        pass  # ejection logicielle impossible : sans gravite, les donnees sont ecrites et synchronisees


# --- pipeline ---------------------------------------------------------------------------------

STEPS = ("write", "verify", "customize", "eject")
STEP_LABELS = {"write": "Ecriture de l'image", "verify": "Verification", "customize": "Personnalisation", "eject": "Ejection"}
STEP_SPAN = {"write": (0, 60), "verify": (60, 85), "customize": (85, 95), "eject": (95, 100)}


def run_flash(*, image_path: Path, target: str, hostname: str, settings: dict, dry_run: bool,
              dry_run_dir: Path | None, update: Callable[..., None],
              unmount=unmount_partitions, boot_mount=mount_boot, root_mount=mount_root, do_eject=eject) -> dict:
    """Execute les 4 etapes. `update(step=..., percent=..., message=...)` rend compte
    de l'avancement. Retourne un resume. Leve FlashError au premier probleme."""
    check_hostname(hostname)
    seed = render_seed(hostname, settings)  # valide tout AVANT de toucher au disque

    def progress(step: str, fraction: float, message: str = ""):
        lo, hi = STEP_SPAN[step]
        update(step=step, percent=round(lo + (hi - lo) * max(0.0, min(1.0, fraction))), message=message)

    started = time.monotonic()
    if not dry_run:
        update(step="write", percent=0, message="Demontage des partitions")
        unmount(target)

    progress("write", 0, f"Ecriture sur {target}")
    written, digest = write_image(
        iter_image(image_path, progress=lambda done, total: progress("write", done / total if total else 0, "Ecriture en cours")),
        target, create=dry_run,
    )
    progress("write", 1, f"{written / 1e9:.2f} Go ecrits")

    progress("verify", 0, "Relecture du support")
    if read_back_sha256(target, written, on_read=lambda done: progress("verify", done / written if written else 1, "Relecture du support")) != digest:
        raise FlashError("La verification a echoue : ce qui est sur le support differe de l'image (carte defectueuse ?). Ne l'utilise pas.")
    progress("verify", 1, "Ecriture verifiee")

    progress("customize", 0, "Reglages de la partition de demarrage")
    actions = []
    if dry_run:
        # Pas de vraies partitions : deux dossiers jouent la partition de demarrage et la racine.
        base = dry_run_dir or Path(tempfile.gettempdir())
        boot_dir, root_dir = base / f"{hostname}-boot", base / f"{hostname}-root"
        (root_dir / "etc" / "ssh").mkdir(parents=True, exist_ok=True)
        boot_dir.mkdir(parents=True, exist_ok=True)
        write_seed(boot_dir, seed)
        progress("customize", 0.5, "Identite de la machine")
        actions = reset_identity(root_dir, hostname)
    else:
        with boot_mount(target) as boot_dir:
            write_seed(boot_dir, seed)
        progress("customize", 0.5, "Identite de la machine")
        with root_mount(target) as root_dir:
            actions = reset_identity(root_dir, hostname)
    progress("customize", 1, f"Hostname {hostname} ecrit")

    progress("eject", 0, "Ejection")
    if not dry_run:
        do_eject(target)
    progress("eject", 1, "Terminee")
    return {"bytes": written, "sha256": digest, "seconds": round(time.monotonic() - started), "dry_run": dry_run,
            "customized": actions}
