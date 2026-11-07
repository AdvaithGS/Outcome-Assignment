# Design

Written for a colleague who has to run this on Monday. Status: **work in progress** (sample mean **0.922** on
`tools/validate.py`; Docker setup written but not yet verified on a clean machine). The long-form record, with every
experiment and table, is `TECHNICAL_REPORT.md`; the working notes are in `notes/`.

## Overview

```
POST /jobs ─► store upload + PENDING row (SQLite) ─► 202 in ~0.15 s
worker: claim (lease) ─► stage 0: analyse the field list (once per schema, cached)
                      ─► ingest by type ─┬─ single-entry (lease, OM): whole-document extraction ─► post-processing
                                          └─ table (field marked primary): LLM writes a plan ─► code executes it
                      ─► contract-valid result ─► SUCCESS / PARTIAL / FAILED (terminal, never changes)
```

## Decisions and why

- **Every option was measured before it was chosen** (E0–E2, all on the sample set, scored with the official
  validator or with a free coverage metric), using the rule *cheapest option within noise of the best*.
- **Ingestion is local and per type.** PDF text via pdfplumber (99–100% of answer values captured, half the tokens
  of layout mode); DOCX through our own XML parser (the sample uses no heading styles, so headings are detected by
  rule, giving ~240-token sections for citations); spreadsheets as grids with cell addresses.
- **Scanned pages are transcribed by a vision model** (gpt-6-luna, reasoning off, ~$0.0006/page). Local OCR
  (Tesseract, RapidOCR) could not read the handwritten correction to a security deposit or a garbled name; vision
  could, and was faster than RapidOCR. Tesseract stays as the per-page fallback.
- **Field lists are analysed once per schema**: types, "value when absent" (0 / false / []), groups, rewritten
  descriptions, and formulas for computed fields in a small safe expression language.
- **Leases and OMs: the whole document in one call.** Cheap models scored the same on full documents as on
  hand-picked relevant passages, so a Locator stage did not pay for itself at these sizes; it is reserved for very
  long documents.
- **Computed fields are calculated in code** from extracted operands; a figure the document prints wins. The model's
  own arithmetic was wrong in 4 of the lease computed cells; the formulas were right in all of them.
- **Tables: the LLM writes an extraction plan, code executes it** (the plan format is the user's design). Cost is
  ~$0.001–0.003 per table regardless of length, values are copied from cells (no invented values), and every value
  carries its cell address. Checks plus one repair call catch bad plans; the LLM-writes-rows fallback is the last resort.
- **Service:** the SQLite job table is the queue (atomic claims, leases + heartbeats, fencing, idempotency by file +
  schema + tenant). A pipeline error never loses a document: it is retried, then finished as PARTIAL with valid nulls.

## Tradeoffs taken

- Cheapest model by default (gpt-6-luna, reasoning off). glm-5.3-flash and claude-haiku-5.5 scored ~0.1 higher on
  leases in E1b at +$0.001–0.002/doc; the switch is a pending decision (one env var).
- DOCX page numbers are not produced: paginating a DOCX needs LibreOffice, which would bloat the image and risk the
  60 s start-up gate. DOCX citations use section anchors and verbatim snippets instead.
- Vision on every scanned page (simple, robust) rather than only on pages that need it; cheaper variants are untested.
- Caching is on in production: identical requests (same document + field list, or a worker restarting a job) cost nothing.

## Models used for what, and spend

| Stage | Model | Typical cost |
|---|---|---|
| Field-list analysis (once per schema) | gpt-6-luna, reasoning off | ~$0.001 per new schema |
| Scanned-page transcription | gpt-6-luna, reasoning off | ~$0.0006 per page |
| Lease / OM extraction | gpt-6-luna, reasoning off (fallback: claude-haiku-5.5) | ~$0.003–0.005 per document |
| Table plan, repair, labels | gpt-6-luna, reasoning off | ~$0.001–0.003 per document |

Per document (uncached): digital lease/OM ~$0.003–0.005, scanned 25-page lease ~$0.022, any table ~$0.002.
**Development spend so far: $0.256 of the $10** (155 calls), almost all of it on experiments; every response is
cached, so re-scoring after a downstream change costs $0.

## What broke and how it was handled

- **A run took 80 minutes**: some calls hung for the full 300 s read timeout before retrying. Now every attempt has a
  *total* time cap (httpx's own timeout only bounds gaps between bytes), and retries stop at the job's deadline.
- **Hidden reasoning tokens** made one experiment cost 3.5× its estimate; estimates now include them, and reasoning
  is off by default where it was shown not to help.
- **Bad table plans** (columns declared "missing", wrapped text read only on its first line): generic checks now
  detect both and trigger one repair call.
- **Page headers in rebuilt PDF tables** looked like section titles: lines repeated on 3+ pages are removed.
- **The rent roll lists 5 units twice** (main list and a later "future residents" section): repeated primary keys
  are merged.

## Running it in production (ops notes)

- `docker compose up` starts `api` (port 8080) and one `worker`; more throughput: `WORKER_CONCURRENCY=8` or
  `docker compose up --scale worker=3` (all workers share the queue on the `data` volume).
- Logs are JSON lines on stdout: `docker compose logs worker | grep <job_id>` shows a job's stages, cost and error.
  A FAILED job's reason is also returned in its `error` field.
- Settings: `LLM_BASE_URL`, `LLM_API_KEY` (required); `PIPELINE` (v1), `EXTRACT_MODEL`, `VISION_MODEL`,
  `TABLE_MODEL`, `JOB_DEADLINE_S` (1200), `LLM_TIMEOUT_S`, `LLM_MAX_CONCURRENCY`.

## Issues found in the sample answers

- No clear errors found so far. One scoring artefact: lease_002's `base_rent_escalation` answer is "none", which the
  validator normalises to null, so any longer phrasing ("none; fixed monthly rent") counts as a hallucination.

## With another week

- Description rewriting with generic domain hints for the interpretation errors that every model shares
  (premises with floor text, landlord entity as property name, rent commencement after free rent).
- Escalation: run a second cheap model and send only disagreeing fields to a stronger one (disagreement caught
  >90% of errors in E1b).
- Generalisation tests on renamed and paraphrased field lists; a load test at 10 jobs in flight with the real pipeline.
