"""LLM judge for text fields, closer to the graders' scorer (which uses an LLM judge on free text).

    python -m app.eval.judge experiments/e2/verbatim [more dirs...] [--go]

For each output dir, text/free-text cells that fail the strict validator match (both values non-null) are judged
once per distinct (field, reference, prediction), cached in experiments/e2/judge_verdicts.json. The judge is
claude-haiku-5.5, a different family from the default extractor (gpt-6-luna), to avoid self-preference.
Reports per document: strict score and judged score (judged-equivalent cells count as correct).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import os
from pathlib import Path

from app.envfile import load_env_file
from app.llm import LLMClient
from app.pipeline.schema_analysis import parse_json

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
VERDICTS = ROOT / "experiments" / "e2" / "judge_verdicts.json"
JUDGE_MODEL = "anthropic/claude-haiku-5.5"
_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

PROMPT = """You grade values extracted from commercial real estate documents against a reference answer.
For each item decide whether the PREDICTION states the same facts as the REFERENCE: the same amounts, percentages,
dates, parties, conditions and limits. Wording, abbreviation, order and harmless extra detail do not matter.
It is NOT equivalent if it omits or changes a number, percentage, condition or party that the reference states, or
asserts a different or additional condition that changes the meaning.
Return ONE JSON object mapping each item number (as a string) to true or false."""


def _key(field: str, truth, pred) -> str:
    return json.dumps([field, truth, pred], sort_keys=True)


def cells(out_dir: Path) -> list[tuple[str, str, object, object, float]]:
    """(doc, field, truth, pred, strict score) for every single-entry cell in out_dir."""
    man = {r["document_id"]: r for r in csv.DictReader(open(SAMPLE / "manifest.csv"))}
    out = []
    for p in sorted(out_dir.glob("*.json")):
        if p.stem not in man or p.name == "scores.json":
            continue
        res = json.loads(p.read_text())
        truth = json.loads((SAMPLE / "sample-data-output-validated" / p.name).read_text())
        if len(truth["entries"]) != 1:
            continue                                      # tables: judged elsewhere if ever needed
        t, e = truth["entries"][0], res["entries"][0]
        for k, fr in t.items():
            tv, pv = fr.get("value"), e.get(k, {}).get("value")
            out.append((p.stem, k, tv, pv, validate.cell_match(pv, tv)))
    return out


def judgeable(c) -> bool:
    _, _, tv, pv, s = c
    return s < 1 and isinstance(tv, str) and isinstance(pv, str) and validate.norm(tv)[0] == "str"


async def judge(pairs: list[tuple[str, object, object]], verdicts: dict) -> None:
    todo = [p for p in pairs if _key(*p) not in verdicts]
    if not todo:
        return
    client = LLMClient.from_env()
    try:
        for i in range(0, len(todo), 30):
            chunk = todo[i:i + 30]
            items = "\n".join(f"{n}. field={f}\n   REFERENCE: {t}\n   PREDICTION: {p}" for n, (f, t, p) in enumerate(chunk))
            r = await client.chat(model=JUDGE_MODEL, messages=[{"role": "system", "content": PROMPT},
                                                                {"role": "user", "content": items}],
                                  stage="e2_judge", temperature=0, max_tokens=3000, timeout_s=90,
                                  response_format={"type": "json_object"})
            got = parse_json(r.content) or {}
            for n, pair in enumerate(chunk):
                if isinstance(got.get(str(n)), bool):
                    verdicts[_key(*pair)] = got[str(n)]
    finally:
        await client.aclose()


def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--go", action="store_true")
    args = ap.parse_args()
    verdicts = json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}
    all_cells = {d: cells(Path(d)) for d in args.dirs}
    pairs = sorted({(c[1], c[2], c[3]) for cs in all_cells.values() for c in cs if judgeable(c)}, key=str)
    todo = [p for p in pairs if _key(*p) not in verdicts]
    print(f"{len(pairs)} judgeable pairs, {len(todo)} not yet judged (~{-(-len(todo) // 30)} calls)")
    if todo and args.go:
        os.environ.setdefault("LLM_RUN_ID", "e2-judge")
        asyncio.run(judge(todo, verdicts))
        VERDICTS.parent.mkdir(parents=True, exist_ok=True)
        VERDICTS.write_text(json.dumps(verdicts, indent=1, sort_keys=True))
    for d, cs in all_cells.items():
        docs = sorted({c[0] for c in cs})
        line = []
        for doc in docs:
            dc = [c for c in cs if c[0] == doc]
            strict = sum(c[4] for c in dc) / len(dc)
            judged = sum(1.0 if (judgeable(c) and verdicts.get(_key(c[1], c[2], c[3]))) else c[4] for c in dc) / len(dc)
            line.append(f"{doc} {strict:.3f}/{judged:.3f}")
        allc = [c for c in cs]
        s_mean = sum(sum(c[4] for c in cs if c[0] == doc) / len([c for c in cs if c[0] == doc]) for doc in docs) / len(docs)
        j_mean = sum(sum(1.0 if (judgeable(c) and verdicts.get(_key(c[1], c[2], c[3]))) else c[4]
                         for c in cs if c[0] == doc) / len([c for c in cs if c[0] == doc]) for doc in docs) / len(docs)
        print(f"{Path(d).name:12s} strict {s_mean:.3f}  judged {j_mean:.3f}   " + "  ".join(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
