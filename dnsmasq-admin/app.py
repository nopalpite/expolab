#!/usr/bin/env python3
"""expolab dnsmasq-admin : baux DHCP et reservations.

Les baux (server/stacks/dnsmasq/data/dnsmasq.leases) sont en lecture
seule - dnsmasq les gere lui-meme. Les reservations DHCP sont ecrites
dans server/stacks/dnsmasq/admin-config/reservations.conf, que dnsmasq
relit A CHAUD sur SIGHUP (--dhcp-hostsfile, voir dnsmasq.conf) - pas
besoin de redemarrer le conteneur ni de couper le DHCP/DNS pour le reste
de la flotte a chaque modification.

Pas d'enregistrements DNS statiques independants du DHCP ici : tous les
services du serveur d'expo vivent sous *.web.expolab.lan, un unique
wildcard dans dnsmasq.conf (le tri par service se fait ensuite cote
Caddy via le Host: HTTP, pas via DNS) - une liste d'enregistrements
individuels par nom aurait ete purement redondante.

Onglet "Topologie" : vue en direct des machines (baux + reservations +
presence ARP), poussee au navigateur par SSE (/api/topology/stream).
"""
import collections
import datetime
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

LEASES_PATH = Path("/dnsmasq-data/dnsmasq.leases")
RESERVATIONS_PATH = Path("/dnsmasq-admin-config/reservations.conf")

DNSMASQ_CONTAINER = "expolab-dnsmasq"
EXPO_LAN_INTERFACE = "expo-lan"  # interface= de dnsmasq.conf, requise par dhcp_release
EXPO_LAN = ipaddress.ip_network("10.42.0.0/24")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

# Duree de bail ecrite sur CHAQUE reservation : longue, une machine reservee garde son
# adresse meme si le serveur s'arrete plusieurs heures. La plage dynamique de
# dnsmasq.conf, elle, a un bail tres court : une carte de remplacement qui a recu une
# IP de repli (adresse reservee encore tenue par la carte morte) la redemande au bout
# d'une minute, dnsmasq la refuse et la carte repart sur son adresse reservee - au
# lieu d'attendre la moitie d'un bail de 12 h (constate : 6 h sur la mauvaise IP).
RESERVED_LEASE = "12h"
LEASE_TIME_RE = re.compile(r"^(\d+[smhdw]?|infinite)$")

# Liberation automatique des baux orphelins (voir reconcile_once).
AUTO_RELEASE = os.environ.get("DNSMASQ_AUTO_RELEASE", "1") != "0"
RECONCILE_INTERVAL = int(os.environ.get("DNSMASQ_RECONCILE_INTERVAL", "10"))
STALE_AFTER_CHECKS = 3  # verifications consecutives sans reponse avant liberation...
STALE_AFTER_SECONDS = int(os.environ.get("DNSMASQ_STALE_AFTER", "60"))  # ...ET au moins cette duree de silence
_events: collections.deque = collections.deque(maxlen=50)
_reconcile_now = threading.Event()   # reveille la reconciliation des qu'un bail change


