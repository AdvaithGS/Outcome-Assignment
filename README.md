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
`WORKER_CONCURRENCY=8` or `docker compose up --scale worker=3`.

## Sample-set scores (`tools/validate.py`)

```bash
python tools/validate.py outputs/ --schema-dir sample/schemas --truth-dir sample/sample-data-output-validated
```

| Document | Score | Hallucinations | Misses | Rows |
|---|---|---|---|---|
| boxscore_001 | 1.000 | 0 | 0 | 10/10 |
| budget_001 | 0.999 | 0 | 0 | 55/55 |
| lease_001 | 0.900 | 0 | 0 | 1/1 |
| lease_002 | 0.825 | 0 | 0 | 1/1 |
| lease_003 | 0.825 | 0 | 0 | 1/1 |
| om_001 | 0.966 | 0 | 0 | 1/1 |
| rentroll_001 | 0.998 | 0 | 0 | 325/325 |
| t12_001 | 0.998 | 0 | 0 | 119/119 |
| **Mean** | **0.939** | | | |

All 8 outputs are VALID. Regenerate them with `python -m app.eval.run_samples --go`;
`LLM_CACHE_MODE=replay` reproduces them from cached responses at $0 (and fails loudly if a prompt changed).

## Tests

```bash
pip install -r requirements-dev.txt
pytest          # 93 tests: service guarantees, retries/timeouts, cache, ingestion, formulas, table plans
```
