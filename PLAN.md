# Working plan: Document Extraction Service

> **Status (8 Nov): all decisions closed.** This is the opening plan as written on day 1 (7 Nov), kept as the record
> of where the work started. Every decision below was closed with evidence: the outcomes, under the same IDs, are in
> `TECHNICAL_REPORT.md`, and the summary in `DESIGN.md`. Statements below about the environment (e.g. Docker and
> Tesseract not installed) describe day 1.

Internal working file. Every decision below was **OPEN** when written. We closed them one at a time, recording
the choice and the reason, and the closed ones feed `DESIGN.md`.

Deadline: **Sun 8 Nov 2026, 11:59 PM IST**. Budget: **$10 OpenRouter, no top-ups.**

---

## 1. The problem in one paragraph

An HTTP service (`docker compose up`, port 8080) that takes one file (PDF / DOCX / XLSX / CSV)
plus one field list (JSON: `name`, `description`, `fields{key: {name, description, primary?}}`).
It runs the extraction asynchronously and returns `{entries[], metadata}`. Each field is
`{value, citation, confidence}`, plus the optional `page_numbers` / `source_locations`. It must work
for **field lists it has never seen**, survive **10 concurrent jobs with 429s**, finish each job in
**≤ 30 min**, use **only the 9 listed models** through `LLM_BASE_URL` / `LLM_API_KEY`, and
balance accuracy against cost.

## 2. Hard constraints (non-negotiable, from the brief, contract and validator)

| # | Constraint | Source |
|---|---|---|
| C1 | `docker compose up` → `/health` 200 `{"status":"ok"}` within 60 s, no manual steps | contract/README §1-2 |
| C2 | `POST /jobs` (multipart: `file`, `schema` string, optional `webhook_url`) returns 202 within 2 s | openapi, contract §5 |
| C3 | Status enum `PENDING/RUNNING/SUCCESS/PARTIAL/FAILED`; terminal states never change | openapi |
| C4 | 400 for malformed request; refusing a supported type = failed doc; 413 over 50 MB; 429 + `Retry-After` allowed | openapi |
| C5 | `X-Organization-Id` optional; if implemented, a cross-tenant GET returns **404** | openapi |
| C6 | Only the 9 listed models; all calls go through the env vars; no paid third-party services | brief §3 |
| C7 | Output validates with `tools/validate.py`. Allowed top-level keys: `entries, metadata, schema, document` | validator |
| C8 | Every entry carries every schema field. Numbers are JSON numbers, never currency strings. Dates `YYYY-MM-DD`. Percentages as printed | contract, validator |
| C9 | Table schema (exactly one `primary`) → one entry per row; otherwise exactly one entry | contract |
| C10 | Repo deliverables: service, `README.md` (role, run, scores), `DESIGN.md` (~1 page), `outputs/<doc>.json` with top-level `"schema"` | brief §5 |
| C11 | Git history from the first commit; no key or sample docs/answers committed; zip with `.git`, < 100 MB, exact name | brief §5 |
| C12 | ≥ 80% of test docs reach a terminal state with valid output (gate) | brief §6 |

## 3. How we are scored (what the decisions optimise for)

- **Accuracy:** per-field match after normalisation (numbers ±0.5%, case and punctuation folded,
  lists scored with order-insensitive F1). **Hallucination (truth null, we give a value) costs more
  than a miss.** Rows are matched on the primary field: extra and missing rows score 0 for every field.
  Harder fields are weighted more. Correct citations earn a bonus.
- **Cost:** $ per document, measured on their gateway.
- **Load/latency:** completion rate, p50 and p95, no lost or duplicated jobs, scales with workers.
- **Design/code:** manual review. DESIGN.md is read first.
- Weights depend on the role: AI/ML 35/15/15/35, Software 20/5/30/45.

## 4. What the sample set tells us (evidence for the decisions)

| Doc | Type | Shape | Notes from inspection |
|---|---|---|---|
| boxscore_001 | CSV | table, 10 rows × 20 fields | Header rows above the table; date range line; per-row "Report Date" column; Totals row to drop |
| budget_001 | PDF, text layer, 8 pp | table, 55 rows × 14 | Layout text; multi-line wrapped "Explanation of Variance"; section headers and totals must not become rows; `N/A` → null |
| lease_001 | PDF, **no text layer**, 25 pp | single, 40 fields, 5 list fields | 1993 lease; rent schedule in a later article |
| lease_002 | DOCX | single, 40 fields | Long digital text; 3 embedded images; no Word tables |
| lease_003 | PDF, **scanned** (14 MB), 21 pp | single, 40 fields, 7 list fields | Needs OCR or vision |
| om_001 | PDF, ReportLab text layer, 22 pp | single, 29 fields | Values spread across sections; current vs pro forma traps; computed fields |
| t12_001 | XLSX, 1 sheet A1:R148, merged cells | table, 119 rows × 11 | GL code and name columns, 12 monthly columns + Total; computed `months_with_activity` |
| rentroll_001 | XLSX, 2 sheets, A1:AM3058 | table, **325 rows** × 17 | One block per unit with charge lines folded into a row; second sheet holds report parameters |

