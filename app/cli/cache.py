"""Inspect or prune the LLM response cache.

    python -m app.cli.cache stats
    python -m app.cli.cache evict --stage extract_lease
    python -m app.cli.cache evict --model openai/gpt-6-luna --older-than-hours 12
    python -m app.cli.cache evict --all
"""
from __future__ import annotations

import argparse
import os
import sys

from app.llm.cache import ResponseCache


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=os.environ.get("LLM_CACHE_PATH", ".cache/llm_cache.sqlite"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    ev = sub.add_parser("evict")
    ev.add_argument("--key", help="key prefix")
    ev.add_argument("--stage")
    ev.add_argument("--model")
    ev.add_argument("--older-than-hours", type=float)
    ev.add_argument("--all", action="store_true")
    args = ap.parse_args()

    cache = ResponseCache(args.path)
    if args.cmd == "stats":
        rows = cache.stats()
        if not rows:
            print("cache is empty")
            return 0
        print(f"{'stage':24s} {'model':36s} {'entries':>7s} {'hits':>6s} {'cost $':>9s} {'saved $':>9s} {'KB':>8s}")
        for r in rows:
            print(f"{r['stage']:24s} {r['model']:36s} {r['entries']:7d} {r['hits']:6d} "
                  f"{r['cost_usd']:9.4f} {r['saved_usd']:9.4f} {r['bytes'] / 1024:8.1f}")
        print(f"{'TOTAL':61s} {sum(r['entries'] for r in rows):7d} {sum(r['hits'] for r in rows):6d} "
              f"{sum(r['cost_usd'] for r in rows):9.4f} {sum(r['saved_usd'] for r in rows):9.4f}")
    else:
        older = args.older_than_hours * 3600 if args.older_than_hours is not None else None
        n = cache.evict(key=args.key, stage=args.stage, model=args.model, older_than_s=older, everything=args.all)
        print(f"evicted {n} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
