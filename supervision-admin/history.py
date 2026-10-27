"""Etat persistant de la supervision : un fichier SQLite (data/supervision.db).

  - settings    : reglages (seuils, delais, conservation) et etat de l'exposition, en JSON ;
  - maintenance : machines eteintes volontairement, hors surveillance (note facultative) ;
  - incidents   : historique des problemes signales (ouverture, fin, raison de la fin), purge selon la conservation.
"""
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance (machine TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    machine TEXT NOT NULL,
    check_id TEXT NOT NULL,
    label TEXT NOT NULL,
    level TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    opened_at REAL NOT NULL,
    closed_at REAL,
    close_reason TEXT,
    notified TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents (closed_at);
CREATE INDEX IF NOT EXISTS incidents_opened ON incidents (opened_at);
"""
COLUMNS = ("id", "machine", "check_id", "label", "level", "detail", "opened_at", "closed_at", "close_reason", "notified")
INFO = "info"          # ligne du journal (exposition ouverte/fermee, maintenance) : close des sa creation


class History:
    def __init__(self, path):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _query(self, sql, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    # --- reglages et etat -----------------------------------------------------------------------

    def get(self, key, default=None):
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def put(self, key, value):
        self._query("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)))

    # --- maintenance ----------------------------------------------------------------------------

    def maintenance(self) -> dict:
        return {m: {"note": note, "since": since} for m, note, since in
                self._query("SELECT machine, note, since FROM maintenance ORDER BY machine")}

    def set_maintenance(self, machine, note, since):
        self._query("INSERT INTO maintenance (machine, note, since) VALUES (?, ?, ?) "
                    "ON CONFLICT(machine) DO UPDATE SET note = excluded.note", (machine, note, since))

    def end_maintenance(self, machine) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM maintenance WHERE machine = ?", (machine,)).rowcount > 0

    # --- incidents ------------------------------------------------------------------------------

    def open_incident(self, machine, check, now) -> int:
        with self._lock, self._db:
            return self._db.execute(
                "INSERT INTO incidents (machine, check_id, label, level, detail, opened_at) VALUES (?, ?, ?, ?, ?, ?)",
                (machine, check.id, check.label, check.level, check.detail, now)).lastrowid

    def journal(self, machine, kind, label, detail, now):
        self._query("INSERT INTO incidents (machine, check_id, label, level, detail, opened_at, closed_at, close_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '')", (machine, kind, label, INFO, detail, now, now))

    def add_notified(self, incident_id, text):
        """Note qui a ete prevenu (« Telegram », « silence de nuit »...), sans doublon."""
        with self._lock, self._db:
            row = self._db.execute("SELECT notified FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is not None and text not in row[0].split(", "):
                self._db.execute("UPDATE incidents SET notified = ? WHERE id = ?",
                                 (", ".join(filter(None, [row[0], text])), incident_id))

    def raise_level(self, incident_id, level, detail):
        self._query("UPDATE incidents SET level = ?, detail = ? WHERE id = ?", (level, detail, incident_id))

    def close_incident(self, incident_id, now, reason):
        self._query("UPDATE incidents SET closed_at = ?, close_reason = ? WHERE id = ? AND closed_at IS NULL",
                    (now, reason, incident_id))

    def open_incidents(self) -> list[dict]:
        return self._rows("WHERE closed_at IS NULL ORDER BY opened_at")

    def incidents(self, since, limit=500) -> list[dict]:
        """Incidents ouverts depuis `since` ou encore en cours, du plus recent au plus ancien."""
        return self._rows("WHERE opened_at >= ? OR closed_at IS NULL ORDER BY opened_at DESC LIMIT ?", (since, limit))

    def purge(self, before) -> int:
        """Supprime les incidents termines avant `before` ; un incident en cours n'est jamais supprime."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM incidents WHERE closed_at IS NOT NULL AND closed_at < ?", (before,)).rowcount

    def _rows(self, where, args=()):
        return [dict(zip(COLUMNS, row)) for row in self._query(f"SELECT {', '.join(COLUMNS)} FROM incidents {where}", args)]