Other observations:
- Field lists vary even for the same doc type (`contract/schema.example.json` boxscore ≠ `sample/schemas/boxscore.json`).
- Field types are never declared: we have to infer number, date, %, bool, list or string from the description.
- Field kinds: document-level (repeated per row), per-row, **computed** (term months, total rent,
  $/RSF, price/unit, sums, counts), **list-valued**, **genuinely absent** (null).
- The answers came from their production pipeline and may contain errors. If we find one, record the evidence for DESIGN.md.
- Local machine: `pdftotext` / `pdfinfo` present; **Docker and tesseract are not installed.**

---

## 5. Decision register (as opened on day 1; all closed in `TECHNICAL_REPORT.md`)

Each entry has the question, the options we know of, and what the decision depends on.

### A. Framing
- **D0 Role: CLOSED, AI/ML Developer.** Weights: Accuracy 35 · Cost 15 · Load/latency 15 · Design/code 35.
- **D1 Repo layout and git.** Repo in this folder (gitignore `sample/`) or a fresh folder? Private remote? When to make the first commit (ASAP: history matters)?
- **D2 Language and stack.** Python / TypeScript / Go …; web framework; libraries we can pull in for PDF, DOCX and XLSX.

### B. Service and job model
- **D3 Job store.** In-memory, SQLite, Postgres, Redis? It must survive a worker crash or restart (C3, Software §7).
- **D4 Queue and workers.** In-process task pool, a separate worker container, Redis queue, DB-polling with leases? How does it scale horizontally ("more workers → faster")?
- **D5 Crash recovery.** Leases, heartbeats, re-claiming stuck RUNNING jobs; guaranteeing terminal states stay put.
- **D6 Idempotency.** Key = hash(file bytes + schema [+ org])? What happens on a duplicate while the first is still running?
- **D7 Backpressure.** Always accept and queue, or return 429 + Retry-After over some depth? The 30-min clock includes queue time.
- **D8 Multi-tenancy.** Implement `X-Organization-Id` scoping or not.
- **D9 Webhook.** Implement or skip; retry policy.
- **D10 File storage.** Where uploads live (volume, DB blob); size limit enforcement (413).
- **D11 PARTIAL vs FAILED semantics.** When is a result PARTIAL (e.g. some chunks failed) and when is it FAILED?
- **D12 Per-job deadline.** Internal time budget so a job finishes before 30 min, degrading gracefully.

### C. Document ingestion (format → a representation the models can use)
- **D13 PDF with a text layer.** Plain text vs layout-preserving text vs page images vs both; table detection.
- **D14 Scanned PDF.** Local OCR (tesseract or another open-source engine) vs a vision model on page images vs a hybrid; DPI, page selection, cost per page.
- **D15 Text layer detection.** How we decide a page is scanned (per page, not per doc?).
- **D16 DOCX.** Text extraction; embedded images (OCR them?); tables.
- **D17 XLSX and CSV.** Serialisation that keeps cell refs (for `source_locations` and citations); merged cells; multiple sheets; very large sheets (3k rows).
- **D18 Page and location tracking.** Carry page numbers and cell refs through the pipeline so citations are verifiable.

### D. Schema understanding (it must work for unseen field lists)
- **D19 Field type inference.** Number, date, %, bool, list, string, computed. LLM, heuristics, or both? Cache per schema?
- **D20 Description rewriting.** Runtime enrichment of descriptions (generic) vs hand-tuned hints for the known schemas vs both. It must not *depend* on a known schema.
- **D21 Document shape.** Single-entry vs table from the `primary` flag; which fields are document-level vs per-row.

### E. Extraction strategy
- **D22 Single-entry docs (leases, OMs).** Whole doc in one call vs chunk + merge vs retrieval per field group vs field-by-field.
- **D23 Table docs.** LLM emits all rows, LLM infers a column mapping and code fills the rows, or a hybrid. Chunking for long tables; row dedupe; header and total filtering. *This is the biggest cost and accuracy lever (rent roll is 325 rows).*
- **D24 Computed fields.** LLM computes vs LLM extracts the inputs and deterministic code computes.
- **D25 Structured output.** JSON mode / tool calling / schema-constrained output; repairing unusable responses.
- **D26 Model routing.** Which of the 9 models for OCR/vision, mapping, extraction, verification; cheap-first with escalation?
- **D27 Prompt design.** System prompt, null discipline, citation format, few-shot or none.

