"""The only models this service may call (assignment brief, section 3).

Calling anything else disqualifies the submission, so the LLM client refuses it up front.
"""
from __future__ import annotations

ALLOWED_MODELS: frozenset[str] = frozenset({
    "google/gemini-3.8-flash",
    "google/gemini-3.1-pro-preview",
    "anthropic/claude-haiku-5.5",
    "anthropic/claude-sonnet-5.5",
    "openai/gpt-6-luna",
    "openai/gpt-6.1-sol",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3-vl-235b-a22b-instruct",
    "deepseek/deepseek-v3.2",
})


class DisallowedModelError(ValueError):
    pass


def ensure_allowed(model: str) -> None:
    if model not in ALLOWED_MODELS:
        raise DisallowedModelError(f"model {model!r} is not on the allowed list")
