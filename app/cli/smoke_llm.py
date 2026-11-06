"""Live smoke test of the LLM chokepoint: one tiny real call, then the same call again (must hit).

Checks that the key works, whether the gateway returns usage.cost, that cost lands in the
ledger, and that the second call is served from the cache for free. Costs a fraction of a cent.

    python -m app.cli.smoke_llm [--model openai/gpt-6-luna]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.envfile import load_env_file
from app.llm import LLMClient, UsageTracker


async def run(model: str) -> int:
    client = LLMClient.from_env()
    usage = UsageTracker()
    msgs = [{"role": "user", "content": "Reply with exactly the word: pong"}]
    try:
        for i in (1, 2):
            r = await client.chat(model=model, messages=msgs, stage="smoke", max_tokens=5, temperature=0, usage=usage)
            print(f"call {i}: cached={r.cached} content={r.content!r} served={r.served_model} "
                  f"cost=${r.cost_usd:.8f} (source={r.cost_source}) usage={r.usage}")
    finally:
        await client.aclose()
    meta = usage.to_metadata(latency_ms=0)
    print("metadata:", meta)
    ok = meta["model_calls"] <= 1 and meta["cache_hits"] >= 1
    print("OK" if ok else "UNEXPECTED: second call was not a cache hit")
    return 0 if ok else 1


def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="openai/gpt-6-luna")
    return asyncio.run(run(ap.parse_args().model))


if __name__ == "__main__":
    sys.exit(main())
