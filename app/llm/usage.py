"""Per-job usage accounting, turned into the result's `metadata` block.

The required keys (model_calls, tokens_in, tokens_out, cost_usd) count real calls only, including
retries, as the contract asks. Two extra keys make cached dev runs useful for cost estimates:
  cache_hits         responses served from the cache
  cost_usd_uncached  what this document would have cost with an empty cache
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _ModelUsage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    cost_usd_uncached: float = 0.0


class UsageTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, _ModelUsage] = {}

    def record(self, *, model: str, tokens_in: int, tokens_out: int, cost_usd: float, cached: bool) -> None:
        with self._lock:
            u = self._models.setdefault(model, _ModelUsage())
            u.cost_usd_uncached += cost_usd
            if cached:
                u.cache_hits += 1
                return
            u.calls += 1
            u.tokens_in += tokens_in
            u.tokens_out += tokens_out
            u.cost_usd += cost_usd

    def to_metadata(self, latency_ms: int) -> dict:
        with self._lock:
            models = dict(self._models)
        return {
            "model_calls": sum(u.calls for u in models.values()),
            "tokens_in": sum(u.tokens_in for u in models.values()),
            "tokens_out": sum(u.tokens_out for u in models.values()),
            "cost_usd": round(sum(u.cost_usd for u in models.values()), 6),
            "latency_ms": int(latency_ms),
            "cache_hits": sum(u.cache_hits for u in models.values()),
            "cost_usd_uncached": round(sum(u.cost_usd_uncached for u in models.values()), 6),
            "models": {
                m: {"calls": u.calls, "tokens_in": u.tokens_in, "tokens_out": u.tokens_out,
                    "cost_usd": round(u.cost_usd, 6), "cache_hits": u.cache_hits}
                for m, u in models.items()
            },
        }
