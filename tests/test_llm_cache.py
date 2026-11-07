"""LLM chokepoint: cache keys, cache modes, ledger, budget guard, single-flight, cost accounting.
All tests use a fake provider (httpx.MockTransport). No network, no spend."""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from app.llm import (BudgetExceeded, BudgetGuard, CacheMissInReplay, CacheMode, DisallowedModelError,
                     LLMBadResponse, LLMClient, LLMHTTPError, ResponseCache, SpendLedger, UsageTracker)
from app.llm.keys import request_key
from app.llm.pricing import load_prices

MODEL = "openai/gpt-6-luna"
MSGS = [{"role": "user", "content": "hello"}]


def ok_body(content="pong", cost: float | None = 0.0001, model=MODEL):
    usage = {"prompt_tokens": 10, "completion_tokens": 2}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen-1", "model": model, "usage": usage,
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


class FakeProvider:
    """Serves queued responses in order, then a default 200."""

    def __init__(self, queue=None, delay: float = 0.0):
        self.queue = list(queue or [])
        self.delay = delay
        self.calls: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(request.content))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.queue:
            status, body, headers = self.queue.pop(0)
            return httpx.Response(status, json=body, headers=headers or {})
        return httpx.Response(200, json=ok_body())


def make_client(tmp_path, provider: FakeProvider, *, mode=CacheMode.READ_WRITE, total_cap=None, run_cap=None,
                run_id="test-run", api_key="test-key", max_attempts=1):
    ledger = SpendLedger(tmp_path / "spend.jsonl")
    cache = ResponseCache(tmp_path / "cache.sqlite") if mode is not CacheMode.OFF else None
    return LLMClient(base_url="https://gateway.test/api/v1", api_key=api_key, cache=cache, cache_mode=mode,
                     ledger=ledger, guard=BudgetGuard(ledger, total_cap_usd=total_cap, run_cap_usd=run_cap, run_id=run_id),
                     run_id=run_id, http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
                     max_attempts=max_attempts, base_backoff_s=0.01, max_backoff_s=0.05)


def run(coro):
    return asyncio.run(coro)


def ledger_lines(tmp_path):
    return list(SpendLedger(tmp_path / "spend.jsonl").records())


# ------------------------------------------------------------------ keys

def test_key_ignores_dict_order_and_non_semantic_params():
    a = request_key({"model": MODEL, "messages": MSGS, "temperature": 0, "stream": False, "usage": {"include": True}})
    b = request_key({"temperature": 0, "messages": MSGS, "model": MODEL})
    assert a == b


@pytest.mark.parametrize("change", [
    {"model": "deepseek/deepseek-v3.2"},
    {"messages": [{"role": "user", "content": "hello!"}]},
    {"temperature": 0.7},
    {"response_format": {"type": "json_object"}},
])
def test_key_changes_with_anything_semantic(change):
    base = {"model": MODEL, "messages": MSGS, "temperature": 0}
    assert request_key(base) != request_key({**base, **change})


def test_key_changes_with_namespace_and_sample_index():
    body = {"model": MODEL, "messages": MSGS}
    keys = {request_key(body), request_key(body, namespace="org-a"), request_key(body, sample_index=1)}
    assert len(keys) == 3


def test_key_hashes_inline_images():
    def body(img):
        return {"model": MODEL, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "read this"},
            {"type": "image_url", "image_url": {"url": img}}]}]}
    img1 = "data:image/png;base64," + "A" * 5000
    img2 = "data:image/png;base64," + "B" * 5000
    assert request_key(body(img1)) == request_key(body(img1))
    assert request_key(body(img1)) != request_key(body(img2))


# ------------------------------------------------------------------ cache modes

def test_second_identical_call_is_a_free_cache_hit(tmp_path):
    p = FakeProvider()
    usage = UsageTracker()

    async def go():
        c = make_client(tmp_path, p)
        r1 = await c.chat(model=MODEL, messages=MSGS, stage="s", usage=usage, temperature=0)
        r2 = await c.chat(model=MODEL, messages=MSGS, stage="s", usage=usage, temperature=0)
        await c.aclose()
        return r1, r2

    r1, r2 = run(go())
    assert len(p.calls) == 1
    assert (r1.cached, r2.cached) == (False, True)
    assert r2.content == "pong" and r2.cost_usd == pytest.approx(0.0001)
    assert len(ledger_lines(tmp_path)) == 1
    meta = usage.to_metadata(latency_ms=5)
    assert meta["model_calls"] == 1 and meta["cache_hits"] == 1
    assert meta["cost_usd"] == pytest.approx(0.0001)
    assert meta["cost_usd_uncached"] == pytest.approx(0.0002)
    assert meta["models"][MODEL]["calls"] == 1


def test_cache_persists_across_clients(tmp_path):
    p = FakeProvider()

    async def go():
        for _ in range(2):
            c = make_client(tmp_path, p)
            await c.chat(model=MODEL, messages=MSGS, stage="s")
            await c.aclose()

    run(go())
    assert len(p.calls) == 1


