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
LONG_SIDE = 1500
RENDER_DPI = 200


def _resize_png(data: bytes) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("L")
    scale = LONG_SIDE / max(im.size)
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def transcribe(image_png: bytes, client: LLMClient, *, model: str, extra: dict, usage: UsageTracker | None,
                     job_id: str | None, doc_id: str | None, deadline: float | None = None) -> str:
    url = "data:image/png;base64," + b64encode(image_png).decode()
    msgs = [{"role": "user", "content": [{"type": "text", "text": VISION_PROMPT},
                                         {"type": "image_url", "image_url": {"url": url}}]}]
    r = await client.chat(model=model, messages=msgs, stage="vision_transcribe", job_id=job_id, doc_id=doc_id,
                          usage=usage, temperature=0, max_tokens=3000, timeout_s=60, deadline=deadline, **extra)
    text = r.content or ""
    if r.finish_reason == "length":
        raise RuntimeError("vision transcription hit the token cap (possible runaway output)")
    return text


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

    async def one(i: int):
        img = await asyncio.to_thread(render_page, path, i, RENDER_DPI, cache=cache, file_sha=sha)
        if client is not None and cfg.scanned_mode == "vision":
            try:
                text = await transcribe(_resize_png(img), client, model=cfg.vision_model, extra=cfg.vision_extra,
                                        usage=usage, job_id=job_id, doc_id=doc_id, deadline=deadline)
                return i, text, f"vision:{cfg.vision_model}"
            except Exception as exc:  # noqa: BLE001 - fall back to local OCR for this page
                log.warning("vision transcription failed; using local OCR", extra={"job_id": job_id, "page": i + 1,
                                                                                   "error": str(exc)[:200]})
        r = await asyncio.to_thread(run_ocr, cfg.fallback_ocr, img, cache=cache)
        return i, r.text, f"ocr:{cfg.fallback_ocr}"

    for i, text, method in await asyncio.gather(*(one(i) for i in scanned)):
        texts[i] = text
        pages[i] = PageInfo(number=i + 1, method=method, chars=len(text))
    return assemble_pdf(path.name, texts, pages, variant=f"{cfg.pdf_text_engine}+{cfg.scanned_mode}",
                        meta={"seconds": round(time.perf_counter() - t0, 2), "scanned_pages": len(scanned)})
