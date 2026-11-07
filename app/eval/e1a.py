"""E1a: can a cheap vision model fix what local OCR gets wrong, and at what cost per page?

Targeted: 4 page images where local OCR failed or that act as a control, not whole documents.
    lease_001 p2   summary page with a handwritten deposit correction ($2,400 struck, 2150 written)
    lease_001 p25  typed guarantor name that both OCR engines garbled
    lease_001 p18  rent schedule table (control: local OCR read it, apart from comma/full-stop slips)
    lease_002 img1 embedded signature page with a handwritten date (second handwriting sample)

Checkpoints are the answer-file values cited on that page (scored with the E0 matcher, strict:
fuzzy string matches do not pass), plus two manual checks: the deposit correction is marked as
handwritten/struck, and the handwritten DOCX date. Local OCR text for the same images is scored
with the same checkpoints as a free baseline.

    python -m app.eval.e1a --models z-ai/glm-5.3-flash openai/gpt-6-luna ...   (round 1 by default)
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import zipfile
from base64 import b64encode
from pathlib import Path

from app.artifacts import ArtifactCache
from app.envfile import load_env_file
from app.eval.coverage import _COMPUTED_HINT, _find, classify, normalise
from app.hashing import sha256_hex
from app.ingest.ocr import run_ocr
from app.ingest.pdf import render_page
from app.llm import LLMClient, UsageTracker

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
OUT = ROOT / "experiments" / "e1a"
ROUND1 = ["z-ai/glm-5.3-flash", "openai/gpt-6-luna", "anthropic/claude-haiku-5.5", "qwen/qwen3-vl-235b-a22b-instruct"]
LONG_SIDE = 1500

PROMPT = (
    "Transcribe all text on this page image into Markdown, exactly as written.\n"
    "- Keep the reading order. Render tables as Markdown tables.\n"
    "- Mark struck-through or crossed-out text as ~~text~~.\n"
    "- Mark handwritten text (including handwritten numbers, dates, initials, signatures) as [handwritten: text].\n"
    "- Do not correct spelling, do not summarise, do not add commentary.\n"
    "- If something is illegible, write [illegible]."
)

PAGES = [
    {"id": "lease_001_p2", "doc": "lease_001", "page": 2,
     "manual": [("deposit 2150 marked handwritten", r"handwritten[^\]]{0,20}2,?150"),
                ("2,400 marked struck", r"~~[^~]{0,10}2,?400[^~]{0,10}~~")]},
    {"id": "lease_001_p25", "doc": "lease_001", "page": 25, "manual": []},
    {"id": "lease_001_p18", "doc": "lease_001", "page": 18, "manual": []},
    {"id": "lease_002_img1", "doc": "lease_002", "docx_image": "word/media/image1.png",
     "manual": [("handwritten date 8/21/25", r"8\s*/\s*21\s*/\s*(20)?25"),
                ("tenant name", r"kestrel analytics usa llc")]},
]


def _resize_png(data: bytes) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("L")
    scale = LONG_SIDE / max(im.size)
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def page_image(spec: dict, cache: ArtifactCache) -> bytes:
    if "docx_image" in spec:
        with zipfile.ZipFile(SAMPLE / "sample-data-documents" / f"{spec['doc']}.docx") as z:
            return _resize_png(z.read(spec["docx_image"]))
    path = SAMPLE / "sample-data-documents" / f"{spec['doc']}.pdf"
    return _resize_png(render_page(path, spec["page"] - 1, 200, cache=cache))


def checkpoints(spec: dict) -> list[tuple[str, object, str]]:
    """(label, value, kind) for every non-computed answer value cited on this page."""
    if "page" not in spec:
        return []
    truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{spec['doc']}.json").read_text())
    schema_name = next(l.split(",")[2] for l in (SAMPLE / "manifest.csv").read_text().splitlines() if l.startswith(spec["doc"]))
    fields = json.loads((SAMPLE / "schemas" / f"{schema_name}.json").read_text())["fields"]
    out = []
    for key, fr in truth["entries"][0].items():
        if spec["page"] not in (fr.get("page_numbers") or []) or fr["value"] is None:
            continue
        if _COMPUTED_HINT.search(fields[key]["description"]):
            continue
        for v in (fr["value"] if isinstance(fr["value"], list) else [fr["value"]]):
            kind = classify(v)
            if kind in ("number", "date", "month", "string"):
                out.append((f"{key}={v}", v, kind))
    return out


def score(text: str, spec: dict) -> dict:
    norm = normalise(text)
    passed, failed = [], []
    for label, v, kind in checkpoints(spec):
        (passed if _find(v, kind, norm)[0] == "found" else failed).append(label)
    for label, pat in spec["manual"]:
        (passed if re.search(pat, text, re.I) else failed).append(label)
    return {"passed": len(passed), "total": len(passed) + len(failed), "failed": failed}


async def run(models: list[str], pages: list[str] | None = None, reasoning: dict | None = None, tag: str = "") -> int:
    load_env_file()
    os.environ.setdefault("LLM_RUN_ID", "e1a")
    os.environ.setdefault("LLM_RUN_BUDGET_USD", "0.05")
    cache = ArtifactCache(ROOT / ".cache" / "artifacts")
    (OUT / "dumps").mkdir(parents=True, exist_ok=True)
    res_path = OUT / "results.json"
    results = json.loads(res_path.read_text()) if res_path.exists() else {}
    images = {s["id"]: page_image(s, cache) for s in PAGES}

    # free baseline: local OCR on the same images
    for s in PAGES:
        for eng in ("tesseract", "rapidocr"):
            r = run_ocr(eng, images[s["id"]], cache=cache, cache_params={"e1a": True})
            results[f"{s['id']}|local:{eng}"] = {"page": s["id"], "model": f"local:{eng}", **score(r.text, s),
                                                  "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "latency_ms": int(r.seconds * 1000)}

    client = LLMClient.from_env()
    sem = asyncio.Semaphore(4)

    async def one(model: str, s: dict):
        usage = UsageTracker()
        url = "data:image/png;base64," + b64encode(images[s["id"]]).decode()
        msgs = [{"role": "user", "content": [{"type": "text", "text": PROMPT},
                                             {"type": "image_url", "image_url": {"url": url}}]}]
        async with sem:
            try:
                extra = {"reasoning": reasoning} if reasoning else {}
                r = await client.chat(model=model, messages=msgs, stage="e1a_transcribe", doc_id=s["doc"],
                                      usage=usage, temperature=0, max_tokens=3000, **extra)
            except Exception as exc:  # noqa: BLE001 - record and continue
                results[f"{s['id']}|{model}{tag}"] = {"page": s["id"], "model": model + tag, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                return
        text = r.content or ""
        safe = model.replace("/", "_") + tag
        (OUT / "dumps" / f"{s['id']}__{safe}.md").write_text(text)
        tin, tout = r.usage.get("prompt_tokens", 0), r.usage.get("completion_tokens", 0)
        results[f"{s['id']}|{model}{tag}"] = {"page": s["id"], "model": model + tag, **score(text, s), "cost_usd": r.cost_usd,
                                         "tokens_in": tin, "tokens_out": tout, "latency_ms": r.latency_ms,
                                         "cached": r.cached, "finish": r.finish_reason,
                                         "reasoning_tokens": (r.usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}

    try:
        await asyncio.gather(*[one(m, s) for m in models for s in PAGES if not pages or s["id"] in pages])
    finally:
        await client.aclose()
    res_path.write_text(json.dumps(results, indent=1))

    print(f"{'page':16s} {'model':36s} {'pass':>7s} {'tok in':>7s} {'tok out':>7s} {'$':>9s} {'ms':>6s}  failed")
    for k, v in sorted(results.items(), key=lambda kv: (kv[1]["page"], kv[1]["model"])):
        if "error" in v:
            print(f"{v['page']:16s} {v['model']:36s} ERROR {v['error']}")
            continue
        print(f"{v['page']:16s} {v['model']:36s} {v['passed']:3d}/{v['total']:<3d} {v['tokens_in']:7d} {v['tokens_out']:7d} "
              f"{v['cost_usd']:9.6f} {v['latency_ms']:6d}  {', '.join(v['failed'])[:150]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=ROUND1)
    ap.add_argument("--pages", nargs="*")
    ap.add_argument("--reasoning", help='JSON, e.g. \'{"enabled": false}\'')
    ap.add_argument("--tag", default="", help="suffix for result keys, e.g. +noreason")
    a = ap.parse_args()
    return asyncio.run(run(a.models, a.pages, json.loads(a.reasoning) if a.reasoning else None, a.tag))


if __name__ == "__main__":
    raise SystemExit(main())