def test_replay_miss_raises_without_calling_provider(tmp_path):
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p, mode=CacheMode.REPLAY)
        try:
            with pytest.raises(CacheMissInReplay):
                await c.chat(model=MODEL, messages=MSGS, stage="s")
        finally:
            await c.aclose()

    run(go())
    assert p.calls == [] and ledger_lines(tmp_path) == []


def test_replay_hit_needs_no_api_key(tmp_path):
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p)
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        c = make_client(tmp_path, p, mode=CacheMode.REPLAY, api_key=None)
        r = await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return r

    assert run(go()).cached is True
    assert len(p.calls) == 1


def test_refresh_calls_again_and_overwrites(tmp_path):
    p = FakeProvider(queue=[(200, ok_body("old"), None), (200, ok_body("new"), None)])

    async def go():
        c = make_client(tmp_path, p)
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        c = make_client(tmp_path, p, mode=CacheMode.REFRESH)
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        c = make_client(tmp_path, p, mode=CacheMode.REPLAY)
        r = await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return r

    assert run(go()).content == "new"
    assert len(p.calls) == 2


def test_off_mode_never_stores(tmp_path):
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p, mode=CacheMode.OFF)
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()

    run(go())
    assert len(p.calls) == 2


# ------------------------------------------------------------------ failures are never cached

def test_http_errors_are_not_cached_and_are_logged(tmp_path):
    p = FakeProvider(queue=[(429, {"error": {"message": "rate limited"}}, {"Retry-After": "3"})])

    async def go():
        c = make_client(tmp_path, p)
        with pytest.raises(LLMHTTPError) as ei:
            await c.chat(model=MODEL, messages=MSGS, stage="s")
        r = await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return ei.value, r

    err, r = run(go())
    assert err.status_code == 429 and err.retry_after == 3.0 and err.retryable
    assert r.cached is False and len(p.calls) == 2
    lines = ledger_lines(tmp_path)
    assert [l["ok"] for l in lines] == [False, True] and lines[0]["status"] == 429


def test_200_with_error_body_is_bad_response_and_not_cached(tmp_path):
    p = FakeProvider(queue=[(200, {"error": {"message": "upstream died"}}, None)])

    async def go():
        c = make_client(tmp_path, p)
        with pytest.raises(LLMBadResponse):
            await c.chat(model=MODEL, messages=MSGS, stage="s")
        r = await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return r

    assert run(go()).cached is False
    assert len(p.calls) == 2


# ------------------------------------------------------------------ guards

def test_budget_guard_blocks_before_any_call(tmp_path):
    SpendLedger(tmp_path / "spend.jsonl").append({"run_id": "old", "cost_usd": 9.5, "ok": True})
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p, total_cap=9.0)
        with pytest.raises(BudgetExceeded):
            await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()

    run(go())
    assert p.calls == []


def test_budget_guard_does_not_block_cache_hits(tmp_path):
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p)
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        SpendLedger(tmp_path / "spend.jsonl").append({"run_id": "old", "cost_usd": 100.0, "ok": True})
        c = make_client(tmp_path, p, total_cap=9.0)
        r = await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return r

    assert run(go()).cached is True


def test_run_cap_only_counts_this_run(tmp_path):
    SpendLedger(tmp_path / "spend.jsonl").append({"run_id": "other", "cost_usd": 5.0, "ok": True})
    SpendLedger(tmp_path / "spend.jsonl").append({"run_id": "mine", "cost_usd": 0.6, "ok": True})
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p, run_cap=0.5, run_id="mine")
        with pytest.raises(BudgetExceeded):
            await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        c = make_client(tmp_path, p, run_cap=0.5, run_id="fresh")
        await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()

    run(go())
    assert len(p.calls) == 1


def test_disallowed_model_is_refused_before_anything(tmp_path):
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p)
        with pytest.raises(DisallowedModelError):
            await c.chat(model="openai/gpt-4o", messages=MSGS, stage="s")
        await c.aclose()

    run(go())
    assert p.calls == []


# ------------------------------------------------------------------ single-flight

def test_concurrent_identical_requests_pay_once(tmp_path):
    p = FakeProvider(delay=0.05)
    usage = UsageTracker()

    async def go():
        c = make_client(tmp_path, p)
        rs = await asyncio.gather(*[c.chat(model=MODEL, messages=MSGS, stage="s", usage=usage) for _ in range(5)])
        await c.aclose()
        return rs

    rs = run(go())
    assert len(p.calls) == 1
    assert sorted(r.cached for r in rs) == [False, True, True, True, True]
    meta = usage.to_metadata(0)
    assert meta["model_calls"] == 1 and meta["cache_hits"] == 4


def test_single_flight_propagates_leader_failure(tmp_path):
    p = FakeProvider(queue=[(500, {"error": "boom"}, None)], delay=0.05)

    async def go():
        c = make_client(tmp_path, p)
        rs = await asyncio.gather(*[c.chat(model=MODEL, messages=MSGS, stage="s") for _ in range(3)],
                                  return_exceptions=True)
        await c.aclose()
        return rs

    rs = run(go())
    assert all(isinstance(r, LLMHTTPError) for r in rs)
    assert len(p.calls) == 1


