"""L1: the LLM response cache (SQLite, WAL mode so several workers can share one file).

Only successful responses are stored. 429s, 5xx, timeouts and malformed bodies never are.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CacheMode(str, Enum):
    OFF = "off"                # no lookup, no store
    READ_WRITE = "read_write"  # normal: return hits, store misses
    REPLAY = "replay"          # lookup only; a miss raises CacheMissInReplay (guaranteed $0)
    REFRESH = "refresh"        # skip lookup, overwrite on store

    @property
    def reads(self) -> bool:
        return self in (CacheMode.READ_WRITE, CacheMode.REPLAY)

    @property
    def writes(self) -> bool:
        return self in (CacheMode.READ_WRITE, CacheMode.REFRESH)


class CacheMissInReplay(RuntimeError):
    """Raised in replay mode when a request has no cached response."""


@dataclass(frozen=True)
class CacheEntry:
    key: str
    model: str
    response: dict
    cost_usd: float      # cost of the original (real) call
    latency_ms: int      # latency of the original call
    created_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_responses (
    key         TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    namespace   TEXT NOT NULL,
    stage       TEXT,
    created_at  REAL NOT NULL,
    response    TEXT NOT NULL,
    cost_usd    REAL NOT NULL,
    latency_ms  INTEGER NOT NULL,
    preview     TEXT,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_stage ON llm_responses(stage);
CREATE INDEX IF NOT EXISTS idx_llm_model ON llm_responses(model);
"""


class ResponseCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, model, response, cost_usd, latency_ms, created_at FROM llm_responses WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE llm_responses SET hits = hits + 1, last_hit_at = ? WHERE key = ?", (time.time(), key)
            )
        return CacheEntry(key=row[0], model=row[1], response=json.loads(row[2]),
                          cost_usd=row[3], latency_ms=row[4], created_at=row[5])

    def put(self, entry: CacheEntry, *, namespace: str = "", stage: str | None = None, preview: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO llm_responses "
                "(key, model, namespace, stage, created_at, response, cost_usd, latency_ms, preview, hits) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (entry.key, entry.model, namespace, stage, entry.created_at,
                 json.dumps(entry.response, ensure_ascii=False), entry.cost_usd, entry.latency_ms, preview),
            )

    def evict(self, *, key: str | None = None, stage: str | None = None, model: str | None = None,
              older_than_s: float | None = None, everything: bool = False) -> int:
        clauses, args = [], []
        if key is not None:
            clauses.append("key LIKE ?")
            args.append(key + "%")
        if stage is not None:
            clauses.append("stage = ?")
            args.append(stage)
        if model is not None:
            clauses.append("model = ?")
            args.append(model)
        if older_than_s is not None:
            clauses.append("created_at < ?")
            args.append(time.time() - older_than_s)
        if not clauses and not everything:
            raise ValueError("evict needs a filter (or everything=True)")
        where = " AND ".join(clauses) if clauses else "1=1"
        with self._lock:
            return self._conn.execute(f"DELETE FROM llm_responses WHERE {where}", args).rowcount

    def stats(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(stage, '-'), model, COUNT(*), SUM(hits), SUM(cost_usd), "
                "SUM(cost_usd * hits), SUM(LENGTH(response)) "
                "FROM llm_responses GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
        return [{"stage": r[0], "model": r[1], "entries": r[2], "hits": r[3] or 0,
                 "cost_usd": r[4] or 0.0, "saved_usd": r[5] or 0.0, "bytes": r[6] or 0} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
