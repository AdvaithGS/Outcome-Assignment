"""L3: the spend ledger and the budget guard.

The ledger is an append-only JSONL file with one line per real HTTP attempt that reached the
provider (including errors, which can still be billed). Cache hits are not spend and are not
recorded here. It is kept apart from the cache on purpose: the cache is disposable, the ledger is
the record of the budget.

Appends are single `write()` calls of one short line in append mode, which is atomic on Linux, so
several processes can share the file. Totals are refreshed incrementally from the last offset read.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Iterator


class SpendLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()
        self._offset = 0
        self._total = 0.0
        self._by_run: dict[str, float] = defaultdict(float)

    def append(self, record: dict) -> None:
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

    def _refresh(self) -> None:
        # Caller holds the lock. Consumes complete lines only; a half-written last line waits.
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        for raw in chunk[: end + 1].splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cost = float(rec.get("cost_usd") or 0.0)
            self._total += cost
            self._by_run[rec.get("run_id") or ""] += cost
        self._offset += end + 1

    def total_usd(self) -> float:
        with self._lock:
            self._refresh()
            return self._total

    def run_total_usd(self, run_id: str) -> float:
        with self._lock:
            self._refresh()
            return self._by_run.get(run_id, 0.0)

    def records(self) -> Iterator[dict]:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    """Refuses new real calls once spend reaches a cap.

    The check happens before each call, so concurrent in-flight calls can overshoot a cap by at
    most (calls in flight × cost of one call). That bound is fine for a dev budget guard.
    No cap configured means no limit. That is the production default, since the scoring run uses
    the graders' key.
    """

    def __init__(self, ledger: SpendLedger, *, total_cap_usd: float | None = None,
                 run_cap_usd: float | None = None, run_id: str | None = None):
        self.ledger = ledger
        self.total_cap_usd = total_cap_usd
        self.run_cap_usd = run_cap_usd
        self.run_id = run_id

    def check(self) -> None:
        if self.total_cap_usd is not None:
            spent = self.ledger.total_usd()
            if spent >= self.total_cap_usd:
                raise BudgetExceeded(f"total spend ${spent:.4f} reached cap ${self.total_cap_usd:.2f}")
        if self.run_cap_usd is not None and self.run_id is not None:
            spent = self.ledger.run_total_usd(self.run_id)
            if spent >= self.run_cap_usd:
                raise BudgetExceeded(f"run {self.run_id} spend ${spent:.4f} reached cap ${self.run_cap_usd:.2f}")
