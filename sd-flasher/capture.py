"""Capture d'une carte SD en image reduite ("shrink"), sans jamais modifier la carte.

La carte est seulement LUE (jamais montee, jamais ouverte en ecriture). Une copie
brute d'une carte de 32 Go demanderait 32 Go au serveur ; ici on ne copie que ce
qui sert :
  1. le debut de la carte, jusqu'a la partition systeme (table des partitions,
     partition de demarrage) ;
  2. les SEULS blocs utilises de la partition systeme (ext2/3/4), par `e2image -ra`,
     dans un fichier creux (seuls les blocs ecrits occupent le disque) ;
  3. e2fsck puis `resize2fs -M` sur ce fichier : systeme de fichiers a sa taille
     minimale ;
  4. la taille de la partition est corrigee dans la table MBR copiee, puis debut +
     partition reduite partent dans le compresseur (ou un .img creux).

Dispositions acceptees : table MBR ("dos"), partition systeme ext2/3/4 primaire et
DERNIERE sur la carte - Raspberry Pi OS, Ubuntu pour Raspberry Pi. Le reste est
refuse avant de rien copier.

Aucune dependance a Flask : les commandes passent par `run` (injectable) et la
source peut etre un fichier image (tests, mode simulation) au lieu d'une carte.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

CHUNK = 4 * 1024 * 1024
EXT_MAGIC = 0xEF53
COMPRESSIONS = {"zst": ".img.zst", "xz": ".img.xz", "none": ".img"}
STEPS = ("read", "shrink", "compress", "checksum")
STEP_LABELS = {"read": "Lecture de la carte", "shrink": "Reduction", "compress": "Compression", "checksum": "Empreinte"}
STEP_SPAN = {"read": (0, 45), "shrink": (45, 60), "compress": (60, 95), "checksum": (95, 100)}


class CaptureError(Exception):
    pass


# --- lecture de la carte ----------------------------------------------------------------------

def read_table(source: str, run=subprocess.run) -> dict:
    """Table des partitions (sfdisk -J) : {"label", "sectorsize", "partitions": [...]}."""
    result = run(["sfdisk", "-J", source], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise CaptureError(f"Table des partitions illisible : {result.stderr.strip() or 'carte vide ?'}")
    table = json.loads(result.stdout)["partitiontable"]
    return {"label": table.get("label"), "sectorsize": int(table.get("sectorsize", 512)),
            "partitions": [{"node": p["node"], "start": int(p["start"]), "size": int(p["size"])} for p in table.get("partitions", [])]}


def partition_number(node: str) -> int:
    digits = ""
    for ch in reversed(node):
        if not ch.isdigit():
            break
        digits = ch + digits
    return int(digits) if digits else 0


def ext_superblock(path: str, offset: int = 0) -> dict | None:
    """Superbloc ext2/3/4 lu directement (1024 octets apres le debut de la partition) :
    {"block_size", "blocks", "free_blocks"} - None si ce n'est pas un systeme ext."""
    with open(path, "rb") as f:
        f.seek(offset + 1024)
        sb = f.read(1024)
    if len(sb) < 1024 or struct.unpack_from("<H", sb, 0x38)[0] != EXT_MAGIC:
        return None
    blocks_lo, free_lo, log_block = struct.unpack_from("<I", sb, 0x04)[0], struct.unpack_from("<I", sb, 0x0C)[0], struct.unpack_from("<I", sb, 0x18)[0]
    incompat = struct.unpack_from("<I", sb, 0x60)[0]
    blocks_hi = free_hi = 0
    if incompat & 0x80:   # 64bit
        blocks_hi, free_hi = struct.unpack_from("<I", sb, 0x150)[0], struct.unpack_from("<I", sb, 0x158)[0]
    return {"block_size": 1024 << log_block, "blocks": (blocks_hi << 32) | blocks_lo, "free_blocks": (free_hi << 32) | free_lo}


