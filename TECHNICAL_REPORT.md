# Technical Report: Document Extraction Service

**Role:** AI/ML Developer. Scoring weights: Accuracy 35 · Cost 15 · Load/latency 15 · Design/code 35.

This is the long-form record of how the pipeline was built. `DESIGN.md` is the one-page summary of it;
`PLAN.md` holds the working decision register (IDs D3–D41 used below).

Each section follows the same layout:
- **Goal**: what this part must achieve and which score it affects.
- **Decisions**: the question, the options we considered, what we chose and the evidence.
- **Experiments**: what we ran, on which documents, at what cost.
- **Results**: numbers from `tools/validate.py` and the spend ledger.
- **Open issues**: known limits and what we would try next.

---

## 1. Evaluation setup

_TODO: sample set (8 docs, 6 schemas), validator scoring rules, how unseen field lists were simulated,
the response cache that keeps re-runs cheap, and the metrics tracked per iteration (score,
hallucinations, misses, $/doc, latency)._

| Iteration | Change | Mean score | Halluc. | Misses | $ / doc | Notes |
|---|---|---|---|---|---|---|
| | | | | | | |

---

## 2. Service and job model

**Goal:** pass the gates (≥ 80% terminal + valid, ≤ 30 min including queue time, POST < 2 s), handle
10 jobs in flight, lose no jobs and duplicate none, scale with workers. Affects the Load/latency score and the gates.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D3 | Job store | In-memory; SQLite; Postgres; Redis | **SQLite (WAL) on the data volume** | No extra container; WAL handles several worker processes; survives restarts |
| D4 | Queue and workers | In-process tasks; Redis queue; DB as queue | **The job table is the queue**; atomic claim; N concurrent jobs per worker process; separate `worker` compose service, scalable | Test: 8 threads × 40 jobs, every job claimed exactly once |
| D5 | Crash recovery | None; restart from scratch; leases | **Leases + heartbeats + fencing**; expired RUNNING jobs re-claimed; poison jobs failed after `JOB_MAX_ATTEMPTS`; LLM/OCR caches make re-runs nearly free | Tests: crashed worker's job finished by another; stale worker cannot finish |
| D6 | Idempotency | None; file hash; file + schema + org | **sha256(file) + canonical schema, per org**; a FAILED job may be resubmitted | Test: duplicate POST returns the same job |
| D7 | Backpressure | Reject when busy; always queue | **Always queue**; 429 + Retry-After only above `QUEUE_LIMIT` (500) pending | Rejecting work would fail the completion gate |
| D8 | Multi-tenancy | Skip; implement | **Implemented**: jobs scoped by `X-Organization-Id`; other org (or none) gets 404 | Test |
| D9 | Webhook | Skip; best effort | **Best effort**, JobStatus body, 3 tries with backoff, after the terminal state is committed | — |
| D10 | File storage and size limits | DB blobs; volume files | **Content-addressed files on the volume**; streamed with a 50 MB limit (413); type detected from content (PDF / DOCX / XLSX / CSV) | Tests: 413 leaves no partial file; legacy .doc → 400 |
| D11 | PARTIAL vs FAILED | — | **FAILED** only for unreadable documents (with reason); pipeline errors are retried, then **PARTIAL** with valid null fields | A bug never loses a document; nulls cost nothing |
| D12 | Per-job deadline and degradation | None; hard kill | **Internal deadline 20 min from submission** (queue included; minimum 30 s run) → PARTIAL with what exists | Test |

**Experiments:** _TODO_
**Results:** _TODO_
**Open issues:** _TODO_

---

## 3. Document ingestion

**Goal:** turn PDF (digital and scanned), DOCX, XLSX and CSV into a representation the models read
accurately and cheaply, keeping page and cell locations for citations. Affects Accuracy and Cost.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D13 | Digital PDF representation | | | |
| D14 | Scanned PDF (OCR vs vision vs hybrid) | | | |
| D15 | Detecting a missing text layer | | | |
| D16 | DOCX (text, tables, embedded images) | | | |
| D17 | XLSX/CSV serialisation (cells, merges, sheets, size) | | | |
| D18 | Page and cell location tracking | | | |

