"""The interface between the service (layer 2) and the extraction pipeline (layer 1).

The service knows nothing about documents or models: it hands a pipeline a file, a field list and a context,
and gets back rows. Everything contract-related (every field present, field-result shape, metadata) is done by
`build_result`, so no pipeline can produce an invalid result.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from app.llm.usage import UsageTracker

FIELD_KEYS = ("value", "citation", "confidence", "page_numbers", "source_locations")


class SchemaError(ValueError):
    """The submitted field list is not usable (400 at the API)."""


class DocumentError(Exception):
    """The document itself cannot be read (corrupt, encrypted, empty). The job is FAILED with this reason;
    any other pipeline error ends the job as PARTIAL with null fields, so a bug never loses a document."""


def validate_schema(obj: Any) -> dict:
    """Accept any field list shaped like contract/schema.example.json; reject only what we cannot use."""
    if not isinstance(obj, dict):
        raise SchemaError("schema must be a JSON object")
    fields = obj.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SchemaError("schema must have a non-empty 'fields' object")
    for k, v in fields.items():
        if not isinstance(k, str) or not k:
            raise SchemaError("field keys must be non-empty strings")
        if not isinstance(v, dict):
            raise SchemaError(f"field {k!r} must be an object with 'name' and 'description'")
    if sum(1 for v in fields.values() if v.get("primary") is True) > 1:
        raise SchemaError("at most one field may be marked primary")
    return obj


def primary_field(schema: dict) -> str | None:
    return next((k for k, v in schema["fields"].items() if v.get("primary") is True), None)


@dataclass
class JobContext:
    job_id: str
    org: str
    file_name: str
    file_type: str                      # pdf | docx | xlsx | csv
    deadline: float                     # time.monotonic() value by which the pipeline should wrap up
    usage: UsageTracker = field(default_factory=UsageTracker)
    set_stage: Callable[[str], None] = lambda stage: None

    def time_left(self) -> float:
        return self.deadline - time.monotonic()


@dataclass
class PipelineOutput:
    rows: list[dict[str, dict]]         # one dict per entry: field key -> partial field result
    partial: bool = False               # some part failed; the rest is still valid (→ PARTIAL)
    notes: list[str] = field(default_factory=list)


class Pipeline(Protocol):
    name: str

    async def run(self, path: Path, schema: dict, ctx: JobContext) -> PipelineOutput: ...


def _clean_field(fr: Any) -> dict:
    if not isinstance(fr, dict):
        fr = {"value": fr}
    value = fr.get("value")
    if isinstance(value, list):
        value = [v for v in value if v is None or isinstance(v, (str, int, float, bool))]
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        value = str(value)
    try:
        conf = float(fr.get("confidence", 0.0 if value is None else 0.5))
    except (TypeError, ValueError):
        conf = 0.0
    out = {"value": value, "citation": "" if value is None else str(fr.get("citation") or ""),
           "confidence": min(1.0, max(0.0, conf))}
    pages = fr.get("page_numbers")
    if isinstance(pages, list) and value is not None:
        pages = sorted({int(p) for p in pages if isinstance(p, (int, float)) and int(p) >= 1})
        if pages:
            out["page_numbers"] = pages
    locs = fr.get("source_locations")
    if isinstance(locs, list) and value is not None:
        locs = [str(x) for x in locs if x]
        if locs:
            out["source_locations"] = locs
    return out


def build_result(schema: dict, out: PipelineOutput, usage: UsageTracker, latency_ms: int) -> dict:
    """Contract-valid result: at least one entry, every schema field in every entry, clean field results."""
    keys = list(schema["fields"])
    rows = out.rows or [{}]
    entries = [{k: _clean_field(row.get(k)) for k in keys} for row in rows]
    return {"entries": entries, "metadata": usage.to_metadata(latency_ms)}


class NullPipeline:
    """Returns one all-null entry, no model calls. Placeholder until layer 1 plugs in; also the safe fallback."""

    name = "null"

    async def run(self, path: Path, schema: dict, ctx: JobContext) -> PipelineOutput:
        ctx.set_stage("EXTRACTING")
        return PipelineOutput(rows=[{}])


PIPELINES: dict[str, Callable[[], Pipeline]] = {"null": NullPipeline}


def get_pipeline(name: str) -> Pipeline:
    if name not in PIPELINES:
        raise KeyError(f"unknown pipeline {name!r}; known: {sorted(PIPELINES)}")
    return PIPELINES[name]()