def plan_capture(source: str, run=subprocess.run) -> dict:
    """Verifie la disposition AVANT de copier quoi que ce soit, et chiffre l'operation."""
    table = read_table(source, run)
    if table["label"] != "dos":
        raise CaptureError(f"Table des partitions '{table['label']}' non prise en charge : seules les cartes MBR (Raspberry Pi OS, Ubuntu) le sont.")
    if not table["partitions"]:
        raise CaptureError("Aucune partition sur la carte.")
    sector = table["sectorsize"]
    root = max(table["partitions"], key=lambda p: p["start"])
    number = partition_number(root["node"])
    if not 1 <= number <= 4:
        raise CaptureError("La partition systeme n'est pas une partition primaire : disposition non prise en charge.")
    sb = ext_superblock(source, root["start"] * sector)
    if sb is None:
        raise CaptureError("La derniere partition n'est pas un systeme de fichiers ext2/3/4 : disposition non prise en charge.")
    used = (sb["blocks"] - sb["free_blocks"]) * sb["block_size"]
    return {"sector": sector, "root_number": number, "root_start": root["start"], "root_sectors": root["size"],
            "head_bytes": root["start"] * sector, "used_bytes": used, "fs_bytes": sb["blocks"] * sb["block_size"]}


def space_needed(plan: dict) -> int:
    """Fichier de travail (~ blocs utilises) + image finale (au pire non compressee)."""
    return 2 * (plan["head_bytes"] + plan["used_bytes"])


# --- table MBR ------------------------------------------------------------------------------

def patch_mbr(head: bytearray, number: int, start: int, sectors: int) -> None:
    """Change la taille (en secteurs) de la partition primaire `number` dans le MBR copie.
    Verifie que l'entree est bien celle attendue (meme debut) avant d'y toucher."""
    if head[510:512] != b"\x55\xaa":
        raise CaptureError("MBR invalide sur la copie : signature absente.")
    entry = 446 + 16 * (number - 1)
    if struct.unpack_from("<I", head, entry + 8)[0] != start:
        raise CaptureError("Entree MBR inattendue pour la partition systeme : rien n'a ete modifie.")
    struct.pack_into("<I", head, entry + 12, sectors)


# --- pipeline -------------------------------------------------------------------------------

def _allocated(path: Path) -> int:
    try:
        return path.stat().st_blocks * 512
    except (OSError, AttributeError):
        return 0


def _watch(path: Path, total: int, report: Callable[[float], None], stop: threading.Event) -> None:
    while not stop.wait(1):
        report(min(1.0, _allocated(path) / total) if total else 0)


def _check(result, what: str, ok=(0,)) -> None:
    if result.returncode not in ok:
        raise CaptureError(f"{what} a echoue : {(result.stderr or result.stdout or '').strip()[-400:]}")


def _feed(stream, head: bytes, part: Path, length: int, progress: Callable[[float], None]) -> None:
    total = len(head) + length
    stream.write(head)
    done = len(head)
    with open(part, "rb") as f:
        remaining = length
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                chunk = b"\0" * min(CHUNK, remaining)   # fin creuse du fichier de travail
            stream.write(chunk)
            remaining -= len(chunk)
            done += len(chunk)
            progress(done / total)


class _SparseWriter:
    """Ecrit un .img en sautant les blocs de zeros (fichier creux)."""
    def __init__(self, path: Path):
        self.f = open(path, "wb")
        self.size = 0

    def write(self, data: bytes) -> None:
        if data.count(0) == len(data):
            self.f.seek(len(data), os.SEEK_CUR)
        else:
            self.f.write(data)
        self.size += len(data)

    def close(self) -> None:
        self.f.truncate(self.size)
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()


def _compressor(compression: str, output: Path) -> subprocess.Popen:
    out = open(output, "wb")
    cmd = ["zstd", "-q", "-T0", "-10", "-c"] if compression == "zst" else ["xz", "-T0", "-6", "-c"]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.PIPE)
    finally:
        out.close()   # le processus garde son propre descripteur


def sha256_file(path: Path, on_read: Callable[[float], None] | None = None) -> str:
    digest, size, done = hashlib.sha256(), path.stat().st_size, 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
            done += len(chunk)
            if on_read:
                on_read(done / size if size else 1)
    return digest.hexdigest()


