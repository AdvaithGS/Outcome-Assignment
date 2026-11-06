"""Content-addressed cache keys for LLM requests.

The key covers everything that can change a model's output (model, messages, generation
params) and nothing that cannot (API key, base URL, streaming flags). Changing a prompt therefore
produces a new key automatically, so prompts never need manual versioning.

Inline blobs (base64 page images, PDFs) are replaced by their sha256, which keeps keys small and
means the cache never stores image bytes.
"""
from __future__ import annotations

from typing import Any

from app.hashing import canonical_json, sha256_hex

# Bump to invalidate every existing entry (e.g. if the key layout itself changes).
KEY_VERSION = 1

# Request params that do not affect the content of the answer.
NON_SEMANTIC_PARAMS = frozenset({"stream", "stream_options", "usage", "user", "metadata"})

_BLOB_MIN_LEN = 256


def _strip_blobs(obj: Any) -> Any:
    if isinstance(obj, str):
        if obj.startswith("data:") and len(obj) >= _BLOB_MIN_LEN:
            return f"blob:sha256:{sha256_hex(obj)}"
        return obj
    if isinstance(obj, list):
        return [_strip_blobs(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _strip_blobs(v) for k, v in obj.items()}
    return obj


def request_key(body: dict, *, namespace: str = "", sample_index: int = 0) -> str:
    """sha256 key for a chat-completions request body.

    namespace:    isolates entries (e.g. per tenant); "" means shared.
    sample_index: lets an experiment draw N distinct samples of the same prompt, and lets a
                  plain re-ask bypass a cached bad answer.
    """
    semantic = {k: v for k, v in body.items() if k not in NON_SEMANTIC_PARAMS}
    material = {"v": KEY_VERSION, "ns": namespace, "sample": sample_index, "body": _strip_blobs(semantic)}
    return sha256_hex(canonical_json(material))


def request_preview(body: dict, limit: int = 200) -> str:
    """Short human-readable hint of what a request was, for cache inspection."""
    messages = body.get("messages") or []
    if not messages:
        return ""
    content = messages[-1].get("content", "")
    if isinstance(content, list):
        content = " ".join(p.get("text", "[" + str(p.get("type")) + "]") for p in content if isinstance(p, dict))
    return str(content)[:limit]
