# Technical Report: Document Extraction Service

**Role:** AI/ML Developer. Scoring weights: Accuracy 35 · Cost 15 · Load/latency 15 · Design/code 35.

This is the long-form record of how the pipeline was built. `DESIGN.md` is the one-page summary of it;
`PLAN.md` holds the working decision register (IDs D3–D41 used below); `notes/` holds the working notes of each step
(ideation → revision → implementation → check), and `experiments/` the raw scores.

Each section follows the same layout:
- **Goal**: what this part must achieve and which score it affects.
- **Decisions**: the question, the options we considered, what we chose and the evidence.
- **Experiments**: what we ran, on which documents, at what cost.
- **Results**: numbers from `tools/validate.py` and the spend ledger.
- **Open issues**: known limits and what we would try next.

**Where it ended:** sample mean **0.936** on `tools/validate.py` (0.964 with an LLM judge on free-text fields),
**0 invented values**, every output valid, **~$0.004 per document** uncached; 10 jobs in flight in Docker from a fresh
clone: 16/16 SUCCESS, p50 25 s, p95 94 s, none lost or duplicated. Development spend **$0.84 of $10**. 117 tests.

---

## 1. Evaluation setup

- **Sample set:** 8 documents (scanned 1993 lease with handwriting, clean scanned lease, DOCX lease, digital OM, digital
  budget PDF table, T12 and rent-roll XLSX, box-score CSV) over 6 field lists. Scored with the official
  `tools/validate.py`: normalised exact match per cell (numbers ±0.5%, dates in any format), lists by order-insensitive
  F1, rows aligned by the primary field; a wrong value is a hallucination, a null where a value exists a miss.
- **Free-text judge:** the graders' scorer is "slightly more lenient on free-text fields", so a judge from another model
  family (claude-haiku-5.5) re-checks **only fields typed free text** (it was too lenient on identifiers, e.g. it
  accepted "Second Floor, West Section, Suite 214W" for "Suite 214W"). Verdicts are cached.
- **Noise:** repeated runs at temperature 0 change 0–4 values per document, **±0.02–0.03** on a 3–4 document mean;
  smaller differences are ties. Decision rule: *the cheapest option within noise of the best*.
- **Cost discipline:** every response is cached (content-addressed); dry-run estimates (including hidden reasoning
  tokens) and a per-run cap before every paid run; free measurements first; `replay` mode re-scores downstream changes
  at $0 and fails instead of spending if a prompt changed.
- **Beyond the samples (E5 and later):** unseen field lists (the contract's box-score list; keys renamed to `field_NN`
  with descriptions removed), format changes (rent roll as CSV, split over two sheets), a scanned copy of the budget
  (every page an image), 20 fresh uncached table plans (plan variance), 15 bad or mismatched inputs (failure matrix),
  old-vs-new behaviour of every code change on every sample cell, confidence calibration, and five Docker load tests.

| Iteration | Change | Mean (8 docs) | Invented | $ / doc (uncached) |
|---|---|---|---|---|
| All-null baseline | valid output, no extraction | 0.087 | 0 | 0 |
| Pipeline v1, single-entry only | stage 0 + whole-document extraction + computed fields in code; tables null | 0.845 on the 4 single-entry docs | 1 (validator artefact) | — |
| + tables | LLM-written plans executed in code | 0.922 | 0 | — |
| + E2 | concise free text, generic hints v3, cheapest model kept | 0.939 | 0 | 0.0075 |
| + checks, pointer for spreadsheets, scanned/DOCX/multi-sheet tables | robustness on unseen formats | 0.939 | 0 | 0.0075 |
| + selective vision | Tesseract first, vision only on hard pages | 0.936 | 0 | **0.0042** |
| + terse definitions, review fixes, failure handling, confidence | unseen field lists and inputs (samples unchanged) | **0.936** | **0** | ~0.004 |

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
| D8 | Multi-tenancy | Skip; implement | **Implemented**: jobs scoped by `X-Organization-Id`; other org (or none) gets 404 | Test; checked in every load test |
| D9 | Webhook | Skip; best effort | **Best effort**, JobStatus body, 3 tries with backoff, after the terminal state is committed | — |
| D10 | File storage and size limits | DB blobs; volume files | **Content-addressed files on the volume**; streamed with a 50 MB limit (413); type detected from content (PDF / DOCX / XLSX / CSV) | Tests: 413 leaves no partial file; legacy .doc → 400 |
| D11 | PARTIAL vs FAILED | — | **FAILED** only for unreadable documents (with reason); pipeline errors are retried, then **PARTIAL** with valid null fields; empty documents and "no table found" are PARTIAL with valid nulls and a note | A bug never loses a document; nulls cost nothing; the gate counts terminal status *with valid output* |
| D12 | Per-job deadline and degradation | None; hard kill | **Internal deadline 20 min from submission** (queue included; minimum 30 s run) → PARTIAL with what exists | Test |
| D12b | Concurrency and CPU work | 4 jobs / 8 model calls per worker; more | **10 jobs and 24 concurrent model calls per worker**; page rendering and OCR in **their own pool** (available CPUs − 2, cgroup quota respected, `OCR_WORKERS`) with Tesseract at **nice 10**; the worker's default thread pool sized to its job slots | Load tests below |

