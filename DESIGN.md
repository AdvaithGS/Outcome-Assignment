# Design

Written for a colleague who has to run this on Monday. Status: **work in progress** (sample mean **0.936** on
`tools/validate.py`, no hallucinations; `docker compose up` verified end to end on the dev machine). The long-form record, with every
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
- **Scanned pages: local OCR first, vision only where it matters.** Tesseract reads every scanned page; pages with
  weak OCR, many low-confidence words, tables or form layouts go to the vision model (gpt-6-luna, reasoning off,
  ~$0.0006/page) — about 15% of a lease's pages, and every page where OCR lost an answer value in the samples.
  A scanned lease costs ~$0.009 instead of ~$0.022 at the same accuracy. Local OCR
  (Tesseract, RapidOCR) could not read the handwritten correction to a security deposit or a garbled name; vision
  could, and was faster than RapidOCR. Tesseract stays as the per-page fallback.
- **Field lists are analysed once per schema**: types, "value when absent" (0 / false / []), groups, rewritten
  descriptions, and formulas for computed fields in a small safe expression language. Field lists that are mostly
  bare names get standard real-estate definitions first (one extra call): a lease list with descriptions removed
  scores 0.80 instead of 0.65, with no invented values.
- **Leases and OMs: the whole document in one call.** Cheap models scored the same on full documents as on
  hand-picked relevant passages, so a Locator stage did not pay for itself at these sizes; it is reserved for very
  long documents.
- **Free-text fields are answered concisely, in the document's words, keeping every fact**, and the extraction prompt
  carries a few generic real-estate rules (identifier only; a property name is not the owner's company; rights count
  only for the named party; "none" for no-change terms). Together with the stage-0 descriptions these closed the gap
  between the cheapest model and the stronger ones (E2: luna 0.879, glm 0.873, haiku 0.873 strict).
- **Computed fields are calculated in code** from extracted operands; a figure the document prints wins. The model's
  own arithmetic was wrong in 4 of the lease computed cells; the formulas were right in all of them.
- **Tables: the LLM writes an extraction plan, code executes it** (the plan format is the user's design). Cost is
  ~$0.001–0.003 per table regardless of length, values are copied from cells (no invented values), and every value
  carries its cell address. Plans must state the cross-field relations their descriptions give ("should equal rent plus
  other charges") as checks that code verifies on every row; failing plans get two repair rounds, the last one by a
  second model (claude-haiku-5.5). The LLM-writes-rows fallback only runs for small tables (≤ 400 rows); larger ones use
  the best plan and finish PARTIAL, so cost stays bounded.
- **Tables in every format:** XLSX/CSV grids (including a table continued over several sheets); PDF tables rebuilt
  from word positions; DOCX tables; and scanned PDF
  tables, transcribed page by page by the vision model into Markdown tables with page 1's column layout imposed on
  the other pages (a scanned copy of the sample budget scores 0.991 against 0.999 for the original).
- **The same "LLM points, code fills" idea for single-entry documents** (pointer mode: each value backed by an exact
  quote that code locates) tied the current approach on accuracy (0.935 judged) with slightly more correct page
  citations; it is used by default only where it is strictly better: single-entry field lists over spreadsheets,
  whose values are read from the exact cells.
- **Service:** the SQLite job table is the queue (atomic claims, leases + heartbeats, fencing, idempotency by file +
  schema + tenant). A pipeline error never loses a document: it is retried, then finished as PARTIAL with valid nulls.

## Tradeoffs taken

- Cheapest model by default (gpt-6-luna, reasoning off): with the final prompt, glm-5.3-flash and claude-haiku-5.5
  were no better (E2), while costing 35–70% more per document.
- Free-text style: concise answers (best under strict scoring) rather than verbatim quotes (best under an LLM judge);
  switchable with `FREETEXT_MODE`, since the graders' judging of free text is not known exactly.
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
**Development spend so far: $0.690 of the $10** (562 calls, incl. two Docker load tests), almost all of it on experiments; every response is
cached, so re-scoring after a downstream change costs $0.

## What broke and how it was handled

- **A run took 80 minutes**: some calls hung for the full 300 s read timeout before retrying. Now every attempt has a
  *total* time cap (httpx's own timeout only bounds gaps between bytes), and retries stop at the job's deadline.
- **Hidden reasoning tokens** made one experiment cost 3.5× its estimate; estimates now include them, and reasoning
  is off by default where it was shown not to help.
- **Bad table plans** (columns declared "missing", wrapped text read only on its first line): generic checks now
  detect both and trigger one repair call.
- **Page headers in rebuilt PDF tables** looked like section titles: lines repeated on 3+ pages are removed.
- **A failed table plan triggered the LLM-writes-rows fallback on a 3,000-row sheet** (31 calls before a run cap
  stopped it): the fallback is now limited to small tables and bad plans get a second-opinion repair instead.
- **glm-5.3-flash rejects `reasoning: off`** (400, "Reasoning is mandatory"): every call fell back to the fallback
  model, which kept jobs succeeding; model settings are now checked per model in experiments.
- **The rent roll lists 5 units twice** (main list and a later "future residents" section): repeated primary keys
  are merged.

## Running it in production (ops notes)

- `docker compose up` starts `api` (port 8080) and one `worker` taking 10 jobs at a time with up to 24 concurrent
  model calls (load test at 10 in flight: p50 50 s, p95 83 s, 16/16 SUCCESS); more throughput: raise
  `WORKER_CONCURRENCY` or
  `docker compose up --scale worker=3` (all workers share the queue on the `data` volume).
- Logs are JSON lines on stdout: `docker compose logs worker | grep <job_id>` shows a job's stages, cost and error.
  A FAILED job's reason is also returned in its `error` field.
- Settings: `LLM_BASE_URL`, `LLM_API_KEY` (required); `PIPELINE` (v1), `EXTRACT_MODEL`, `VISION_MODEL`,
  `TABLE_MODEL`, `JOB_DEADLINE_S` (1200), `LLM_TIMEOUT_S`, `LLM_MAX_CONCURRENCY`.

## Issues found in the sample answers

- No clear errors found so far. One scoring artefact: lease_002's `base_rent_escalation` answer is "none", which the
  validator normalises to null, so any longer phrasing ("none; fixed monthly rent") counts as a hallucination; the
  extraction prompt now answers exactly "none" for no-change terms.

## With another week

- Rent commencement after free rent: a rule for it fixed that field but leaked into the rent schedule; needs a
  field-scoped hint rather than a global one.
- Escalation: run a second cheap model and send only disagreeing fields to a stronger one (disagreement caught
  >90% of errors in E1b).
- Generalisation tests on renamed and paraphrased field lists; a load test at 10 jobs in flight with the real pipeline.