def run_capture(*, source: str, work_dir: Path, output: Path, compression: str,
                update: Callable[..., None], run=subprocess.run, free_bytes: int | None = None) -> dict:
    """Capture `source` (carte ou fichier) dans `output` (ecrit sous output.part puis
    renomme). `update(step=, percent=, message=)` rend compte. Leve CaptureError."""
    if compression not in COMPRESSIONS:
        raise CaptureError("Compression inconnue (zst, xz ou none).")

    def progress(step: str, fraction: float, message: str = "") -> None:
        lo, hi = STEP_SPAN[step]
        update(step=step, percent=round(lo + (hi - lo) * max(0.0, min(1.0, fraction))), message=message)

    started = time.monotonic()
    progress("read", 0, "Analyse de la carte")
    plan = plan_capture(source, run)
    if free_bytes is not None and space_needed(plan) > free_bytes:
        raise CaptureError(f"Espace insuffisant sur le serveur : {space_needed(plan) / 1e9:.1f} Go necessaires, {free_bytes / 1e9:.1f} Go disponibles.")

    work_dir.mkdir(parents=True, exist_ok=True)
    part, final_part = work_dir / "systeme.ext", Path(str(output) + ".part")
    try:
        # 1. debut de la carte (table + demarrage), puis les blocs utilises du systeme
        with open(source, "rb") as f:
            head = bytearray(f.read(plan["head_bytes"]))
        if len(head) != plan["head_bytes"]:
            raise CaptureError("Lecture du debut de la carte incomplete.")
        progress("read", 0.05, f"Copie des donnees utilisees ({plan['used_bytes'] / 1e9:.1f} Go)")
        stop = threading.Event()
        watcher = threading.Thread(target=_watch, args=(part, plan["used_bytes"], lambda x: progress("read", 0.05 + 0.95 * x, "Copie des donnees utilisees"), stop), daemon=True)
        watcher.start()
        try:
            result = run(["e2image", "-ra", "-p", "-o", str(plan["head_bytes"]), source, str(part)], capture_output=True, text=True)
        finally:
            stop.set()
            watcher.join()
        _check(result, "La copie de la partition systeme (e2image)")
        progress("read", 1, "Carte lue")

        # 2. reduction (sur la COPIE)
        progress("shrink", 0, "Verification du systeme de fichiers")
        _check(run(["e2fsck", "-fy", str(part)], capture_output=True, text=True), "La verification (e2fsck)", ok=(0, 1))
        progress("shrink", 0.3, "Reduction au minimum")
        _check(run(["resize2fs", "-M", str(part)], capture_output=True, text=True), "La reduction (resize2fs)")
        sb = ext_superblock(str(part))
        if sb is None:
            raise CaptureError("Systeme de fichiers illisible apres reduction.")
        fs_bytes = sb["blocks"] * sb["block_size"]
        sectors = -(-fs_bytes // plan["sector"])
        patch_mbr(head, plan["root_number"], plan["root_start"], sectors)
        progress("shrink", 1, f"Partition systeme : {plan['fs_bytes'] / 1e9:.1f} Go -> {fs_bytes / 1e9:.2f} Go")

        # 3. image finale
        image_bytes = len(head) + sectors * plan["sector"]
        label = "Compression" if compression != "none" else "Ecriture de l'image"
        report = lambda x: progress("compress", x, label)  # noqa: E731
        if compression == "none":
            writer = _SparseWriter(final_part)
            try:
                _feed(writer, bytes(head), part, sectors * plan["sector"], report)
            finally:
                writer.close()
        else:
            proc = _compressor(compression, final_part)
            try:
                _feed(proc.stdin, bytes(head), part, sectors * plan["sector"], report)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise CaptureError(f"La compression a echoue : {err.strip()[-400:]}")
        progress("compress", 1, f"Image : {image_bytes / 1e9:.2f} Go")

        # 4. empreinte, puis l'image apparait dans la liste (renommage atomique)
        digest = sha256_file(final_part, lambda x: progress("checksum", x, "Calcul de l'empreinte"))
        os.replace(final_part, output)
        progress("checksum", 1, "Terminee")
        return {"name": output.name, "bytes": output.stat().st_size, "image_bytes": image_bytes, "sha256": digest,
                "used_bytes": plan["used_bytes"], "seconds": round(time.monotonic() - started)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        final_part.unlink(missing_ok=True)
