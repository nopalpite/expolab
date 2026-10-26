"""Releases GitHub d'un mirroir : les programmes des machines (ex. un interactif Godot)
sont publies en release par la CI de leur depot, pas dans le git. Le mirroir les
recupere a chaque sync pour que le serveur les ait sans internet cote machines.

Seules les archives Linux d'une architecture connue (arm64 : Raspberry Pi ; x86_64 :
PC) sont telechargees, pour les KEEP releases les plus recentes (hors brouillons).
L'index donne l'architecture de chaque archive (convention des exports Godot :
<depot>-<version>-linux-<arch>.tar.gz) : la machine recoit celle qui lui correspond. Rangement : <dossier>/<mirroir>/<tag>/<archive>, et index.json par
mirroir (ce que ansible-web lit). Une archive deja la, de la bonne taille, n'est pas
retelechargee ; une release sortie des KEEP plus recentes est supprimee.

Le jeton est celui deja present dans l'URL du mirroir (lecture du depot et de ses
releases). Il n'est jamais ecrit ailleurs ni affiche.
"""
import hashlib
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# Architecture d'une archive d'apres son nom (aarch64/amd64 : autres noms courants).
ARCH_RE = re.compile(r"(?:^|[-_.])(arm64|aarch64|x86_64|amd64)(?=[-_.]|$)")
ARCH_NAMES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}
KEEP = int(os.environ.get("GIT_MIRROR_RELEASE_KEEP", "5"))
API = "https://api.github.com"
TAG_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
ASSET_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$")
TIMEOUT = 120


def github_repo(remote_url: str) -> tuple[str, str] | None:
    """(owner/depot, jeton) si le mirroir est un depot GitHub en HTTPS, sinon None."""
    parts = urlsplit(remote_url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        return None
    path = parts.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.count("/") != 1:
        return None
    return path, (parts.password or parts.username or "")


def asset_arch(name: str) -> str | None:
    """arm64 / x86_64 pour une archive Linux d'architecture reconnue, sinon None."""
    if "linux" not in name.lower():
        return None
    found = ARCH_RE.search(name)
    return ARCH_NAMES[found.group(1)] if found else None


def http_get(url: str, token: str, accept: str):
    """Requete GitHub authentifiee. Le jeton ne suit jamais une redirection (le
    telechargement d'une archive est redirige vers un stockage tiers)."""
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "expolab-git-mirror"})
    if token:
        req.add_unredirected_header("Authorization", "Bearer " + token)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def list_releases(repo: str, token: str, get=http_get) -> list[dict]:
    with get(f"{API}/repos/{repo}/releases?per_page=30", token, "application/vnd.github+json") as resp:
        return json.load(resp)


def download(asset_id: int, repo: str, token: str, dest: Path, size: int, get=http_get) -> str:
    """Telecharge une archive (fichier .part puis renommage) et renvoie son sha256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    written = 0
    with get(f"{API}/repos/{repo}/releases/assets/{asset_id}", token, "application/octet-stream") as resp, \
            open(part, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    if written != size:
        part.unlink(missing_ok=True)
        raise ValueError(f"{dest.name} : {written} octets recus, {size} attendus")
    os.replace(part, dest)
    return digest.hexdigest()


def read_index(root: Path, name: str) -> list[dict]:
    try:
        data = json.loads((root / name / "index.json").read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def sync_releases(name: str, remote_url: str, root: Path, get=None) -> list[dict]:
    """Met a jour les releases d'un mirroir et renvoie son index (les plus recentes d'abord).
    Leve une exception si GitHub est injoignable : le git du mirroir reste synchronise."""
    get = get or http_get
    target = github_repo(remote_url)
    if target is None:
        return []
    repo, token = target
    known = {(r["tag"], a["name"]): a for r in read_index(root, name) for a in r["assets"]}
    releases = [r for r in list_releases(repo, token, get)
                if not r.get("draft") and TAG_RE.match(str(r.get("tag_name", "")))]
    releases.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    index = []
    for rel in releases[:KEEP]:
        tag = rel["tag_name"]
        assets = []
        for asset in rel.get("assets") or []:
            asset_name = str(asset.get("name", ""))
            arch = asset_arch(asset_name)
            if arch is None or not ASSET_RE.match(asset_name):
                continue
            dest = root / name / tag / asset_name
            old = known.get((tag, asset_name))
            if old and dest.is_file() and dest.stat().st_size == asset["size"]:
                sha = old["sha256"]
            else:
                sha = download(asset["id"], repo, token, dest, asset["size"], get)
            assets.append({"name": asset_name, "arch": arch, "size": asset["size"], "sha256": sha})
        if assets:
            index.append({"tag": tag, "published_at": rel.get("published_at"),
                          "prerelease": bool(rel.get("prerelease")), "assets": assets})
    # Releases sorties de la liste (trop anciennes, ou supprimees sur GitHub) : place libre.
    keep = {r["tag"] for r in index}
    for path in (root / name).iterdir() if (root / name).is_dir() else []:
        if path.is_dir() and path.name not in keep:
            shutil.rmtree(path, ignore_errors=True)
    (root / name).mkdir(parents=True, exist_ok=True)
    tmp = root / name / "index.json.tmp"
    tmp.write_text(json.dumps(index, indent=2))
    os.replace(tmp, root / name / "index.json")
    return index


def asset_path(root: Path, name: str, tag: str, asset: str) -> Path | None:
    """Chemin d'une archive presente dans l'index (jamais un chemin construit librement)."""
    for rel in read_index(root, name):
        if rel["tag"] == tag and any(a["name"] == asset for a in rel["assets"]):
            path = root / name / tag / asset
            return path if path.is_file() else None
    return None
