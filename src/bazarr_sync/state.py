"""SQLite-backed catalog and durable queue; every accepted job is committed."""
import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
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
              CREATE TABLE IF NOT EXISTS attempts(
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, worker TEXT NOT NULL,
                lease_hash TEXT UNIQUE NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
                result_id TEXT, created REAL NOT NULL, updated REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id);
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

    # Remote GPU workers: at-least-once execution under renewable leases.
    # Only a hash of each lease token is stored.

    def claim_remote(self, worker, lease_seconds):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='awaiting_worker' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            now, attempt = time.time(), uuid.uuid4().hex
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?)",
                       (attempt, row["id"], worker, _hash(token), "active", now + lease_seconds, None, now, now))
            db.execute("UPDATE jobs SET status='worker_running',updated=? WHERE id=?", (now, row["id"]))
            return {"job": self.decode(row), "attempt_id": attempt, "lease_token": token}

    def attempt(self, job_id, token):
        """The attempt this lease token belongs to, whatever its state; None for unknown tokens."""
        if not isinstance(token, str) or not token:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE lease_hash=? AND job_id=?", (_hash(token), job_id)).fetchone()
            return dict(row) if row else None

    def heartbeat(self, attempt_id, lease_seconds):
        with self.connect() as db:
            now = time.time()
            changed = db.execute("UPDATE attempts SET expires=?,updated=? WHERE id=? AND status='active' AND expires>?",
                                 (now + lease_seconds, now, attempt_id, now)).rowcount
            return changed == 1

    def close_attempt(self, attempt_id, status, result_id=None):
        with self.connect() as db:
            db.execute("UPDATE attempts SET status=?,result_id=?,updated=? WHERE id=?",
                       (status, result_id, time.time(), attempt_id))

    def revoke_attempts(self, job_id, status="revoked"):
        with self.connect() as db:
            db.execute("UPDATE attempts SET status=?,updated=? WHERE job_id=? AND status='active'",
                       (status, time.time(), job_id))

    def expire_leases(self, max_attempts):
        """Expired leases leave an auditable attempt; the job returns to the queue until max_attempts."""
        expired = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            for row in db.execute("SELECT * FROM attempts WHERE status='active' AND expires<=?", (now,)).fetchall():
                db.execute("UPDATE attempts SET status='expired',updated=? WHERE id=?", (now, row["id"]))
                used = db.execute("SELECT COUNT(*) FROM attempts WHERE job_id=?", (row["job_id"],)).fetchone()[0]
                job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
                if job is None or job["status"] != "worker_running":
                    continue
                if used >= max_attempts:
                    report = dict(json.loads(job["report"]), reason=f"Worker lease expired {used} times; retry explicitly")
                    db.execute("UPDATE jobs SET status='failed',report=?,updated=? WHERE id=?",
                               (json.dumps(report), now, job["id"]))
                else:
                    db.execute("UPDATE jobs SET status='awaiting_worker',updated=? WHERE id=?", (now, job["id"]))
                expired.append(row["job_id"])
        return expired

    def attempts(self, job_id):
        with self.connect() as db:
            return [{k: r[k] for k in r.keys() if k != "lease_hash"}
                    for r in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY created", (job_id,))]


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
