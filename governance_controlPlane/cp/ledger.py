"""Append-only, hash-chained audit ledger on SQLite.

Each event's hash is SHA-256 over the previous hash and the event's fields, so changing
any stored event breaks every hash after it. Triggers reject UPDATE and DELETE; the demo's
tamper() drops them first to imitate someone with raw access to the database file.
In production: write-once object storage (S3 Object Lock) plus a stream to the SIEM.
"""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("ts", "kind", "bu", "agent", "actor", "action", "decision", "rule", "detail", "corr")
GENESIS = "0" * 64

GUARDS = """
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'audit ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'audit ledger is append-only'); END;
"""


def _hash(prev: str, seq: int, e: dict) -> str:
    body = json.dumps([seq, *(e.get(f) for f in FIELDS)], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{prev}|{body}".encode()).hexdigest()


class Ledger:
    def __init__(self, path: str, fresh: bool = True):
        if fresh:
            Path(path).unlink(missing_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, {", ".join(f"{f} TEXT" for f in FIELDS)}, prev TEXT, hash TEXT);
            CREATE TABLE IF NOT EXISTS tamper_log (seq INTEGER PRIMARY KEY, original TEXT);
            {GUARDS}""")
        self.db.commit()

    def append(self, **e) -> dict:
        e.setdefault("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        e.setdefault("corr", "")
        with self.lock:
            row = self.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (row["seq"] + 1, row["hash"]) if row else (1, GENESIS)
            h = _hash(prev, seq, e)
            self.db.execute(f"INSERT INTO events (seq, {', '.join(FIELDS)}, prev, hash) VALUES ({', '.join('?' * (len(FIELDS) + 3))})",
                            (seq, *(e.get(f) for f in FIELDS), prev, h))
            self.db.commit()
        return {"seq": seq, **{f: e.get(f) for f in FIELDS}, "prev": prev, "hash": h}

    def events(self, bu: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events", ()
        if bu:
            q, args = q + " WHERE bu = ?", (bu,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY seq", args)]

    def verify(self) -> dict:
        prev = GENESIS
        rows = self.events()
        for r in rows:
            if r["prev"] != prev or _hash(prev, r["seq"], r) != r["hash"]:
                return {"ok": False, "seq": r["seq"], "n": len(rows)}
            prev = r["hash"]
        return {"ok": True, "n": len(rows), "head": prev}

    def tamper(self, seq: int, decision: str) -> None:
        """Demo only: rewrite one event's decision without fixing its hash."""
        with self.lock:
            orig = self.db.execute("SELECT decision FROM events WHERE seq = ?", (seq,)).fetchone()["decision"]
            self.db.executescript("DROP TRIGGER events_no_update; DROP TRIGGER events_no_delete;")
            self.db.execute("INSERT OR IGNORE INTO tamper_log VALUES (?, ?)", (seq, orig))
            self.db.execute("UPDATE events SET decision = ? WHERE seq = ?", (decision, seq))
            self.db.executescript(GUARDS)
            self.db.commit()

    def untamper(self) -> None:
        with self.lock:
            self.db.executescript("DROP TRIGGER events_no_update; DROP TRIGGER events_no_delete;")
            for r in self.db.execute("SELECT seq, original FROM tamper_log").fetchall():
                self.db.execute("UPDATE events SET decision = ? WHERE seq = ?", (r["original"], r["seq"]))
            self.db.execute("DELETE FROM tamper_log")
            self.db.executescript(GUARDS)
            self.db.commit()

    def tampered(self) -> list[int]:
        return [r["seq"] for r in self.db.execute("SELECT seq FROM tamper_log")]