**Experiments:** `app/eval/loadtest.py` keeps 10 jobs in flight and tops up as jobs finish, polling every 2 s like the
graders' harness; 16 jobs = the 8 samples × 2 tenants (separate jobs; isolation checked on every job); Docker, real
pipeline, LLM cache **off** (realistic cost). Dev machine: 12-thread laptop CPU (i7-1355U).

**Results**

| Run | Setup | Terminal | p50 / p95 / max | POST max | Wall (16 jobs) | Cost |
|---|---|---|---|---|---|---|
| 1 | 4 jobs / 8 calls per worker, full vision | 16/16 SUCCESS | 129 / 203 / 227 s | 145 ms | 249 s | $0.105 |
| 2 | 10 / 24 | 16/16 | 49.5 / 82.6 / 98.7 s | 447 ms | 99 s | $0.054 |
| 3 | + selective vision | 16/16 | 104.7 / 149.7 / 183.9 s | **4,179 ms** | 184 s | $0.044 |
| 4 | + own OCR pool, nice | 16/16 | 29.4 / 114.7 / 130.8 s | 146 ms | 131 s | $0.034 |
| **5** | **all fixes, fresh clone** | **16/16 SUCCESS, 0 lost / duplicated, isolation ✓** | **25.2 / 94.4 / 105.3 s** | **95 ms** | **107 s** | $0.036 |

