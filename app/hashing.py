"""Stable hashing helpers shared by the LLM cache, the artifact cache and (later) job idempotency."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace. Equal objects give equal strings."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()
