"""Stage 1 inside the pipeline: file → anchored Document, using the choices from E0/E1a.

  PDF   pdfplumber text per page; pages without a text layer are rendered and transcribed by a vision model
        (marks ~~struck~~ and [handwritten: …]); local OCR (Tesseract) is the fallback if the vision call fails.
  DOCX  own XML parser with section anchors; embedded images OCR'd locally (RapidOCR).
Rendering, OCR and vision results are all cached (artifact cache / LLM cache), so re-runs cost nothing.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from base64 import b64encode
from pathlib import Path

from app.artifacts import ArtifactCache
from app.hashing import file_sha256
from app.ingest.document import Document, PageInfo
from app.ingest.docx import ingest_docx
from app.ingest.ocr import run_ocr
from app.ingest.pdf import assemble_pdf, is_scanned_page, page_texts, render_page
from app.llm import LLMClient, UsageTracker
from app.pipeline.base import DocumentError

log = logging.getLogger("ingest")

VISION_PROMPT = (
    "Transcribe all text on this page image into Markdown, exactly as written.\n"
    "- Keep the reading order. Render tables as Markdown tables.\n"
    "- Mark struck-through or crossed-out text as ~~text~~.\n"
    "- Mark handwritten text (including handwritten numbers, dates, initials, signatures) as [handwritten: text].\n"
    "- Do not correct spelling, do not summarise, do not add commentary.\n"
    "- If something is illegible, write [illegible]."
)
# For table documents: the grid is built from these Markdown tables and one plan maps fixed columns, so every page
# must keep the printed column layout exactly (pages are transcribed independently and in parallel).
TABLE_VISION_PROMPT = (
    "Transcribe this page of a table document into Markdown, exactly as written.\n"
    "- Every printed column of the table becomes exactly ONE Markdown column, including columns without a heading "
    "(e.g. an account code and its name printed side by side are TWO columns). Never merge two printed columns into one "
    "cell and never split one.\n"
    "- Keep one Markdown row per printed row; a section title row keeps its text in the column where it is printed and "
    "leaves the other cells empty. Text that wraps onto several printed lines inside one cell stays in that one cell.\n"
    "- Text outside the table (titles, header lines, dates) goes on its own lines above or below the table.\n"
    "- Mark struck-through text as ~~text~~ and handwriting as [handwritten: text]. Do not correct spelling and do not "
    "add commentary."
)


def table_header(markdown: str) -> str | None:
    """The first Markdown table row of a transcribed page (the column header), if any."""
    for line in markdown.splitlines():
        t = line.strip()
        if t.startswith("|") and t.count("|") >= 3:
            return t
    return None


async def transcribe_table_pages(path: Path, n_pages: int, **kw) -> dict[int, tuple[str, str]]:
    """Table documents: transcribe page 1 first, then the remaining pages in parallel with page 1's column header as a
    layout to follow, so every page yields the same columns (one plan maps fixed columns)."""
    first = await transcribe_pages(path, [0], prompt=TABLE_VISION_PROMPT, **kw)
    header = table_header(first[0][0]) if 0 in first else None
    prompt = TABLE_VISION_PROMPT
    if header:
        ncols = header.strip("|").count("|") + 1
        prompt += ("\n- This page continues a table whose columns are (from its first page):\n" + header +
                   f"\n  Use exactly these {ncols} columns in this order for every table row on this page.")
    rest = await transcribe_pages(path, list(range(1, n_pages)), prompt=prompt, **kw) if n_pages > 1 else {}
    return {**first, **rest}
LONG_SIDE = 1500
RENDER_DPI = 200

# Selective vision (SCANNED_MODE=selective): a scanned page is sent to the vision model only when local OCR looks
# unreliable or the page is table/form-like. Thresholds from the per-page study on both scanned leases: they flag every
# page where OCR lost an answer value (handwritten correction, garbled name, rent table) and ~15% of pages overall.
SELECT_MIN_CONF = 0.88        # Tesseract mean word confidence below this
SELECT_LOW_SHARE = 0.05       # share of words with confidence < 50 at or above this
SELECT_NUMERIC_LINES = 0.15   # share of lines with 3+ numbers (tables, rent schedules)
SELECT_FORM_LINES = 0.10      # share of "LABEL:" lines (summary / reference pages)


def page_needs_vision(text: str, confidence: float | None, low_share: float | None) -> tuple[bool, str]:
    lines = [l for l in text.splitlines() if l.strip()]
    n = max(1, len(lines))
    numeric = sum(1 for l in lines if len(re.findall(r"\d[\d,]*\.?\d*", l)) >= 3) / n
    form = sum(1 for l in lines if re.match(r"^[A-Z][A-Z '’&/()-]{2,40}:", l.strip())) / n
    if confidence is None or confidence < SELECT_MIN_CONF:
        return True, f"low OCR confidence ({confidence})"
    if (low_share or 0) >= SELECT_LOW_SHARE:
        return True, f"{low_share:.0%} low-confidence words"
    if numeric >= SELECT_NUMERIC_LINES:
        return True, f"table-like ({numeric:.0%} numeric lines)"
    if form >= SELECT_FORM_LINES:
        return True, f"form-like ({form:.0%} label lines)"
    return False, "OCR adequate"


def _resize_png(data: bytes, long_side: int = LONG_SIDE) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("L")
    scale = long_side / max(im.size)
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def transcribe(image_png: bytes, client: LLMClient, *, model: str, extra: dict, usage: UsageTracker | None,
                     job_id: str | None, doc_id: str | None, deadline: float | None = None,
                     prompt: str = VISION_PROMPT) -> str:
    url = "data:image/png;base64," + b64encode(image_png).decode()
    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": url}}]}]
    r = await client.chat(model=model, messages=msgs, stage="vision_transcribe", job_id=job_id, doc_id=doc_id,
                          usage=usage, temperature=0, max_tokens=3000, timeout_s=60, deadline=deadline, **extra)
    text = r.content or ""
    if r.finish_reason == "length":
        raise RuntimeError("vision transcription hit the token cap (possible runaway output)")
    return text


async def transcribe_pages(path: Path, indices: list[int], *, client: LLMClient | None, cfg, cache: ArtifactCache,
                           usage: UsageTracker | None = None, job_id: str | None = None, doc_id: str | None = None,
                           deadline: float | None = None, file_sha: str | None = None,
                           prompt: str = VISION_PROMPT) -> dict[int, tuple[str, str]]:
    """Render the given 0-based pages and transcribe them (vision model; local OCR fallback). -> {index: (text, method)}"""
    sha = file_sha or await asyncio.to_thread(file_sha256, path)

    async def one(i: int):
        img = await asyncio.to_thread(render_page, path, i, RENDER_DPI, cache=cache, file_sha=sha)
        if client is not None and cfg.scanned_mode == "selective":
            r = await asyncio.to_thread(run_ocr, "tesseract", img, cache=cache)
            need, why = page_needs_vision(r.text, r.confidence, r.low_share)
            if not need:
                return i, r.text, "ocr:tesseract (selective)"
        if client is not None and cfg.scanned_mode in ("vision", "selective"):
            try:
                text = await transcribe(_resize_png(img, getattr(cfg, "vision_long_side", LONG_SIDE)), client,
                                        model=cfg.vision_model, extra=cfg.vision_extra,
                                        usage=usage, job_id=job_id, doc_id=doc_id, deadline=deadline, prompt=prompt)
                return i, text, f"vision:{cfg.vision_model}"
            except Exception as exc:  # noqa: BLE001 - fall back to local OCR for this page
                log.warning("vision transcription failed; using local OCR", extra={"job_id": job_id, "page": i + 1,
                                                                                   "error": str(exc)[:200]})
        r = await asyncio.to_thread(run_ocr, cfg.fallback_ocr, img, cache=cache)
        return i, r.text, f"ocr:{cfg.fallback_ocr}"

    return {i: (t, m) for i, t, m in await asyncio.gather(*(one(i) for i in indices))}


async def ingest(path: Path, file_type: str, *, client: LLMClient | None, cfg, cache: ArtifactCache,
                 usage: UsageTracker | None = None, job_id: str | None = None, doc_id: str | None = None,
                 deadline: float | None = None) -> Document:
    if file_type == "docx":
        try:
            return await asyncio.to_thread(ingest_docx, path, ocr_engine=cfg.docx_image_ocr, cache=cache)
        except Exception as exc:  # noqa: BLE001
            raise DocumentError(f"cannot read DOCX: {exc}") from exc
    if file_type != "pdf":
        raise NotImplementedError(file_type)

    t0 = time.perf_counter()
    try:
        texts = await asyncio.to_thread(page_texts, path, cfg.pdf_text_engine)
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"cannot read PDF: {exc}") from exc
    if not texts:
        raise DocumentError("PDF has no pages")
    sha = await asyncio.to_thread(file_sha256, path)
    scanned = [i for i, t in enumerate(texts) if is_scanned_page(t)]
    pages = [PageInfo(number=i + 1, method="text", chars=len(t)) for i, t in enumerate(texts)]

    got = await transcribe_pages(path, scanned, client=client, cfg=cfg, cache=cache, usage=usage, job_id=job_id,
                                 doc_id=doc_id, deadline=deadline, file_sha=sha)
    for i, (text, method) in got.items():
        texts[i] = text
        pages[i] = PageInfo(number=i + 1, method=method, chars=len(text))
    return assemble_pdf(path.name, texts, pages, variant=f"{cfg.pdf_text_engine}+{cfg.scanned_mode}",
                        meta={"seconds": round(time.perf_counter() - t0, 2), "scanned_pages": len(scanned)})
