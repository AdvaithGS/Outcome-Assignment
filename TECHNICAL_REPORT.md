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
| D3 | Job store | | | |
| D4 | Queue and workers | | | |
| D5 | Crash recovery | | | |
| D6 | Idempotency | | | |
| D7 | Backpressure | | | |
| D8 | Multi-tenancy | | | |
| D9 | Webhook | | | |
| D10 | File storage and size limits | | | |
| D11 | PARTIAL vs FAILED | | | |
| D12 | Per-job deadline and degradation | | | |

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
| budget_001 | PDF (text) | | | |
| lease_001 | PDF (scanned) | | | |
| lease_002 | DOCX | | | |
| lease_003 | PDF (scanned) | | | |
| om_001 | PDF (text) | | | |
| t12_001 | XLSX | | | |
| rentroll_001 | XLSX | | | |

**Experiments:** _TODO (e.g. OCR quality and cost per page vs a vision model on the scanned leases)._
**Results:** _TODO_
**Open issues:** _TODO_

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
| D33 | Retries, backoff, rate limiting, fallback model | | | |
| D34 | Cost accounting and the `metadata` breakdown | | | |
| D35 | Dev-time cache, spend ledger, budget caps | Custom vs LiteLLM/diskcache/HTTP record-replay; SQLite vs files vs Redis; prod cache off / per-job / shared | Custom content-addressed cache in SQLite (WAL) with off/read_write/replay/refresh modes and single-flight; append-only JSONL ledger kept apart from the cache; file-based artifact cache; $9 dev cap, none in prod | `notes/01-caching.md`; 30 unit tests |

**Spend summary**

| Phase | $ spent | Share of $10 |
|---|---|---|
| | | |

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
| D40 | Logging and observability | | | |
| D41 | Docker packaging | | | |

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