Run 1 was queueing: 20 s table jobs waited ~200 s behind scanned leases in 4 slots. Run 3 was a regression: selective
vision moved scanned pages from the network to the CPU (Tesseract ~4 s/page single-threaded), and 92 page tasks filled
the event loop's default thread pool, so claims, heartbeats and spreadsheet reads queued behind OCR (table jobs 103 s
instead of 16 s) and the saturated CPU slowed the API (POST 4.2 s, over the brief's 2 s). A heartbeat stuck in that
queue for > 120 s could also have let a lease expire. Costs of runs 2–5 are halved by the client's in-flight
deduplication of the two identical copies; distinct documents cost ~$0.0035 each in run 5. `/health` answered 1.5 s
after `docker compose up` (image built; a first build takes ~3 min).

**Open issues:** scaling by worker *containers* (`--scale worker=N`) is designed and unit-tested (shared SQLite queue)
but was not load-tested; scanned leases remain CPU-bound (on a small VM they would be slower; `SCANNED_MODE=vision`
trades ~$0.013 per scanned lease for network-bound latency).

---

## 3. Document ingestion

**Goal:** turn PDF (digital and scanned), DOCX, XLSX and CSV into a representation the models read
accurately and cheaply, keeping page and cell locations for citations. Affects Accuracy and Cost.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D13 | Digital PDF representation | pdftotext (plain, layout), pdfplumber, PyMuPDF, pymupdf4llm | **pdfplumber** per page | E0: all capture 99–100%; layout mode 2× tokens for nothing; pdfplumber best on the budget table |
| D14 | Scanned PDF (OCR vs vision vs hybrid) | Tesseract, RapidOCR, vision on every page, hybrid | **Selective vision**: Tesseract on every page, vision (gpt-6-luna, reasoning off, 1,500 px) only on pages flagged hard; Tesseract fallback; table documents vision on every page | E1a: only vision reads handwriting; selective = same accuracy, −60% scanned cost |
| D15 | Detecting a missing text layer | Whole-document flag; per page | **Per page**: < 50 non-space characters → scanned; blank pages (< 0.05% dark pixels) skipped before OCR or vision | OM has one image-only page; blank test PDF |
| D16 | DOCX (text, tables, embedded images) | python-docx, mammoth, own XML parser | **Own parser**: heuristic and run-in headings, TOC mode, auto-numbering, tables, tracked deletions skipped, **strikethrough marked ~~like this~~**, hidden text skipped, embedded images OCR'd (RapidOCR) | E0 sections (49, ~240 tokens) and image transcription |
| D17 | XLSX/CSV serialisation | Text dump; grid | **Grid with cell addresses**; merged cells filled; formulas as cached values; **percent-formatted cells as displayed** ("5.25%"); CSV encoding and delimiter sniffed | Sample rent roll (13 percent cells) |
| D18 | Page and cell location tracking | Post-hoc search; inline anchors | **Inline anchors** `[Page N]`, `[Section NNN: …]`, `[Table ID]`, `[Image ID]`, cell refs `Sheet!F12`; rows of rebuilt PDF tables keep their page | Citations and `page_numbers` / `source_locations` come from these |

**Per-document findings**

| Doc | Format | Representation used | Issues seen | Fix |
|---|---|---|---|---|
| boxscore_001 | CSV | Grid (213 cells) | Header values (property, date) above the table | Plan constants with their cell |
| budget_001 | PDF (text) | Grid rebuilt from word positions (pdfplumber) | GL code and name in one cell; wrapped explanations; repeated page headers looked like section titles | Partial-cell columns; `join` blocks; lines recurring on 3+ pages kept once |
| lease_001 | PDF (scanned, 1993) | Tesseract + vision on 6 of 25 pages | Handwritten correction (deposit $2,400 struck → 2150), pre-printed "19__" years filled by hand, garbled guarantor name | Vision reads all three (E1a); the page rule flags those pages |
| lease_002 | DOCX | Own XML parser + image OCR | No heading styles; run-in headings ("17. HOLDING OVER. Tenant…"); TOC repeats headings; no Word tables; no pagination; 3 images (signature page with handwritten date, floor plan, logo); Word stores one image twice | Heuristic + run-in heading detection, TOC mode, numbered anchors, tabs → " \| ", image dedupe |
| lease_003 | PDF (scanned, clean) | Tesseract + vision on 2 of 21 pages | Rent table garbled by OCR | Table rule sends it to vision |
| om_001 | PDF (text) | pdfplumber | One image-only page; ~110 tiny heading sections | Per-page scan detection; pages as the location unit |
| t12_001 | XLSX | Grid (2,288 cells) | TOTAL rows carry their own GL code; section header rows have no amounts; header value "Property =  417 .417" | Header fields; constants as printed in their cell (§5) |
| rentroll_001 | XLSX | Grid (15,754 cells, 3,000+ rows) | Multi-line unit records; 5 units listed twice ("future residents"); percent-formatted summary cells | `record_end` / block modes; primary-key dedupe; displayed percentages |

**Experiments**

- **E0 (free, no LLM):** every applicable ingestion option on the PDF and DOCX samples. Metrics: *captured* =
  share of non-computed answer values present in the extracted text in any common format (an upper bound on
  extraction accuracy); *right page* = found on a page the answer cites; citations located; tokens; CPU time;
  anchor slice sizes. Digital docs were also rendered and OCR'd to give scans with a known answer key.
- **E1a (targeted, $0.0142):** 4 images where local OCR failed or that act as a control (lease_001 p2, p25, p18;
  lease_002 signature image) × the 4 cheapest vision models, scored on the answer values cited on each page plus
  handwriting checks. Local OCR scored on the same images as a free baseline. A second, more expensive round was
  planned but not needed.
- **Selective vision ($0.013):** a per-page study ($0) labelled each scanned page by whether OCR lost an answer value
  that vision caught, then tested Tesseract-only signals; then a paid comparison against vision on every page, and an
  image-size test on the 4 hardest pages.

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

**Results: E0, coordinates**

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

**Results: selective vision.** Vision marks "handwriting" on nearly every page of the 1993 lease (initials), so the
useful label is *OCR lost an answer value*: lease_001 p2 (handwritten deposit), p25 (garbled guarantor), lease_003 p3
(rent table). A page goes to vision when any signal fires:

| Signal | Threshold | On the samples |
|---|---|---|
| Mean Tesseract word confidence | < 0.88 | lease_001 p2, p25 and others |
| Words below confidence 50 | ≥ 5% | overlaps confidence |
| Lines with 3+ numbers (tables, rent schedules) | ≥ 15% | lease_003 p3 |
| "LABEL:" lines (summary / reference pages) | ≥ 10% | form pages |
| Garbled text: broken tokens / unusual characters | ≥ 5% / ≥ 2% | only a page already flagged (safety net) |
| One uncertain line: 2+ words below 60, a digit and a real word, not a stamp/footer recurring on 2+ other pages (fuzzy, letters only, similarity ≥ 0.75) | 1 line | alone it catches both lease_001 pages; no extra pages |

All 3 needed pages flagged; 6/25 + 2/21 pages sent to vision. Exact-match footer filtering failed (Tesseract reads the
same stamp as "1D#3 03122", "ID#3 03118"); looser line rules flagged 16–19 of 25 pages; a dictionary check was not
possible (no word list in the image).

| Scanned leases | Strict | Judged | Cost per scanned lease |
|---|---|---|---|
| Vision on every page | 0.879 | 0.935 | ~$0.022 |
| **Selective (adopted)** | 0.873 | 0.923 | **~$0.009** |

Changed cells: one yes/no judgement and two free-text phrasings; no OCR-sensitive value changed. Image size: 1,100 px
cuts vision input ~45% but lost the landlord's legal name on the hardest page; with selective vision the saving would
be ~$0.001 per document → **1,500 px for every document** (`VISION_LONG_SIDE`).

**Open issues:** scanned PDFs that carry a scanner's hidden OCR text layer are treated as digital (our OCR and the
vision rule are bypassed); in table PDFs where fewer than half the pages are scanned, rows on the scanned pages are not
read; DOCX has no pagination (no `page_numbers`; LibreOffice would bloat the image).

