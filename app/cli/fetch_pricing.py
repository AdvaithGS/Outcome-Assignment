"""Regenerate app/llm/pricing.json from OpenRouter's public model list (no key, no credit used).

    python -m app.cli.fetch_pricing
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import httpx

from app.llm.models import ALLOWED_MODELS
from app.llm.pricing import PRICING_PATH

MODELS_URL = "https://openrouter.ai/api/v1/models"


def _num(v) -> float | None:
    return float(v) if v not in (None, "") else None


def main() -> int:
    data = httpx.get(MODELS_URL, timeout=30).raise_for_status().json()["data"]
    found = {m["id"]: m for m in data if m["id"] in ALLOWED_MODELS}
    missing = sorted(ALLOWED_MODELS - found.keys())
    if missing:
        print(f"missing from {MODELS_URL}: {missing}", file=sys.stderr)
        return 1
    out = {
        "source": MODELS_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unit": "USD per token",
        "models": {
            mid: {
                "prompt": _num(m["pricing"]["prompt"]),
                "completion": _num(m["pricing"]["completion"]),
                "image": _num(m["pricing"].get("image")),
                "input_cache_read": _num(m["pricing"].get("input_cache_read")),
                "context_length": m.get("context_length"),
            }
            for mid, m in sorted(found.items())
        },
    }
    PRICING_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PRICING_PATH} ({len(out['models'])} models)")
    for mid, p in out["models"].items():
        print(f"  {mid:36s} in ${p['prompt'] * 1e6:6.3f}/M  out ${p['completion'] * 1e6:6.3f}/M")
    return 0


if __name__ == "__main__":
    sys.exit(main())
