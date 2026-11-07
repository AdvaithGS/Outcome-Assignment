"""The single chokepoint for model traffic.

    chat(request)
      ├─ allowed-model check
      ├─ cache lookup ──hit──► return (no spend, no rate limit)
      ├─ single-flight: identical request already in flight? wait for it instead of paying twice
      ├─ budget guard
      ├─ HTTP POST {LLM_BASE_URL}/chat/completions   (one attempt; the retry layer wraps this later)
      ├─ ledger line + usage tracking (every real attempt, including errors)
      └─ cache store (successful, usable responses only)
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from app.llm.cache import CacheEntry, CacheMissInReplay, CacheMode, ResponseCache
from app.llm.keys import request_key, request_preview
from app.llm.ledger import BudgetGuard, SpendLedger
from app.llm.models import ensure_allowed
from app.llm.pricing import cost_from_usage, tokens_of
from app.llm.usage import UsageTracker


class CallError(RuntimeError):
    """Base class for failed model calls."""


class LLMHTTPError(CallError):
    """Non-2xx status, or a network failure (status_code 0)."""

    def __init__(self, status_code: int, body: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status_code in (0, 408, 409, 425, 429) or self.status_code >= 500


class LLMBadResponse(CallError):
    """2xx status but a body we cannot use (no choices, or an embedded error object)."""

    retryable = True


@dataclass(frozen=True)
class LLMResponse:
    key: str
    model: str
    raw: dict
    usage: dict
    cost_usd: float      # for a cache hit: the cost of the original call (not spent now)
    cost_source: str
    latency_ms: int      # for a cache hit: the latency of the original call
    cached: bool

    @property
    def _message(self) -> dict:
        try:
            return self.raw["choices"][0].get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            return {}

    @property
    def content(self) -> str | None:
        c = self._message.get("content")
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return c

    @property
    def finish_reason(self) -> str | None:
        try:
            return self.raw["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return None

    @property
    def served_model(self) -> str | None:
        return self.raw.get("model")


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"


def _float_env(env: Mapping[str, str], name: str) -> float | None:
    v = env.get(name)
    return float(v) if v not in (None, "") else None


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class LLMClient:
    def __init__(self, *, base_url: str | None, api_key: str | None, cache: ResponseCache | None = None,
                 cache_mode: CacheMode = CacheMode.OFF, ledger: SpendLedger | None = None,
                 guard: BudgetGuard | None = None, run_id: str | None = None,
                 http: httpx.AsyncClient | None = None, timeout_s: float = 90.0,
                 extra_body: dict | None = None, max_attempts: int = 6, base_backoff_s: float = 1.0,
                 max_backoff_s: float = 60.0, max_concurrency: int = 8):
        if cache_mode is not CacheMode.OFF and cache is None:
            raise ValueError(f"cache mode {cache_mode.value} needs a ResponseCache")
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key
        self.cache = cache
        self.cache_mode = cache_mode
        self.ledger = ledger
        self.guard = guard
        self.run_id = run_id or _default_run_id()
        self.extra_body = extra_body or {}   # sent on the wire, never part of the cache key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=15.0))
        self._owns_http = http is None
        self.timeout_s = timeout_s
        self._inflight: dict[str, asyncio.Future] = {}
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self._limiter = asyncio.Semaphore(max_concurrency)   # concurrent HTTP calls per process

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, http: httpx.AsyncClient | None = None) -> "LLMClient":
        """Configuration (all optional except the two LLM_* endpoint vars for real calls):

        LLM_BASE_URL, LLM_API_KEY  gateway and key (contract-mandated)
        LLM_CACHE_MODE             off | read_write (default) | replay | refresh
        LLM_CACHE_PATH             default .cache/llm_cache.sqlite
        LLM_LEDGER_PATH            default ledger/spend.jsonl; empty string disables the ledger
        LLM_BUDGET_USD             hard cap on total ledger spend; unset = no cap
        LLM_RUN_BUDGET_USD         cap for this run (LLM_RUN_ID); unset = no cap
        LLM_RUN_ID                 tag for ledger lines; default = timestamp + pid
        LLM_REQUEST_USAGE          1 = ask the gateway for usage.cost in responses (OpenRouter)
        """
        env = os.environ if env is None else env
        mode = CacheMode(env.get("LLM_CACHE_MODE", CacheMode.READ_WRITE.value))
        cache = ResponseCache(env.get("LLM_CACHE_PATH", ".cache/llm_cache.sqlite")) if mode is not CacheMode.OFF else None
        ledger_path = env.get("LLM_LEDGER_PATH", "ledger/spend.jsonl")
        ledger = SpendLedger(ledger_path) if ledger_path else None
        run_id = env.get("LLM_RUN_ID") or _default_run_id()
        guard = (BudgetGuard(ledger, total_cap_usd=_float_env(env, "LLM_BUDGET_USD"),
                             run_cap_usd=_float_env(env, "LLM_RUN_BUDGET_USD"), run_id=run_id)
                 if ledger else None)
        extra = {"usage": {"include": True}} if env.get("LLM_REQUEST_USAGE", "0") == "1" else None
        return cls(base_url=env.get("LLM_BASE_URL"), api_key=env.get("LLM_API_KEY"), cache=cache,
                   cache_mode=mode, ledger=ledger, guard=guard, run_id=run_id, http=http, extra_body=extra,
                   max_attempts=int(env.get("LLM_MAX_ATTEMPTS", "6")), timeout_s=float(env.get("LLM_TIMEOUT_S", "90")),
                   max_concurrency=int(env.get("LLM_MAX_CONCURRENCY", "24")))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
        if self.cache is not None:
            self.cache.close()

    # ---------------------------------------------------------------- public

    async def chat(self, *, model: str, messages: list[dict], stage: str, doc_id: str | None = None,
                   job_id: str | None = None, namespace: str = "", sample_index: int = 0,
                   usage: UsageTracker | None = None, timeout_s: float | None = None,
                   deadline: float | None = None, **params: Any) -> LLMResponse:
        """One chat-completions call. `params` are passed through (temperature, max_tokens,
        response_format, tools, ...) and are part of the cache key."""
        ensure_allowed(model)
        body = {"model": model, "messages": messages, **params}
        key = request_key(body, namespace=namespace, sample_index=sample_index)

        if self.cache_mode.reads:
            entry = await asyncio.to_thread(self.cache.get, key)
            if entry is not None:
                resp = LLMResponse(key=key, model=model, raw=entry.response,
                                   usage=entry.response.get("usage") or {}, cost_usd=entry.cost_usd,
                                   cost_source="cache", latency_ms=entry.latency_ms, cached=True)
                self._track_hit(usage, resp)
                return resp
            if self.cache_mode is CacheMode.REPLAY:
                raise CacheMissInReplay(f"no cached response (stage={stage}, model={model}, key={key[:12]})")

        pending = self._inflight.get(key)
        if pending is not None:
            resp = dataclasses.replace(await asyncio.shield(pending), cached=True)
            self._track_hit(usage, resp)
            return resp

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            resp = await self._call_with_retry(body, key=key, model=model, stage=stage, doc_id=doc_id, job_id=job_id,
                                               namespace=namespace, sample_index=sample_index, usage=usage,
                                               timeout_s=timeout_s or self.timeout_s, deadline=deadline)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so an unawaited future does not log a warning
            raise
        else:
            fut.set_result(resp)
            return resp
        finally:
            self._inflight.pop(key, None)

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _track_hit(usage: UsageTracker | None, resp: LLMResponse) -> None:
        if usage is not None:
            tin, tout = tokens_of(resp.usage)
            usage.record(model=resp.model, tokens_in=tin, tokens_out=tout, cost_usd=resp.cost_usd, cached=True)

    async def _call_with_retry(self, body: dict, *, timeout_s: float, deadline: float | None, **kw) -> LLMResponse:
        """Retry transient failures (429, 5xx, timeouts, network errors, unusable 200 bodies) with exponential
        backoff and full jitter, honouring Retry-After. Every attempt is logged and counted by `_call`.
        With a deadline (time.monotonic() value), no attempt runs past it and no retry starts that cannot finish."""
        attempt = 0
        while True:
            t = timeout_s
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 5:
                    raise CallError("no time left before the job deadline")
                t = min(t, remaining)
            try:
                return await self._call(body, timeout_s=t, **kw)
            except (LLMHTTPError, LLMBadResponse) as exc:
                attempt += 1
                if not exc.retryable or attempt >= self.max_attempts:
                    raise
                delay = min(self.max_backoff_s, self.base_backoff_s * (2 ** (attempt - 1)))
                delay = random.uniform(0, delay)
                ra = getattr(exc, "retry_after", None)
                if ra is not None:
                    delay = max(delay, min(ra, self.max_backoff_s))
                if deadline is not None and time.monotonic() + delay + 5 > deadline:
                    raise
                await asyncio.sleep(delay)

    async def _call(self, body: dict, *, key: str, model: str, stage: str, doc_id: str | None,
                    job_id: str | None, namespace: str, sample_index: int,
                    usage: UsageTracker | None, timeout_s: float = 90.0) -> LLMResponse:
        if self.guard is not None:
            await asyncio.to_thread(self.guard.check)
        if not self.base_url or not self._api_key:
            raise CallError("LLM_BASE_URL and LLM_API_KEY must be set for real model calls")

        wire = {**body, **self.extra_body}
        t0 = time.perf_counter()
        try:
            async with self._limiter:
                t0 = time.perf_counter()
                # wait_for caps the TOTAL time of the attempt; httpx's read timeout only bounds gaps between bytes
                r = await asyncio.wait_for(
                    self._http.post(f"{self.base_url}/chat/completions", json=wire,
                                    headers={"Authorization": f"Bearer {self._api_key}"},
                                    timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))),
                    timeout=timeout_s)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            self._log_attempt(key=key, model=model, stage=stage, doc_id=doc_id, job_id=job_id,
                              sample_index=sample_index, status=0, data=None, cost=0.0, source="none",
                              latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}")
            if usage is not None:
                usage.record(model=model, tokens_in=0, tokens_out=0, cost_usd=0.0, cached=False)
            raise LLMHTTPError(0, f"{type(exc).__name__}: {exc}") from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            data = r.json()
        except ValueError:
            data = None
        u = data.get("usage") if isinstance(data, dict) else None
        cost, source = cost_from_usage(model, u)
        usable = (200 <= r.status_code < 300 and isinstance(data, dict) and not data.get("error")
                  and isinstance(data.get("choices"), list) and len(data["choices"]) > 0)
        error = None if usable else (r.text[:300] or f"HTTP {r.status_code}")

        self._log_attempt(key=key, model=model, stage=stage, doc_id=doc_id, job_id=job_id,
                          sample_index=sample_index, status=r.status_code, data=data, cost=cost,
                          source=source, latency_ms=latency_ms, error=error)
        if usage is not None:
            tin, tout = tokens_of(u)
            usage.record(model=model, tokens_in=tin, tokens_out=tout, cost_usd=cost, cached=False)

        if not 200 <= r.status_code < 300:
            raise LLMHTTPError(r.status_code, r.text[:2000], _retry_after(r.headers.get("retry-after")))
        if not usable:
            raise LLMBadResponse(f"unusable response body: {r.text[:500]}")

        resp = LLMResponse(key=key, model=model, raw=data, usage=u or {}, cost_usd=cost,
                           cost_source=source, latency_ms=latency_ms, cached=False)
        if self.cache_mode.writes:
            entry = CacheEntry(key=key, model=model, response=data, cost_usd=cost,
                               latency_ms=latency_ms, created_at=time.time())
            await asyncio.to_thread(self.cache.put, entry, namespace=namespace, stage=stage,
                                    preview=request_preview(body))
        return resp

    def _log_attempt(self, *, key: str, model: str, stage: str, doc_id: str | None, job_id: str | None,
                     sample_index: int, status: int, data: dict | None, cost: float, source: str,
                     latency_ms: int, error: str | None) -> None:
        if self.ledger is None:
            return
        tin, tout = tokens_of(data.get("usage") if isinstance(data, dict) else None)
        self.ledger.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id, "job_id": job_id, "doc_id": doc_id, "stage": stage,
            "model": model, "served_model": data.get("model") if isinstance(data, dict) else None,
            "status": status, "ok": error is None, "tokens_in": tin, "tokens_out": tout,
            "cost_usd": cost, "cost_source": source, "latency_ms": latency_ms,
            "key": key[:16], "sample_index": sample_index, "error": error,
        })