---

## 4. Schema understanding

**Goal:** work with any field list, including unseen ones: infer field types, improve the
descriptions, and tell single-entry documents from tables. Affects Accuracy and the robustness gate.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D19 | Field type inference | Heuristics only; LLM only; both | **Heuristics first (free, deterministic), one LLM call confirms**: types (number, integer, percent, date, month, year, bool, text, freetext, list), value when absent (only when the description says so), scope, group, computed-field formulas | If the call fails, the heuristic types and original descriptions are used |
| D20 | Description rewriting | None; per known schema; generic | **Generic rewrite per new field list** (keep every constraint, state the output format, resolve ambiguity in the direction the original implies), cached by schema hash; **standard real-estate definitions first** when ≥ 25% of fields are terse (empty, a repeat of the name, ≤ 3 words) | E1b: most errors were interpretation errors shared by every model; E5 terse variants below |
| D21 | Document shape and document vs row fields | Detect from the document; from the field list | **From the field list**: a field marked `primary` → one entry per row; scope per field (row / document) | Contract: exactly one primary field in table lists |

**Field taxonomy observed in the samples**

| Kind | Examples | Handling |
|---|---|---|
| Document-level scalar | `property_name`, `report_date` | Single-entry: extracted once; tables: plan `constants` with their cell, read as printed |
| Row-level scalar | `gl_code`, `unit_number` | Plan columns / block fields, copied from cells |
| Computed | `lease_term_months`, `total_base_rent`, `price_per_unit`, `months_with_activity` | Stage-0 formula in a safe expression language, evaluated in code; the printed figure wins when there is one |
| List-valued | `rent_schedule_*`, `broker_names`, `unit_types` | JSON arrays in document order; aligned lists checked by length |
| Frequently absent | `parking_monthly_charge`, `guarantor_name` | Null unless the description defines an absence value (0 / false / []), applied only after a real extraction |

**Experiments:** E5 generalisation ($0.0125 + re-runs) and the terse-definition step ($0.027, two iterations).

**Results**

| Variant | Score | Invented |
|---|---|---|
| Box score with the contract's field list (different keys) | 1.000 | 0 |
| T12, keys → `field_NN`, descriptions = display name only: before → **with definitions** | 0.542 → **0.781** (119/119 rows) | 0 |
| lease_002, same: before → with definitions | 0.650 → **0.800** (full descriptions 0.825) | 0 |
| lease_001, same: before → with definitions | 0.625 → **0.750** (full descriptions 0.850) | 0 |

The first version of the definitions regressed (lease_002 0.550 with 9 invented values): it invented absence
conventions ("0 if absent", "empty string if absent") that the pipeline then applied. The adopted version says "null if
the document does not state it", with two exceptions (yes/no → false, lists → empty). The OM's single "City."
description triggered expansion on a rich list, hence the 25% threshold. None of the 6 sample lists triggers it.

**Open issues:** a field list of ~75+ fields would exceed the extraction call's 12,000-token output cap (the 40-field
lease list uses up to 6,500); stage 0 already writes groups, which could be extracted as parallel calls.

---

## 5. Extraction strategy

