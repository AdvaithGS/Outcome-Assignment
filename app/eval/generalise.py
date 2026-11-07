"""E5: generalisation to field lists and formats the pipeline has not seen.

    python -m app.eval.generalise            # dry run (lists variants)
    python -m app.eval.generalise --go

Variants are built from the samples; predictions are mapped back to the answer file's keys and scored with
tools/validate.py on the fields the variant shares with the answers. Derived files (CSV conversions) live in .cache/e5
(gitignored: they contain sample data); scores go to experiments/e5/scores.json.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import os
import time
from pathlib import Path

from app.envfile import load_env_file
from app.llm.usage import UsageTracker
from app.pipeline.base import JobContext, build_result
from app.pipeline.v1 import PipelineV1

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
DERIVED = ROOT / ".cache" / "e5"
OUT = ROOT / "experiments" / "e5"
_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)


def load(name: str) -> dict:
    return json.loads((SAMPLE / "schemas" / f"{name}.json").read_text())


def terse_renamed(schema: dict) -> tuple[dict, dict]:
    """Keys → field_NN (no meaning), descriptions → the display name only. Returns (schema, new_key → original key)."""
    fields, mapping = {}, {}
    for i, (k, f) in enumerate(schema["fields"].items(), start=1):
        nk = f"field_{i:02d}"
        fields[nk] = {"name": f["name"], "description": f["name"], **({"primary": True} if f.get("primary") else {})}
        mapping[nk] = k
    return {"name": "unnamed", "fields": fields}, mapping


def identity(schema: dict) -> tuple[dict, dict]:
    return schema, {k: k for k in schema["fields"]}


def contract_boxscore() -> tuple[dict, dict]:
    schema = json.loads((ROOT / "contract" / "schema.example.json").read_text())
    shared = {"property_name": "property_name", "report_date": "date", "code": "code", "units": "units",
              "avg_rent": "avg_rent", "percent_leased": "percent_leased"}     # unit_type / vacant have no answer key
    return schema, shared


def rentroll_csv() -> Path:
    import openpyxl
    DERIVED.mkdir(parents=True, exist_ok=True)
    out = DERIVED / "rentroll_001.csv"
    if not out.exists():
        ws = openpyxl.load_workbook(SAMPLE / "sample-data-documents" / "rentroll_001.xlsx", data_only=True).worksheets[0]
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            for row in ws.iter_rows(values_only=True):
                w.writerow(["" if v is None else (v.strftime("%m/%d/%Y") if hasattr(v, "strftime") else v) for v in row])
    return out


def image_only_pdf(name: str, dpi: int = 200) -> Path:
    """A scanned copy of a sample PDF: every page rendered to an image, no text layer (known answers, real scan path)."""
    import pymupdf
    DERIVED.mkdir(parents=True, exist_ok=True)
    out = DERIVED / f"{name}_scanned.pdf"
    if not out.exists():
        src = pymupdf.open(SAMPLE / "sample-data-documents" / f"{name}.pdf")
        dst = pymupdf.open()
        for page in src:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            p = dst.new_page(width=page.rect.width, height=page.rect.height)
            p.insert_image(p.rect, pixmap=pix)
        dst.save(out)
    return out


def rentroll_two_sheets() -> Path:
    """The rent roll split across two sheets by building (header block repeated on each)."""
    import openpyxl
    DERIVED.mkdir(parents=True, exist_ok=True)
    out = DERIVED / "rentroll_001_two_sheets.xlsx"
    if not out.exists():
        src = openpyxl.load_workbook(SAMPLE / "sample-data-documents" / "rentroll_001.xlsx", data_only=True).worksheets[0]
        rows = [[c.value for c in row] for row in src.iter_rows()]
        split = next(i for i, r in enumerate(rows) if i >= 7 and isinstance(r[0], str) and r[0].startswith("200-"))
        wb = openpyxl.Workbook()
        a = wb.active
        a.title = "Bldg 100"
        b = wb.create_sheet("Bldg 200")
        for r in rows[:7] + rows[7:split]:
            a.append(r)
        for r in rows[:7] + rows[split:]:
            b.append(r)
        wb.save(out)
    return out


def variants() -> list[dict]:
    d = SAMPLE / "sample-data-documents"
    return [
        {"name": "lease_002 terse+renamed", "doc": "lease_002", "file": d / "lease_002.docx", "type": "docx",
         "schema": terse_renamed(load("lease_abstraction"))},
        {"name": "lease_001 terse+renamed", "doc": "lease_001", "file": d / "lease_001.pdf", "type": "pdf",
         "schema": terse_renamed(load("lease_abstraction"))},
        {"name": "boxscore contract schema", "doc": "boxscore_001", "file": d / "boxscore_001.csv", "type": "csv",
         "schema": contract_boxscore()},
        {"name": "rentroll as CSV", "doc": "rentroll_001", "file": None, "type": "csv",
         "schema": identity(load("rent_roll_units"))},
        {"name": "t12 terse+renamed", "doc": "t12_001", "file": d / "t12_001.xlsx", "type": "xlsx",
         "schema": terse_renamed(load("operating_statement_lines"))},
        {"name": "rentroll split across two sheets", "doc": "rentroll_001", "file": "two_sheets", "type": "xlsx",
         "schema": identity(load("rent_roll_units"))},
        {"name": "budget scanned (image-only PDF)", "doc": "budget_001", "file": "scan:budget_001", "type": "pdf",
         "schema": identity(load("budget_variance"))},
    ]


def score(result: dict, mapping: dict, doc: str) -> dict:
    truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{doc}.json").read_text())
    orig_schema = load(next(r["schema_name"] for r in csv.DictReader(open(SAMPLE / "manifest.csv")) if r["document_id"] == doc))
    keep = set(mapping.values())
    schema = {"fields": {k: v for k, v in orig_schema["fields"].items() if k in keep}}
    pred = {"entries": [{mapping[k]: v for k, v in e.items() if k in mapping} for e in result["entries"]],
            "metadata": result["metadata"]}
    tr = {"entries": [{k: v for k, v in e.items() if k in keep} for e in truth["entries"]]}
    return validate.score(pred, tr, schema)


async def run_all(only: str | None = None) -> list[dict]:
    pipe = PipelineV1()
    out = []
    try:
        for v in variants():
            if only and only.lower() not in v["name"].lower():
                continue
            path = v["file"] or rentroll_csv()
            if isinstance(path, str) and path.startswith("scan:"):
                path = image_only_pdf(path.split(":", 1)[1])
            elif path == "two_sheets":
                path = rentroll_two_sheets()
            schema, mapping = v["schema"]
            ctx = JobContext(job_id=f"e5-{v['doc']}", org="", file_name=Path(path).name, file_type=v["type"],
                             deadline=time.monotonic() + 1800, usage=UsageTracker())
            t0 = time.time()
            try:
                ex = await pipe.extract(Path(path), schema, ctx)
                res = build_result(schema, pipe.rows(ex), ctx.usage, int((time.time() - t0) * 1000))
                sc = score(res, mapping, v["doc"])
                out.append({"variant": v["name"], "score": sc["score"], "rows": sc["rows"],
                            "hallucinations": len(sc["hallucinations"]), "misses": len(sc["misses"]),
                            "per_field_below_1": {k: x for k, x in sc["per_field"].items() if x < 1},
                            "notes": ex.notes, "cost_usd": ctx.usage.to_metadata(0)["cost_usd"]})
            except Exception as exc:  # noqa: BLE001
                out.append({"variant": v["name"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    finally:
        await pipe.client.aclose()
    return out


def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--cap", type=float, default=0.04)
    ap.add_argument("--only", help="run only variants whose name contains this text")
    args = ap.parse_args()
    for v in variants():
        print(f"  {v['name']:28s} {len(v['schema'][0]['fields'])} fields, {len(v['schema'][1])} scored")
    if not args.go:
        print("dry run: add --go to run (est. ≈ $0.02)")
        return 0
    os.environ.setdefault("LLM_RUN_ID", "e5-generalise")
    os.environ.setdefault("LLM_RUN_BUDGET_USD", str(args.cap))
    results = asyncio.run(run_all(args.only))
    OUT.mkdir(parents=True, exist_ok=True)
    name = "scores.json" if not args.only else f"scores_{args.only.replace(' ', '_')}.json"
    (OUT / name).write_text(json.dumps(results, indent=1, default=str))
    for r in results:
        if "error" in r:
            print(f"{r['variant']:28s} ERROR {r['error']}")
            continue
        print(f"{r['variant']:28s} score={r['score']:.3f} rows={r['rows']['predicted']}/{r['rows']['truth']} "
              f"halluc={r['hallucinations']} miss={r['misses']} ${r['cost_usd']:.4f}  weak: "
              + ", ".join(f"{k} {x:.2f}" for k, x in sorted(r['per_field_below_1'].items(), key=lambda kv: kv[1])[:6]))
        for n in r["notes"]:
            print(f"{'':28s} note: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
