# Step 3: End-to-end pipeline and service skeleton

Status: **IDEATION** (user's idea: build the rest of the pipeline now, and use the cached responses to produce
full outputs, so the pipeline is assessed by the real validator while it is being built).

---

## 1. Why now

| Benefit | How |
|---|---|
| **Official score from day one** | `outputs/<doc>.json` → `tools/validate.py --truth-dir …`, not our proxy metrics |
| **Integration bugs surface early** | Normalisation, every-entry-carries-every-field, list alignment, page_numbers, metadata |
| **True $/doc end to end** | UsageTracker metadata per document, cached vs uncached |
| **The gates are met early** | Always a submittable version; later work only raises the score |
| **Cheap** | Single-entry docs reuse the E1b full-context prompts exactly → cache hits → $0 |

## 2. Two layers, built in order

### Layer 1: the pipeline (in-process, no HTTP), `app/pipeline/`
```
run(file, schema) -> result
  1. triage       file type by content (magic bytes), not the extension
  2. ingest       PDF: pdfplumber + per-page scan detection → RapidOCR (+ vision on chosen pages later)
                  DOCX: XML parser + image OCR; XLSX/CSV: stub for now
  3. schema       shape: table if a field has "primary": true, else single entry
                  (types, rewriting, formulas: later steps, E2)
  4. extract      single-entry: the E1b full-document prompt with one configurable model (default: decided below)
                  table: stub returning no rows → one all-null entry (valid, scores the nulls)
  5. post-process value normalisation (numbers, dates, percentages, booleans, lists), every field present,
                  citation → page_numbers (from the [Page N] tag), confidence placeholder, metadata
  6. result       {entries, metadata} validated against contract/output.schema.json
```
- **Pipeline config as data** (one object: models, reasoning, ingest options), so E2's winners plug in without code changes.
- **`python -m app.eval.run_samples`** runs all 8 samples → `outputs/` → runs the validator → prints scores and $.
  Replay mode guarantees $0 when only post-processing changed.

### Layer 2: the service around it, `app/service/`

| Decision | Proposal | Why |
|---|---|---|
| D3 Job store | **SQLite (WAL) on a Docker volume**; table `jobs` (id, org, idempotency key, status, stage, timestamps, attempts, lease expiry, error, result path) | No extra container; WAL handles several workers; survives restarts |
| D4 Queue/workers | **The DB is the queue**: workers claim jobs with an atomic `UPDATE … WHERE status='PENDING' … RETURNING`; N worker tasks per process, more processes via `WORKERS`/compose scale | Scales with compute; no Redis needed |
| D5 Crash recovery | **Leases + heartbeat**: RUNNING jobs whose lease expired are re-claimed; LLM and OCR caches make the re-run nearly free | A crashed or restarted worker loses no job and pays no twice |
| Terminal states | Conditional update `WHERE status NOT IN ('SUCCESS','PARTIAL','FAILED')` | Terminal states can never change |
| D6 Idempotency | key = sha256(file) + sha256(canonical schema) + org → return the existing job | Same document + schema twice = no work twice |
| D7 Backpressure | Always accept and queue (the harness keeps 10 in flight); 429 + Retry-After only above a large queue depth | The 30-min clock includes queue time, but rejecting work fails the gate |
| D8 Tenancy | `X-Organization-Id` stored; GET with another org → **404** | Contract behaviour, cheap to do right |
| D9 Webhook | Optional, best effort with a few retries, after the terminal state is committed | Contract mentions it; low priority |
| D10 Files | Uploads to the volume, 50 MB limit → 413; type checked by content | Contract |
| D11 PARTIAL vs FAILED | FAILED: file unreadable or no extraction succeeded. PARTIAL: some extraction calls failed after retries, rest filled with nulls | Never lose a document over one failed call |
| D12 Deadline | Internal soft deadline (~20 min) → finish with what we have as PARTIAL | Stay inside 30 min including queue time |
| D33 Retries | Retry layer around the LLM client: 429/5xx/timeouts, exponential backoff + jitter, honour Retry-After, global concurrency limit | Throttling must not fail jobs |
| Web framework | FastAPI + uvicorn | Async, small, standard |
| Logging | Structured JSON logs with job_id, stage, durations, errors; failure reason stored on the job | "Find out why from the logs" |
| D41 Docker | python slim image + tesseract (fallback OCR); RapidOCR models ship in the wheel; healthcheck; volume for DB, uploads, caches | `/health` fast; nothing to download at start |

## 3. Order of work

1. **Layer 1 + `run_samples`** on all 8 docs with the E1b cache → first official scores (≈ $0.003: lease_003 full context is the only uncached call).
2. Then E1b options A/B/C (approved), whose winners plug into the pipeline config.
3. **Layer 2 service** + retry layer + tests (terminal immutability, idempotency, tenancy, crash recovery, 429 handling).
4. **Docker**: needs Docker installed on this machine to test the `docker compose up` gate.

## 4. Open questions for revision
1. The default single-entry model for v0: **glm-5.3-flash** (best non-free-text score on relevant text, $0 billed now, but it looped once),
   **claude-haiku-5.5** (best on full documents), or **gpt-6-luna with reasoning off** (cheapest, fastest)? Only the default; E2 decides for real.
2. Layer 2 now (right after layer 1) or after E1b A/B/C?
3. Docker on this machine: can you install it (needed for the gate test)?

---

## 5. Revision and implementation

**Revision (user):** default model = the cheapest (gpt-6-luna, reasoning off); order = E1b follow-ups → layer 2 →
layer 1 discussion. Layer 2 was built first with a **null pipeline** behind the pipeline interface, so layer 1
plugs in without touching the service.

**Built (layer 2):**
- `app/pipeline/base.py`: pipeline interface (`run(path, schema, ctx) → rows`), `validate_schema`,
  `build_result` (contract-valid by construction), `NullPipeline`, `DocumentError`.
- `app/service/api.py`: FastAPI; POST /jobs streams the upload (50 MB limit), detects the type by content,
  validates the schema, de-duplicates, queues; GET /jobs/{id} per tenant; GET /health with the git SHA.
- `app/service/store.py`: SQLite job store and queue with atomic claims, leases, fencing, terminal immutability,
  idempotency, poison-job protection.
- `app/service/worker.py`: concurrent job slots, heartbeats, internal deadline, outcome policy, webhook.
- `app/llm/client.py`: retry layer and concurrency limit.
- `Dockerfile`, `docker-compose.yml` (api + worker on one data volume).

**Check:**
- 55 tests pass (17 new service tests, 3 new retry tests).
- Real server (uvicorn, embedded worker): all 8 samples submitted concurrently, POST 134–240 ms, all SUCCESS,
  all **VALID** in `tools/validate.py`. All-null baseline mean score **0.087**: the floor layer 1 must beat.
- Docker not yet tested: the compose plugin and daemon need setting up on the dev machine.
