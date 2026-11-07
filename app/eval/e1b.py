"""E1b: targeted model screen for extraction (single-entry documents: leases and the OM).

Question: given the right text, which model extracts each kind of field correctly, at what cost?
Design (notes/02-experiments.md §13 plus the four fixes agreed in revision):

  contexts   oracle = only the pages/sections the answers cite + 2 fixed distractor slices
             full   = the whole document (cheap models only, to measure the oracle→full gap)
  documents  lease_001 (local OCR; pages 2 and 25 from the cached E1a vision transcriptions),
             lease_002 (DOCX), om_001 (digital PDF); lease_003 oracle-only (renewal-option lists)
  arms       round 1: cheap models, luna with reasoning on and off
             round 2: stronger models / reasoning, only on the fields a base arm got wrong
  scoring    tools/validate.py cell matching; outcomes correct / partial / miss / wrong / hallucination;
             penalised score (wrong or invented = -1, alpha = 1); reported per field kind
  signals    production-available escalation signals (cross-model disagreement, null disagreement,
             citation not found in the context) evaluated against the real failures, for free

    python -m app.eval.e1b                       # dry run: prints the cost estimate, spends nothing
    python -m app.eval.e1b --go                  # round 1
    python -m app.eval.e1b --round2 --base luna-off --arms gemini-flash --go
    python -m app.eval.e1b --report              # tables + signal analysis from saved results ($0)
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import re
from pathlib import Path

from app.artifacts import ArtifactCache
from app.envfile import load_env_file
from app.eval.coverage import _COMPUTED_HINT, _locate, classify, normalise, normalise_with_map
from app.ingest.document import Document
from app.ingest.docx import ingest_docx
from app.ingest.pdf import ingest_pdf
from app.llm import LLMClient, LLMHTTPError, UsageTracker
from app.llm.pricing import estimate_cost

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
OUT = ROOT / "experiments" / "e1b"

_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

ARMS = {
    # round 1: cheap
    "luna-off": ("openai/gpt-6-luna", {"reasoning": {"enabled": False}}),
    "luna": ("openai/gpt-6-luna", {}),
    "haiku": ("anthropic/claude-haiku-5.5", {}),
    "deepseek": ("deepseek/deepseek-v3.2", {}),
    "glm": ("z-ai/glm-5.3-flash", {}),
    # round 2: escalation candidates
    "gemini-flash": ("google/gemini-3.8-flash", {}),
    "luna-high": ("openai/gpt-6-luna", {"reasoning": {"effort": "high"}}),
    "sonnet": ("anthropic/claude-sonnet-5.5", {}),
    "sol": ("openai/gpt-6.1-sol", {}),
    "gemini-pro": ("google/gemini-3.1-pro-preview", {}),
}
ROUND1 = ["luna-off", "luna", "haiku", "deepseek", "glm"]
DOCS = {  # doc -> contexts to run in round 1
    "lease_001": ("oracle", "full"),
    "lease_002": ("oracle", "full"),
    "om_001": ("oracle", "full"),
    "lease_003": ("oracle",),
}
VISION_PAGES = {"lease_001": {2: "lease_001_p2", 25: "lease_001_p25"}}
VISION_MODEL_DUMP = "openai_gpt-6-luna"

SYSTEM = """You extract fields from commercial real estate documents.

Return ONE JSON object. Its keys are exactly the field keys listed. Each value is an object:
  {"value": <the value>, "citation": "<short verbatim snippet from the text, prefixed with its location tag, e.g. [Page 3] or [Section 012: BASE RENT.]>"}

Rules for "value":
- Numbers as JSON numbers: no currency symbols, no thousands separators, no units.
- Dates as "YYYY-MM-DD".
- Percentages exactly as the document prints them (5.25 for "5.25%").
- Booleans as true / false. Lists as JSON arrays, in document order.
- Text exactly as written in the document unless the field description asks for a short phrase.
- Text marked ~~like this~~ is struck out and does not apply; [handwritten: ...] marks handwriting, which governs.
- If the document does not state the value, use null and an empty citation. Never guess or infer from market norms.
  A field whose description defines a value for absence (e.g. "0 if none", "false otherwise", "empty list if none") uses that value instead of null.
