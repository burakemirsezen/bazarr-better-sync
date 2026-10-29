"""SQLite-backed catalog and durable queue; every accepted job is committed."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


class State:
    def __init__(self, data_dir):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.sqlite3"
        with self.connect() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS catalog(id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, report TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL);
            """)
            db.execute("UPDATE jobs SET status='interrupted', updated=? WHERE status='running'", (time.time(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_catalog(self, entries):
        # Materialize before transaction so failed discovery preserves the previous catalog.
        entries = list(entries)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT id,data FROM catalog").fetchall():
                data = json.loads(row["data"])
                data["present"] = False
                db.execute("UPDATE catalog SET data=? WHERE id=?", (json.dumps(data), row["id"]))
            for entry in entries:
                db.execute("INSERT INTO catalog VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (entry["id"], json.dumps(entry)))
        return len(entries)

    def catalog(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM catalog ORDER BY id")]

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["report"] = json.loads(result["report"])
        result.pop("fingerprint", None)
        return result

    def enqueue_many(self, payloads):
        results = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for payload in payloads:
                fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                existing = db.execute("SELECT * FROM jobs WHERE fingerprint=?", (fingerprint,)).fetchone()
                if existing:
                    results.append(self.decode(existing))
                    continue
                now, ident = time.time(), uuid.uuid4().hex
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                           (ident, fingerprint, "queued", json.dumps(payload), "{}", now, now))
                results.append(self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()))
        return results

    def jobs(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 500")]

    def job(self, ident):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row["id"]))
            return self.decode(row)

    def finish(self, ident, status, report):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,report=?,updated=? WHERE id=?",
                       (status, json.dumps(report), time.time(), ident))