### F. Post-processing and trust
- **D28 Normalisation.** Numbers (parentheses negatives, `$`, commas), dates → ISO, percentages as printed, booleans, lists, `N/A` → null.
- **D29 Verification.** Check citation text exists in the source; cross-field checks (rent + other = total, start/end alignment); second-model check on low-confidence fields only?
- **D30 Confidence.** How we compute it (model self-report, verification signals, agreement) and whether it drives nulling.
- **D31 Null policy.** Threshold for replacing an unverifiable value with null (hallucinations cost more than misses).
- **D32 Citations.** Format `[Page N] …` / `[Sheet!A1] …`; `page_numbers` / `source_locations` bonus.

### G. LLM client and cost
- **D33 Client.** Retries with backoff and jitter on 429/5xx, `Retry-After`, timeouts, concurrency limiter (global and per model), fallback model on persistent failure.
- **D34 Cost accounting.** Reading OpenRouter usage fields; per-model breakdown in `metadata`.
- **D35 Dev-time spend control.** Response cache on disk keyed by (model, prompt) so eval reruns cost $0; a spend ledger; per-run budget cap.

### H. Quality loop, tests, ops
- **D36 Eval harness.** Script: run all samples → `outputs/` → `validate.py` → per-field diff report; track score and $ per iteration.
- **D37 Generalisation check.** How we test unseen field lists (write our own variant schemas, re-key fields, synthetic docs).
- **D38 Tests.** Unit (normalisers, parsers, idempotency, terminal-state immutability, tenant isolation, retry logic) and integration (API end-to-end with a mocked LLM).
- **D39 Load test.** 10-in-flight harness with a mocked slow or 429-ing LLM; measure p50/p95, lost or duplicate jobs.
- **D40 Logging and observability.** Structured logs with job_id; failure reason stored on the job; stage field.
- **D41 Docker.** Base image, system deps (poppler, OCR engine…), compose services, healthcheck, volumes, startup time < 60 s.

---

## 6. Phased plan (order of work; each phase ends with a commit)

| Phase | Goal | Closes decisions | Exit check |
|---|---|---|---|
| **P0 Setup** | Pick the role, create the repo and first commit, pick the stack, `.env` handling | D0, D1, D2 | Repo with a skeleton, `.gitignore` protecting samples and keys |
| **P1 Contract shell** | API + job model + worker that returns a *valid all-null* result for any doc and field list; Dockerised | D3–D12, D41 (first pass) | `docker compose up` → `/health`; `validate.py` prints VALID on every sample |
| **P2 Ingestion** | Per-format readers into a common document representation with page/cell locations; OCR path for scanned PDFs | D13–D18 | Dump of each sample's representation looks right; no LLM spend yet |
| **P3 LLM plumbing** | Client with retries, limiter, cost accounting, dev cache, spend ledger | D33–D35 | A single cheap call end-to-end, cost recorded |
| **P4 Schema layer** | Type inference, description enrichment, shape detection | D19–D21 | Works on all 6 sample schemas + the contract example |
| **P5 Extraction v1** | Single-entry docs, then table docs; normalisation; computed fields | D22–D28 | First real scores from `validate.py` |
| **P6 Eval loop** | Harness, per-field diff, iterate on prompts and routing within budget | D36, D37, D26 | Score and $/doc tracked per iteration in a log |
| **P7 Trust** | Verification, confidence, null policy, citations | D29–D32 | Hallucinations go down without misses going up |
| **P8 Robustness** | Tests, load test, crash/restart test, logging | D38–D40 | 10 in flight, no lost jobs, tests green |
| **P9 Submission** | `outputs/`, README scores, DESIGN.md, zip check | — | Clean-clone `docker compose up` + validator VALID; zip named correctly |

Safety principle for the order: get to **P1 (valid output and the gates passing)** as early as
possible, so there is always a scorable submission, then improve accuracy on top of it.

## 7. Budget ledger

| Date/time | Phase | What ran | Models | $ spent | Running total |
|---|---|---|---|---|---|
| | | | | | 0.00 |

## 8. Decision log (filled in as we close items)

| ID | Decision | Why | Date |
|---|---|---|---|
| D0 | AI/ML Developer role | Candidate's choice; accuracy and cost carry 50 of the 100 points | 2026-11-07 |
| D1 | Repo = this package folder; sample docs, answers, `.env` and `.cache/` gitignored | Keeps the brief's paths (`tools/validate.py`, `sample/schemas`) working as written | 2026-11-07 |
| D2 | Python 3.11+, httpx, pytest | Best document-parsing ecosystem; validator is Python | 2026-11-07 |
| D35 | Custom content-addressed LLM cache (SQLite WAL, 4 modes, single-flight), JSONL spend ledger (committed), $9 dev cap, file artifact cache | See `notes/01-caching.md` §9 | 2026-11-07 |

## 9. Suspected errors in the sample answers

| Doc | Field | Truth says | Document says (evidence) |
|---|---|---|---|
| | | | |
