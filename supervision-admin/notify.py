"""Canaux d'alerte de la supervision (Telegram, Discord, Slack, Mailjet ou toute URL Apprise) et silence de nuit.

Une seule liste de canaux, reglee dans la page. Chaque canal choisit ses niveaux (alerte, avertissement) et peut etre
coupe sans etre supprime ; son dernier envoi (reussi ou non) est garde pour la page. Les secrets (jetons, webhooks,
cles Mailjet) restent dans data/supervision.db : ils ne sont jamais renvoyes au navigateur.

Nuit (19:00 -> 08:00 par defaut) : silence complet. A la fin de la nuit, si l'exposition est ouverte, un recapitulatif
part sur tous les canaux actifs : problemes apparus pendant la nuit et problemes encore en cours.
Les changements d'un meme passage partent en UN message par canal (une coupure reseau ne fait pas 20 messages).
"""
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import checks as C

MAILJET_SMTP = "in-v3.mailjet.com"
RECAP_WINDOW = timedelta(hours=1)        # recapitulatif seulement dans l'heure qui suit la fin de la nuit
DEFAULT_NIGHT = {"enabled": True, "start": "19:00", "end": "08:00"}
ICONS = {C.ALERT: "🔴", C.WARNING: "🟠", "resolved": "✅"}

# {type: (libelle, [(champ, libelle, secret)])}
KINDS = {
    "telegram": ("Telegram", [("token", "Jeton du bot", True), ("chat_id", "Identifiant de la conversation", False)]),
    "discord": ("Discord", [("webhook", "URL du webhook", True)]),
    "slack": ("Slack", [("webhook", "URL du webhook entrant", True)]),
    "mailjet": ("Mailjet (e-mail)", [("api_key", "Clé API", True), ("secret_key", "Clé secrète", True),
                                     ("sender", "Expéditeur (adresse validée chez Mailjet)", False),
                                     ("recipients", "Destinataires (séparés par des virgules)", False)]),
    "apprise": ("Autre (URL Apprise)", [("url", "URL Apprise", True)]),
}
EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ChannelError(ValueError):
    pass


def build_url(kind: str, f: dict) -> str:
    """URL Apprise du canal, a partir des champs saisis (verifies)."""
    if kind == "telegram":
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", f["token"]):
            raise ChannelError("Jeton du bot Telegram invalide (forme 123456:ABC...).")
        if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", f["chat_id"]):
            raise ChannelError("Identifiant de conversation Telegram invalide (nombre, ou @canal).")
        return f"tgram://{f['token']}/{f['chat_id']}"
    if kind in ("discord", "slack"):
        prefixes = {"discord": ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/"),
                    "slack": ("https://hooks.slack.com/services/",)}[kind]
        if not f["webhook"].startswith(prefixes) or re.search(r"\s", f["webhook"]):
            raise ChannelError(f"URL de webhook {KINDS[kind][0]} invalide (elle commence par {prefixes[0]}).")
        return f["webhook"]
    if kind == "mailjet":
        recipients = [r.strip() for r in f["recipients"].split(",") if r.strip()]
        if not EMAIL.match(f["sender"]) or not recipients or not all(EMAIL.match(r) for r in recipients):
            raise ChannelError("Adresse d'expéditeur ou de destinataire invalide.")
        query = urlencode({"smtp": MAILJET_SMTP, "port": 587, "mode": "starttls", "user": f["api_key"],
                           "pass": f["secret_key"], "from": f["sender"], "to": ",".join(recipients)})
        return f"mailtos://{quote(f['sender'].split('@')[1])}?{query}"
    if kind == "apprise":
        if not apprise_accepts(f["url"]):
            raise ChannelError("URL Apprise non reconnue.")
        return f["url"]
    raise ChannelError("Type de canal inconnu.")


def apprise_accepts(url: str) -> bool:
    import apprise
    return apprise.Apprise().add(url)


