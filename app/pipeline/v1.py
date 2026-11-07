"""Pipeline v1: stage 0 → ingestion → route → stage 3a (single-entry) → stage 4.

Tables (a field marked primary) are not extracted yet (layer 1, step 2): they return one all-null entry.
Every model choice is configuration (env or V1Config), so experiments swap models without code changes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.artifacts import ArtifactCache
from app.ingest.document import Document
from app.llm import LLMClient
from app.pipeline.base import JobContext, PipelineOutput
from app.pipeline.ingest import ingest
from app.pipeline.postprocess import postprocess
from app.pipeline.schema_analysis import SchemaSpec, analyse
from app.pipeline.single import extract

LUNA_OFF = ("openai/gpt-6-luna", {"reasoning": {"enabled": False}})


def _extra(env_value: str | None, default: dict) -> dict:
    return json.loads(env_value) if env_value else dict(default)


@dataclass
class V1Config:
    analysis_model: str = LUNA_OFF[0]
    analysis_extra: dict = field(default_factory=lambda: dict(LUNA_OFF[1]))
    extract_model: str = LUNA_OFF[0]
    extract_extra: dict = field(default_factory=lambda: dict(LUNA_OFF[1]))
    fallback_model: str = "anthropic/claude-haiku-5.5"
    fallback_extra: dict = field(default_factory=dict)
    vision_model: str = LUNA_OFF[0]
    vision_extra: dict = field(default_factory=lambda: dict(LUNA_OFF[1]))
    scanned_mode: str = "vision"          # vision | ocr
    fallback_ocr: str = "tesseract"
    docx_image_ocr: str = "rapidocr"
    pdf_text_engine: str = "pdfplumber"
    computed_mode: str = "H"              # M | C | H

    @classmethod
    def from_env(cls, env=None) -> "V1Config":
        e = os.environ if env is None else env
        c = cls()
        c.analysis_model = e.get("ANALYSIS_MODEL", c.analysis_model)
        c.analysis_extra = _extra(e.get("ANALYSIS_EXTRA"), c.analysis_extra)
        c.extract_model = e.get("EXTRACT_MODEL", c.extract_model)
        c.extract_extra = _extra(e.get("EXTRACT_EXTRA"), c.extract_extra)
        c.fallback_model = e.get("FALLBACK_MODEL", c.fallback_model)
        c.vision_model = e.get("VISION_MODEL", c.vision_model)
        c.vision_extra = _extra(e.get("VISION_EXTRA"), c.vision_extra)
        c.scanned_mode = e.get("SCANNED_MODE", c.scanned_mode)
        c.computed_mode = e.get("COMPUTED_MODE", c.computed_mode)
        return c


@dataclass
class Extraction:
    spec: SchemaSpec
    doc: Document | None
    raw: dict | None
    notes: list[str]


class PipelineV1:
    name = "v1"

    def __init__(self, cfg: V1Config | None = None, client: LLMClient | None = None, cache: ArtifactCache | None = None):
        self.cfg = cfg or V1Config.from_env()
        self._client = client
        self.cache = cache or ArtifactCache.from_env()

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient.from_env()
        return self._client

    async def extract(self, path: Path, schema: dict, ctx: JobContext) -> Extraction:
        ctx.set_stage("ANALYSING_SCHEMA")
        # leave headroom before the job deadline for post-processing and writing the result
        deadline = ctx.deadline - 20
        spec = await analyse(schema, self.client, model=self.cfg.analysis_model, extra=self.cfg.analysis_extra,
                             usage=ctx.usage, job_id=ctx.job_id, deadline=deadline)
        if spec.table or ctx.file_type not in ("pdf", "docx"):
            return Extraction(spec, None, None, ["table extraction not implemented yet (layer 1 step 2)"])
        ctx.set_stage("PARSING")
        doc = await ingest(path, ctx.file_type, client=self.client, cfg=self.cfg, cache=self.cache, usage=ctx.usage,
                           job_id=ctx.job_id, doc_id=ctx.file_name, deadline=deadline)
        ctx.set_stage("EXTRACTING")
        raw, notes = await extract(spec, doc, self.client, model=self.cfg.extract_model, extra=self.cfg.extract_extra,
                                   fallback_model=self.cfg.fallback_model, fallback_extra=self.cfg.fallback_extra,
                                   usage=ctx.usage, job_id=ctx.job_id, doc_id=ctx.file_name, deadline=deadline)
        return Extraction(spec, doc, raw, notes)

    def rows(self, ex: Extraction, mode: str | None = None) -> PipelineOutput:
        if ex.doc is None:
            return PipelineOutput(rows=[{}], notes=ex.notes)
        row = postprocess(ex.spec, ex.raw, ex.doc, mode or self.cfg.computed_mode)
        return PipelineOutput(rows=[row], partial=ex.raw is None, notes=ex.notes)

    async def run(self, path: Path, schema: dict, ctx: JobContext) -> PipelineOutput:
        ex = await self.extract(path, schema, ctx)
        ctx.set_stage("POSTPROCESSING")
        return self.rows(ex)
