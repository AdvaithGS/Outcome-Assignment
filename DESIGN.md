# Design

Written for a colleague who has to run this on Monday. Sample mean **0.936** on `tools/validate.py`, no invented
values, **~$0.004 per document**; at 10 jobs in flight (fresh clone, cache off): 16/16 SUCCESS, p50 25 s, p95 94 s,
none lost or duplicated. Every experiment and table is in `TECHNICAL_REPORT.md`, the working notes in `notes/`.

```
POST /jobs ─► store upload + PENDING row (SQLite) ─► 202 in ~0.15 s
worker: claim (lease) ─► stage 0: analyse the field list (once per schema, cached)
                      ─► ingest by type ─┬─ single entry (lease, OM): whole document, one call ─► post-processing in code
                                          └─ table (a field marked primary): LLM writes a plan ─► code executes it
                      ─► contract-valid result ─► SUCCESS / PARTIAL / FAILED (terminal, never changes)
```

## Decisions and why

Every option was measured on the samples before it was chosen, using the rule *cheapest option within noise of the
best* (noise ±0.02–0.03). The LLM does the understanding; code does everything mechanical (cells, arithmetic,
formatting, citations, page numbers).

- **Ingestion is local.** pdfplumber for PDF text; our own DOCX parser (sections for citations, image OCR);
  spreadsheets as grids with cell addresses. **Scanned pages:** Tesseract reads every page and only pages it signals
  as hard (low confidence, garbled text, one uncertain line, tables, forms) go to the vision model. Only vision read
  the handwritten deposit correction; sending only hard pages kept accuracy and cut a scanned lease from $0.022 to $0.009.
- **Stage 0 analyses each new field list once:** types, absent values, rewritten descriptions, and formulas for
  computed fields in a small safe expression language. Lists of bare names first get standard real-estate
  definitions (descriptions removed: lease 0.65 → 0.80, T12 0.54 → 0.78, no invented values).
- **Leases and OMs: the whole document in one call**, with concise free text and a few generic domain rules. A passage
  Locator did not pay for itself at these sizes. With this prompt the cheapest model tied the dearer ones.
- **Computed fields are calculated in code**; a figure the document prints wins. The model's arithmetic was wrong on
  4 lease cells, the formulas on none.
- **Tables: the LLM writes an extraction plan from a fixed-size preview; code runs it on the full grid.** ~$0.0005–0.003
  whatever the length (the rent roll has 3,000 rows); values are copied from cells with their address; the
  description's cross-field relations are checks verified on every row. XLSX/CSV (several sheets too), PDF tables
  rebuilt from word positions, DOCX tables, scanned tables (scanned budget 0.991).
- **Absent, bad and failed inputs end in a defined state** (15 bad or mismatched inputs tested): unstated values are
  null; unreadable files are FAILED with the reason; empty documents, or a table field list on a document without a
  table, give valid nulls (PARTIAL, with a note) and no model calls; unusable model output gets a repair retry, the
  fallback model, then valid nulls.
- **Confidence is evidence-based**, checked against the answers: free text and yes/no judgements score lower, printed
  values higher when their citation and the value are both found. Values at ≥ 0.9 were right 96% of the time (82% before).
- **Service:** the SQLite job table is the queue (atomic claims, leases + heartbeats, fencing, idempotency per file +
  field list + tenant). A pipeline error never loses a document: it is retried, then finished PARTIAL with valid nulls.
  OCR runs in its own CPU pool at low priority, so it never delays claims, heartbeats or the API.

## Tradeoffs taken

- Cheapest model (gpt-6-luna, reasoning off) everywhere; glm and haiku were no better and 35–70% dearer.
- Concise free text (best strict) over verbatim quotes (best under an LLM judge); `FREETEXT_MODE` switches it.
- Vision only on flagged scanned pages: a hard page every signal misses is read by Tesseract alone
  (`SCANNED_MODE=vision` sends every page, ~+$0.013 per scanned lease).
- No DOCX page numbers (would need LibreOffice in the image); DOCX citations use section anchors.
- The image takes ~3 min to build the first time; we read the 60 s gate as start-up of a built image (~2 s).
- `sample/schemas/` stays in the repo because the validator command needs it; documents and answers never do.

## Where the brief is silent, we decided