**Per-document findings**

| Doc | Format | Representation used | Issues seen | Fix |
|---|---|---|---|---|
| boxscore_001 | CSV | | | |
| budget_001 | PDF (text) | pdfplumber (proposed) | Table document: heading sections are meaningless, citations are row snippets | Table branch (later) |
| lease_001 | PDF (scanned, 1993) | Local OCR + vision on the pages that matter (proposed) | Handwritten correction (deposit $2,400 struck → 2150), pre-printed "19__" years filled by hand, garbled guarantor name | Vision model reads all three (E1a) |
| lease_002 | DOCX | Own XML parser + image OCR | No heading styles; run-in headings ("17. HOLDING OVER. Tenant…"); TOC repeats headings; no Word tables; no pagination; 3 images (signature page with handwritten date, floor plan, logo); Word stores one image twice | Heuristic + run-in heading detection, TOC mode, numbered anchors, tabs → " \| ", image dedupe |
| lease_003 | PDF (scanned, clean) | Local OCR | None material: every printed answer value captured | — |
| om_001 | PDF (text) | pdfplumber (proposed) | One image-only page; ~110 tiny heading sections | Per-page scan detection; pages as Locator unit |
| t12_001 | XLSX | | | |
| rentroll_001 | XLSX | | | |

**Experiments**

- **E0 (free, no LLM):** every applicable ingestion option on the PDF and DOCX samples. Metrics: *captured* =
  share of non-computed answer values present in the extracted text in any common format (an upper bound on
  extraction accuracy); *right page* = found on a page the answer cites; citations located; tokens; CPU time;
  anchor slice sizes. Digital docs were also rendered and OCR'd to give scans with a known answer key.
- **E1a (targeted, $0.0142):** 4 images where local OCR failed or that act as a control (lease_001 p2, p25, p18;
  lease_002 signature image) × the 4 cheapest vision models, scored on the answer values cited on each page plus
  handwriting checks. Local OCR scored on the same images as a free baseline. A second, more expensive round was
  planned but not needed.

**Results: E0, digital PDFs**

| Reader | OM captured | Budget captured | OM tokens | OM citations located | Right page |
|---|---|---|---|---|---|
| pdfplumber | 100% | 99.8% | 7.4k | 95% | 100% |
| PyMuPDF | 100% | 99.1% | 7.5k | 95% | 100% |
| pdftotext (plain) | 100% | 99.8% | 7.8k | 86% | 84% |
| pdftotext (layout) | 100% | 99.8% | 14.2k | 91% | 100% |
| pymupdf4llm | 100% | 94.0% | 8.4k | 95% | 100% |

**Results: E0, scanned PDFs**

| Engine | lease_001 verbatim / incl. near-matches | lease_003 verbatim / incl. near-matches | Citations located (lease_001 / lease_003) | CPU s/page (under contention) | Wall time, 25 pages |
|---|---|---|---|---|---|
| RapidOCR @200 dpi | 85.0% / 97.5% | 88.5% / 98.4% | 72% / 100% | ~27–31 | ~227 s |
| Tesseract @200 dpi | 82.5% / 97.5% | 85.3% / 96.7% | 62% / 97% | ~9 | ~27 s |
| Tesseract @300 dpi | 80.0% / 95.0% | 85.3% / 96.7% | 59% / 97% | ~11–12 | ~33 s |
| Tesseract psm 6 @300 | 80.0% / 95.0% | 85.3% / 96.7% | 66% / 97% | ~10 | ~28 s |

Rendered (clean) scans of om_001 and budget_001: every engine 99.6–100%. The remaining lease misses are values
the lease implies but never prints, plus the handwritten deposit, which no local engine reads.

**Results: E0, DOCX (lease_002)**

| Reader | Captured verbatim / incl. near-matches | Sections | Median slice | Citations located | Images transcribed |
|---|---|---|---|---|---|
| Own XML parser + RapidOCR | 82.6% / 91.3% | 49 | 244 tokens | 100% | 2 (handwritten date read) |
| Own XML parser + Tesseract | 82.6% / 91.3% | 49 | 244 tokens | 100% | 2 (handwritten date garbled) |
| python-docx / mammoth | 82.6% / 91.3% | 21 (mostly fixed chunks) | 1,043 tokens | 100% | none |