def apprise_send(url: str, title: str, body: str) -> tuple[bool, str]:
    """Envoi par Apprise ; (reussi, erreur lisible)."""
    import apprise
    with apprise.LogCapture(level=apprise.logging.WARNING) as log:
        sender = apprise.Apprise()
        if not sender.add(url):
            return False, "URL du canal refusée par Apprise"
        ok = bool(sender.notify(title=title, body=body))
        errors = [line.split(" - ", 2)[-1] for line in log.getvalue().splitlines() if line.strip()]
    return ok, "" if ok else (errors[-1] if errors else "envoi refusé")[:300]


# --- messages ---------------------------------------------------------------------------------------

def event_line(e) -> str:
    icon = ICONS["resolved"] if e.kind == "resolved" else ICONS[e.level]
    text = f"{e.label} : rétabli" if e.kind == "resolved" else f"{e.label} : {e.detail}" if e.detail else e.label
    return f"{icon} {e.machine} · {text}"


def message(events) -> tuple[str, str]:
    if len(events) == 1:
        e = events[0]
        title = (f"{ICONS['resolved']} Rétabli · {e.machine}" if e.kind == "resolved"
                 else f"{ICONS[e.level]} {e.level.capitalize()} · {e.machine}")
        return title, event_line(e)
    return f"Supervision expolab : {len(events)} changements", "\n".join(event_line(e) for e in events)


def recap(night_incidents, still_open) -> tuple[str, str]:
    lines = []
    if night_incidents:
        resolved = sum(1 for i in night_incidents if i["closed_at"] is not None)
        lines.append(f"Cette nuit : {len(night_incidents)} problème(s) apparu(s), {resolved} rétabli(s).")
    else:
        lines.append("Cette nuit : rien à signaler.")
    if still_open:
        lines.append("En cours :")
        lines += [f"{ICONS.get(i['level'], '')} {i['machine']} · {i['label']} : {i['detail']}".rstrip(" :") for i in still_open]
    else:
        lines.append("Aucun problème en cours.")
    return "Supervision expolab : récapitulatif de la nuit", "\n".join(lines)


# --- canaux, nuit, envois ---------------------------------------------------------------------------

