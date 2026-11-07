"""Summarise the spend ledger.

    python -m app.cli.spend                 # totals, budget left, by model / stage / doc / run
    python -m app.cli.spend --run <run_id>  # one run only
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

from app.envfile import load_env_file
from app.llm.ledger import SpendLedger


def _table(title: str, groups: dict[str, list[dict]], top: int | None = None) -> None:
    rows = sorted(groups.items(), key=lambda kv: -sum(r["cost_usd"] or 0 for r in kv[1]))
    if top:
        rows = rows[:top]
    print(f"\n{title}")
    print(f"  {'':40s} {'calls':>6s} {'errors':>6s} {'tok in':>10s} {'tok out':>9s} {'cost $':>9s}")
    for name, recs in rows:
        print(f"  {str(name)[:40]:40s} {len(recs):6d} {sum(not r.get('ok') for r in recs):6d} "
              f"{sum(r.get('tokens_in') or 0 for r in recs):10d} {sum(r.get('tokens_out') or 0 for r in recs):9d} "
              f"{sum(r.get('cost_usd') or 0 for r in recs):10.6f}")


def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=os.environ.get("LLM_LEDGER_PATH", "ledger/spend.jsonl"))
    ap.add_argument("--run")
    ap.add_argument("--budget", type=float, default=float(os.environ.get("LLM_BUDGET_USD") or 10.0))
    args = ap.parse_args()

    recs = [r for r in SpendLedger(args.path).records() if not args.run or r.get("run_id") == args.run]
    total = sum(r.get("cost_usd") or 0 for r in recs)
    unpriced = sum(1 for r in recs if r.get("ok") and r.get("cost_source") in ("unknown", "none"))
    scope = f"run {args.run}" if args.run else "all runs"
    print(f"{scope}: {len(recs)} calls, ${total:.6f} spent")
    if not args.run:
        print(f"budget ${args.budget:.2f}: ${args.budget - total:.6f} left ({total / args.budget:.3%} used)")
    if unpriced:
        print(f"WARNING: {unpriced} successful calls have no cost (unknown model price)")
    if not recs:
        return 0

    by: dict[str, dict[str, list[dict]]] = {k: defaultdict(list) for k in ("model", "stage", "doc_id", "run_id")}
    for r in recs:
        for k in by:
            by[k][r.get(k) or "-"].append(r)
    _table("by model", by["model"])
    _table("by stage", by["stage"])
    _table("by document", by["doc_id"])
    if not args.run:
        _table("by run (top 15)", by["run_id"], top=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