# ------------------------------------------------------------------ cost

def test_cost_falls_back_to_price_table_when_gateway_omits_it(tmp_path):
    p = FakeProvider(queue=[(200, ok_body(cost=None), None)])

    async def go():
        c = make_client(tmp_path, p)
        r = await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return r

    r = run(go())
    price = load_prices()[MODEL]
    assert r.cost_source == "price_table"
    assert r.cost_usd == pytest.approx(10 * price.prompt + 2 * price.completion)


def test_price_table_covers_every_allowed_model():
    from app.llm.models import ALLOWED_MODELS
    assert ALLOWED_MODELS <= load_prices().keys()


# ------------------------------------------------------------------ inspection tools

def test_cache_stats_and_evict(tmp_path):
    p = FakeProvider()

    async def go():
        c = make_client(tmp_path, p)
        await c.chat(model=MODEL, messages=MSGS, stage="a")
        await c.chat(model=MODEL, messages=[{"role": "user", "content": "x"}], stage="b")
        await c.chat(model=MODEL, messages=MSGS, stage="a")  # hit
        await c.aclose()

    run(go())
    cache = ResponseCache(tmp_path / "cache.sqlite")
    stats = {r["stage"]: r for r in cache.stats()}
    assert stats["a"]["entries"] == 1 and stats["a"]["hits"] == 1 and stats["b"]["hits"] == 0
    assert cache.evict(stage="a") == 1
    assert [r["stage"] for r in cache.stats()] == ["b"]
    with pytest.raises(ValueError):
        cache.evict()


# ------------------------------------------------------------------ ledger

def test_ledger_concurrent_appends_from_threads(tmp_path):
    path = tmp_path / "spend.jsonl"

    def worker(n):
        led = SpendLedger(path)
        for _ in range(200):
            led.append({"run_id": f"r{n}", "cost_usd": 0.001, "ok": True, "pad": "x" * 200})

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    led = SpendLedger(path)
    assert len(list(led.records())) == 1600
    assert led.total_usd() == pytest.approx(1.6)
    assert led.run_total_usd("r3") == pytest.approx(0.2)


def test_ledger_ignores_half_written_last_line_until_complete(tmp_path):
    path = tmp_path / "spend.jsonl"
    led = SpendLedger(path)
    led.append({"cost_usd": 1.0})
    with open(path, "a") as f:
        f.write('{"cost_usd": 2.0')
    assert led.total_usd() == pytest.approx(1.0)
    with open(path, "a") as f:
        f.write("}\n")
    assert led.total_usd() == pytest.approx(3.0)


# ------------------------------------------------------------------ retries

def test_retries_429_and_5xx_then_succeeds(tmp_path):
    p = FakeProvider(queue=[(429, {"error": "slow down"}, {"Retry-After": "0"}), (502, {"error": "bad gateway"}, None),
                            (200, {"error": {"message": "upstream"}}, None)])
    usage = UsageTracker()

    async def go():
        c = make_client(tmp_path, p, max_attempts=6)
        r = await c.chat(model=MODEL, messages=MSGS, stage="s", usage=usage)
        await c.aclose()
        return r

    r = run(go())
    assert r.content == "pong" and len(p.calls) == 4
    assert [l["ok"] for l in ledger_lines(tmp_path)] == [False, False, False, True]
    assert usage.to_metadata(0)["model_calls"] == 4          # retries count, as the contract asks


def test_gives_up_after_max_attempts(tmp_path):
    p = FakeProvider(queue=[(503, {"error": "down"}, None)] * 3)

    async def go():
        c = make_client(tmp_path, p, max_attempts=3)
        with pytest.raises(LLMHTTPError):
            await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()

    run(go())
    assert len(p.calls) == 3


def test_non_retryable_error_is_not_retried(tmp_path):
    p = FakeProvider(queue=[(400, {"error": "bad request"}, None)])

    async def go():
        c = make_client(tmp_path, p, max_attempts=6)
        with pytest.raises(LLMHTTPError) as ei:
            await c.chat(model=MODEL, messages=MSGS, stage="s")
        await c.aclose()
        return ei.value

    assert run(go()).retryable is False
    assert len(p.calls) == 1


def test_slow_provider_times_out_and_deadline_stops_retries(tmp_path):
    p = FakeProvider(delay=0.5)

    async def go():
        import time as _t
        c = make_client(tmp_path, p, max_attempts=6)
        t0 = _t.monotonic()
        with pytest.raises((LLMHTTPError, Exception)):
            await c.chat(model=MODEL, messages=MSGS, stage="s", timeout_s=0.1, deadline=_t.monotonic() + 5.5)
        await c.aclose()
        return _t.monotonic() - t0

    elapsed = run(go())
    assert elapsed < 5.5                          # never waits past the deadline
    assert 1 <= len(p.calls) < 6                  # stopped retrying once an attempt could not finish in time