**Goal:** get correct values at the lowest cost. Single-entry documents (leases, OMs) and table
documents (rent roll, T12, budget, box score) need different strategies. Affects Accuracy and Cost.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D22 | Single-entry docs: one call / chunked / per field group | Locator + slices; per group; whole document | **Whole document, one call** (Locator reserved for 100+ page documents) | E1b: cheap models scored the same or better on full documents than on hand-picked passages |
| D23 | Table docs: LLM rows / LLM mapping + code / hybrid | LLM writes rows; LLM writes code; **LLM writes a plan, code executes it** | **Plan** from a fixed-size preview, executed on the full grid, required cross-field checks, 2 repairs (last by a second model), LLM-writes-rows fallback only ≤ 400 rows | Cost flat (~$0.001–0.003 for 3,000 rows); values copied from cells; tables 0.999–1.000 |
| D24 | Computed fields: LLM vs deterministic code | M (model), C (formula), H (printed figure, else formula, else model) | **H** | E2a: model arithmetic wrong on 4 lease cells; formulas right on all |
| D25 | Structured output and repairing bad responses | Trust; repair; fallback | JSON mode → **one repair retry** with the error stated → **fallback model** → valid nulls; plans: checks → repairs → bounded fallback; a failed call moves to the next attempt | Fallback proven in practice (glm's 400s all fell back to haiku) |
| D26 | Model routing per stage | Strong models; per-stage cheapest within noise | **gpt-6-luna, reasoning off, everywhere**; claude-haiku-5.5 as fallback and last table-plan repair | E2-models below; reasoning tokens were pure cost |
| D27 | Prompt design | Describe; verbatim; concise; hints | **Concise free text** + **generic hints v3**; pointer/hybrid modes available, hybrid used for single-entry lists over spreadsheets | E2 tables below |

**Model routing table**

| Stage | Model | Why | Fallback | Typical $ |
|---|---|---|---|---|
| Field-list analysis, terse definitions | gpt-6-luna, reasoning off | Cheapest; once per new field list | Heuristics | ~$0.001 per list |
| Scanned-page transcription (hard pages; every page of scanned tables) | gpt-6-luna, reasoning off | Reads handwriting and strike-through (E1a); cheapest | Tesseract per page | ~$0.0007 per page |
| Lease / OM extraction | gpt-6-luna, reasoning off | Tied glm and haiku with the final prompt (E2) | claude-haiku-5.5 | ~$0.003–0.007 per doc |
| Table plan, repairs, labels | gpt-6-luna; last repair claude-haiku-5.5 | Second opinion from another family only when a plan fails twice | Rows fallback ≤ 400 rows | ~$0.0005–0.003 per doc |
| Free-text judge (evaluation only) | claude-haiku-5.5 | Different family from the extractor | — | — |

**Experiments and results**

*E1b, model screen ($0.172; original descriptions, later superseded).* Full documents, free text judged:

| Model | Score, full documents | $/doc (list) | s/doc |
|---|---|---|---|
| glm-5.3-flash | 0.853 | 0.0038 (billed $0) | 13 |
| claude-haiku-5.5 | 0.835 | 0.0048 | 17 |
| deepseek-v3.2 | 0.794 | 0.0051 | 44 |
| gpt-6-luna, reasoning off | 0.743 | 0.0025 | 13 |

Findings: no long-context penalty; most non-free-text errors were shared by every model (interpretation, not
capability); free text was mostly correct paraphrase failed by the strict matcher; hallucination near zero (19
should-be-null cells all right); **cross-model disagreement flagged ~24% of cells and caught 91–93% of the errors**;
reasoning on was not better and cost ~40% more; a gemini-flash second round fixed 8 of 17 failures but none of the
shared interpretation errors.

*E2, closing the gap with the prompt (4 single-entry docs; judged = free-text fields judged):*

| Change | Strict | Judged |
|---|---|---|
| Free text as described (baseline) | 0.845 | 0.889 |
| Free text verbatim (quote) | 0.835 | 0.928 |
| **Free text concise** (example's format, document's words, every fact kept) | 0.858 | 0.914 |
| + hints v1 | 0.885 | 0.929 (an address-like building name became "None") |
| + hints v2 | 0.864 | 0.933 (free-rent rule spilled into the rent schedule) |
| **+ hints v3** (identifier only; property name ≠ owner's company; rights only for the named party; "none" for no-change terms) | **0.879** | **0.935** |

Model choice with the final prompt: **gpt-6-luna 0.879 / 0.935 ($0.0035/doc)** · glm 0.873 / 0.935 ($0.0047) · haiku
0.873 / 0.923 ($0.0060) → keep luna. Hint tuning stopped at v3 to avoid fitting three leases.

*E2a, computed fields (one response, three strategies):*

| Mode | Mean (4 single-entry docs) | Notes |
|---|---|---|
| M: the model's own arithmetic | 0.833 | Wrong on 4 lease cells (sums over periods, rounding, net vs gross of free rent) |
| C: code evaluates the stage-0 formula | 0.845 | All 4 right |
| **H: printed figure if printed, else formula** | **0.845** | Chosen; agreement raises confidence |

*Pointer mode (the "LLM points, code fills" idea applied to prose):*

| Approach | Strict | Judged | Correct pages / given | $/doc |
|---|---|---|---|---|
| **value (default)** | 0.879 | 0.935 | 78 / 84 | ~0.0035 |
| pointer | 0.879 | 0.935 | 81 / 91 | ~0.0039 |
| hybrid | 0.860 | 0.935 | 81 / 91 | ~0.0039 |

Tied → value stays the default for documents; hybrid is used automatically for single-entry lists over spreadsheets.

*Tables.* LLM-written plans on the samples: boxscore 1.000, rentroll 1.000, t12 0.999, budget 0.999. Safeguards, each
added after a measured failure:

| Safeguard | Failure that motivated it |
|---|---|
| A per-row field whose description mentions a column cannot be "missing" | T12 plan dropped the first/last month columns |
| Free text continuing onto following lines must be joined | Budget plan read only the first line of each explanation |
| `value` mode matching several different lines is flagged | Rent roll units with two "Rent" lines |
| **Required cross-field checks** from the descriptions, verified on every row | A plan read the rent-roll total from the wrong line; optional checks were ignored |
| Two repair rounds, the last by claude-haiku-5.5 | luna's repairs could not fix the CSV rent roll; haiku's did (0.946 → 1.000) |
| LLM-writes-rows fallback only ≤ 400 rows, else best plan + PARTIAL | A failed plan on a 3,000-row sheet ran 31 fallback calls |
| Last multi-line record no longer than the longest other record | Without a closing row it absorbed the totals below the table |
| Document-wide constants as printed in their cell (text fields) | Fresh plans shortened "417 .417" to "417" (below) |

Not adopted: other plan models (glm, haiku each failed a different table); majority vote over three plans (fixed one
table, broke another). Scanned tables: page 1 transcribed first and its column header imposed on the other pages
(scanned budget 0.701 → 0.930 → **0.991**); tables over several sheets: one plan per sheet (two-sheet rent roll 1.000).

*Plan variance ($0.033).* 5 fresh, uncached plans per sample table (stage 0 from cache): box score, budget and rent roll
scored the same every time; T12 scored 0.904–0.989 because plans typed the header constant themselves and shortened it
in 4 of 5 runs, and the label step varied with its inputs.

| T12, the same 5 fresh plans | Mean | Category (label) score |
|---|---|---|
| Before | 0.923 | 0.891–1.000 |
| Constants as printed + labels on the row's own fields, sorted order (**adopted**) | **0.998–0.999** | 0.950–1.000 (two repeats: 0.983, 0.993) |
| Labels in document order | 0.996 | 0.958–0.975 (consistently worse) |
| Majority of 3 label calls | 0.998 | 0.958–1.000, 4× the label cost |

**Open issues:** the remaining lease errors are free-text phrasing under strict matching and four interpretation calls
every model makes (early termination right, rent commencement after abatement, property name vs building alias,
subject property's in-place rent); field-scoped hints would fix some but risk fitting the samples.

---

## 6. Post-processing and trust

**Goal:** a plausible guess is worse than a null. Normalise values, verify them against the
source, give each one a meaningful confidence, and turn unverifiable values into nulls. Affects
Accuracy (the hallucination penalty and the citation bonus).

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D28 | Normalisation rules | Trust the model; normalise by type | **By stage-0 type**: one figure per value ($, thousands, "(1,234)" negatives, units and multipliers such as "$24.50/SF", "$1.2 million"); **a string with several figures is null** ("1,200-1,500" was read as 12001500); labels are not amounts ("C5", "Feb 2023"); dates in many formats (day first only when it must be); a value that does not parse is null | Old-vs-new on every sample cell (below) |
| D29 | Verification | None; citation grounding; second pass | **Citation grounding** (the cited snippet must be in the document) + **value search** (the value itself, any format) + table cross-field checks; a citation that cannot be found lowers confidence but never nulls a value | E1b: citation-not-found is a weak error signal (recall ≤ 0.41) |
| D30 | Confidence estimation | Flat; self-reported; evidence-based | **Evidence-based** (below) | Calibration against the answers |
| D31 | Null policy | Fill defaults; null | **Null unless stated**; absence values (0 / false / []) only when the description defines them *and* something was extracted; empty or unreadable documents → all null | Failure matrix (below) |
| D32 | Citations, `page_numbers`, `source_locations` | — | Verbatim snippet with its anchor; pages from the anchor or by locating the value (≤ 2 pages); cell refs for spreadsheets; page per row for rebuilt PDF tables | Budget: 741/741 values with pages matching the answers |

**Experiments and results**

*Code review, old vs new on every sample cell (~35,000, $0).* The final parser changes only: 330 unit ids ("100-101")
and 140 GL codes ("500-000") previously read as the numbers 100101 and 500000 → null (codes, not amounts); 13 rent-roll
percent cells 0.8765 → 87.65; two "2021." → 2021. A first version read labels as amounts (`C5` → 5, `PR - P1` → 1,
`Property ID: 417` → 417), which would also have hidden a plan that maps a number field to a label column; caught by
this comparison and fixed. Sample outputs byte-identical.

*Failure matrix (15 bad or mismatched inputs, model calls stubbed, $0):*

| Input | Before | After |
|---|---|---|
| Corrupt / password-protected PDF, corrupt DOCX | FAILED `unreadable_document` | unchanged |
| Random bytes, legacy .doc, PNG named .pdf | 400 `unsupported_type` at upload | unchanged |
| Every extraction call failed | PARTIAL with **8 values guessed from absence defaults** | PARTIAL, all null |
| Corrupt XLSX | retried 3×, then PARTIAL | **FAILED** `unreadable_document` with the reason |
| Blank PDF / empty DOCX | 1 vision + 2 extraction calls | PARTIAL all-null, **no model call** after stage 0 |
| Empty XLSX / header-only CSV | plan calls, job retried | PARTIAL all-null, no plan call |
| Digital prose PDF + table field list | **vision on every page** | text-line grid, no calls |
| Table plan call fails | job retried up to 3× | next attempt, then fallback, then valid nulls |
| "No table found" | SUCCESS | PARTIAL with a note |

*Confidence calibration (sample outputs; values unchanged, mean 0.936):* free text 0.7 cited / 0.55 not; yes/no 0.8 /
0.65; computed 0.85 ± agreement of printed figure, formula and model; printed values 0.92 when both the citation and
the value are found, 0.75 one of them, 0.5 neither; nulls after a real extraction 0.75, 0.3 when nothing was read.
Grounding is the real signal: text and date values located on a page were right 100% of the time (29 cells), those that
could not be located 60–75%; free text matched strictly 25% of the time (paraphrase).

| | Brier (lower is better) | Calibration error (ECE) | Values at ≥ 0.9: right |
|---|---|---|---|
| Single-entry, before | 0.152 | 0.164 | 82% |
| **Single-entry, after** | **0.108** | **0.078** | **96%** |
| Tables, before → after | 0.022 → 0.012 | 0.115 → 0.095 | 100% |

**Open issues:** confidence is not yet *acted on*; the obvious next step is a second cheap model on fields below ~0.8,
escalating only disagreements (E1b: disagreement caught > 90% of errors). The calibration was checked on 8 documents
and uses generic evidence rules, not fitted thresholds.

---

## 7. LLM client and cost

**Goal:** survive 429s and transient errors without failing jobs, account for every token, and
keep development spend inside $10. Affects Cost and Load/latency.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D33 | Retries, backoff, rate limiting, fallback model | — | **Inside the LLM client**: 429 / 5xx / timeouts / unusable 200 bodies retried up to 6 times, exponential backoff with full jitter, Retry-After honoured; a *total* time cap per attempt (`asyncio.wait_for`; httpx's timeout only bounds gaps between bytes); deadline-aware retries; per-process concurrency limit (`LLM_MAX_CONCURRENCY`, 24) | Tests: 429 → 502 → bad body → success counts 4 calls; non-retryable 400 not retried; the 80-minute run (calls hanging 300 s) |
| D34 | Cost accounting and the `metadata` breakdown | Estimate from tokens; provider cost | **Provider-reported cost** (`usage: include`) where given, price table otherwise; per-job `UsageTracker` → `metadata` with calls, tokens, cost, latency, cache hits, uncached cost and a per-model breakdown | `metadata` of every output; each ledger row records whether its cost came from the provider or the price table |
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
| Pipeline v1, run 1 (4 single-entry docs, incl. 46 vision pages) | 67 | 0.0487 | 0.49% |
| Pipeline v1, run 2 (re-extraction after fixes) | 6 | 0.0135 | 0.13% |
| Pipeline v1, table documents (stage 0 + plans + repairs + classify) | 11 | 0.0074 | 0.07% |
| Table plans re-run after a prompt change (reproducibility) | 3 | 0.0029 | 0.03% |
| Docker end-to-end check (box score in the container) | 2 | 0.0013 | 0.01% |
| E2 free-text variants (verbatim, concise) + judge | 12 | 0.0291 | 0.29% |
| E2 hints (3 runs) + judges | 17 | 0.0434 | 0.43% |
| E2 extractor model choice (glm, haiku) + judge | 13 | 0.0236 | 0.24% |
| E5 generalisation variants | 9 | 0.0125 | 0.13% |
| Improvement round 1: table checks, plan-model comparison, cross-field checks | 26 | 0.0253 | 0.25% |
| Pointer mode on samples + E5 re-runs (incl. a capped 31-call fallback) + judge | 44 | 0.0578 | 0.58% |
| Hybrid pointer mode + haiku final table repair + judge | 5 | 0.0156 | 0.16% |
| Scanned table PDFs (3 iterations on a scanned budget) | 29 | 0.0198 | 0.20% |
| Multi-sheet tables + re-run of sample table plans | 8 | 0.0057 | 0.06% |
| Load tests in Docker (2 configurations × 16 jobs, cache off) | 206 | 0.1587 | 1.59% |
| Selective vision + resolution test + judge | 8 | 0.0124 | 0.12% |
| Terse field-list expansion (2 iterations on E5 variants) | 16 | 0.0268 | 0.27% |
| Load tests in Docker with selective vision (3 runs × 16 jobs, cache off) | 107 | 0.1134 | 1.13% |
| Table variants re-run on current code (scanned budget, CSV rent roll, contract box score) | 4 | 0.0022 | 0.02% |
| Plan variance (20 fresh table plans) | 32 | 0.0128 | 0.13% |
| Label-step variants (4 + 4 variants × 5 plans) | 60 | 0.0199 | 0.20% |
| **Total** | **765** | **0.8379** | **8.38%** |

Computed from `ledger/spend.jsonl` by run id, plus the Docker containers' own ledgers (`experiments/load/*.json`).

**Results:** ~$0.004 per document uncached on the samples (scanned 25-page lease ~$0.007–0.009, digital lease or OM
~$0.003–0.005, tables $0.0005–0.003); stage 0 is shared by all documents with the same field list; production keeps
the cache on, so a restarted job never pays twice.

**Open issues:** the graders measure cost on their own gateway; the remaining cost is mostly the extraction call on
long documents (prompt tokens), which a Locator would cut for 100+ page documents.

---

## 8. Quality loop, tests and operations

**Goal:** show that the eval loop was used, catch regressions, prove behaviour under load, make
failures diagnosable, and start cleanly with Docker. Affects Design/code and the gates.

**Decisions**

| ID | Question | Options considered | Chosen | Evidence |
|---|---|---|---|---|
| D36 | Eval harness | Ad hoc; harness | **`app/eval/`**: `run_samples` (official validator, per-doc errors, cost estimate, `--tag` per variant), `judge` (free-text fields only), `generalise` (E5 variants), `loadtest`, E0/E1a/E1b; replay mode for $0 re-scoring | Every decision above has a run behind it |
| D37 | Generalisation to unseen field lists | Trust; simulate | **Simulated**: contract field list, renamed keys, removed descriptions, CSV and two-sheet conversions, a scanned copy | §4 results; re-run on current code |
| D38 | Test suite scope | Happy path; regressions in what matters | **117 tests**: service guarantees (claims, leases, fencing, idempotency, isolation, deadlines, 413/400), LLM client (retries, timeouts, cache modes, single-flight, budget), ingestion (DOCX headings/strike/hidden, grids, percent cells, scanned detection), formulas, normalisation (ranges, units, labels, dates), table plans (checks, repairs, last-record cap, constants), failure handling (blank, empty, corrupt, failed calls) | `pytest` |
| D39 | Load test | None; in-process; Docker | **Docker, real pipeline, cache off**, 10 in flight, 2 tenants | §2 results |
| D40 | Logging and observability | Plain logs; structured | **JSON lines** with job_id, worker, stage, timings, cost, error; pages sent to vision with the reason; failure reason stored on the job and returned in `error` | — |
| D41 | Docker packaging | — | python:3.14-slim + tesseract + OpenCV libs; RapidOCR models inside the wheel; compose services `api` + `worker` on one `data` volume; healthcheck; `LLM_*`, pipeline and service settings passed through | Fresh clone: build ~3 min, `/health` 1.5 s after `up`; five load tests |

**Results:** the reproducibility loop caught real problems at $0 several times: a stale cached plan after a prompt
change (replay miss), the scanned-table regression from selective vision (replay of the scanned-budget variant), a
constants change that broke a typed field (scanned budget 0.991 → 0.919, fixed back to 0.991), and a parser change that
read labels as amounts (old-vs-new comparison on every cell).

**Open issues:** a load test with several worker containers; the image is 1.46 GB (duplicate OpenCV builds).

---

## 9. Issues found in the sample answers

| Doc | Field | Truth | Document evidence |
|---|---|---|---|
| lease_002 | `base_rent_escalation` | "none" | The validator normalises "none" to null, so any longer phrasing ("none; fixed monthly rent") counts as a hallucination; the prompt answers exactly "none" for no-change terms |
| t12_001 | `property_name` | "417 .417" | The header cell reads "Property =  417 .417"; the answer is the printed value, not the property id "417" (plans now take printed values) |

No clear errors were found in the answers.

## 10. Future work

1. **Act on confidence:** a second cheap model on fields below ~0.8, escalating only disagreements to a stronger model
   (E1b: disagreement caught > 90% of errors at ~+$0.004/doc).
2. **Field-scoped interpretation hints** (rent starts after free rent; the subject property's in-place rent), tested on
   documents outside the samples.
3. **Large field lists:** extract by stage-0 group in parallel calls above ~50 fields.
4. **Scanned PDFs with a hidden OCR text layer** treated as scanned; mixed scanned/digital table PDFs.
5. **A Locator path for 100+ page documents** (designed in `notes/04-layer1.md`); send a page to vision when the
   extraction leaves a field on it null.
6. **Operations:** a multi-container load test, commit SHA in `/health`, a slimmer image.