def reload_dnsmasq() -> tuple[bool, str]:
    result = subprocess.run(
        ["docker", "kill", "--signal=HUP", DNSMASQ_CONTAINER],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def valid_ip_in_expo_lan(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in EXPO_LAN
    except ValueError:
        return False


def load_leases() -> list[dict]:
    if not LEASES_PATH.exists():
        return []
    leases = []
    for line in LEASES_PATH.read_text().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        expiry_epoch, mac, ip, hostname = parts[0], parts[1], parts[2], parts[3]
        client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else ""
        try:
            expiry = int(expiry_epoch)
        except ValueError:
            continue
        remaining = expiry - int(time.time())
        leases.append(
            {
                "hostname": hostname if hostname != "*" else "(inconnu)",
                "ip": ip,
                "mac": mac,
                "client_id": client_id,
                "expires_at": expiry,   # epoch : change a CHAQUE renouvellement (empreinte du bail)
                "expires_in_min": max(0, remaining // 60),
            }
        )
    leases.sort(key=lambda l: l["ip"])
    return leases


def parse_reservation_line(line: str) -> dict | None:
    """Une ligne de dhcp-hostsfile : `mac,ip[,hostname]` (reservation par
    MAC) ou `hostname,ip` (par NOM, sans MAC - dnsmasq l'applique a toute
    machine qui s'annonce sous ce nom, ce qui permet de remplacer une carte
    sans toucher a la reservation). Une duree de bail finale (`,12h`) est
    ignoree ici : elle est reecrite par format_reservation_line."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) > 2 and LEASE_TIME_RE.match(parts[-1]):   # jamais un nom : un nom commence par une lettre
        parts = parts[:-1]
    if len(parts) < 2:
        return None
    if MAC_RE.match(parts[0].lower()):
        return {"mac": parts[0], "ip": parts[1], "hostname": parts[2] if len(parts) > 2 else ""}
    return {"mac": "", "ip": parts[1], "hostname": parts[0]}


def format_reservation_line(r: dict) -> str:
    if not r["mac"]:
        line = f"{r['hostname']},{r['ip']}"
    else:
        line = f"{r['mac']},{r['ip']},{r['hostname']}" if r["hostname"] else f"{r['mac']},{r['ip']}"
    return f"{line},{RESERVED_LEASE}"


def migrate_reservations() -> bool:
    """Reservations ecrites avant l'ajout de la duree de bail : sans elle, dnsmasq leur
    applique le bail COURT de la plage dynamique. Reecrit le fichier une fois (et fait
    relire dnsmasq). True si quelque chose a change."""
    if not RESERVATIONS_PATH.exists():
        return False
    lines = [ln.strip() for ln in RESERVATIONS_PATH.read_text().splitlines()]
    expected = [format_reservation_line(r) for r in load_reservations()]
    if sorted(ln for ln in lines if ln and not ln.startswith("#")) == sorted(expected):
        return False
    save_reservations(load_reservations())
    return True


def reservation_id(r: dict) -> str:
    """Identifiant stable d'une reservation : sa MAC, ou son nom si elle n'en a pas."""
    return r["mac"] or r["hostname"]


def find_reservation(reservations: list[dict], ident: str) -> dict | None:
    ident = ident.lower()
    return next((r for r in reservations if reservation_id(r).lower() == ident), None)


def load_reservations() -> list[dict]:
    if not RESERVATIONS_PATH.exists():
        return []
    reservations = [r for r in map(parse_reservation_line, RESERVATIONS_PATH.read_text().splitlines()) if r]
    reservations.sort(key=lambda r: r["ip"])
    return reservations


def save_reservations(reservations: list[dict]) -> None:
    RESERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [format_reservation_line(r) for r in reservations]
    content = "\n".join(lines) + ("\n" if lines else "")
    # Ecriture atomique (fichier temporaire + rename) : evite toute
    # fenetre ou une lecture/un rechargement dnsmasq concurrent verrait
    # un fichier partiellement ecrit ou vide, et garantit qu'un crash en
    # cours d'ecriture ne laisse jamais un fichier tronque a la place des
    # reservations existantes.
    tmp_path = RESERVATIONS_PATH.with_suffix(".tmp")
    tmp_path.write_text(content)
    os.replace(tmp_path, RESERVATIONS_PATH)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/leases")
def api_leases():
    return jsonify({"leases": load_leases()})


@app.route("/api/reservations", methods=["GET"])
def api_reservations_list():
    reservations = load_reservations()
    leases = load_leases()
    leases_by_mac = {l["mac"]: l for l in leases}
    leases_by_name = {l["hostname"]: l for l in leases}
    for r in reservations:
        # dnsmasq applique la reservation immediatement pour toute
        # NOUVELLE negociation DHCP, mais un appareil qui a deja un bail
        # actif garde son IP jusqu'a son prochain renouvellement naturel
        # (ou un redemarrage) - signale ce cas plutot que de le
        # redemarrer nous-memes (voir discussion).
        lease = leases_by_mac.get(r["mac"]) if r["mac"] else leases_by_name.get(r["hostname"])
        r["id"] = reservation_id(r)
        r["pending_renewal"] = lease is not None and lease["ip"] != r["ip"]
    return jsonify({"reservations": reservations})


@app.route("/api/reservations", methods=["POST"])
def api_reservations_create():
    body = request.get_json(force=True, silent=True) or {}
    mac = (body.get("mac") or "").strip().lower()
    ip = (body.get("ip") or "").strip()
    hostname = (body.get("hostname") or "").strip().lower()

    if mac and not MAC_RE.match(mac):
        return jsonify({"error": "Adresse MAC invalide (format aa:bb:cc:dd:ee:ff)"}), 400
    if not mac and not hostname:
        return jsonify({"error": "Une adresse MAC ou un hostname est requis (sans MAC, la reservation suit le NOM de la machine)"}), 400
    if not valid_ip_in_expo_lan(ip):
        return jsonify({"error": "IP invalide (doit etre dans 10.42.0.0/24)"}), 400
    if hostname and not HOSTNAME_RE.match(hostname):
        return jsonify({"error": "Hostname invalide (minuscules/chiffres/tirets)"}), 400

    reservations = load_reservations()
    if mac and any(r["mac"] == mac for r in reservations):
        return jsonify({"error": f"Une reservation existe deja pour {mac}"}), 409
    if hostname and any(r["hostname"] == hostname for r in reservations):
        return jsonify({"error": f"Le hostname {hostname} est deja reserve"}), 409
    if any(r["ip"] == ip for r in reservations):
        return jsonify({"error": f"L'IP {ip} est deja reservee"}), 409

    reservations.append({"mac": mac, "ip": ip, "hostname": hostname})
    save_reservations(reservations)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation ajoutee mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True}), 201


@app.route("/api/reservations/<ident>", methods=["PUT"])
def api_reservations_update(ident: str):
    body = request.get_json(force=True, silent=True) or {}
    ip = (body.get("ip") or "").strip()
    hostname = (body.get("hostname") or "").strip().lower()

    if not valid_ip_in_expo_lan(ip):
        return jsonify({"error": "IP invalide (doit etre dans 10.42.0.0/24)"}), 400
    if hostname and not HOSTNAME_RE.match(hostname):
        return jsonify({"error": "Hostname invalide (minuscules/chiffres/tirets)"}), 400

    reservations = load_reservations()
    target = find_reservation(reservations, ident)
    if target is None:
        return jsonify({"error": "introuvable"}), 404
    if any(r["ip"] == ip and r is not target for r in reservations):
        return jsonify({"error": f"L'IP {ip} est deja reservee par une autre entree"}), 409

    if target["mac"]:
        if hostname and any(r["hostname"] == hostname and r is not target for r in reservations):
            return jsonify({"error": f"Le hostname {hostname} est deja reserve"}), 409
        target["hostname"] = hostname
    # Une reservation par nom ne change jamais de nom (c'est sa cle) :
    # seule son IP est modifiable, supprimer/recreer pour la renommer.
    target["ip"] = ip
    save_reservations(reservations)

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation modifiee mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True})


@app.route("/api/reservations/<ident>", methods=["DELETE"])
def api_reservations_delete(ident: str):
    reservations = load_reservations()
    target = find_reservation(reservations, ident)
    if target is None:
        return jsonify({"error": "introuvable"}), 404
    save_reservations([r for r in reservations if r is not target])

    ok, err = reload_dnsmasq()
    if not ok:
        return jsonify({"error": f"Reservation retiree mais rechargement dnsmasq echoue : {err}"}), 500
    return jsonify({"ok": True})


def release_lease(lease: dict) -> tuple[bool, str]:
    """Libere un bail aupres du dnsmasq en cours (aucun redemarrage, les
    autres machines ne sont pas touchees). dhcp_release est dans l'image
    dnsmasq (paquet dnsmasq-utils)."""
    cmd = ["docker", "exec", DNSMASQ_CONTAINER, "dhcp_release", EXPO_LAN_INTERFACE, lease["ip"], lease["mac"]]
    if lease["client_id"]:
        # Un bail cree avec un identifiant client ne se libere que si on le
        # redonne (constate : sans lui, le bail reste en place).
        cmd.append(lease["client_id"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return False, result.stderr.strip() or "dhcp_release a echoue"
    return True, ""


@app.route("/api/leases/<mac>/release", methods=["POST"])
def api_leases_release(mac: str):
    """Libere a la main le bail d'une machine (bouton "Liberer"). Normalement
    inutile : reconcile_once le fait seul pour une carte remplacee."""
    mac = mac.lower()
    if not MAC_RE.match(mac):
        return jsonify({"error": "Adresse MAC invalide"}), 400
    lease = next((l for l in load_leases() if l["mac"].lower() == mac), None)
    if lease is None:
        return jsonify({"error": "Aucun bail actif pour cette MAC"}), 404
    ok, err = release_lease(lease)
    if not ok:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True, "ip": lease["ip"]})


def host_alive(ip: str) -> bool | None:
    """True/False selon qu'une machine repond en ARP sur expo-lan, None si on
    ne peut pas le savoir (arping absent...) - dans le doute on ne libere rien.
    ARP est repondu par le noyau meme derriere un pare-feu : une carte allumee
    repond toujours."""
    try:
        result = subprocess.run(
            ["docker", "exec", DNSMASQ_CONTAINER, "arping", "-I", EXPO_LAN_INTERFACE, "-c", "2", "-w", "2", ip],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode in (125, 126, 127) or "not found" in result.stderr:
        return None
    return False


def log_event(kind: str, message: str) -> None:
    now = datetime.datetime.now().isoformat(timespec="seconds")
    _events.appendleft({"time": now, "kind": kind, "message": message})
    print(f"[{now}] {kind}: {message}", flush=True)
    notify_change()


def lease_signature(lease: dict) -> tuple:
    """Empreinte d'un bail : sa MAC et son echeance, qui change a chaque renouvellement ou
    nouvelle attribution. Deux empreintes differentes = la machine a donne signe de vie
    entre-temps."""
    return (lease["mac"], lease.get("expires_at"))


def still_the_same_lease(lease: dict) -> bool:
    """Relit les baux : celui-ci existe-t-il encore, inchange ? La decision de liberer repose
    sur un ARP qui dure plusieurs secondes ; une machine qui redemarre peut obtenir son bail
    PENDANT ce temps, et liberer l'ancien instantane effacerait le bail tout neuf (la machine
    resterait en ligne mais disparaitrait du DNS, de la topologie et des baux jusqu'a son
    prochain renouvellement, jusqu'a la moitie de la duree du bail)."""
    current = next((l for l in load_leases() if l["ip"] == lease["ip"]), None)
    return current is not None and lease_signature(current) == lease_signature(lease)


def seen_recently(mac: str, now: float) -> bool:
    """La sonde de presence (son propre ARP, toutes les PRESENCE_INTERVAL s) a-t-elle vu cette
    machine repondre dans les STALE_AFTER_SECONDS dernieres secondes ? En Wi-Fi (economie
    d'energie), une machine vivante rate un ARP sur deux : les verifications de la liberation
    peuvent tomber trois fois de suite sur un silence pendant que la presence la voit repondre
    entre-temps (cas vecu : bail libere, machine en ligne mais absente des baux et du DNS)."""
    with _change:
        last_seen = (_presence.get(mac) or {}).get("last_seen")
    return last_seen is not None and now - last_seen < STALE_AFTER_SECONDS


def reconcile_once(misses: dict, now: float | None = None) -> None:
    """Remplacement de carte sans intervention : pour chaque reservation PAR
    NOM (sans MAC), l'IP appartient au nom, pas a une carte. Deux situations
    bloquent sinon la carte neuve (meme nom, autre MAC) :

    - le bail de la morte tient encore l'IP : il est libere des qu'il ne
      repond plus en ARP pendant STALE_AFTER_CHECKS verifications de suite ET
      au moins STALE_AFTER_SECONDS secondes.
      Sans danger si la machine n'etait qu'eteinte : a son retour elle
      retrouve la meme IP (la reservation la lui redonne). Quand la carte
      neuve demarre, l'IP est deja libre et elle la prend d'emblee.
    - la carte neuve a demarre AVANT et a recu une IP de repli : on libere le
      bail de la morte (si elle ne repond pas) ET celui de la neuve. A son
      prochain echange DHCP (renouvellement, redemarrage) dnsmasq - qui fait
      autorite - la renvoie sur l'IP reservee. Un renouvellement simple ne
      suffisait pas : dnsmasq prolongeait le bail de repli.

    Un detenteur qui repond en ARP n'est JAMAIS libere (deux machines vivantes
    pour un meme nom : conflit signale, a trancher par un humain)."""
    now = time.time() if now is None else now
    reservations = [r for r in load_reservations() if not r["mac"]]
    leases = load_leases()
    by_ip = {l["ip"]: l for l in leases}
    by_name = {l["hostname"]: l for l in leases}

    for r in reservations:
        ip, name = r["ip"], r["hostname"]
        holder = by_ip.get(ip)
        claimant = by_name.get(name)

        if claimant and claimant["ip"] != ip:
            # La machine qui s'annonce sous ce nom n'est pas sur son IP reservee.
            if holder and holder["mac"] != claimant["mac"]:
                if host_alive(ip) is not False or not still_the_same_lease(holder):
                    if ("conflit", ip) not in misses:
                        log_event("conflit", f"{name} : l'IP {ip} est tenue par {holder['mac']} qui repond encore ; rien n'est libere")
                        misses[("conflit", ip)] = 1
                    continue
                ok, err = release_lease(holder)
                log_event(
                    "liberation" if ok else "erreur",
                    f"bail de {holder['mac']} ({ip}) libere, la carte '{name}' le remplace" if ok else f"echec liberation {ip} : {err}",
                )
            ok, err = release_lease(claimant)
            log_event(
                "liberation" if ok else "erreur",
                f"bail de repli de {name} ({claimant['mac']}, {claimant['ip']}) libere : la carte retrouvera {ip} a son prochain echange DHCP"
                if ok
                else f"echec liberation {claimant['ip']} : {err}",
            )
            misses.pop(ip, None)
            misses.pop(("conflit", ip), None)
            continue

        misses.pop(("conflit", ip), None)
        if holder is None:
            misses.pop(ip, None)
            continue
        alive = host_alive(ip)
        if alive is None or alive or seen_recently(holder["mac"], now):
            misses.pop(ip, None)
            continue
        signature = lease_signature(holder)
        miss = misses.get(ip)
        if miss is None or miss.get("signature") != signature:
            # Premiere absence, OU le bail a change depuis la derniere (renouvele, repris par une
            # machine qui redemarre) : elle vient de donner signe de vie, le silence repart de zero.
            miss = misses[ip] = {"since": now, "count": 0, "signature": signature}
        miss["count"] += 1
        # Une DUREE de silence, pas un nombre de passages : la reconciliation est aussi
        # reveillee a chaque changement de bail (une fois par minute par appareil non
        # reserve), et le Wi-Fi rate parfois des requetes ARP. Liberer le bail d'une
        # machine vivante ferait disparaitre son nom du DNS jusqu'a son renouvellement.
        if miss["count"] >= STALE_AFTER_CHECKS and now - miss["since"] >= STALE_AFTER_SECONDS:
            if not still_the_same_lease(holder):
                misses.pop(ip, None)   # renouvele ou deja libere pendant le test ARP : rien a faire
                continue
            ok, err = release_lease(holder)
            log_event(
                "liberation" if ok else "erreur",
                f"{name} : {holder['mac']} ne repond plus, bail {ip} libere pour une carte de remplacement" if ok else f"echec liberation {ip} : {err}",
            )
            misses.pop(ip, None)


def reconcile_loop() -> None:
    misses: dict = {}
    while True:
        _reconcile_now.wait(RECONCILE_INTERVAL)   # toutes les RECONCILE_INTERVAL s, ou aussitot qu'un bail change
        _reconcile_now.clear()
        try:
            reconcile_once(misses)
        except Exception as exc:  # noqa: BLE001 - ne jamais tuer la boucle
            log_event("erreur", f"reconciliation : {exc}")


@app.route("/api/events")
def api_events():
    return jsonify({"events": list(_events), "auto_release": AUTO_RELEASE})


# --- topologie (onglet "Topologie") ------------------------------------------------------
#
# Une seule source de verite par information : les baux (fichier de dnsmasq), les
# reservations (notre fichier) et la presence ARP (sondee ici). Le navigateur recoit
# l'etat complet par SSE a chaque changement : arrivee/depart d'une machine, bail
# ajoute/modifie/retire, reservation modifiee, evenement de liberation.
#
# Temps reel sans toucher a dnsmasq : il reecrit son fichier de baux a chaque
# changement de bail, on en surveille la date de modification chaque seconde.

SERVER_IP = "10.42.0.1"
PRESENCE_INTERVAL = int(os.environ.get("DNSMASQ_PRESENCE_INTERVAL", "10"))
_change = threading.Condition()
_version = 0
_presence: dict = {}   # mac -> {"alive": bool, "since": epoch du dernier changement d'etat, "last_seen": epoch | None}
_presence_ok = True    # False si la sonde ARP est indisponible (conteneur dnsmasq arrete...)


def notify_change() -> None:
    global _version
    with _change:
        _version += 1
        _change.notify_all()


def probe_alive(ips: list[str]) -> set[str] | None:
    """IP qui repondent en ARP sur expo-lan, toutes sondees EN PARALLELE en une seule
    commande dans le conteneur dnsmasq (le seul sur le reseau de l'hote). None si la
    sonde est impossible (conteneur arrete, arping absent) : etat inconnu, pas "hors ligne"."""
    if not ips:
        return set()
    script = f'for ip in "$@"; do (arping -q -I {EXPO_LAN_INTERFACE} -c 1 -w 2 "$ip" && echo "$ip") & done; wait'
    try:
        result = subprocess.run(
            ["docker", "exec", DNSMASQ_CONTAINER, "sh", "-c", script, "sonde", *ips],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    alive = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not alive and result.stderr.strip():
        return None
    return alive


def label(lease: dict) -> str:
    return lease["hostname"] if lease["hostname"] != "(inconnu)" else lease["mac"]


def diff_leases(old: dict, new: dict) -> list[tuple[str, str]]:
    """Evenements entre deux etats des baux ({mac: bail})."""
    events = []
    for mac, lease in new.items():
        before = old.get(mac)
        if before is None:
            events.append(("bail", f"{label(lease)} obtient {lease['ip']}"))
        elif before["ip"] != lease["ip"]:
            events.append(("bail", f"{label(lease)} passe de {before['ip']} a {lease['ip']}"))
    for mac, lease in old.items():
        if mac not in new:
            events.append(("bail", f"{label(lease)} ({lease['ip']}) : bail libere ou expire"))
    return events


def update_presence(presence: dict, leases: list[dict], alive: set[str], now: float) -> list[tuple[str, str]]:
    """Met a jour la presence par MAC et renvoie les arrivees/departs. La premiere
    observation d'une machine ne produit pas d'evenement (le bail l'a deja annoncee,
    et un redemarrage de dnsmasq-admin ne doit pas tout re-signaler)."""
    events = []
    current = {l["mac"] for l in leases}
    for mac in list(presence):
        if mac not in current:
            del presence[mac]
    for lease in leases:
        is_alive = lease["ip"] in alive
        p = presence.get(lease["mac"])
        if p is None:
            presence[lease["mac"]] = {"alive": is_alive, "since": now, "last_seen": now if is_alive else None}
            continue
        if is_alive:
            p["last_seen"] = now
        if is_alive != p["alive"]:
            p["alive"], p["since"] = is_alive, now
            events.append(("arrivee", f"{label(lease)} ({lease['ip']}) repond")
                          if is_alive else ("depart", f"{label(lease)} ({lease['ip']}) ne repond plus"))
    return events


def build_topology(leases: list[dict], reservations: list[dict], presence: dict, presence_ok: bool) -> dict:
    """Vue fusionnee : une entree par bail, plus une par reservation sans bail."""
    by_mac = {r["mac"]: r for r in reservations if r["mac"]}
    by_name = {r["hostname"]: r for r in reservations if not r["mac"]}
    names = collections.Counter(l["hostname"] for l in leases if l["hostname"] != "(inconnu)")
    used = set()
    machines = []
    for l in leases:
        r, kind = (by_mac[l["mac"]], "mac") if l["mac"] in by_mac else (by_name.get(l["hostname"]), "nom")
        if r is None:
            kind = None
        else:
            used.add(id(r))
        p = presence.get(l["mac"]) if presence_ok else None
        alerts = []
        if r is None:
            alerts.append("sans reservation")
        elif r["ip"] != l["ip"]:
            alerts.append(f"IP de repli (reservee : {r['ip']})")
        if names[l["hostname"]] > 1:
            alerts.append("nom en double")
        machines.append({
            "id": l["mac"], "hostname": l["hostname"], "ip": l["ip"], "mac": l["mac"],
            "state": "unknown" if p is None else ("online" if p["alive"] else "offline"),
            "since": p["since"] if p else None, "last_seen": p["last_seen"] if p else None,
            "reserved": kind, "reservation_id": reservation_id(r) if r else None,
            "lease_expires_in_min": l["expires_in_min"], "alerts": alerts,
        })
    for r in reservations:
        if id(r) not in used:
            machines.append({
                "id": reservation_id(r), "hostname": r["hostname"] or "(sans nom)", "ip": r["ip"], "mac": r["mac"],
                "state": "absent", "since": None, "last_seen": None,
                "reserved": "mac" if r["mac"] else "nom", "reservation_id": reservation_id(r),
                "lease_expires_in_min": None, "alerts": [],
            })
    machines.sort(key=lambda m: ipaddress.ip_address(m["ip"]) if valid_ip_in_expo_lan(m["ip"]) else ipaddress.ip_address("255.255.255.255"))
    present = [m for m in machines if m["state"] != "absent"]
    return {
        "server": {"ip": SERVER_IP, "interface": EXPO_LAN_INTERFACE, "network": str(EXPO_LAN)},
        "machines": machines,
        "summary": {
            "online": sum(m["state"] == "online" for m in machines),
            "known": len(present),
            "absent": len(machines) - len(present),
            "alerts": sum(len(m["alerts"]) for m in machines),
        },
        "presence_ok": presence_ok,
        "presence_interval": PRESENCE_INTERVAL,
    }


def current_topology() -> dict:
    with _change:
        presence = {mac: dict(p) for mac, p in _presence.items()}
        presence_ok = _presence_ok
    data = build_topology(load_leases(), load_reservations(), presence, presence_ok)
    data["events"] = list(_events)[:30]
    data["now"] = time.time()
    return data


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def monitor_loop() -> None:
    """Surveille baux et reservations (chaque seconde) et sonde la presence
    (toutes les PRESENCE_INTERVAL s, et aussitot apres un changement de bail)."""
    global _presence_ok
    known = None
    stamps = None
    last_probe = 0.0
    while True:
        try:
            now = time.time()
            new_stamps = (_mtime(LEASES_PATH), _mtime(RESERVATIONS_PATH))
            if new_stamps != stamps:
                stamps = new_stamps
                current = {l["mac"]: l for l in load_leases()}
                if known is not None:
                    for kind, message in diff_leases(known, current):
                        log_event(kind, message)
                    if set(current) != set(known):
                        last_probe = 0.0   # machine arrivee ou partie : sonder tout de suite
                    if current != known:
                        _reconcile_now.set()   # carte de remplacement sur une IP de repli : traitee sans attendre
                known = current
                notify_change()
            if now - last_probe >= PRESENCE_INTERVAL:
                last_probe = now
                leases = list((known or {}).values())
                alive = probe_alive([l["ip"] for l in leases])
                with _change:
                    was_ok, _presence_ok = _presence_ok, alive is not None
                    events = update_presence(_presence, leases, alive, now) if alive is not None else []
                if events or was_ok != _presence_ok:
                    for kind, message in events:
                        log_event(kind, message)
                    notify_change()
        except Exception as exc:  # noqa: BLE001 - ne jamais tuer la boucle
            log_event("erreur", f"topologie : {exc}")
        time.sleep(1)


@app.route("/api/topology")
def api_topology():
    return jsonify(current_topology())


@app.route("/api/topology/stream")
def api_topology_stream():
    """SSE : l'etat complet a la connexion puis a chaque changement ; un commentaire
    toutes les 15 s garde la connexion ouverte a travers Caddy."""
    def stream():
        seen = -1
        while True:
            with _change:
                if _version == seen:
                    _change.wait(timeout=15)
                version = _version
            if version != seen:
                seen = version
                yield f"data: {json.dumps(current_topology())}\n\n"
            else:
                yield ": ping\n\n"

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    if migrate_reservations():
        ok, err = reload_dnsmasq()
        log_event("reservation" if ok else "erreur",
                  f"duree de bail {RESERVED_LEASE} ajoutee aux reservations" if ok else f"rechargement dnsmasq echoue : {err}")
    if AUTO_RELEASE:
        threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5051, threaded=True)
