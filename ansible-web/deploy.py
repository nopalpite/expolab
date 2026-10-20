"""Deploiement automatique d'une machine apres un flash par l'exploitant.

Le flasheur (sd-flasher) depose une DEMANDE par machine dans le dossier partage
`/deploy/requests/<nom>.json` : {hostname, ip, instance_id, requested_at}. ansible-web
la traite et publie l'ETAT dans `/deploy/status/<nom>.json`, que le flasheur lit pour
la page de l'exploitant. Un fichier par cote : jamais deux ecrivains sur le meme.

Etats : pending (pas encore traitee) -> waiting (attend que la carte demarre) ->
running (run Ansible en cours) -> done | failed ; timeout (jamais vue dans le delai) ;
none (rien a deployer : machine sans role dans Bastion/site.yml).

La carte attendue est reconnue a son instance-id cloud-init (unique a chaque flash) :
une ancienne carte encore allumee sous le meme nom n'est jamais prise pour la neuve.
Le run ne part qu'une fois le premier demarrage termine (cloud-init ne tourne plus).
Ce module ne fait que la logique ; app.py fournit Bastion, le clone et les runs.
"""
import json
import os
import re
from pathlib import Path

WAIT_SECONDS = int(os.environ.get("ANSIBLE_WEB_DEPLOY_WAIT", str(30 * 60)))
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")   # meme regle que le hostname du flasheur
FINAL = ("done", "failed", "timeout", "none")


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def valid_request(req: dict | None) -> bool:
    return bool(req) and bool(NAME_RE.match(str(req.get("hostname", "")))) \
        and isinstance(req.get("requested_at"), (int, float)) and bool(req.get("instance_id"))


def inventory_name(machines: list[dict], hostname: str) -> str | None:
    """Nom Bastion (= hote de l'inventaire) d'une machine, retrouve par son nom court :
    la reservation dit `rpi-test-02`, Bastion `rpi-test-02.expolab.lan`."""
    for m in machines:
        if str(m.get("name", "")).split(".")[0].lower() == hostname:
            return m["name"]
    return None


HOSTS_RE = re.compile(r"^hosts \((\d+)\):$")


def count_planned_tasks(list_output: str) -> int:
    """Taches prevues d'apres `ansible-playbook --list-hosts --list-tasks --limit <machine>` :
    seulement les plays qui visent la machine (`hosts (1):`). `--list-tasks` seul liste les
    taches de TOUS les plays, meme ceux dont les hotes ne correspondent pas a --limit."""
    count, in_tasks, targeted = 0, False, False
    for line in list_output.splitlines():
        stripped = line.strip()
        hosts = HOSTS_RE.match(stripped)
        if stripped.startswith("play #"):
            in_tasks, targeted = False, False
        elif hosts:
            targeted = int(hosts.group(1)) > 0
        elif stripped == "tasks:":
            in_tasks = True
        elif in_tasks and targeted and stripped:
            count += 1
    return count


def count_done_tasks(log: str) -> int:
    """Taches commencees dans le log d'un run (les `Gathering Facts` ne sont pas prevues)."""
    return sum(1 for line in log.splitlines()
               if line.startswith("TASK [") and not line.startswith("TASK [Gathering Facts]"))


def progress(done: int, planned: int) -> int:
    """Pourcentage affiche : jamais 100 avant la fin reelle du run."""
    if planned <= 0:
        return 0
    return max(1, min(99, round(100 * done / planned)))


def card_ready(output: str, instance_id: str) -> bool:
    """Sortie de `cat /var/lib/cloud/data/instance-id; cloud-init status` sur la machine :
    c'est la carte attendue, et son premier demarrage est fini."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != instance_id:
        return False
    status = next((line for line in lines[1:] if line.startswith("status:")), "")
    return bool(status) and "running" not in status and "not started" not in status