"""


# ------------------------------------------------------------------------------- documents

def _manifest() -> dict[str, dict]:
    import csv
    return {r["document_id"]: r for r in csv.DictReader(open(SAMPLE / "manifest.csv"))}


def load_doc(doc_id: str) -> Document:
    cache = ArtifactCache(ROOT / ".cache" / "artifacts")
    row = _manifest()[doc_id]
    path = SAMPLE / "sample-data-documents" / row["file"]
    if row["kind"] == "docx":
        return ingest_docx(path, ocr_engine="rapidocr", cache=cache)
    return ingest_pdf(path, text_engine="pdfplumber", ocr_engine="rapidocr", ocr_dpi=200, cache=cache, workers=3)


def units(doc: Document) -> list[tuple[str, str]]:
    """(unit id, text) in document order: pages for PDFs, section/chunk leaf slices for DOCX.
    Scanned pages listed in VISION_PAGES are replaced by the cached E1a vision transcription."""
    if doc.kind == "pdf":
        out = []
        for a in doc.anchors:
            if a.kind != "page":
                continue
            n = int(a.id[1:])
            vp = VISION_PAGES.get(doc.source.rsplit(".", 1)[0], {}).get(n)
            if vp:
                text = (ROOT / "experiments" / "e1a" / "dumps" / f"{vp}__{VISION_MODEL_DUMP}.md").read_text()
                out.append((a.id, f"[Page {n}]\n{text.strip()}\n\n"))
            else:
                out.append((a.id, doc.markdown[a.start:a.end]))
        return out
    marks = sorted((a for a in doc.anchors if a.kind in ("section", "chunk")), key=lambda a: a.start)
    out = [("pre", doc.markdown[: marks[0].start])] if marks and marks[0].start > 0 else []
    for i, a in enumerate(marks):
        end = marks[i + 1].start if i + 1 < len(marks) else len(doc.markdown)
        out.append((a.id, doc.markdown[a.start:end]))
    return out


def oracle_units(doc_id: str, us: list[tuple[str, str]], truth_entry: dict, fields: list[str] | None = None,
                 distractors: int = 2) -> list[str]:
    """Units holding the answers' citations (PDF: cited pages; DOCX: located citation snippets)."""
    wanted = fields or list(truth_entry)
    held: set[str] = set()
    if not any(uid.startswith("P") for uid, _ in us):   # DOCX: locate citations in section slices
        joined, offs, pos = "", [], 0
        for uid, text in us:
            offs.append((pos, uid))
            joined += text
            pos += len(text)
        norm, idx = normalise_with_map(joined)
        for k in wanted:
            fr = truth_entry[k]
            if fr.get("citation") and fr["value"] is not None:
                p = _locate(fr["citation"], norm)
                if p is not None:
                    raw = idx[p]
                    held.add(max((o for o in offs if o[0] <= raw), key=lambda o: o[0])[1])
    else:
        for k in wanted:
            for p in truth_entry[k].get("page_numbers") or []:
                held.add(f"P{p}")
    rest = [uid for uid, _ in us if uid not in held and uid != "pre"]
    held |= set(random.Random(doc_id).sample(rest, min(distractors, len(rest))))
    return [uid for uid, _ in us if uid in held]


def context_text(us: list[tuple[str, str]], keep: list[str] | None) -> str:
    return "".join(t for uid, t in us if keep is None or uid in keep)


# ------------------------------------------------------------------------------- prompt / parse

def build_messages(schema: dict, fields: list[str], text: str) -> list[dict]:
    spec = "\n".join(f'- "{k}" ({schema["fields"][k]["name"]}): {schema["fields"][k]["description"]}' for k in fields)
    user = (f"Document type: {schema.get('description', schema['name'])}\n\nFields to extract:\n{spec}\n\n"
            f"Document text:\n<<<\n{text}\n>>>")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse(content: str | None) -> dict | None:
    if not content:
        return None
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


# ------------------------------------------------------------------------------- scoring

def field_kind(key: str, schema: dict, truth_value) -> str:
    desc = schema["fields"][key]["description"]
    if _COMPUTED_HINT.search(desc):
        return "computed"
    if truth_value is None:
        return "null"
    if isinstance(truth_value, list):
        return "list"
    k = classify(truth_value)
    if k == "string" and (len(str(truth_value).split()) >= 4 or re.search(r"short phrase|in a short|sentence", desc, re.I)):
        return "freetext"
    return {"month": "date"}.get(k, k)


