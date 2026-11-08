# Document Extraction Service

**Role applied for:** AI/ML Developer

Takes a document (PDF, DOCX, XLSX or CSV) and a field list, returns the extracted values as JSON with citations.
Design and decisions: [`DESIGN.md`](DESIGN.md). Full experiment record: [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md).

## Run

```bash
export LLM_BASE_URL=https://openrouter.ai/api/v1 LLM_API_KEY=...   # or put them in a local .env file
docker compose up                                                  # API on :8080 + one worker
curl localhost:8080/health
```

Submit a document and poll it:

```bash
curl -s -X POST localhost:8080/jobs -F file=@lease.pdf -F schema="$(cat schema.json)"
# → 202 {"job_id": "…", "status": "PENDING", …}
curl -s localhost:8080/jobs/<job_id>
# → {"status": "SUCCESS", "result": {"entries": [...], "metadata": {...}}, …}
```

Optional: `X-Organization-Id` header (tenant isolation), `webhook_url` form field. Scale with
`WORKER_CONCURRENCY=20` (default 10 jobs per worker) on one machine, or `docker compose up --scale worker=3` when the
workers get more CPU (two containers sharing one laptop's cores were slower: scanned pages are OCR'd on the CPU).

## Settings

Read from the shell environment (or a local `.env`) by `docker compose`; defaults are the tested configuration.

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY` | OpenRouter, — | Model gateway and key (key required) |
| `WORKER_CONCURRENCY` | 10 | Jobs each worker runs at once |
| `LLM_MAX_CONCURRENCY` | 24 | Concurrent model calls per worker |
| `JOB_DEADLINE_S` | 1200 | Internal deadline per job, from submission (the brief allows 30 min) |
| `LLM_TIMEOUT_S` | 90 | Total time cap per model-call attempt |
| `LLM_CACHE_MODE` | `read_write` | Response cache: `read_write`, `off`, `replay` (never spends), `refresh` |
| `EXTRACT_MODEL`, `VISION_MODEL`, `TABLE_MODEL`, `ANALYSIS_MODEL` | `openai/gpt-6-luna` | Model per stage (reasoning off) |
| `FALLBACK_MODEL` | `anthropic/claude-haiku-5.5` | Used when a model's output is unusable; last table-plan repair |
| `SCANNED_MODE` | `selective` | `selective`: Tesseract first, vision for hard pages; `vision`: every page; `ocr`: no vision |
| `FREETEXT_MODE` | `concise` | Free-text answers: `concise`, `verbatim` (quotes) or `describe` |
| `OCR_WORKERS` | CPUs − 2 | Size of the page-rendering / OCR pool (runs at low priority) |

## Sample-set scores (`tools/validate.py`)

```bash
python tools/validate.py outputs/ --schema-dir sample/schemas --truth-dir sample/sample-data-output-validated
```

| Document | Score | Hallucinations | Misses | Rows |
|---|---|---|---|---|
| boxscore_001 | 1.000 | 0 | 0 | 10/10 |
| budget_001 | 0.999 | 0 | 0 | 55/55 |
| lease_001 | 0.850 | 0 | 0 | 1/1 |
| lease_002 | 0.825 | 0 | 0 | 1/1 |
| lease_003 | 0.850 | 0 | 0 | 1/1 |
| om_001 | 0.966 | 0 | 0 | 1/1 |
| rentroll_001 | 1.000 | 0 | 0 | 325/325 |
| t12_001 | 0.999 | 0 | 0 | 119/119 |
| **Mean** | **0.936** | | | |

All 8 outputs are VALID. Uncached cost **~$0.004 per document** on average (scanned 25-page lease ~$0.007–0.009,
tables ~$0.0005–0.003). Load test in Docker from a fresh clone, 10 jobs in flight, cache off: 16/16 SUCCESS, p50 25 s,
p95 94 s, `POST /jobs` ≤ 95 ms, none lost or duplicated (`experiments/load/final-wc10-lmc24.json`).

The `outputs/` files are regenerated with `python -m app.eval.run_samples --go`; with `LLM_CACHE_MODE=replay` this
reproduces them from cached responses at $0 (and fails loudly if a prompt changed). With an LLM judge on free-text
fields (the graders' scorer is "slightly more lenient" there) the sample mean is 0.964.

## Tests

```bash
pip install -r requirements-dev.txt
pytest          # 119 tests: service guarantees, retries/timeouts, cache, ingestion, normalisation, formulas,
                #            table plans, large field lists, scanner OCR layers, bad / empty / failed inputs
```
