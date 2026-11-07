"""Run every sample document through the real pipeline (v1) and score it with tools/validate.py.

    python -m app.eval.run_samples                 # dry run: cost estimate only
    python -m app.eval.run_samples --go            # run (cached calls cost $0), write outputs/, score
    LLM_CACHE_MODE=replay python -m app.eval.run_samples --go   # guaranteed $0 re-score after downstream changes

Writes:
  outputs/<doc>.json                    the submission outputs (computed-field mode H), with "schema"
  experiments/e2a/<M|C|H>/<doc>.json    the same responses post-processed three ways (E2a), scored side by side
  experiments/schema_specs/<schema>.json  stage-0 analysis per field list (types, rewritten descriptions, formulas)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from app.envfile import load_env_file
from app.ingest.pdf import is_scanned_page, page_texts
from app.llm.pricing import estimate_cost
from app.llm.usage import UsageTracker
from app.pipeline.base import JobContext, build_result
from app.pipeline.schema_analysis import heuristic_spec
from app.pipeline.single import build_messages
from app.pipeline.v1 import PipelineV1, V1Config

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)


def manifest() -> list[dict]:
    return list(csv.DictReader(open(SAMPLE / "manifest.csv")))


def estimate(rows: list[dict], cfg: V1Config) -> tuple[float, list[str]]:
    total, lines, seen_schemas = 0.0, [], set()
    for r in rows:
        schema = json.loads((SAMPLE / "schemas" / f"{r['schema_name']}.json").read_text())
        path = SAMPLE / "sample-data-documents" / r["file"]
        cost = 0.0
        if r["schema_name"] not in seen_schemas:
            seen_schemas.add(r["schema_name"])
            cost += estimate_cost(cfg.analysis_model, 1500 + len(json.dumps(schema)) // 4, 220 * len(schema["fields"])) or 0
        spec = heuristic_spec(schema)
        if not spec.table and r["kind"] in ("pdf", "docx"):
            if r["kind"] == "pdf":
                texts = page_texts(path, cfg.pdf_text_engine)
                scanned = sum(is_scanned_page(t) for t in texts)
                cost += scanned * (estimate_cost(cfg.vision_model, 1900, 900) or 0)
                doc_tokens = sum(len(t) for t in texts) // 4 + scanned * 1000
            else:
                doc_tokens = 19000
                scanned = 0
            prompt = sum(len(m["content"]) for m in build_messages(spec, "")) // 4 + doc_tokens
            cost += estimate_cost(cfg.extract_model, prompt, 60 * len(spec.fields)) or 0
            lines.append(f"  {r['document_id']:14s} scanned pages={scanned:3d} prompt≈{prompt:6d} tok  ${cost:.4f}")
        elif r["kind"] in ("xlsx", "csv", "pdf"):
            from app.ingest.grid import grid_from_pdf, load_grid
            from app.pipeline.table import plan_messages
            grid = grid_from_pdf(path) if r["kind"] == "pdf" else load_grid(path, r["kind"])
            prompt = sum(len(m["content"]) for m in plan_messages(spec, grid)) // 4
            cost += (estimate_cost(cfg.table_model, prompt, 2500) or 0) * 2      # plan + possible repair
            cost += estimate_cost(cfg.table_model, 3000, 1500) or 0               # classify, if used
            lines.append(f"  {r['document_id']:14s} table plan prompt≈{prompt:6d} tok             ${cost:.4f}")
        else:
            lines.append(f"  {r['document_id']:14s} (PDF table: grid not built yet)              ${cost:.4f}")
        total += cost
    return total, lines


async def run(rows: list[dict], modes: list[str]) -> dict:
    pipe = PipelineV1()
    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    specs_dir = ROOT / "experiments" / "schema_specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(3)
    report = {}

    async def one(r: dict):
        schema = json.loads((SAMPLE / "schemas" / f"{r['schema_name']}.json").read_text())
        path = SAMPLE / "sample-data-documents" / r["file"]
        ctx = JobContext(job_id=f"sample-{r['document_id']}", org="", file_name=r["file"], file_type=r["kind"],
                         deadline=time.monotonic() + 1800, usage=UsageTracker())
        t0 = time.time()
        async with sem:
            try:
                ex = await pipe.extract(path, schema, ctx)
            except Exception as exc:  # noqa: BLE001 - report per document, keep going
                report[r["document_id"]] = {"notes": [f"ERROR {type(exc).__name__}: {str(exc)[:200]}"],
                                            "meta": ctx.usage.to_metadata(0)}
                return
        latency = int((time.time() - t0) * 1000)
        (specs_dir / f"{r['schema_name']}.json").write_text(json.dumps(asdict(ex.spec), indent=1, default=str))
        report[r["document_id"]] = {"notes": ex.notes, "meta": ctx.usage.to_metadata(latency)}
        for mode in modes:
            res = build_result(schema, pipe.rows(ex, mode), ctx.usage, latency)
            res["schema"] = r["schema_name"]
            d = ROOT / "experiments" / "e2a" / mode
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{r['document_id']}.json").write_text(json.dumps(res, indent=1))
            if mode == pipe.cfg.computed_mode:
                (out_dir / f"{r['document_id']}.json").write_text(json.dumps(res, indent=1))

    try:
        await asyncio.gather(*(one(r) for r in rows))
    finally:
        await pipe.client.aclose()
    return report


def score_dir(d: Path, rows: list[dict]) -> dict[str, dict]:
    out = {}
    for r in rows:
        p = d / f"{r['document_id']}.json"
        if not p.exists():
            continue
        res = json.loads(p.read_text())
        schema = json.loads((SAMPLE / "schemas" / f"{r['schema_name']}.json").read_text())
        errs = validate.validate_contract(res, schema)
        truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{r['document_id']}.json").read_text())
        out[r["document_id"]] = {"valid": not errs, "errors": errs[:3], **validate.score(res, truth, schema)}
    return out


def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--docs", nargs="*")
    ap.add_argument("--modes", nargs="*", default=["H", "M", "C"])
    ap.add_argument("--cap", type=float, default=0.08)
    args = ap.parse_args()
    rows = [r for r in manifest() if not args.docs or r["document_id"] in args.docs]
    cfg = V1Config.from_env()

    est, lines = estimate(rows, cfg)
    print(f"models: analysis={cfg.analysis_model} extract={cfg.extract_model} vision={cfg.vision_model} "
          f"computed={cfg.computed_mode}  cache={os.environ.get('LLM_CACHE_MODE', 'read_write')}")
    print("\n".join(lines))
    print(f"estimated ≤ ${est:.4f} at list price before cache hits (run cap ${args.cap:.2f})")
    if not args.go:
        print("dry run: add --go to run")
        return 0

    os.environ.setdefault("LLM_RUN_ID", "samples-" + time.strftime("%Y%m%dT%H%M%S"))
    os.environ.setdefault("LLM_RUN_BUDGET_USD", str(args.cap))
    report = asyncio.run(run(rows, args.modes))

    print(f"\n{'doc':14s} {'mode':4s} {'valid':5s} {'score':>6s} {'rows':>9s} {'halluc':>6s} {'miss':>4s}   calls  $real    $uncached  s")
    scores = {m: score_dir(ROOT / "experiments" / "e2a" / m, rows) for m in args.modes}
    for r in rows:
        did = r["document_id"]
        meta = report.get(did, {}).get("meta", {})
        for m in args.modes:
            s = scores[m].get(did)
            if not s:
                continue
            print(f"{did:14s} {m:4s} {str(s['valid']):5s} {s['score']:6.3f} {s['rows']['predicted']:4d}/{s['rows']['truth']:<4d} "
                  f"{len(s['hallucinations']):6d} {len(s['misses']):4d}   {meta.get('model_calls', 0):5d} "
                  f"{meta.get('cost_usd', 0):.4f}  {meta.get('cost_usd_uncached', 0):.4f}  {meta.get('latency_ms', 0) / 1000:.0f}")
        for n in report.get(did, {}).get("notes", []):
            print(f"{'':14s} note: {n}")
    for m in args.modes:
        vals = [s["score"] for s in scores[m].values()]
        print(f"MEAN {m}: {sum(vals) / len(vals):.3f} over {len(vals)} docs")
    Path(ROOT / "experiments" / "e2a" / "scores.json").write_text(json.dumps(scores, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