class Notifier:
    def __init__(self, history, send=apprise_send, clock=time.time, background=True):
        self.history, self.send, self.clock = history, send, clock
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1) if background else None    # un seul fil : l'ordre est garde

    # canaux

    def _channels(self) -> list[dict]:
        return self.history.get("channels") or []

    def channels_view(self) -> list[dict]:
        out = []
        for ch in self._channels():
            fields = {name: (bool(ch["fields"].get(name)) if secret else ch["fields"].get(name, ""))
                      for name, _, secret in KINDS[ch["kind"]][1]}
            out.append({k: ch[k] for k in ("id", "kind", "name", "enabled", "levels", "last")} | {"fields": fields})
        return out

    def save_channel(self, data: dict) -> dict:
        """Cree (sans id) ou modifie un canal ; en modification, un champ laisse vide garde sa valeur (les secrets ne
        reviennent jamais au navigateur)."""
        with self._lock:
            channels = self._channels()
            current = next((c for c in channels if c["id"] == data.get("id")), None) if data.get("id") else None
            if data.get("id") and current is None:
                raise ChannelError("Canal inconnu.")
            kind = current["kind"] if current else str(data.get("kind", ""))
            if kind not in KINDS:
                raise ChannelError("Type de canal inconnu.")
            given = data.get("fields") if isinstance(data.get("fields"), dict) else {}
            fields = {}
            for name, label, secret in KINDS[kind][1]:
                value = str(given.get(name) or "").strip()
                if not value and current:
                    value = current["fields"].get(name, "")
                if not value:
                    raise ChannelError(f"{label} : obligatoire.")
                fields[name] = value
            levels = [lv for lv in (C.ALERT, C.WARNING) if lv in (data.get("levels") or [])]
            if not levels:
                raise ChannelError("Choisir au moins un niveau (alerte ou avertissement).")
            name = str(data.get("name") or "").strip()[:60] or KINDS[kind][0]
            channel = {"id": current["id"] if current else uuid.uuid4().hex[:12], "kind": kind, "name": name,
                       "fields": fields, "url": build_url(kind, fields), "levels": levels,
                       "enabled": bool(data.get("enabled", True)), "last": current["last"] if current else None}
            channels = [channel if c["id"] == channel["id"] else c for c in channels] if current else channels + [channel]
            self.history.put("channels", channels)
            return channel

    def delete_channel(self, channel_id: str):
        with self._lock:
            channels = self._channels()
            if not any(c["id"] == channel_id for c in channels):
                raise ChannelError("Canal inconnu.")
            self.history.put("channels", [c for c in channels if c["id"] != channel_id])

    def test_channel(self, channel_id: str) -> dict:
        channel = next((c for c in self._channels() if c["id"] == channel_id), None)
        if channel is None:
            raise ChannelError("Canal inconnu.")
        return self._deliver(channel, "Supervision expolab : test", "Message de test : ce canal recevra les alertes.")

    def _deliver(self, channel, title, body, incident_ids=()) -> dict:
        try:
            ok, error = self.send(channel["url"], title, body)
        except Exception as exc:                 # un canal en panne ne doit jamais arreter la supervision
            ok, error = False, f"{exc.__class__.__name__} : {exc}"[:300]
        last = {"at": self.clock(), "ok": ok, "error": error}
        with self._lock:
            channels = self._channels()
            for c in channels:
                if c["id"] == channel["id"]:
                    c["last"] = last
            self.history.put("channels", channels)
        for incident_id in incident_ids if ok else ():
            self.history.add_notified(incident_id, channel["name"])
        return last

    def _broadcast(self, select, build):
        """Pour chaque canal actif : picked = select(canal) ; rien a envoyer si vide. Les evenements envoyes
        notent le canal dans la colonne « Prévenus » de l'historique."""
        jobs = []
        for channel in self._channels():
            if channel["enabled"]:
                picked = select(channel)
                if picked:
                    ids = tuple(dict.fromkeys(e.incident_id for e in picked if hasattr(e, "incident_id")))
                    jobs.append((channel, *build(picked), ids))
        for job in jobs:
            if self._pool:
                self._pool.submit(self._deliver, *job)
            else:
                self._deliver(*job)

    # nuit

    def night(self) -> dict:
        return {**DEFAULT_NIGHT, **(self.history.get("night") or {})}

    def set_night(self, data: dict) -> dict:
        night = {**self.night(), **{k: data[k] for k in ("enabled", "start", "end") if k in data}}
        if not isinstance(night["enabled"], bool) or not all(HHMM.match(str(night[k])) for k in ("start", "end")):
            raise ChannelError("Heures de nuit au format HH:MM.")
        if night["start"] == night["end"]:
            raise ChannelError("Le début et la fin de la nuit doivent différer.")
        self.history.put("night", night)
        return night

    def is_night(self, when: datetime, night=None) -> bool:
        night = night or self.night()
        if not night["enabled"]:
            return False
        t, start, end = when.strftime("%H:%M"), night["start"], night["end"]
        return start <= t < end if start < end else (t >= start or t < end)

    # apres chaque passage

    def after_tick(self, events, expo_open: bool):
        now = datetime.fromtimestamp(self.clock())
        night = self.night()
        if events and self.is_night(now, night):
            for e in events:
                self.history.add_notified(e.incident_id, "silence de nuit")
        elif events:
            self._broadcast(lambda ch: [e for e in events if e.level in ch["levels"]], message)
        self._maybe_recap(now, night, expo_open)

    def _maybe_recap(self, now: datetime, night, expo_open):
        if not night["enabled"] or self.is_night(now, night):
            return
        hour, minute = map(int, night["end"].split(":"))
        end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not end <= now < end + RECAP_WINDOW or self.history.get("recap_day") == now.date().isoformat():
            return
        self.history.put("recap_day", now.date().isoformat())
        if not expo_open:
            return
        hour, minute = map(int, night["start"].split(":"))
        start = end.replace(hour=hour, minute=minute)
        if start >= end:
            start -= timedelta(days=1)
        night_incidents = [i for i in self.history.incidents(start.timestamp(), limit=1000)
                           if start.timestamp() <= i["opened_at"] < end.timestamp() and i["level"] != "info"]
        title, body = recap(night_incidents, self.history.open_incidents())
        self._broadcast(lambda ch: [True], lambda _: (title, body))
