"""A bounded pool for CPU-heavy per-page work (page rendering, local OCR).

Scanned documents fan out into one task per page (a 25-page lease is 25 Tesseract runs of ~4 s each). Run in the
event loop's default pool, those tasks filled every thread, so the worker's other blocking calls (claiming jobs,
heartbeats, reading spreadsheets) queued behind OCR: in the load test, table jobs took 103 s instead of 16 s. This pool
is sized to the CPUs actually available (affinity and the container's CPU quota) minus two, which are left for the API,
the event loop and everything else. OCR_WORKERS overrides the size.
"""
from __future__ import annotations

import asyncio
import functools
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def available_cpus() -> int:
    try:
        n = len(os.sched_getaffinity(0))
    except AttributeError:                      # not Linux
        n = os.cpu_count() or 2
    try:                                        # cgroup v2 quota, e.g. `docker run --cpus 2` → "200000 100000"
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            n = min(n, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return n


def cpu_slots(env: dict | None = None) -> int:
    e = os.environ if env is None else env
    if e.get("OCR_WORKERS"):
        return max(1, int(e["OCR_WORKERS"]))
    return max(1, available_cpus() - 2)


_pool: ThreadPoolExecutor | None = None


def pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=cpu_slots(), thread_name_prefix="cpu")
    return _pool


async def run_cpu(fn, *args, **kwargs):
    """Run a CPU-heavy call in the bounded pool (instead of asyncio.to_thread)."""
    return await asyncio.get_running_loop().run_in_executor(pool(), functools.partial(fn, *args, **kwargs))