def score_fields(pred: dict | None, truth_entry: dict, schema: dict, fields: list[str], context: str) -> list[dict]:
    norm_ctx = normalise(context)
    out = []
    for k in fields:
        tv = truth_entry[k]["value"]
        fr = (pred or {}).get(k)
        pv = fr.get("value") if isinstance(fr, dict) else fr
        cite = fr.get("citation", "") if isinstance(fr, dict) else ""
        s = validate.cell_match(pv, tv)
        t_null, p_null = validate.norm(tv)[0] == "null", validate.norm(pv if not isinstance(pv, list) else (pv or None))[0] == "null"
        if isinstance(tv, list):
            t_null = len(tv) == 0
        if s == 1.0:
            outcome = "correct"
        elif s > 0:
            outcome = "partial"
        elif t_null and not p_null:
            outcome = "hallucination"
        elif not t_null and p_null:
            outcome = "miss"
        else:
            outcome = "wrong"
        penal = s if s > 0 else (-1.0 if outcome in ("wrong", "hallucination") else 0.0)
        snippet = normalise(re.sub(r"^\s*\[[^\]]*\]\s*", "", cite or ""))
        cite_ok = bool(snippet) and (snippet[:40] in norm_ctx)
        out.append({"field": k, "kind": field_kind(k, schema, tv), "truth": tv, "pred": pv, "score": s,
                    "penal": penal, "outcome": outcome, "cite_found": cite_ok})
    return out


# ------------------------------------------------------------------------------- runs

def plan_round1() -> list[dict]:
    jobs = []
    for doc_id, contexts in DOCS.items():
        for ctx in contexts:
            for arm in ROUND1:
                jobs.append({"arm": arm, "doc": doc_id, "ctx": ctx, "fields": None})
    return jobs


def plan_round2(results: dict, base: str, arms: list[str]) -> list[dict]:
    jobs = []
    for doc_id in DOCS:
        rec = results.get(f"{base}|{doc_id}|oracle")
        if not rec or not rec.get("fields"):
            continue
        failed = [f["field"] for f in rec["fields"] if f["outcome"] != "correct"]
        if failed:
            for arm in arms:
                jobs.append({"arm": arm, "doc": doc_id, "ctx": f"oracle-r2:{base}", "fields": failed})
    return jobs


def materialise(job: dict, cache_docs: dict) -> tuple[list[dict], str, dict, list[str]]:
    doc_id = job["doc"]
    if doc_id not in cache_docs:
        cache_docs[doc_id] = units(load_doc(doc_id))
    us = cache_docs[doc_id]
    row = _manifest()[doc_id]
    schema = json.loads((SAMPLE / "schemas" / f"{row['schema_name']}.json").read_text())
    truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{doc_id}.json").read_text())["entries"][0]
    fields = job["fields"] or list(schema["fields"])
    if job["ctx"] == "full":
        keep = None
    else:
        keep = oracle_units(doc_id, us, truth, job["fields"], distractors=2 if job["fields"] is None else 1)
    text = context_text(us, keep)
    return build_messages(schema, fields, text), text, schema, fields


def estimate(jobs: list[dict], cache_docs: dict) -> float:
    total = 0.0
    for j in jobs:
        msgs, _, _, fields = materialise(j, cache_docs)
        tin = sum(len(m["content"]) for m in msgs) // 4
        tout = 45 * len(fields) + (1200 if j["arm"] in ("luna", "haiku", "luna-high", "sonnet", "sol", "gemini-pro") else 0)
        total += estimate_cost(ARMS[j["arm"]][0], tin, tout) or 0.0
    return total


