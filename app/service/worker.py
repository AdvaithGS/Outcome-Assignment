"""Workers: claim jobs from the store, run the pipeline, write the result, finish the job.

    python -m app.service.worker          # one process, WORKER_CONCURRENCY jobs at a time

Scale with compute: raise WORKER_CONCURRENCY, or run more worker processes/containers on the same data volume
(`docker compose up --scale worker=3`). The store guarantees each job is claimed by exactly one of them.

Outcome policy:
  pipeline returns           → SUCCESS (or PARTIAL if it reports a partial result)
  DocumentError              → FAILED with the reason (the document cannot be read)
  internal deadline reached  → PARTIAL with whatever is available (all-null fields at worst)
  any other error            → retried by re-claiming (up to JOB_MAX_ATTEMPTS), then PARTIAL with null fields
  lost lease                 → abandon silently: another worker owns the job now
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from app.pipeline.base import DocumentError, JobContext, PipelineOutput, build_result, get_pipeline
from app.service.config import Settings
from app.service.logs import setup_logging
from app.service.store import JobStore, iso

log = logging.getLogger("worker")
MIN_RUN_S = 30.0          # even a job that queued too long gets this long to produce something


class LostLease(Exception):
    pass


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Worker:
    def __init__(self, settings: Settings, store: JobStore, worker_id: str | None = None):
        self.s = settings
        self.store = store
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.pipeline = get_pipeline(settings.pipeline)

    async def run(self, stop: asyncio.Event) -> None:
        # the default pool (cores + 4 threads) serves the queue, heartbeats and per-document reads; size it to the job
        # slots so it never runs out (CPU-heavy per-page work has its own bounded pool: app/ingest/cpu.py)
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=max(32, 3 * self.s.worker_concurrency + 8), thread_name_prefix="io"))
        log.info("worker started", extra={"worker": self.id, "concurrency": self.s.worker_concurrency,
                                          "pipeline": self.pipeline.name})
        await asyncio.gather(*(self._slot(stop, i) for i in range(self.s.worker_concurrency)))
        log.info("worker stopped", extra={"worker": self.id})

    async def _slot(self, stop: asyncio.Event, n: int) -> None:
        while not stop.is_set():
            try:
                job = await asyncio.to_thread(self.store.claim, self.id, self.s.lease_s, self.s.max_attempts)
            except Exception:  # noqa: BLE001 - a store hiccup must not kill the slot
                log.exception("claim failed", extra={"worker": self.id})
                job = None
            if job is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.s.poll_s)
                except asyncio.TimeoutError:
                    pass
                continue
            await self.process(job)

    async def process(self, job: dict) -> None:
        jid = job["id"]
        extra = {"job_id": jid, "worker": self.id, "attempt": job["attempts"], "file": job["file_name"],
                 "file_type": job["file_type"]}
        queued_s = time.time() - job["created_at"]
        log.info("job started", extra={**extra, "queued_s": round(queued_s, 2)})
        schema = json.loads(job["schema_json"])
        time_left = max(MIN_RUN_S, self.s.job_deadline_s - queued_s)

        def set_stage(stage: str) -> None:
            self.store.set_stage(jid, self.id, stage)
            log.info("stage", extra={**extra, "stage": stage})

        ctx = JobContext(job_id=jid, org=job["org"], file_name=job["file_name"], file_type=job["file_type"],
                         deadline=time.monotonic() + time_left, set_stage=set_stage)
        lost = asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(jid, lost))
        run = asyncio.create_task(self.pipeline.run(Path(job["file_path"]), schema, ctx))
        status, out, err = "SUCCESS", None, None
        lost_wait = asyncio.create_task(lost.wait())
        try:
            done, _ = await asyncio.wait({run, lost_wait}, timeout=time_left, return_when=asyncio.FIRST_COMPLETED)
            if lost.is_set():
                run.cancel()
                log.warning("lease lost; abandoning job (another worker owns it)", extra=extra)
                return
            if run not in done:
                run.cancel()
                status, out = "PARTIAL", PipelineOutput(rows=[{}], partial=True, notes=["internal deadline reached"])
                log.warning("deadline reached; finishing with partial result", extra={**extra, "time_left_s": time_left})
            else:
                out = run.result()
                status = "PARTIAL" if out.partial else "SUCCESS"
        except DocumentError as exc:
            status, err = "FAILED", ("unreadable_document", str(exc))
            log.warning("document unreadable", extra={**extra, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            if job["attempts"] < self.s.max_attempts:
                log.exception("pipeline error; releasing job for retry", extra=extra)
                await asyncio.to_thread(self.store.release, jid, self.id, f"{type(exc).__name__}: {exc}")
                return
            log.exception("pipeline error on final attempt; finishing with null fields", extra=extra)
            status, out = "PARTIAL", PipelineOutput(rows=[{}], partial=True, notes=[f"{type(exc).__name__}: {exc}"])
        finally:
            hb.cancel()
            lost_wait.cancel()

        latency_ms = int((time.time() - job["created_at"]) * 1000)
        result_path = None
        if status != "FAILED":
            result = build_result(schema, out, ctx.usage, latency_ms)
            result_path = str(self.s.results_dir / f"{jid}.json")
            await asyncio.to_thread(_atomic_write_json, Path(result_path), result)
        ok = await asyncio.to_thread(self.store.finish, jid, self.id, status, result_path=result_path,
                                     error_code=err[0] if err else None, error_message=err[1] if err else None)
        if not ok:
            log.warning("finish rejected (lease lost or already terminal)", extra=extra)
            return
        meta = ctx.usage.to_metadata(latency_ms)
        log.info("job finished", extra={**extra, "status": status, "latency_ms": latency_ms,
                                        "model_calls": meta["model_calls"], "cost_usd": meta["cost_usd"],
                                        "notes": out.notes if out else None})
        if job.get("webhook_url"):
            asyncio.create_task(self._webhook(job["webhook_url"], jid, job["org"]))

    async def _heartbeat(self, jid: str, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.s.heartbeat_s)
            ok = await asyncio.to_thread(self.store.heartbeat, jid, self.id, self.s.lease_s)
            if not ok:
                lost.set()
                return

    async def _webhook(self, url: str, jid: str, org: str) -> None:
        from app.service.api import job_status   # local import: api imports worker for embedded mode
        job = await asyncio.to_thread(self.store.get, jid, org)
        body = job_status(job)
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(url, json=body)
                if r.status_code < 500:
                    log.info("webhook delivered", extra={"job_id": jid, "status_code": r.status_code})
                    return
            except httpx.HTTPError as exc:
                log.warning("webhook attempt failed", extra={"job_id": jid, "error": str(exc)})
            await asyncio.sleep(2 ** attempt)
        log.error("webhook gave up", extra={"job_id": jid})


def main() -> int:
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    store = JobStore(settings.db_path)
    stop = asyncio.Event()

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await Worker(settings, store).run(stop)

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
