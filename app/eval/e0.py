"""E0: free comparison of ingestion options for PDF and DOCX (no LLM calls, $0).

    python -m app.eval.e0                    # all PDF/DOCX samples, all applicable variants
    python -m app.eval.e0 --docs om_001      # subset
    python -m app.eval.e0 --only-ocr         # just the OCR variants

For digital PDFs (om_001, budget_001) the OCR engines are also run on rendered page images
("forced OCR"), which simulates scans whose answers are known.

Writes:
    experiments/e0/results.json   metrics per (document, variant), committed (no document content)
    experiments/e0/summary.md     the same as a table
    experiments/e0/dumps/*.md     the extracted Markdown, for inspection (gitignored: holds sample text)
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from app.artifacts import ArtifactCache
from app.eval.coverage import anchor_quality, coverage
from app.ingest.docx import ingest_docx, ingest_docx_baseline
from app.ingest.pdf import TEXT_ENGINES, ingest_pdf

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
OUT = ROOT / "experiments" / "e0"

OCR_VARIANTS = [("tesseract", 300), ("tesseract", 200), ("tesseract_psm6", 300), ("rapidocr", 200)]


def is_scanned(path: Path, threshold: int = 50) -> bool:
    import pymupdf
    with pymupdf.open(path) as d:
        texty = sum(len("".join(p.get_text("text").split())) >= threshold for p in d)
        return texty < d.page_count / 2


def variants_for(path: Path, kind: str, only_ocr: bool, no_ocr: bool = False):
    cache = ArtifactCache(ROOT / ".cache" / "artifacts")
    if kind == "docx":
        if not only_ocr:
            yield "python-docx", lambda: ingest_docx_baseline(path, "python-docx")
            yield "mammoth", lambda: ingest_docx_baseline(path, "mammoth")
        for eng in ("tesseract", "rapidocr"):
            yield f"docx-xml+{eng}", lambda eng=eng: ingest_docx(path, ocr_engine=eng, cache=cache)
        return
    scanned = is_scanned(path)
    if not scanned and not only_ocr:
        for te in TEXT_ENGINES:
            yield te, lambda te=te: ingest_pdf(path, text_engine=te, cache=cache, variant=te)
    if no_ocr:
        return
    for eng, dpi in OCR_VARIANTS:
        workers = 3 if eng == "rapidocr" else 10
        yield (f"ocr:{eng}@{dpi}",
               lambda eng=eng, dpi=dpi, w=workers: ingest_pdf(path, ocr_engine=eng, ocr_dpi=dpi, force_ocr=True,
                                                              cache=cache, workers=w, variant=f"ocr:{eng}@{dpi}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", nargs="*")
    ap.add_argument("--only-ocr", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(SAMPLE / "manifest.csv")))
    rows = [r for r in rows if r["kind"] in ("pdf", "docx") and (not args.docs or r["document_id"] in args.docs)]
    (OUT / "dumps").mkdir(parents=True, exist_ok=True)
    res_path = OUT / "results.json"
    results = json.loads(res_path.read_text()) if res_path.exists() else {}

    for r in rows:
        doc_id, path = r["document_id"], SAMPLE / "sample-data-documents" / r["file"]
        truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{doc_id}.json").read_text())
        schema = json.loads((SAMPLE / "schemas" / f"{r['schema_name']}.json").read_text())
        for name, build in variants_for(path, r["kind"], args.only_ocr, args.no_ocr):
            t0 = time.perf_counter()
            doc = build()
            wall = time.perf_counter() - t0
            cov = coverage(doc, truth, schema)
            aq = anchor_quality(doc, truth)
            confs = [p.ocr_confidence for p in doc.pages if p.ocr_confidence is not None]
            rec = {
                "doc": doc_id, "variant": name, "tokens": doc.approx_tokens, "wall_s": round(wall, 2),
                "ocr_pages": sum(p.method.startswith("ocr") for p in doc.pages),
                "ocr_cpu_s_per_page": round(sum(p.seconds for p in doc.pages if p.method.startswith("ocr")) /
                                            max(1, sum(p.method.startswith("ocr") for p in doc.pages)), 2),
                "ocr_conf_mean": round(sum(confs) / len(confs), 3) if confs else None,
                "sections": sum(a.kind == "section" for a in doc.anchors),
                "chunks": sum(a.kind == "chunk" for a in doc.anchors),
                "tables": sum(a.kind == "table" for a in doc.anchors),
                "images": sum(a.kind == "image" for a in doc.anchors),
                **{k: v for k, v in cov.items() if k not in ("detail",)},
                **{f"anchor_{k}": v for k, v in aq.items()},
                "meta": doc.meta,
            }
            results[f"{doc_id}|{name}"] = rec
            (OUT / "dumps" / f"{doc_id}__{name.replace(':', '_').replace('@', '_')}.md").write_text(doc.markdown)
            print(f"{doc_id:12s} {name:24s} tok={rec['tokens']:6d} wall={rec['wall_s']:6.1f}s "
                  f"cov={rec['direct_found']} (+fuzzy {rec['direct_found_or_fuzzy']}) page={rec['on_cited_page']} "
                  f"cite_loc={aq['located']} page_agree={aq['page_agree']} slices={aq['slices']} "
                  f"med/p90/max={aq['slice_tokens_median']}/{aq['slice_tokens_p90']}/{aq['slice_tokens_max']}", flush=True)
            res_path.write_text(json.dumps(results, indent=1, default=str))

    write_summary(results)
    return 0


def write_summary(results: dict) -> None:
    cols = ["doc", "variant", "tokens", "wall_s", "ocr_cpu_s_per_page", "ocr_conf_mean", "direct_found",
            "direct_found_or_fuzzy", "on_cited_page", "anchor_located", "anchor_page_agree", "sections", "chunks",
            "tables", "images", "anchor_slice_tokens_median", "anchor_slice_tokens_p90", "anchor_slice_tokens_max"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for rec in sorted(results.values(), key=lambda r: (r["doc"], r["variant"])):
        lines.append("| " + " | ".join("" if rec.get(c) is None else str(rec.get(c)) for c in cols) + " |")
    (OUT / "summary.md").write_text(
        "# E0 results: ingestion options (free, no LLM)\n\n"
        "direct_found = share of non-computed answer values present verbatim (any common format);\n"
        "on_cited_page = share found on a page the answer cites; anchor_* = answer citations located in the text.\n\n"
        + "\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