The two lease_002 misses are inferred values (a rent start date following free rent; a renewal count of 0).

**Results: E0, coordinates for the Locator**

| Document | Heading sections | Median slice | Verdict |
|---|---|---|---|
| lease_002 (DOCX) | 48 | ~240 tokens | Sections work |
| om_001 (PDF) | 70–110 | 20–50 tokens | Too fine: use pages (~350 tokens each) |
| lease_001 (OCR) | 69–120 | 26–187 tokens | Too fine and engine-dependent: use pages (~1,000 tokens each) |

**Results: E1a, vision transcription of hard pages**

| Model | Handwritten deposit (`~~2,400~~ [handwritten: 2150]`) | Guarantor name | Rent table (control) | DOCX handwritten date | $ per page (list) | Latency | Notes |
|---|---|---|---|---|---|---|---|
| Local RapidOCR | ✗ | ✗ | ✓ | ✓ | 0 | 3–5 s CPU | |
| Local Tesseract | ✗ | ✗ | partial | ✗ | 0 | ~1 s CPU | |
| z-ai/glm-5.3-flash | ✓ | ✓ | ✓ | ✓ | ~0.0003–0.0007 | 3–6 s | Billed $0 on our key; looped to the 3,000-token cap once |
| openai/gpt-6-luna | ✓ | ✓ | ✓ | ✓ | 0.0011; 0.0006 with reasoning off (still ✓) | 13–18 s; 7 s reasoning off | 950–1,300 hidden reasoning tokens/page by default |
| anthropic/claude-haiku-5.5 | ✓ | ✓ | ✓ | ✓ | 0.0006–0.0011 | 6–9 s | With reasoning off, wrongly marked the year as struck ("19 ~~93~~") |
| qwen/qwen3-vl-235b | ✗ ambiguous (`$ 2,400– 2150M`) | ✓ | ✓ | ✓ | 0.0007–0.0020 | 11–19 s | Loses the strike-through |

A page image costs about 1.5–2.4k input tokens at 1,500 px on the long side. No model invented content on the control table.

**Proposed (pending decision):** pdfplumber for digital PDF text; per-page scan detection; local OCR (RapidOCR,
Tesseract as the fast fallback) plus a vision model where answers live; own XML parser for DOCX; pages as the
Locator unit for PDFs, sections for DOCX; vision shortlist luna (reasoning off) / glm (with a length cap) / haiku.

**Open issues:** OCR CPU cost under 10 concurrent jobs (RapidOCR ~3× Tesseract); glm's runaway output needs a
cap and a fallback; whether to transcribe every scanned page or send only Locator-picked page images (E3).

---

## 4. Schema understanding

**Goal:** work with any field list, including unseen ones: infer field types, improve the
descriptions, and tell single-entry documents from tables. Affects Accuracy and the robustness gate.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D19 | Field type inference | | | |
| D20 | Description rewriting (generic vs per known schema) | | | |
| D21 | Document shape and document-level vs row-level fields | | | |

**Field taxonomy observed in the samples**

| Kind | Examples | Handling |
|---|---|---|
| Document-level scalar | `property_name`, `report_date` | |
| Row-level scalar | `gl_code`, `unit_number` | |
| Computed | `lease_term_months`, `total_base_rent`, `price_per_unit`, `months_with_activity` | |
| List-valued | `rent_schedule_*`, `broker_names`, `unit_types` | |
| Frequently absent | `parking_monthly_charge`, `guarantor_name` | |

**Experiments:** _TODO (original vs rewritten descriptions; renamed-key variants of the sample schemas)._
**Results:** _TODO_
**Open issues:** _TODO_

---

## 5. Extraction strategy

**Goal:** get correct values at the lowest cost. Single-entry documents (leases, OMs) and table
documents (rent roll, T12, budget, box score) are likely to need different strategies. Affects Accuracy and Cost.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D22 | Single-entry docs: one call / chunked / per field group | | | |
| D23 | Table docs: LLM rows / LLM mapping + code / hybrid | | | |
| D24 | Computed fields: LLM vs deterministic code | | | |
| D25 | Structured output and repairing bad responses | | | |
| D26 | Model routing per stage | | | |
| D27 | Prompt design | | | |

