"""Load test against a running service, shaped like the graders' harness.

    python -m app.eval.loadtest --base http://localhost:8080 --jobs 16 --inflight 10 --tag baseline

Keeps `--inflight` jobs in flight and submits a new one as soon as one finishes, polling every 2 s. Jobs cycle over
the sample documents; each submission uses its own X-Organization-Id so identical documents are separate jobs (and
tenant isolation is exercised). Records per job: POST latency, time from submission to a terminal state, status, and
whether the result passes the output contract. Writes experiments/load/<tag>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample"
_spec = importlib.util.spec_from_file_location("validate", ROOT / "tools" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))] if xs else 0.0


async def main_async(base: str, n_jobs: int, inflight: int, tag: str, timeout_s: float) -> dict:
    docs = list(csv.DictReader(open(SAMPLE / "manifest.csv")))
    results: list[dict] = []
    sem = asyncio.Semaphore(inflight)
    t_start = time.time()

    async def one(i: int, client: httpx.AsyncClient):
        d = docs[i % len(docs)]
        org = f"load-{tag}-{i}"
        schema_text = (SAMPLE / "schemas" / f"{d['schema_name']}.json").read_text()
        async with sem:
            t0 = time.time()
            with open(SAMPLE / "sample-data-documents" / d["file"], "rb") as f:
                r = await client.post(f"{base}/jobs", files={"file": (d["file"], f.read())},
                                      data={"schema": schema_text}, headers={"X-Organization-Id": org})
            post_ms = (time.time() - t0) * 1000
            rec = {"i": i, "doc": d["document_id"], "org": org, "post_status": r.status_code, "post_ms": round(post_ms)}
            if r.status_code != 202:
                rec.update(status="REJECTED", body=r.text[:200])
                results.append(rec)
                return
            jid = r.json()["job_id"]
            rec["job_id"] = jid
            while time.time() - t0 < timeout_s:
                await asyncio.sleep(2)
                g = await client.get(f"{base}/jobs/{jid}", headers={"X-Organization-Id": org})
                body = g.json()
                if body["status"] in ("SUCCESS", "PARTIAL", "FAILED"):
                    rec.update(status=body["status"], latency_s=round(time.time() - t0, 1), error=body.get("error"))
                    if body.get("result"):
                        schema = json.loads(schema_text)
                        rec["valid"] = not validate.validate_contract(body["result"], schema)
                        meta = body["result"]["metadata"]
                        rec.update(cost_usd=meta.get("cost_usd"), model_calls=meta.get("model_calls"),
                                   server_latency_ms=meta.get("latency_ms"))
                        truth = json.loads((SAMPLE / "sample-data-output-validated" / f"{d['document_id']}.json").read_text())
                        rec["score"] = validate.score(body["result"], truth, schema)["score"]
                    # tenant isolation: another org must not see this job
                    other = await client.get(f"{base}/jobs/{jid}", headers={"X-Organization-Id": "someone-else"})
                    rec["isolation_ok"] = other.status_code == 404
                    break
            else:
                rec.update(status="TIMEOUT", latency_s=round(time.time() - t0, 1))
            results.append(rec)

    async with httpx.AsyncClient(timeout=60) as client:
        await asyncio.gather(*(one(i, client) for i in range(n_jobs)))

    lat = [r["latency_s"] for r in results if "latency_s" in r and r["status"] != "TIMEOUT"]
    ids = [r.get("job_id") for r in results if r.get("job_id")]
    summary = {
        "tag": tag, "jobs": n_jobs, "inflight": inflight, "wall_s": round(time.time() - t_start, 1),
        "statuses": {s: sum(1 for r in results if r["status"] == s) for s in {r["status"] for r in results}},
        "valid": sum(1 for r in results if r.get("valid")), "duplicate_job_ids": len(ids) - len(set(ids)),
        "isolation_ok": all(r.get("isolation_ok", True) for r in results),
        "post_ms_max": max((r["post_ms"] for r in results), default=0),
        "latency_p50_s": round(statistics.median(lat), 1) if lat else None, "latency_p95_s": round(pct(lat, 95), 1),
        "latency_max_s": max(lat, default=None),
        "cost_usd_total": round(sum(r.get("cost_usd") or 0 for r in results), 4),
        "mean_score": round(statistics.mean([r["score"] for r in results if "score" in r]), 3) if results else None,
        "by_doc": {}, "jobs_detail": sorted(results, key=lambda r: r["i"]),
    }
    for doc in sorted({r["doc"] for r in results}):
        rs = [r for r in results if r["doc"] == doc and "latency_s" in r]
        summary["by_doc"][doc] = {"latency_s": [r["latency_s"] for r in rs], "cost_usd": [r.get("cost_usd") for r in rs],
                                  "score": [r.get("score") for r in rs], "status": [r["status"] for r in rs]}
    out = ROOT / "experiments" / "load"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{tag}.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--inflight", type=int, default=10)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--timeout", type=float, default=1800)
    a = ap.parse_args()
    s = asyncio.run(main_async(a.base, a.jobs, a.inflight, a.tag, a.timeout))
    print(json.dumps({k: v for k, v in s.items() if k not in ("jobs_detail", "by_doc")}, indent=1))
    for doc, x in s["by_doc"].items():
        print(f"  {doc:14s} latency {x['latency_s']} s  cost {x['cost_usd']}  score {x['score']}  {x['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
