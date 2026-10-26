"""Programmes des machines (onglet « Applications ») : quel depot du mirroir, et quelle
release, doit tourner sur quelle machine.

- L'association est enregistree ici (STORE, hors git) : {machine Bastion: {app, version,
  installed, run_id}}. `version` = un tag de release, ou « latest » (la plus recente au
  moment du run).
- Les programmes sont les depots de git-mirror qui ont des releases (archives linux-arm64
  recuperees par git-mirror, voir git-mirror/releases.py).
- A CHAQUE run, ansible-web resout l'association en variables (`expolab_apps`) passees a
  ansible-playbook (-e @fichier) : apps.yml (role `internal_app`) de bastion-ansible installe le programme
  sur les machines du run qui en ont un. Un run lance pour autre chose (tag, flash
  exploitant) installe donc aussi le programme.
- « Installee » = ce qu'un run a reussi a poser sur la machine, d'apres son PLAY RECAP.
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MIRROR_URL = os.environ.get("ANSIBLE_WEB_MIRROR_URL", "http://127.0.0.1:5054")
LATEST = "latest"
RECAP_LINE_RE = re.compile(r"^(\S+)\s+:\s+ok=\d+\s+changed=\d+\s+unreachable=(\d+)\s+failed=(\d+)", re.MULTILINE)


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def mirror_get(path: str):
    with urllib.request.urlopen(MIRROR_URL + path, timeout=10) as resp:
        return json.load(resp)


def programs(get=None) -> list[dict]:
    """Depots du mirroir qui ont au moins une release : [{name, releases: [index git-mirror]}]."""
    get = get or mirror_get
    found = []
    for mirror in get("/api/mirrors").get("mirrors", []):
        if not mirror.get("releases"):
            continue
        rel = get(f"/api/mirrors/{mirror['name']}/releases").get("releases", [])
        if rel:
            found.append({"name": mirror["name"], "releases": rel})
    return found


def resolve(program: dict | None, version: str) -> dict | None:
    """La release voulue (« latest » = la plus recente), ou None si elle n'existe plus."""
    if not program or not program["releases"]:
        return None
    if version == LATEST:
        return program["releases"][0]
    return next((r for r in program["releases"] if r["tag"] == version), None)


def run_vars(store: dict, progs: list[dict]) -> dict:
    """Variables passees a un run : le programme resolu de chaque machine qui en a un."""
    by_name = {p["name"]: p for p in progs}
    apps = {}
    for host, entry in sorted(store.items()):
        if not entry.get("app"):
            continue
        rel = resolve(by_name.get(entry["app"]), entry.get("version") or LATEST)
        archives = {}
        for asset in (rel or {}).get("assets") or []:
            arch = asset_arch(asset)
            if arch and arch not in archives:
                archives[arch] = {
                    "archive": asset["name"],
                    "sha256": asset["sha256"],
                    "url": f"{MIRROR_URL}/releases/{entry['app']}/{rel['tag']}/{asset['name']}",
                }
        if not archives:
            continue
        # Toutes les architectures de la release : le role prend celle de la machine.
        apps[host] = {"name": entry["app"], "version": rel["tag"], "archives": archives}
    return {"expolab_apps": apps}


def asset_arch(asset: dict) -> str | None:
    """arm64 / x86_64 d'une archive (donnee par git-mirror ; d'apres son nom pour un
    index plus ancien, qui ne la donnait pas)."""
    if asset.get("arch") in ("arm64", "x86_64"):
        return asset["arch"]
    name = asset.get("name", "")
    return "arm64" if "linux-arm64" in name else "x86_64" if "linux-x86_64" in name else None


def release_arches(release: dict) -> list[str]:
    return sorted({a for a in (asset_arch(x) for x in release.get("assets") or []) if a})


def hosts_ok(log: str) -> set[str]:
    """Machines sans echec ni injoignabilite dans le dernier PLAY RECAP d'un run."""
    recap = log.rsplit("PLAY RECAP", 1)
    if len(recap) < 2:
        return set()
    return {m.group(1) for m in RECAP_LINE_RE.finditer(recap[1]) if m.group(2) == "0" and m.group(3) == "0"}


def record_installed(store: dict, snapshot: dict, log: str, now: str | None = None) -> bool:
    """Apres un run : la version installee des machines qui l'ont recue sans echec. Seulement
    si le role `internal_app` a vraiment tourne (un depot Ansible sans lui ne pose rien)."""
    if "TASK [internal_app : " not in log:
        return False
    now = now or datetime.now(timezone.utc).isoformat()
    changed = False
    for host in hosts_ok(log) & set(snapshot):
        app = snapshot[host]
        entry = store.setdefault(host, {})
        entry["installed"] = {"app": app["name"], "version": app["version"], "at": now}
        entry.pop("run_id", None)   # l'etat vient desormais de ce qui est installe
        changed = True
    return changed


def row_state(entry: dict, wanted: dict | None, run: dict | None) -> str:
    """none | missing (release disparue) | queued | running | failed | uptodate | outdated."""
    if not entry.get("app"):
        return "none"
    if run and run.get("status") in ("queued", "running"):
        return run["status"]
    if run and run.get("status") in ("failed", "cancelled"):
        return "failed"
    if wanted is None:
        return "missing"
    installed = entry.get("installed") or {}
    if installed.get("app") == entry["app"] and installed.get("version") == wanted["tag"]:
        return "uptodate"
    return "outdated"
