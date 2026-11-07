"""Local OCR engines for scanned pages and embedded images. Results are cached in the artifact cache.

Engines (all free, local, open source):
    tesseract        Tesseract 5 LSTM, page segmentation mode 3 (automatic)
    tesseract_psm6   Tesseract, psm 6 (one uniform block: sometimes better on forms and tables)
    rapidocr         RapidOCR (PP-OCR models on onnxruntime; models ship with the package, no torch)

Each returns text (lines grouped into paragraphs) and a mean confidence in [0, 1], which later
decides whether a page is escalated to a vision model.
"""
from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass

from app.artifacts import ArtifactCache
from app.hashing import sha256_hex

OCR_VERSION = 1


@dataclass
class OcrResult:
    text: str
    confidence: float | None
    seconds: float
    engine: str


def _tesseract(image: bytes, psm: int) -> tuple[str, float | None]:
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}   # pages run in parallel; one thread each
    out = subprocess.run(["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", "eng", "tsv"],
                         input=image, capture_output=True, env=env, timeout=300, check=True).stdout.decode("utf-8", "replace")
    lines: dict[tuple, list[str]] = {}
    confs: list[tuple[float, int]] = []
    order: list[tuple] = []
    for row in out.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        word = cols[11].strip()
        if not word:
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))   # block, paragraph, line
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(word)
        c = float(cols[10])
        if c >= 0:
            confs.append((c, len(word)))
    text, prev = [], None
    for key in order:
        if prev is not None and key[:2] != prev[:2]:
            text.append("")                              # new paragraph
        text.append(" ".join(lines[key]))
        prev = key
    conf = (sum(c * n for c, n in confs) / sum(n for _, n in confs) / 100.0) if confs else None
    return "\n".join(text), conf


_rapid_local = threading.local()


def _rapidocr(image: bytes) -> tuple[str, float | None]:
    import logging

    import numpy as np
    from PIL import Image
    from rapidocr import RapidOCR

    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    eng = getattr(_rapid_local, "engine", None)
    if eng is None:
        eng = _rapid_local.engine = RapidOCR()
    arr = np.array(Image.open(io.BytesIO(image)).convert("RGB"))
    r = eng(arr)
    if r is None or r.txts is None or len(r.txts) == 0:
        return "", None
    boxes = []
    for box, txt, score in zip(r.boxes, r.txts, r.scores):
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        boxes.append((min(ys), max(ys), min(xs), txt, float(score)))
    boxes.sort(key=lambda b: ((b[0] + b[1]) / 2, b[2]))
    # group boxes into lines by vertical overlap
    lines: list[list[tuple]] = []
    for b in boxes:
        cy, h = (b[0] + b[1]) / 2, b[1] - b[0]
        if lines:
            last = lines[-1]
            ly = sum((x[0] + x[1]) / 2 for x in last) / len(last)
            lh = sum(x[1] - x[0] for x in last) / len(last)
            if abs(cy - ly) < 0.5 * max(h, lh):
                last.append(b)
                continue
        lines.append([b])
    out, prev_bottom, prev_h = [], None, None
    for ln in lines:
        ln.sort(key=lambda x: x[2])
        top = min(x[0] for x in ln)
        h = sum(x[1] - x[0] for x in ln) / len(ln)
        if prev_bottom is not None and top - prev_bottom > 1.2 * max(h, prev_h):
            out.append("")
        out.append("  ".join(x[3] for x in ln))
        prev_bottom, prev_h = max(x[1] for x in ln), h
    conf = sum(len(b[3]) * b[4] for b in boxes) / max(1, sum(len(b[3]) for b in boxes))
    return "\n".join(out), conf


ENGINES = {
    "tesseract": lambda img: _tesseract(img, 3),
    "tesseract_psm6": lambda img: _tesseract(img, 6),
    "rapidocr": _rapidocr,
}


def run_ocr(engine: str, image: bytes, *, cache: ArtifactCache | None = None, cache_params: dict | None = None) -> OcrResult:
    """OCR one image. Cached by image hash + engine when a cache is given."""
    fn = ENGINES[engine]

    def compute() -> dict:
        t0 = time.perf_counter()
        text, conf = fn(image)
        return asdict(OcrResult(text=text, confidence=conf, seconds=round(time.perf_counter() - t0, 3), engine=engine))

    if cache is None:
        return OcrResult(**compute())
    data = cache.get_or_compute_json(file_sha=sha256_hex(image), stage=f"ocr-{engine}", version=OCR_VERSION,
                                     params=cache_params or {}, compute=compute)
    return OcrResult(**data)