- **FAILED** only when the file cannot be read (corrupt, encrypted; reason in `error`); everything else ends with valid
  output: **PARTIAL** when part of the work failed or there was nothing to extract (empty document, no table), with a note.
- An internal **deadline of 20 minutes** from submission (queue included), leaving margin inside the 30-minute limit;
  past it the job finishes PARTIAL with what exists.
- **Idempotency per tenant:** the same file + field list from the same `X-Organization-Id` returns the same job;
  a FAILED job may be resubmitted.
- A table that lists the same **primary key twice** keeps the first row (the rent roll repeats 5 units under "future
  residents").
- **Percentages as displayed**, also in spreadsheets (a cell storing 0.0525 shown as 5.25% → 5.25).
- A term the document provides for but with **no change** ("no escalation") is "none"; a term it does not mention is null.
- **Struck-through text does not apply** (scans and DOCX); handwriting governs over the printed text it corrects.

## Models used for what, and spend

| Stage | Model | Typical cost |
|---|---|---|
| Field-list analysis, terse definitions (once per list) | gpt-6-luna, reasoning off | ~$0.001 |
| Scanned-page transcription (hard pages only) | gpt-6-luna, reasoning off | ~$0.0007 per page |
| Lease / OM extraction | gpt-6-luna, reasoning off (fallback claude-haiku-5.5) | ~$0.003–0.007 |
| Table plan and repairs | gpt-6-luna; last repair claude-haiku-5.5 | ~$0.0005–0.003 |

**Development spend: $0.84 of the $10** (765 calls, almost all experiments, incl. five Docker load tests). Every
response is cached, so re-scoring after a downstream change costs $0; replay mode fails instead of spending.

## What broke and how it was handled

- **An 80-minute run** (calls hung on a 300 s read timeout): every attempt has a total time cap; retries stop at the
  job deadline. **Hidden reasoning tokens** tripled a test's cost: estimates include them; reasoning off by default.
- **Table plans** dropped columns, cut wrapped text, picked the wrong "Rent" line, and once ran 31 fallback calls:
  generic and required cross-field checks, two repairs, a bounded fallback. **Fresh plans** shortened a header value
  in 4 of 5 runs: document-wide values are now taken as printed in their cell (T12, five fresh plans: 0.923 → 0.998).
- **Scanned tables**: pages came back with different column layouts (page 1's header is now imposed), and later
  selective vision turned "easy" pages into plain text (1 of 55 rows; tables always use vision again, 0.991).
- **Load tests**: table jobs queued behind scanned leases (p50 129 s → concurrency 10 jobs / 24 calls, 50 s); then
  per-page OCR filled the worker's thread pool (tables 103 s, POST 4.2 s → own CPU pool at low priority, p50 25 s).
- **Our own fixes**: terse-list definitions invented "0 if absent" (9 invented values → "null unless stated"), and a
  number-parser fix read labels as amounts ("C5" → 5; caught by comparing old and new parsing on every sample cell).

## Running it (ops notes)

- `docker compose up` starts `api` (:8080) and one `worker`. More throughput: `WORKER_CONCURRENCY` or
  `docker compose up --scale worker=3` (workers share the queue on the `data` volume).
- JSON logs on stdout: `docker compose logs worker | grep <job_id>` shows a job's stages, cost and errors; a FAILED
  job's reason is in its `error` field.
- Required settings: `LLM_BASE_URL`, `LLM_API_KEY`; the rest are listed in `README.md` (e.g. `OCR_WORKERS` sizes the
  OCR pool; default: available CPUs − 2).
- Sample answers: no errors found; lease_002's `base_rent_escalation` is "none", which the validator treats as null.

## With another week

- Field-scoped interpretation hints (rent starts after free rent; the subject property's in-place rent), tested on
  documents outside the samples so they do not just fit three leases.
- Act on low confidence: a second cheap model on fields scored below ~0.8, and only fields where the two disagree go to
  a stronger one (disagreement caught > 90% of errors in E1b).
- Field lists of ~75+ fields: extract by stage-0 group in parallel calls (one call's output cap would truncate them).
- Scanned PDFs that carry a scanner's hidden OCR text layer: treat as scanned; mixed scanned/digital table PDFs.
- A load test with several worker containers; send a page to vision when the extraction leaves a field on it null;
  a slimmer image; a Locator path for 100+ page documents.
