"""Job store and queue in one SQLite table (WAL mode; safe for several worker processes on one volume).

Guarantees:
- **Claim is atomic** (BEGIN IMMEDIATE): two workers never get the same job.
- **Leases**: a RUNNING job carries lease_owner + lease_expires, renewed by heartbeats. If the worker dies, the
  lease expires and another worker re-claims the job. A job claimed `max_attempts` times without finishing is
  failed (poison-job protection).
- **Fencing**: finish/heartbeat/stage updates only apply if the caller still owns the lease, so a slow or
  partitioned worker cannot overwrite a job someone else re-claimed.
- **Terminal states never change**: every update requires status = RUNNING (or PENDING for claims).
- **Idempotency**: (org, idempotency key) returns the existing job unless that job FAILED.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = ("SUCCESS", "PARTIAL", "FAILED")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    org           TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    status        TEXT NOT NULL,
    stage         TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    lease_owner   TEXT,
    lease_expires REAL,
    file_path     TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    file_type     TEXT NOT NULL,
    schema_json   TEXT NOT NULL,
    webhook_url   TEXT,
    result_path   TEXT,
    error_code    TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(org, idem_key);
"""


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ API side

    def create(self, *, org: str, idem_key: str, file_path: str, file_name: str, file_type: str,
               schema_json: str, webhook_url: str | None) -> tuple[dict, bool]:
        """-> (job, created). An existing non-failed job with the same (org, idem_key) is returned instead."""
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE org = ? AND idem_key = ? AND status != 'FAILED' "
                            "ORDER BY created_at DESC LIMIT 1", (org, idem_key)).fetchone()
            if row is not None:
                return dict(row), False
            job_id = uuid.uuid4().hex
            c.execute("INSERT INTO jobs (id, org, idem_key, status, stage, created_at, updated_at, file_path, file_name, "
                      "file_type, schema_json, webhook_url) VALUES (?, ?, ?, 'PENDING', NULL, ?, ?, ?, ?, ?, ?, ?)",
                      (job_id, org, idem_key, now, now, file_path, file_name, file_type, schema_json, webhook_url))
            return dict(c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()), True

    def get(self, job_id: str, org: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ? AND org = ?", (job_id, org)).fetchone()
        return dict(row) if row else None

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'PENDING'").fetchone()[0]

    # ------------------------------------------------------------------ worker side

    def claim(self, worker_id: str, lease_s: float, max_attempts: int) -> dict | None:
        now = time.time()
        with self._tx() as c:
            # jobs whose worker died too many times: fail them instead of retrying forever
            c.execute("UPDATE jobs SET status = 'FAILED', stage = NULL, error_code = 'worker_lost', "
                      "error_message = 'job was abandoned by its worker ' || attempts || ' times', "
                      "finished_at = ?, updated_at = ?, lease_owner = NULL "
                      "WHERE status = 'RUNNING' AND lease_expires < ? AND attempts >= ?",
                      (now, now, now, max_attempts))
            row = c.execute("SELECT id FROM jobs WHERE status = 'PENDING' OR (status = 'RUNNING' AND lease_expires < ?) "
                            "ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET status = 'RUNNING', stage = 'STARTING', lease_owner = ?, lease_expires = ?, "
                      "attempts = attempts + 1, started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                      (worker_id, now + lease_s, now, now, row["id"]))
            return dict(c.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())

    def _owned_update(self, job_id: str, worker_id: str, sets: str, args: tuple) -> bool:
        with self._tx() as c:
            cur = c.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ? AND status = 'RUNNING' "
                            f"AND lease_owner = ?", (*args, time.time(), job_id, worker_id))
            return cur.rowcount == 1

    def heartbeat(self, job_id: str, worker_id: str, lease_s: float) -> bool:
        return self._owned_update(job_id, worker_id, "lease_expires = ?", (time.time() + lease_s,))

    def set_stage(self, job_id: str, worker_id: str, stage: str) -> bool:
        return self._owned_update(job_id, worker_id, "stage = ?", (stage,))

    def finish(self, job_id: str, worker_id: str, status: str, *, result_path: str | None = None,
               error_code: str | None = None, error_message: str | None = None) -> bool:
        assert status in TERMINAL
        return self._owned_update(job_id, worker_id,
                                  "status = ?, stage = NULL, result_path = ?, error_code = ?, error_message = ?, "
                                  "finished_at = ?, lease_owner = NULL, lease_expires = NULL",
                                  (status, result_path, error_code, (error_message or "")[:2000] or None, time.time()))

    def release(self, job_id: str, worker_id: str, reason: str) -> bool:
        """Give a job back to the queue (transient infrastructure error); it will be claimed again."""
        return self._owned_update(job_id, worker_id, "status = 'PENDING', stage = NULL, lease_owner = NULL, "
                                                     "lease_expires = NULL, error_message = ?", (reason[:2000],))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
