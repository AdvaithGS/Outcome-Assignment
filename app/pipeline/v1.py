"""Pipeline v1: stage 0 → ingestion → route → stage 3a (single-entry) → stage 4.

Tables (a field marked primary) go to stage 3b (an LLM-written plan executed in code, app/pipeline/table.py).
Every model choice is configuration (env or V1Config), so experiments swap models without code changes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
from app.pipeline.table import extract_table
from app.ingest.grid import (grid_document, grid_from_docx, grid_from_markdown_pages, grid_from_pdf,
                             grid_from_text_lines, load_grid)
from app.pipeline.base import DocumentError
from app.ingest.pdf import hidden_ocr_pages, is_scanned_page, page_texts
from app.pipeline.ingest import transcribe_table_pages

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
    scanned_mode: str = "selective"       # selective (OCR first, vision for flagged pages; ideas 2) | vision | ocr
    vision_long_side: int = 1500          # image size sent to the vision model (px, long side)
    fallback_ocr: str = "tesseract"
    docx_image_ocr: str = "rapidocr"
    pdf_text_engine: str = "pdfplumber"
    computed_mode: str = "H"              # M | C | H
    freetext_mode: str = "concise"        # concise | verbatim | describe (E2-freetext; concise = safer under strict scoring)
    hints: bool = True                    # generic CRE extraction rules (E2-hints, v3 adopted)
    extract_style: str = "value"          # value | pointer | hybrid (values backed by exact quotes, resolved in code)
    table_model: str = LUNA_OFF[0]
    table_extra: dict = field(default_factory=lambda: dict(LUNA_OFF[1]))

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
        c.vision_long_side = int(e.get("VISION_LONG_SIDE", c.vision_long_side))
        c.computed_mode = e.get("COMPUTED_MODE", c.computed_mode)
        c.table_model = e.get("TABLE_MODEL", c.table_model)
        c.freetext_mode = e.get("FREETEXT_MODE", c.freetext_mode)
        c.hints = e.get("HINTS", "on" if c.hints else "off") == "on"
        c.extract_style = e.get("EXTRACT_STYLE", c.extract_style)
        c.table_extra = _extra(e.get("TABLE_EXTRA"), c.table_extra)
        return c


@dataclass
class Extraction:
    spec: SchemaSpec
    doc: Document | None
    raw: dict | None
    notes: list[str]
    table_rows: list[dict] | None = None
    partial: bool = False
    style: str | None = None                # overrides cfg.extract_style for this document


_ANCHOR = re.compile(r"\[(?:Page \d+|Section [^\]]*|--- CHUNK \d+ ---|Table ID: \d+|Image ID: \d+)\]")


def has_text(doc: Document, min_chars: int = 20) -> bool:
    """Does the document carry any readable content once anchors and placeholders are removed?"""
    body = _ANCHOR.sub(" ", doc.markdown).replace("(no text recognised)", " ")
    return len(re.findall(r"[A-Za-z0-9]", body)) >= min_chars


async def read_grid(path: Path, file_type: str):
    """Spreadsheet → grid; a file that cannot be opened is an unreadable document (FAILED with the reason), not a
    pipeline error to retry."""
    try:
        return await asyncio.to_thread(load_grid, path, file_type)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"cannot read {file_type.upper()}: {type(exc).__name__}: {str(exc)[:200]}") from exc


def grid_is_empty(grid) -> bool:
    """No sheet with at least two non-empty rows (a header and a record): there is no table to extract."""
    return not grid or not grid.sheets or max(len(s.nonempty_rows()) for s in grid.sheets) < 2


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
        if spec.table:
            return await self._extract_table(path, spec, ctx, deadline)
        if ctx.file_type not in ("pdf", "docx"):
            # a single-entry field list on a spreadsheet: read the grid as text and use the prose extractor
            # (pointer: cell addresses are exact, so values are read from the cells themselves)
            grid = await read_grid(path, ctx.file_type)
            doc = grid_document(grid)
            if not has_text(doc):
                return Extraction(spec, doc, None, ["spreadsheet has no data; no extraction call made"])
            raw, notes = await extract(spec, doc, self.client, model=self.cfg.extract_model, extra=self.cfg.extract_extra,
                                       fallback_model=self.cfg.fallback_model, fallback_extra=self.cfg.fallback_extra,
                                       usage=ctx.usage, job_id=ctx.job_id, doc_id=ctx.file_name, deadline=deadline,
                                   freetext_mode=self.cfg.freetext_mode, hints=self.cfg.hints, style="hybrid")
            return Extraction(spec, doc, raw, notes, style="hybrid")
        ctx.set_stage("PARSING")
        doc = await ingest(path, ctx.file_type, client=self.client, cfg=self.cfg, cache=self.cache, usage=ctx.usage,
                           job_id=ctx.job_id, doc_id=ctx.file_name, deadline=deadline)
        if not has_text(doc):
            return Extraction(spec, doc, None, ["document has no readable text; no extraction call made"])
        ctx.set_stage("EXTRACTING")
        raw, notes = await extract(spec, doc, self.client, model=self.cfg.extract_model, extra=self.cfg.extract_extra,
                                   fallback_model=self.cfg.fallback_model, fallback_extra=self.cfg.fallback_extra,
                                   usage=ctx.usage, job_id=ctx.job_id, doc_id=ctx.file_name, deadline=deadline,
                                   freetext_mode=self.cfg.freetext_mode, hints=self.cfg.hints,
                                   style=self.cfg.extract_style)
        return Extraction(spec, doc, raw, notes)

    async def _extract_table(self, path: Path, spec: SchemaSpec, ctx: JobContext, deadline: float) -> Extraction:
        ctx.set_stage("PARSING")
        notes: list[str] = []
        if ctx.file_type in ("xlsx", "csv"):
            grid = await read_grid(path, ctx.file_type)
        elif ctx.file_type == "docx":
            try:
                grid = await asyncio.to_thread(grid_from_docx, path)
            except Exception as exc:  # noqa: BLE001
                raise DocumentError(f"cannot read DOCX: {type(exc).__name__}: {str(exc)[:200]}") from exc
            if grid is None:
                return Extraction(spec, None, None, ["DOCX has no table for a table field list"])
        else:
            try:
                texts = await asyncio.to_thread(page_texts, path, self.cfg.pdf_text_engine)
            except Exception as exc:  # noqa: BLE001
                raise DocumentError(f"cannot read PDF: {exc}") from exc
            if not texts:
                raise DocumentError("PDF has no pages")
            hidden = await asyncio.to_thread(hidden_ocr_pages, path)
            scanned = [i for i, t in enumerate(texts) if is_scanned_page(t) or i in hidden]
            grid = None
            if len(scanned) * 2 < len(texts):
                # a text layer: rebuild the table from word positions; if no table-like lines are found, the text
                # lines themselves are the grid (never vision on pages that already have text)
                grid = await asyncio.to_thread(grid_from_pdf, path) or grid_from_text_lines(texts, path.name)
            else:
                # scanned (or mostly scanned) table: transcribe every page, tables as Markdown, then build the grid
                got = await transcribe_table_pages(path, len(texts), client=self.client, cfg=self.cfg,
                                                   cache=self.cache, usage=ctx.usage, job_id=ctx.job_id,
                                                   doc_id=ctx.file_name, deadline=deadline)
                grid = grid_from_markdown_pages([got[i][0] for i in range(len(texts))], path.name)
                notes.append(f"table rebuilt from {len(texts)} transcribed page(s)")
                if grid is None:
                    return Extraction(spec, None, None, notes + ["no table found in the transcribed pages"])
        if grid_is_empty(grid):
            return Extraction(spec, None, None, notes + ["no table data in the document; no plan call made"])
        ctx.set_stage("EXTRACTING")
        res = await extract_table(spec, grid, self.client, model=self.cfg.table_model, extra=self.cfg.table_extra,
                                  usage=ctx.usage, job_id=ctx.job_id, deadline=deadline,
                                  repair_model=self.cfg.fallback_model, repair_extra=self.cfg.fallback_extra)
        return Extraction(spec, None, None, notes + res.notes, table_rows=res.rows, partial=res.partial)

    def rows(self, ex: Extraction, mode: str | None = None) -> PipelineOutput:
        if ex.table_rows is not None:
            return PipelineOutput(rows=ex.table_rows or [{}], partial=ex.partial, notes=ex.notes)
        if ex.doc is None:                  # no table found / nothing to extract: valid nulls, flagged PARTIAL
            return PipelineOutput(rows=[{}], partial=True, notes=ex.notes)
        row = postprocess(ex.spec, ex.raw, ex.doc, mode or self.cfg.computed_mode, ex.style or self.cfg.extract_style)
        return PipelineOutput(rows=[row], partial=ex.raw is None, notes=ex.notes)

    async def run(self, path: Path, schema: dict, ctx: JobContext) -> PipelineOutput:
        ex = await self.extract(path, schema, ctx)
        ctx.set_stage("POSTPROCESSING")
        return self.rows(ex)