async def execute(jobs: list[dict], results: dict, cache_docs: dict) -> None:
    client = LLMClient.from_env()
    sem = asyncio.Semaphore(4)

    async def one(j: dict):
        model, extra = ARMS[j["arm"]]
        msgs, text, schema, fields = materialise(j, cache_docs)
        truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{j['doc']}.json").read_text())["entries"][0]
        usage = UsageTracker()
        key = f"{j['arm']}|{j['doc']}|{j['ctx']}"
        async with sem:
            for attempt in range(4):
                try:
                    r = await client.chat(model=model, messages=msgs, stage="e1b_extract", doc_id=j["doc"], usage=usage,
                                          temperature=0, max_tokens=8000, response_format={"type": "json_object"}, **extra)
                    break
                except LLMHTTPError as exc:
                    if exc.retryable and attempt < 3:
                        await asyncio.sleep(exc.retry_after or 2 ** (attempt + 1))
                        continue
                    results[key] = {"arm": j["arm"], "doc": j["doc"], "ctx": j["ctx"], "error": str(exc)[:300]}
                    return
                except Exception as exc:  # noqa: BLE001
                    results[key] = {"arm": j["arm"], "doc": j["doc"], "ctx": j["ctx"], "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                    return
        pred = parse(r.content)
        (OUT / "dumps").mkdir(parents=True, exist_ok=True)
        (OUT / "dumps" / f"{key.replace('|', '__').replace(':', '_')}.json").write_text(r.content or "")
        tin, tout = r.usage.get("prompt_tokens", 0), r.usage.get("completion_tokens", 0)
        results[key] = {
            "arm": j["arm"], "model": model, "doc": j["doc"], "ctx": j["ctx"], "parsed": pred is not None,
            "tokens_in": tin, "tokens_out": tout,
            "reasoning_tokens": (r.usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "cost_billed": r.cost_usd, "cost_list": estimate_cost(model, tin, tout), "latency_ms": r.latency_ms,
            "finish": r.finish_reason, "cached": r.cached, "context_tokens": len(text) // 4,
            "fields": score_fields(pred, truth, schema, fields, text),
        }

    try:
        await asyncio.gather(*[one(j) for j in jobs])
    finally:
        await client.aclose()


# ------------------------------------------------------------------------------- report

KINDS = ["string", "number", "date", "list", "bool", "computed", "freetext", "null"]


def report(results: dict) -> None:
    print("\n== per arm × context (penalised accuracy, alpha = 1; outcome counts)")
    print(f"{'arm':14s} {'ctx':22s} {'docs':>4s} {'penal':>6s} {'acc':>6s} {'corr':>5s} {'part':>4s} {'miss':>4s} {'wrong':>5s} "
          f"{'hall':>4s} {'unusable':>8s} {'$billed':>9s} {'$list':>9s} {'$list/doc':>9s} {'out tok':>8s} {'reas':>6s} {'s/doc':>6s}")
    groups: dict[tuple, list[dict]] = {}
    for rec in results.values():
        groups.setdefault((rec["arm"], rec["ctx"]), []).append(rec)
    for (arm, ctx), recs in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ok = [r for r in recs if "fields" in r]
        fs = [f for r in ok for f in r["fields"]]
        n = max(1, len(fs))
        cnt = lambda o: sum(f["outcome"] == o for f in fs)  # noqa: E731
        print(f"{arm:14s} {ctx:22s} {len(recs):4d} {sum(f['penal'] for f in fs) / n:6.3f} {sum(f['score'] for f in fs) / n:6.3f} "
              f"{cnt('correct'):5d} {cnt('partial'):4d} {cnt('miss'):4d} {cnt('wrong'):5d} {cnt('hallucination'):4d} "
              f"{sum(not r.get('parsed') for r in recs):8d} {sum(r.get('cost_billed') or 0 for r in ok):9.5f} "
              f"{sum(r.get('cost_list') or 0 for r in ok):9.5f} {sum(r.get('cost_list') or 0 for r in ok) / max(1, len(ok)):9.5f} "
              f"{sum(r.get('tokens_out', 0) for r in ok):8d} {sum(r.get('reasoning_tokens') or 0 for r in ok):6d} "
              f"{sum(r.get('latency_ms', 0) for r in ok) / max(1, len(ok)) / 1000:6.1f}")

    print("\n== per field kind (penalised accuracy; n = cells) for oracle and full contexts")
    for ctx in ("oracle", "full"):
        print(f"-- {ctx}")
        print(f"{'arm':14s} " + " ".join(f"{k:>10s}" for k in KINDS))
        for (arm, c), recs in sorted(groups.items()):
            if c != ctx:
                continue
            fs = [f for r in recs if "fields" in r for f in r["fields"]]
            cells = []
            for k in KINDS:
                xs = [f["penal"] for f in fs if f["kind"] == k]
                cells.append(f"{sum(xs) / len(xs):6.2f}({len(xs):2d})" if xs else f"{'-':>10s}")
            print(f"{arm:14s} " + " ".join(cells))

    errors = [r for r in results.values() if "error" in r]
    for r in errors:
        print(f"ERROR {r['arm']} {r['doc']} {r['ctx']}: {r['error'][:160]}")

    signals(results)


def signals(results: dict) -> None:
    """Would a production-available signal have flagged the fields a cheap arm got wrong?"""
    cheap = [a for a in ROUND1]
    by_cell: dict[tuple, dict[str, dict]] = {}
    for r in results.values():
        if r.get("ctx") != "oracle" or "fields" not in r or r["arm"] not in cheap:
            continue
        for f in r["fields"]:
            by_cell.setdefault((r["doc"], f["field"]), {})[r["arm"]] = f
    if not by_cell:
        return
    print("\n== escalation signals on oracle context (precision / recall of flagging a non-correct cell)")
    for target in cheap:
        rows = [(cell, arms) for cell, arms in by_cell.items() if target in arms]
        if not rows:
            continue

        def disagree(arms):
            vals = {json.dumps(validate.norm(a["pred"]) if not isinstance(a["pred"], list) else sorted(map(str, a["pred"]))) for a in arms.values()}
            return len(vals) > 1

        def null_split(arms):
            return len({a["pred"] is None or a["pred"] == [] for a in arms.values()}) > 1

        sigs = {
            "models disagree": lambda arms: disagree(arms),
            "null vs value split": lambda arms: null_split(arms),
            "citation not in text": lambda arms: arms[target]["pred"] not in (None, []) and not arms[target]["cite_found"],
            "any of the above": lambda arms: disagree(arms) or (arms[target]["pred"] not in (None, []) and not arms[target]["cite_found"]),
        }
        bad = [(c, a) for c, a in rows if a[target]["outcome"] != "correct"]
        line = []
        for name, fn in sigs.items():
            flagged = [(c, a) for c, a in rows if fn(a)]
            tp = sum(1 for c, a in flagged if a[target]["outcome"] != "correct")
            prec = tp / len(flagged) if flagged else 0.0
            rec = tp / len(bad) if bad else 1.0
            line.append(f"{name}: P={prec:.2f} R={rec:.2f} flags={len(flagged)}")
        print(f"{target:10s} bad={len(bad):3d}/{len(rows):3d}  " + " | ".join(line))


# ------------------------------------------------------------------------------- main

def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually spend (otherwise dry run)")
    ap.add_argument("--round2", action="store_true")
    ap.add_argument("--base", default="luna-off")
    ap.add_argument("--arms", nargs="*")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--cap", type=float, default=0.15, help="run budget cap in USD")
    args = ap.parse_args()

    res_path = OUT / "results.json"
    OUT.mkdir(parents=True, exist_ok=True)
    results = json.loads(res_path.read_text()) if res_path.exists() else {}
    if args.report:
        report(results)
        return 0

    cache_docs: dict = {}
    if args.round2:
        jobs = plan_round2(results, args.base, args.arms or ["gemini-flash"])
        os.environ.setdefault("LLM_RUN_ID", f"e1b-r2-{'-'.join(args.arms or ['gemini-flash'])}")
    else:
        jobs = [j for j in plan_round1() if not args.arms or j["arm"] in args.arms]
        os.environ.setdefault("LLM_RUN_ID", "e1b-r1")
    est = estimate(jobs, cache_docs)
    print(f"{len(jobs)} calls planned, estimated ${est:.4f} at list price (cap ${args.cap:.2f})")
    if not args.go:
        print("dry run: add --go to spend")
        return 0
    os.environ.setdefault("LLM_RUN_BUDGET_USD", str(args.cap))
    asyncio.run(execute(jobs, results, cache_docs))
    res_path.write_text(json.dumps(results, indent=1, default=str))
    report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
