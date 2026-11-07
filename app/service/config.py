"""Service settings, all from the environment (docker compose sets them)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _git_sha() -> str:
    if os.environ.get("APP_VERSION"):
        return os.environ["APP_VERSION"]
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=2).stdout.strip() or "dev"
    except Exception:  # noqa: BLE001
        return "dev"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_bytes: int = 50 * 1024 * 1024
    lease_s: float = 120.0             # a RUNNING job whose lease expires is re-claimed by another worker
    heartbeat_s: float = 20.0
    job_deadline_s: float = 20 * 60.0  # from submission (queue time included); the harness allows 30 min
    max_attempts: int = 3              # claims per job before it is failed (protects against poison jobs)
    worker_concurrency: int = 4        # jobs processed at once per worker process
    queue_limit: int = 500             # above this many PENDING jobs, POST /jobs returns 429
    poll_s: float = 0.5
    pipeline: str = "null"
    version: str = "dev"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        s = cls(
            data_dir=Path(e.get("DATA_DIR", "data")),
            max_upload_bytes=int(e.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024,
            lease_s=float(e.get("JOB_LEASE_S", "120")),
            heartbeat_s=float(e.get("JOB_HEARTBEAT_S", "20")),
            job_deadline_s=float(e.get("JOB_DEADLINE_S", str(20 * 60))),
            max_attempts=int(e.get("JOB_MAX_ATTEMPTS", "3")),
            worker_concurrency=int(e.get("WORKER_CONCURRENCY", "4")),
            queue_limit=int(e.get("QUEUE_LIMIT", "500")),
            poll_s=float(e.get("WORKER_POLL_S", "0.5")),
            pipeline=e.get("PIPELINE", "v1"),     # the dataclass default stays "null" so tests never call a model
            version=_git_sha(),
        )
        for d in (s.data_dir, s.uploads_dir, s.results_dir):
            d.mkdir(parents=True, exist_ok=True)
        return s