**Model routing table**

| Stage | Model | Why | Fallback | Typical $ |
|---|---|---|---|---|
| | | | | |

**Experiments:** _TODO_
**Results:** _TODO_
**Open issues:** _TODO_

---

## 6. Post-processing and trust

**Goal:** a plausible guess is worse than a null. Normalise values, verify them against the
source, give each one a meaningful confidence, and turn unverifiable values into nulls. Affects
Accuracy (the hallucination penalty and the citation bonus).

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D28 | Normalisation rules | | | |
| D29 | Verification (citation grounding, cross-field checks, second pass) | | | |
| D30 | Confidence estimation | | | |
| D31 | Null policy and thresholds | | | |
| D32 | Citations, `page_numbers`, `source_locations` | | | |

**Experiments:** _TODO (hallucinations vs misses before and after each trust step)._
**Results:** _TODO_
**Open issues:** _TODO_

---

## 7. LLM client and cost

**Goal:** survive 429s and transient errors without failing jobs, account for every token, and
keep development spend inside $10. Affects Cost and Load/latency.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D33 | Retries, backoff, rate limiting, fallback model | — | **Inside the LLM client**: 429 / 5xx / timeouts / unusable 200 bodies retried up to 6 times, exponential backoff with full jitter, Retry-After honoured; per-process concurrency limit (`LLM_MAX_CONCURRENCY`) | Tests: 429 → 502 → bad body → success counts 4 calls; non-retryable 400 not retried |
| D34 | Cost accounting and the `metadata` breakdown | | | |
| D35 | Dev-time cache, spend ledger, budget caps | Custom vs LiteLLM/diskcache/HTTP record-replay; SQLite vs files vs Redis; prod cache off / per-job / shared | Custom content-addressed cache in SQLite (WAL) with off/read_write/replay/refresh modes and single-flight; append-only JSONL ledger kept apart from the cache; file-based artifact cache; $9 dev cap, none in prod | `notes/01-caching.md`; 30 unit tests |

**Spend summary**

| Phase | Calls | $ spent | Share of $10 |
|---|---|---|---|
| Live check (caching step) | 1 | 0.000004 | 0.00% |
| E0 ingestion comparison | 0 | 0 | 0% |
| E1a vision transcription (round 1 + reasoning-off test) | 18 | 0.0142 | 0.14% |
| E1b model screen, round 1 | 38 | 0.0961 | 0.96% |
| E1b free-text judge | 4 | 0.0011 | 0.01% |
| E1b noise check | 6 | 0.0130 | 0.13% |
| E1b round 2a (gemini-flash on failures) | 4 | 0.0618 | 0.62% |
| **Total to date** | **71** | **0.1863** | **1.86%** |

**Experiments:** _TODO_
**Results:** _TODO_
**Open issues:** _TODO_

---

## 8. Quality loop, tests and operations

**Goal:** show that the eval loop was used, catch regressions, prove behaviour under load, make
failures diagnosable, and start cleanly with Docker. Affects Design/code and the gates.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D36 | Eval harness | | | |
| D37 | Generalisation to unseen field lists | | | |
| D38 | Test suite scope | | | |
| D39 | Load test | | | |
| D40 | Logging and observability | Plain logs; structured | **JSON lines** with job_id, worker, stage, timings, cost, error; failure reason stored on the job and returned in `error` | — |
| D41 | Docker packaging | — | python:3.14-slim + tesseract + OpenCV libs; RapidOCR models inside the wheel; compose services `api` + `worker` on one `data` volume; healthcheck; only `LLM_*` and service settings passed in | Not yet tested: Docker compose plugin / daemon not set up on the dev machine |

**Experiments:** _TODO_
**Results:** _TODO (test counts, load test p50/p95, crash and restart test outcome)._
**Open issues:** _TODO_

---

## 9. Issues found in the sample answers

| Doc | Field | Truth | Document evidence |
|---|---|---|---|
| | | | |

## 10. Future work

_TODO_
