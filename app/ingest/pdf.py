"""PDF branch: per-page text extraction with per-page scan detection and OCR fallback.

Text engines (compared in E0): pdftotext (raw), pdftotext_layout, pdfplumber, pymupdf, pymupdf4llm.
A page whose text layer has fewer than `scan_threshold` non-space characters is rendered and OCR'd.
`force_ocr` OCRs every page (used to simulate scans of digital docs whose answers are known).
"""
from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.artifacts import ArtifactCache
from app.hashing import file_sha256
from app.ingest.document import Document, MarkdownBuilder, PageInfo, feed_lines
from app.ingest.ocr import run_ocr

RENDER_VERSION = 1


def _pdftotext(path: Path, layout: bool) -> list[str]:
    args = ["pdftotext", "-enc", "UTF-8"] + (["-layout"] if layout else []) + [str(path), "-"]
    out = subprocess.run(args, capture_output=True, check=True, timeout=300).stdout.decode("utf-8", "replace")
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def _pdfplumber(path: Path) -> list[str]:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]


def _pymupdf(path: Path) -> list[str]:
    import pymupdf
    with pymupdf.open(path) as doc:
        return [p.get_text("text") for p in doc]


def _pymupdf4llm(path: Path) -> list[str]:
    import pymupdf4llm
    # use_ocr=False: our own per-page scan detection decides OCR, so engines are compared on text alone
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, show_progress=False, use_ocr=False)
    return [c.get("text", "") for c in chunks]


TEXT_ENGINES = {
    "pdftotext": lambda p: _pdftotext(p, False),
    "pdftotext_layout": lambda p: _pdftotext(p, True),
    "pdfplumber": _pdfplumber,
    "pymupdf": _pymupdf,
    "pymupdf4llm": _pymupdf4llm,
}


def page_count(path: Path) -> int:
    import pymupdf
    with pymupdf.open(path) as doc:
        return doc.page_count


def render_page(path: Path, index: int, dpi: int, *, cache: ArtifactCache | None = None, file_sha: str | None = None) -> bytes:
    def compute() -> bytes:
        import pymupdf
        with pymupdf.open(path) as doc:
            return doc[index].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY).tobytes("png")

    if cache is None:
        return compute()
    return cache.get_or_compute_bytes(file_sha=file_sha or file_sha256(path), stage="render", version=RENDER_VERSION,
                                      params={"page": index, "dpi": dpi}, compute=compute, ext=".png")


def ingest_pdf(path: str | Path, *, text_engine: str = "pymupdf", ocr_engine: str = "tesseract", ocr_dpi: int = 300,
               scan_threshold: int = 50, force_ocr: bool = False, cache: ArtifactCache | None = None,
               workers: int = 8, variant: str | None = None) -> Document:
    path = Path(path)
    sha = file_sha256(path)
    t0 = time.perf_counter()
    if force_ocr:
        texts = [""] * page_count(path)
    else:
        texts = TEXT_ENGINES[text_engine](path)
    t_text = time.perf_counter() - t0

    need_ocr = [i for i, t in enumerate(texts) if force_ocr or len("".join(t.split())) < scan_threshold]
    ocr_results = {}

    def do(i: int):
        img = render_page(path, i, ocr_dpi, cache=cache, file_sha=sha)
        return i, run_ocr(ocr_engine, img, cache=cache)

    t1 = time.perf_counter()
    if need_ocr:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, res in ex.map(do, need_ocr):
                ocr_results[i] = res
    t_ocr = time.perf_counter() - t1

    pages: list[PageInfo] = []
    for i, text in enumerate(texts):
        if i in ocr_results:
            r = ocr_results[i]
            texts[i] = r.text
            pages.append(PageInfo(number=i + 1, method=f"ocr:{ocr_engine}", chars=len(r.text),
                                  ocr_confidence=r.confidence, seconds=r.seconds))
        else:
            pages.append(PageInfo(number=i + 1, method="text", chars=len(text)))

    name = variant or (f"ocr:{ocr_engine}@{ocr_dpi}" if force_ocr else f"{text_engine}+{ocr_engine}@{ocr_dpi}")
    return assemble_pdf(path.name, texts, pages, name,
                        meta={"seconds_text": round(t_text, 2), "seconds_ocr": round(t_ocr, 2), "ocr_pages": len(need_ocr),
                              "ocr_page_seconds_sum": round(sum(r.seconds for r in ocr_results.values()), 2)})


def page_texts(path: str | Path, text_engine: str = "pdfplumber") -> list[str]:
    return TEXT_ENGINES[text_engine](Path(path))


def is_scanned_page(text: str, threshold: int = 50) -> bool:
    return len("".join(text.split())) < threshold


def hidden_ocr_pages(path: str | Path, min_invisible: float = 0.9, min_image_cover: float = 0.6) -> set[int]:
    """0-based pages that are scans carrying a scanner's own OCR layer: (almost) all text is invisible (render mode 3)
    and images cover most of the page. Their text layer is someone else's OCR, unchecked; we treat them as scanned
    (our OCR, the vision rule). A photo behind real, visible text (marketing pages) does not qualify."""
    import pymupdf
    out: set[int] = set()
    try:
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc):
                counts = {}
                for span in page.get_texttrace():
                    counts[span["type"]] = counts.get(span["type"], 0) + len(span["chars"])
                total = sum(counts.values())
                if not total or counts.get(3, 0) / total < min_invisible:
                    continue
                area = page.rect.get_area() or 1
                cover = sum((pymupdf.Rect(b["bbox"]) & page.rect).get_area() for b in page.get_image_info()) / area
                if cover >= min_image_cover:
                    out.add(i)
    except Exception:  # noqa: BLE001 - detection is an enhancement; the text layer is still usable
        return set()
    return out


def assemble_pdf(source: str, texts: list[str], pages: list[PageInfo], variant: str, meta: dict | None = None) -> Document:
    """Build the anchored Document from final per-page texts (text layer, OCR or vision transcription)."""
    b = MarkdownBuilder()
    for i, text in enumerate(texts):
        b.page(i + 1)
        feed_lines(b, text)
    return b.build(source=source, kind="pdf", variant=variant, pages=pages, meta=meta or {})
