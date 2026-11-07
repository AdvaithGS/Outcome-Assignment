# Step 2: Experiment design for groups 3–5 (ingestion, schema understanding, extraction)

Status: **IDEATION.** No option is chosen. This note defines *how* we choose: what we measure,
which options compete, in what order, at what cost, and the decision rule. The final choices are
made together at the decision points (marked ◆).

---

## 1. Principles

1. **Measure before choosing.** Every option in groups 3–5 is a configuration of one pipeline, and
   the winner is the one the numbers pick.
2. **Cost and accuracy are the objective; latency and reliability are constraints.**
3. **Free measurements before paid ones.** Most ingestion questions can be answered with zero LLM spend.
4. **Never cross everything with everything.** A full factorial (formats × OCR × strategies × 9 models)
   costs hundreds of dollars. We screen stages separately, then confirm interactions on the finalists.
5. **Guard against overfitting 8 documents.** The test set is larger, has unseen field lists, and is
   both easier and harder. Prompts stay generic, and a holdout of perturbed variants is kept untouched until the end.

## 2. What the numbers say so far (free measurements, done during ideation)

### 2.1 Document sizes (≈ chars / 4 = tokens)

| Doc | Representation | ~Input tokens | Notes |
|---|---|---|---|
| boxscore_001 | CSV text | 0.3k | |
| budget_001 | pdftotext raw / -layout | 4k / 13k | Layout mode triples tokens; does it buy accuracy? (to test) |
| om_001 | raw / -layout | 7k / 15k | |
| lease_002 | DOCX text | 18k | |
| lease_001 | 25 scanned pages | ~19k as OCR text, or 25 page images | Image token cost per page varies by model and resolution, to be measured |
| lease_003 | 21 scanned pages | ~16k OCR, or 21 images | |
| t12_001 | 1,894 non-empty cells | ~6–10k | |
| rentroll_001 | 15,714 non-empty cells | ~50–70k | One third of all input; 325 output rows |
| **Full sample pass** | | **≈ 150–180k in** | |

**Output tokens are the hidden cost driver for tables.** The rent roll is 325 rows × 17 fields. As
full JSON with value, citation and confidence per cell, that is roughly 100k+ output tokens, which
on the stronger models costs more than all the input put together. The output format and *who
writes the citations* (model or code) are therefore cost decisions, not just style.

### 2.2 Model capabilities and prices (from OpenRouter's public list)

| Model | $/M in | $/M out | Input | Tier |
|---|---|---|---|---|
| openai/gpt-6-luna | 0.10 | 0.50 | text, image, file | cheap |
| anthropic/claude-haiku-5.5 | 0.10 | 0.50 | text, image, file | cheap |
| z-ai/glm-5.3-flash | 0.15 | 0.50 | text, image | cheap vision |
| qwen/qwen3-vl-235b-a22b-instruct | 0.21 | 1.90 | text, image | cheap vision |
| deepseek/deepseek-v3.2 | 0.26 | 0.38 | **text only** | cheap, strong reasoning |
| google/gemini-3.8-flash | 0.825 | 4.125 | text, image, file, … | mid |
| anthropic/claude-sonnet-5.5 | 2.00 | 10.00 | text, image, file | strong |
| openai/gpt-6.1-sol | 2.00 | 10.00 | text, image, file | strong |
| google/gemini-3.1-pro-preview | 2.00 | 12.00 | text, image, file, … | strong |

All nine support `response_format` / structured outputs and tools.

**Rough cost of one full sample pass** (single-call strategies, ~170k in, ~60k compact out):
cheap tier ≈ $0.05–0.07 · qwen-vl ≈ $0.15 · gemini-flash ≈ $0.40 · strong tier ≈ $0.95–1.10.
Screening all 9 models on the full set would cost about $3.7 *per strategy*, so the strong models are screened on a probe set only.

**Hidden risk: reasoning tokens.** "Reasoning" models can bill hidden thinking tokens as output,
which can multiply cost 2–10×. Every experiment records output tokens, so we will see it.
Reasoning effort is a parameter to test (◆ D6).

### 2.3 "Local vs API": what the rules allow

The brief: *only the listed models*, *no other AI provider*, *any open-source library*. So:

| Tier | Examples | Status |
|---|---|---|
| **Forbidden** | Any local LLM/VLM used for extraction (Llama, a local Qwen, SmolDocling…) | Not on the list, so it disqualifies |
| **T0: safe** | Parsers: poppler `pdftotext`, pdfplumber, pypdf, openpyxl, python-docx, mammoth; classical OCR: **Tesseract** | Open-source libraries |
| **T1: grey, low** | Open-source neural OCR libraries: RapidOCR / PaddleOCR, docTR, EasyOCR | "Library", but runs a neural model |
| **T2: grey, higher** | Document AI with layout or vision models: docling (layout models), marker/surya | Closest to "a model not on this list" |

Local tools are free in the graders' $ metric, but they cost CPU. Under 10 jobs in flight, slow
local OCR hurts p95 latency, and model weights bloat the Docker image and must be baked in (the `/health`
check has a 60 s limit). ◆ **D1: which tiers we allow** is a rules-risk decision for you.

## 3. The option space (what competes)

### 3.1 Group 3: ingestion

| Input | Options |
|---|---|
| Digital PDF | (a) pdftotext raw · (b) pdftotext `-layout` · (c) pdfplumber words + table finder · (d) page images to a vision model · (e) text + images of selected pages |
| Scanned PDF | (a) Tesseract · (b) T1 neural OCR · (c) vision-LLM transcription (glm-flash, qwen-vl, gemini-flash) · (d) **no OCR**: page images straight into extraction · (e) hybrid: local OCR, vision LLM only for low-confidence or table pages |
| Scan detection | Per page: text-layer chars < threshold → treat as scanned (the threshold is a parameter) |
| DOCX | (a) python-docx paragraphs + tables · (b) mammoth → markdown · (c) also OCR embedded images if they hold text |
| XLSX/CSV | (a) markdown table per sheet · (b) CSV with row numbers and column letters · (c) sparse `A1=value` cell list · (d) JSON rows. Plus: merged-cell fill, empty row/col pruning, sheet selection |

### 3.2 Group 4: schema understanding

| Question | Options |
|---|---|
| Field types | (a) none (the extractor decides, then normalise) · (b) heuristics on the description · (c) one LLM call per schema (cached, shared across jobs) |
| Descriptions | (a) as given · (b) generic runtime LLM rewrite per schema (cached) · (c) offline hand-tuned hint packs for known schemas, with generic fallback |
| Field kind | Document-level vs row-level, computed, list: same three options as types |

### 3.3 Group 5: extraction

**Single-entry docs (leases, OMs)**

| ID | Strategy |
|---|---|
| S1 | Whole document, one call, all fields |
| S2 | Whole document, parallel calls per field group (the document prefix is shared, so provider prompt caching cuts the repeat cost) |
| S3 | Locate then extract: local keyword/BM25 retrieval per field group (no embedding models are on the list), small contexts |
| S4 | S1 or S2 with the cheap model, then **escalate** only failing or low-confidence fields to a stronger model |

**Table docs (rent roll, T12, budget, box score)**

| ID | Strategy |
|---|---|
| T1 | The LLM emits every row (JSON or compact TSV), chunked by row range, in parallel |
| T2 | The LLM maps the structure (header row, columns → fields, row filter rules); **code** extracts every row |
| T3 | The LLM **writes extraction code** against the parsed grid; we run it sandboxed and check the output; retry on failure |
| T4 | Hybrid: code extracts the rows, and the LLM fills only the semantic columns (section, category, free text) |

**Cross-cutting**

| Axis | Options |
|---|---|
| Computed fields | LLM computes · LLM extracts the inputs and code computes · LLM emits a formula in a tiny safe expression language that code evaluates |
| Output format | Full JSON per field · values-only JSON + citations by code · TSV rows |
| Citations | Model-written snippet · code-derived from cell refs or page spans (spreadsheets get exact `source_locations` for free) |
| Model | Any of the 9, per stage, per document kind (scanned/digital × table/single) |
| Reasoning effort | Off / low / default on the models that support it |

## 4. Metrics (all logged per run, per document, per field)

**Accuracy**
- **Validator score** (`tools/validate.py`, macro mean over documents): the official proxy.
- **Penalised accuracy** (ours): per cell, correct = +1, null where truth has a value (miss) = 0, wrong value or
  hallucination = −α. The brief says wrong costs more than null, so α > 0 (◆ D2: α).
- Broken down by **field kind** (document-level scalar, row scalar, computed, list, should-be-null),
  because "harder fields count more" in their scorer.
- Tables: **row precision and recall** on the primary key (extra and missing rows zero a whole row).
- **Citation grounding**: the share of citations whose snippet really occurs in the representation (free to check).

**Cost:** uncached $/doc (`cost_usd_uncached`), by doc kind; output-token share; projected $/doc for the test mix.

**Constraints:** schema-valid output 100%, unrecoverable parse failures 0, estimated p95 latency at
10 in flight comfortably under 30 min (target < 10 min).

**Free (LLM-less) metrics that answer most of group 3 and part of group 4:**
- **Representation coverage:** for each non-null, non-computed truth value, does it appear (in any
  common formatting variant: `4,250.00`, `4250`, `January 1, 1994`, `1/1/94` …) in the extracted
  text? This is an *upper bound* on extraction accuracy, paired with the representation's token count.
- **OCR coverage** on the scanned leases, and on *rasterised* copies of budget_001 and om_001 (their
  truth is known, so they simulate scans with a real answer key).
- **Type-inference accuracy:** field types derived from the truth values give a free answer key.
- **Deterministic row recall** for T2/T4: how many truth rows code alone recovers.

## 5. Experiment sequence, budget and decision points

Planned spend **≈ $6.8**, leaving **≈ $2.2** under the $9 cap for bugs, re-runs and the final `outputs/` run.

| Phase | What | Docs | Budget | Output | ◆ |
|---|---|---|---|---|---|
| **E0 Free** | Ingestion options × formats: coverage + tokens; scan detection; OCR engines on scans and rasterised copies; type inference vs truth; deterministic row recall | all | **$0** | Shortlist of representations per format | ◆ representations |
| **E1 Model screen** | All 9 models (8 for image input), one fixed simple strategy (S1/T1, generic prompt), best E0 representation | **probe set** (below) | **$1.0** | Accuracy × cost per model per field kind and doc kind; reasoning-token usage | ◆ shortlist ≤ 3 models (cheap / mid / strong) |
| **E2 Strategy ablations** | Single-entry: S1 vs S2 vs S3, descriptions as given vs rewritten, computed-in-LLM vs code. Tables: T1 (JSON vs TSV) vs T2 vs T3 vs T4. Shortlisted models only | probe → full doc for winners | **$2.5** | Best strategy per doc kind | ◆ strategy per route |
| **E3 Scanned path** | Local OCR → text extraction vs vision-LLM transcription vs images straight into extraction vs hybrid | lease_001, lease_003, rasterised om/budget pages | **$1.0** | Best scanned route | ◆ scanned route |
| **E4 Escalation** | Cheap-first + escalate low-confidence or failed-check fields to the strong model; Δaccuracy vs Δ$ | full sample | **$0.8** | Is escalation worth it, and on which field kinds | ◆ routing |
| **E5 Generalisation holdout** | Finalists only, once: renamed keys, paraphrased and terse descriptions, dropped fields, `contract/schema.example.json` boxscore schema, rentroll exported as CSV, rasterised PDFs | holdout variants | **$0.5** | Reject configs that degrade sharply | ◆ veto |
| **E6 Confirmation** | Top 2 full configs on the full sample, 2 samples each (variance) | full sample | **$1.0** | Final numbers + noise estimate | ◆ **final pipeline** |

**Probe set** (cheap, but covers every doc kind and field kind):
lease_002 (full) · om_001 (full) · boxscore_001 (full) · t12_001 (full) · budget_001 pages 1–2 ·
rentroll_001 first ~40 units · lease_001 rent-schedule pages (scanned). Truth is filtered to the rows
or fields present in each slice.

**Time box (deadline Sun 23:59 IST):** E0–E2 Saturday; E3–E6 Sunday morning; the rest of Sunday goes to
service hardening, the trust layer and docs.

## 6. Decision rule (proposal, ◆ D3)

For each route (digital-single, scanned-single, spreadsheet-table, PDF-table) independently, since they compose:
1. **Hard constraints:** 100% valid output, no unrecoverable failures, latency within target.
2. **Accuracy band:** keep every config whose penalised accuracy is within **δ** of the best, where δ ≈
   the run-to-run noise measured in E6 (expect 1–3 points).
3. **Pick the cheapest** config in the band.
4. **An escalation tier** is kept only if Δaccuracy/Δ$ beats a threshold we set together (e.g. +1 point per +$0.01/doc).
5. **E5 veto:** a config that collapses on the holdout is dropped, however well it scored on the sample.

The deliverable for each ◆: a Pareto chart (accuracy vs $/doc) plus a table, and then we choose.

## 7. Harness (built in the next implementation step)

- **Swappable pipeline interfaces**, so that every option is a config value, not a code fork:
  `Ingestor(file) → Document` (pages/sheets, text with location anchors, optional page images) →
  `SchemaAnalyzer(schema) → FieldSpecs` → `Extractor(Document, FieldSpecs) → rows` → `PostProcessor` (normalise, compute, validate).
- **Experiments as TOML configs** (stdlib `tomllib`); `python -m app.eval.run --config … --set probe`.
- **Dry-run estimate** before every paid run (tokens × price table), plus a per-run cap (`LLM_RUN_BUDGET_USD`).
- **Scorer** reusing `tools/validate.py`, extended with the outcome classes (correct / miss / wrong / hallucination), field kinds and row P/R.
- **`experiments/results.jsonl`** (committed; scores and costs only, no document content) and a leaderboard / Pareto view. This is the "eval loop" evidence for DESIGN.md.
- Tools: `coverage` (E0), `rasterise` (pdftoppm → image-only PDF), `perturb` (schema variants for E5).
- Everything goes through the cached client, so re-scoring and re-running unchanged stages is free.

## 8. Risks

| Risk | Mitigation |
|---|---|
| 8 docs → overfitting | Generic prompts, probe-wide evaluation, E5 holdout, δ band instead of argmax |
| Run-to-run noise hides small wins | Variance measured in E6; δ set from it |
| Reasoning tokens blow the budget | Token logging, effort parameter, dry-run estimates, per-run caps |
| Interactions missed by staged screening | E6 confirms finalists end to end |
| Local OCR CPU cost under load | E0 records seconds per page; latency is a constraint in the rule |
| Rules risk of neural local tools | ◆ D1 |
| Time | The time box above; E3 can run in parallel with E2 |

## 9. Decisions for you (◆ in revision)

1. **D1 Local tools:** T0 only · T0 + T1 · T0 + T1 + T2?
2. **D2 Penalty α** for a wrong value (proposal: α = 1, i.e. a wrong value costs as much as a correct one earns).
3. **D3 Decision rule:** the δ-band + cheapest rule above, or a single weighted score?
4. **D4 Budget split:** ~$6.8 across E0–E6 plus ~$2.2 reserve. OK, or shift it (e.g. more for E2)?
5. **D5 Hint packs:** test hand-tuned hints for the known schemas (with generic fallback), or generic only?
6. **D6 Reasoning:** allow reasoning tokens on the strong models, or test with effort off/low only?
7. **D7 T3 (LLM-written code):** include it? It is powerful for block-structured sheets like the rent roll,
   but it needs a sandbox (subprocess, no network, CPU/memory/time limits).
8. **D8 Probe set:** composition as above?

---

## 10. Revision 1: the proposed architecture (from the user), reviewed

### 10.1 The proposal, as understood

```
Pre-execution (TBD) → Phase 1 Triage: per-type branch → token-efficient Markdown
                        (spreadsheets: pandas/polars → MD tables; digital PDF/DOCX: PyMuPDF/pdfplumber;
                         scanned: glm-5.3-flash / qwen3-vl transcription)
                    → Phase 2 Locator: cheap model maps field groups → page/chunk IDs
                    → Phase 3 Collator/Extractor: per-field prompts on located snippets only,
                         scatter-gather in parallel, routed models, strict JSON per field
                    → Phase 4 Post-processing: computed fields in code from LLM-extracted operands,
                         type casting, aggressive null override, merge
```

**Scope order:** PDF + DOCX first (lease_001, lease_002, lease_003, om_001, budget_001), spreadsheets after.

### 10.2 New evidence from the answer files (free check)

- **The answers carry `page_numbers` for almost every value** (lease_001 32/35 non-null, lease_002 29/32,
  lease_003 37/38, om_001 24/24, budget_001 741/769). That makes the page-number bonus real, and it
  makes **Locator recall measurable for free**: did the Locator point at the page the answer came from?
- **lease_002 is a DOCX, but its answers cite pages** (up to page 33). Their pipeline paginated the
  DOCX, most likely by rendering it to PDF. Getting the page-number bonus on DOCX means paginating the
  same way: **DOCX → PDF via LibreOffice headless** (open source; not installed locally; fine in
  Docker). This can be checked for free: do the answers' cited snippets land on the same page numbers in our rendering?
- **Handwritten amendments matter.** lease_001's governing security deposit is a handwritten correction
  (`"$ 2,400 2150. M Delacroix"` → 2150). That favours a vision model over classical OCR for scans.
- The answers' citations look like Markdown table rows (`| | | | 60 | $ 123,084 |`), which suggests their
  pipeline also works from a Markdown transcription. That is consistent with Phase 1.

### 10.3 Review: what holds and what to amend

| # | Point | Assessment | Proposed amendment (to be tested, not assumed) |
|---|---|---|---|
| 1 | Markdown as the common representation | ✅ Good: one format for the extractor | Embed **location anchors**: `<!-- page N -->` markers for PDFs/DOCX, and column letters + row numbers in spreadsheet tables, so `page_numbers` / `source_locations` and citations survive Markdown conversion |
| 2 | PyMuPDF / pdfplumber | ✅ Both worth testing | Add pdftotext and `pymupdf4llm` (Markdown with tables) to E0. Note PyMuPDF is AGPL; fine for this use, but worth a line in DESIGN.md |
| 3 | Scanned → vision transcription (glm / qwen) | ✅ Matches the handwriting evidence; avoids the local neural OCR grey area | Detect **per page** (mixed PDFs exist: digital body + scanned signed exhibits). Keep Tesseract only as a free E0 baseline to quantify the gain. Cost estimate is cheap: ~$0.02 per 25-page lease on glm-flash |
| 4 | Locator to shrink context | ✅ Good for focus. ⚠ The *cost* argument is weak at these sizes: a 20k-token lease read once by sonnet costs ~$0.04, and Locator + 8 targeted sonnet calls cost about the same or more. The payoff is accuracy (focused context) and parallelism; cost savings appear only for long docs or expensive extractors | Locator returns **top-k pages per group** (k = 2–3) rather than one chunk; measure page recall against the answers' `page_numbers`; on a null result, an optional single widen-context retry (measure whether it adds hallucinations) |
| 5 | Per-*field* prompts | ⚠ Breaks cross-field consistency: the four `rent_schedule_*` lists must align item by item, renewal terms with notice periods, total rent with the schedule. 40 calls per lease × 10 docs in flight = heavy 429 pressure | Make the **field group** the unit (the Locator already groups); per-field calls are kept as an ablation |
| 6 | Model routing by intuition | ⚠ gemini-3.8-flash is 8× the input price of luna/haiku, and deepseek is text-only | The suggested routing becomes the *hypothesis* E1 tests; routing is set from measured accuracy × cost per field kind |
| 7 | Tables in this flow | ⚠ The Locator + per-field approach does not fit table documents: budget_001 (in the PDF scope) is 55 rows × 14 fields, and the rent roll 325 × 17 | A separate **table branch** inside Phase 3 (options T1–T4 from §3.3), selected by the schema's `primary` flag |
| 8 | Computed fields in code | ✅ Agree. ⚠ For *unseen* schemas, code cannot know the formula in advance | Pre-execution schema analysis (one cached LLM call per schema) emits, per computed field: the operands to extract + a formula in a tiny safe expression language (e.g. `round(asking_price / units)`, `months_between(start, end)`, `sum(rent_schedule_monthly_rent * months)`). **Stated value wins** when the doc prints it ("use the lease's figure if stated"); the stated vs computed agreement feeds confidence |
| 9 | Aggressive null override | ✅ Right for hallucinations. ⚠ Some fields have **defined absent values**: "0 if none", "false otherwise", "empty list if none". Nulling those is wrong | Schema analysis records each field's `absent_value`; the override nulls only where null is the defined absence |
| 10 | Page-number bonus | ✅ Pursue | The Extractor returns the page ID with each value; code fills `page_numbers` and **checks the citation snippet really occurs on that page** (fuzzy match). A failed check lowers confidence, which can trigger the null override |
| 11 | "1. Pre-Execution Steps" (left blank) | Needs content | Proposal: (a) schema analysis per schema hash, cached: types, groups, computed formulas, absent values, rewritten descriptions; (b) file hash → artifact/LLM cache lookup; (c) page-level triage (text layer vs scan) |

### 10.4 What gets tested (PDF + DOCX first)

The proposed flow is **candidate A**. Two simpler baselines must beat or lose to it in numbers:

| ID | Pipeline (after the same Phase 1 Markdown with page anchors) |
|---|---|
| **A** | Locator (cheap) → per-group extraction on top-k located pages (routed model) → code post-processing |
| **A-field** | A, but per-field calls (ablation of amendment 5) |
| **B** | Whole document, one extraction call, all fields → same post-processing |
| **C** | Whole document, parallel per-group calls with a shared document prefix (provider prompt caching) → same post-processing |
| **A+img** | A, plus the located *page images* sent alongside the transcription, for scanned pages (handwriting) |

Revised early phases for this scope:
- **E0 (free):** PDF text extractors (pdftotext / pdfplumber / PyMuPDF / pymupdf4llm): coverage of
  answer values + tokens on om_001 and budget_001; DOCX → PDF (LibreOffice) vs python-docx/mammoth,
  including the **pagination match** against the answers' page numbers; per-page scan detection; Tesseract baseline coverage on scans.
- **E1a (≈ $0.10):** vision transcription glm-5.3-flash vs qwen3-vl vs gemini-3.8-flash on the
  *answer-cited pages* of lease_001 (8 pages) and lease_003 (11 pages): coverage of answer values (handwritten deposit included), tokens, $.
- **E1b (≈ $0.6):** model screen with pipeline B on the four single-entry docs (all text-capable models; strong ones included, since only 4 docs).
- **E2 (≈ $1.5):** A vs A-field vs B vs C (and A+img on the scans) with the 2–3 shortlisted models; Locator page recall reported separately.
- **Table branch on budget_001:** T1 vs T2 vs T4, as part of E2.
- Spreadsheets (boxscore, t12, rent roll) follow in a later round with the same harness.

### 10.5 Still open for the user

1. **Pre-execution (blank heading):** is §10.3 #11 what you had in mind?
2. **LibreOffice for DOCX pagination** (Docker image +~400 MB, a few seconds per DOCX): OK to test?
3. **Tesseract** as a free E0 baseline only (not in the service unless it wins on numbers): OK?
4. From §9, still needed for this phase: **α** (penalty for a wrong value; proposal 1), the **decision rule**
   (cheapest within noise of the best), **reasoning tokens** (off/low only?), **hint packs** (generic only?).

---

## 11. Revision 2 (from the user): DOCX coordinates and the answers to the open questions

### 11.1 Decisions recorded

| Topic | Decision |
|---|---|
| Per-type branches | Every document type gets its own local ingestion branch; LLMs are used for *semantic* extraction, never for plain text extraction of digital files |
| DOCX coordinates | **Synthetic anchors injected during local XML parsing**: `[Section: …]` at headings (semantic chunking, preferred), `[--- CHUNK NNN ---]` every ~1,000 words when there are no headings, `[Table ID: NNN]` before each table |
| Locator | Returns **anchors, not pages**; the Collator slices the Markdown deterministically from an anchor to the next one of the same kind |
| LibreOffice | **Dropped** (image size and cold-start risk). DOCX page numbers are therefore not produced. Accepted tradeoff: citations use the section anchor plus a verbatim snippet |
| Schema analysis | **Online**: runs the first time a schema hash is seen, cached by hash (persistent, single-flight so concurrent jobs share one analysis), then extraction proceeds |
| Tesseract | **Tested** as a full candidate for scanned pages (not just a baseline). If it is accurate enough on a page, the vision model is skipped |
| α / decision rule | **α = 1**; pick the **cheapest config within noise of the best** |
| Hints | **Programmatic and LLM-generated hints are in**: generic type and format rules, null discipline, absent-value defaults, list alignment, plus an LLM rewrite of each new schema. **Not allowed:** hand-written hints for specific documents |

### 11.2 Evidence from lease_002.docx (free inspection) and the resulting design details

| Finding | Consequence for the DOCX branch |
|---|---|
| **No Word heading styles at all**: 392 paragraphs, styles only `BodyText` (223), `ListParagraph` (157), none (12) | Heading detection must be **heuristic**: short all-caps lines, bold short paragraphs, numbered list items such as `4. BASE RENT.`. Style-based detection is kept for documents that do use styles |
| **The table of contents repeats every heading** (`TABLE OF CONTENTS`, then `BASE RENT.`, `ADDITIONAL RENT.` …) | Anchors get **unique numbers**, `[Section 012: BASE RENT.]`, and the TOC block is detected and tagged as one section. Otherwise a string split on the heading text would slice the TOC instead of the article |
| **No Word tables**: the rent schedule is laid out as paragraphs and tabs (`MONTHLY BASE RENT … October 1, 2025 through April 30, 2027 … $2,845.00`) | Table tagging stays for documents that have `w:tbl`; tab-separated runs are kept as ` \| ` so pseudo-tables stay readable |
| **No page breaks or rendered-page markers** (0 of each; 23 section breaks) | Confirms that DOCX pagination cannot be recovered without rendering. Synthetic anchors are the right call |
| **3 embedded images that hold text**: a scanned signature page with a handwritten date `8/21/25` (image1, 2555×3296), a floor plan reading `SUITE 310 / 1,842 RSF` (image3), and a logo (image2) | In this sample every answer is also in the body text, but in the test set an image may be the only source (signed execution date, scanned exhibit). Each image gets `[Image ID: NNN]` plus a transcription (Tesseract first, vision model when OCR confidence is low or it holds handwriting). Tiny images and logos are skipped by size |
| Section sizes vary a lot (one-line articles to multi-page ones) | Long sections split into `[Section 012.2: BASE RENT.]` parts at paragraph boundaries; tiny adjacent sections merge for the Locator's view |
| Fields often span sections (a definitions/summary block **and** the operative article) | The Locator returns a **list** of anchors per field group (top-k), not one |

### 11.3 Unified coordinate system (applies to PDF too)

- **DOCX:** `[Section NNN: title]`, `[--- CHUNK NNN ---]`, `[Table ID: NNN]`, `[Image ID: NNN]`.
- **PDF:** the same section and table anchors, **plus** `[Page N]` markers. When the Locator picks a section, the Collator knows which pages it spans, and fills `page_numbers` from that (keeping the PDF page bonus).
- **Spreadsheets (later):** `[Sheet: name]` plus column letters and row numbers in the tables, giving `source_locations`.

### 11.4 Next: implementation of E0 (free, $0)

1. Ingestion branches for **PDF** (pdftotext / pdfplumber / PyMuPDF / pymupdf4llm variants) and **DOCX**
   (XML parser with the anchors above), all emitting the same Markdown-with-anchors document model.
2. Per-page scan detection; **Tesseract** OCR for scanned pages and DOCX images (with word confidences recorded).
3. The E0 measurement tool: **coverage** of answer values per representation, **tokens**, **seconds**,
   and **anchor quality** (did each answer's citation land inside exactly one section, and how big are the sections).
4. Prerequisite: Tesseract on this machine (`sudo pacman -S tesseract tesseract-data-eng`). pip packages go in the venv.

---

## 12. Results: E0 (free) and E1a (vision transcription, $0.0141)

### 12.1 E0 conclusions (details: `experiments/e0/summary.md`)
- **Digital PDF:** every text engine captures 99–100% of non-computed answer values; pdfplumber ≈ PyMuPDF
  (95% citations located on om_001, all on the right page, ~7.4k tokens). Layout mode costs 2–3.5× tokens for
  no gain; pymupdf4llm is slower and worse on the budget table. Per-page scan detection caught om_001's image-only page.
- **Scanned PDF:** RapidOCR@200 > Tesseract@200 ≈ @300 ≈ psm6 (lease_001 85.0 vs 82.5%, lease_003 88.5 vs 85.3%
  verbatim), but ~3× the CPU per page (~27–31 s vs ~9 s under contention; 227 s vs 27 s wall for 25 pages).
  Clean scans are solved by every engine (rasterised om/budget 99.6–100%).
  Local OCR cannot read the handwritten correction and garbles the guarantor name.
- **DOCX:** all readers give the same text; only the XML parser yields usable sections (49, median ~240 tokens)
  and image transcriptions.
- **Coordinates:** DOCX sections work. PDF heading sections are too fine (median 20–50 tokens on om_001),
  so **pages** are the natural Locator unit for PDFs.
- Matcher fixes during E0 (measurement only): OCR comma/full-stop slips, "March 5. 1993", trailing zeros ("9.520"),
  pre-printed "19 93" years.

### 12.2 E1a: targeted vision test (4 images: lease_001 p2, p25, p18; lease_002 signature image)

| Model | p2 handwritten deposit (`~~2,400~~ [handwritten: 2150]`) | p25 guarantor | p18 table (control) | DOCX handwritten date | $/page (list) | Latency | Notes |
|---|---|---|---|---|---|---|---|
| local RapidOCR | ✗ | ✗ | ✓ | ✓ | 0 | ~3–5 s CPU | |
| local Tesseract | ✗ | ✗ | partial | ✗ | 0 | ~1 s CPU | |
| **z-ai/glm-5.3-flash** | ✓ (also marks handwritten "93") | ✓ | ✓ | ✓ | ~0.0003–0.0007 (provider billed **$0** on our key) | **3–6 s** | **Looped to the 3,000-token cap on the signature page** |
| **openai/gpt-6-luna** | ✓ | ✓ | ✓ | ✓ | 0.0010–0.0011; **0.0006 with reasoning off** (still ✓) | 13–18 s; 7 s reasoning off | 950–1,300 hidden reasoning tokens/page by default |
| **anthropic/claude-haiku-5.5** | ✓ | ✓ | ✓ | ✓ | 0.0006–0.0011 | 6–9 s | Reasoning off: **wrongly marked the year "19 ~~93~~" as struck** |
| qwen/qwen3-vl-235b | **✗ ambiguous** (`$ 2,400– 2150M`) | ✓ | ✓ | ✓ | 0.0007–0.0020 | 11–19 s | Merges "19 93" → "1993" but loses the strike-through |

- Image input ≈ 1.5–2.4k tokens per page at 1,500 px long side.
- **Round 2 (gemini-3.8-flash) skipped:** every achievable checkpoint was solved by a round-1 model.
- **Cost:** round 1 $0.0128 (16 calls) + reasoning-off test $0.0014 (2 calls) + smoke $0.000004 = **$0.0141**.
- Re-scoring after the matcher fix ran in `replay` mode: $0.

---

## 13. E1b: targeted model screen for extraction (IDEATION)

### 13.1 The questions E1b must answer (and nothing else)
1. Given the **right text**, which cheap model extracts each **field kind** correctly: plain strings, numbers,
   dates, lists (the aligned rent schedule), booleans, computed fields, and **nulls** (hallucination)?
2. Where the cheap models fail, does a **stronger model, or the same model with reasoning on**, fix it, and at what $?
3. Real $ per document per model, including hidden reasoning tokens; unusable-output rate.

**Out of scope on purpose:** locating, long-context degradation, whole-document vs Locator pipelines. Those
are pipeline questions for E2, with the shortlisted models only.

### 13.2 Cost levers (same approach as E1a)

| Lever | Effect |
|---|---|
| **Oracle context.** Feed only the pages/sections the answers cite, plus 2 fixed non-cited distractor slices per doc. Isolates extraction skill (the shared core of pipelines A, B, C) | Input 75k → ~29k tokens across 4 docs (measured) |
| **3 documents, not 4.** lease_002 (digital DOCX, 8 nulls: null discipline), lease_001 (OCR noise; pages 2 and 25 use the cached E1a vision transcripts, mirroring the proposed pipeline), om_001 (different schema). **Drop lease_003:** same schema, clean OCR, and the most expensive (14k oracle tokens) for the least new information | ~29k → ~15k input + ~5k prompt and distractors ≈ **20k per model** |
| **Field-kind coverage is still complete:** 38 strings, 22 numbers, 8 dates, 6 computed, 11 lists, 6 booleans, **18 nulls** | — |
| **Staged by price.** Round 1: cheap models on everything. Round 2: only the **fields that failed**, with only their slices: gemini-flash first, strong models only on what gemini-flash still fails | Strong models see ~10k tokens, not 75k |
| **Reasoning off by default** where E1a showed it is safe (luna); haiku on default. "Same cheap model + reasoning low" becomes an escalation option in round 2 | Avoids paying for hidden reasoning tokens up front |
| **Compact output:** value + short citation per field; confidence comes later from the trust layer | ~35 output tokens per field |
| **Official scorer:** `tools/validate.py` cell matching, plus outcome classes (correct / miss / wrong / hallucination), penalised accuracy with α = 1, per field kind | — |
| **Cache + run cap + dry-run estimate** before each round; re-scoring always in replay mode ($0) | — |
| **Variance check** on the best cheap model only: one repeat with `sample_index = 1` | Sets the noise band δ cheaply |

### 13.3 Estimated cost

| Round | Models | Est. $ |
|---|---|---|
| 1 | gpt-6-luna (reasoning off), claude-haiku-5.5, deepseek-v3.2, glm-5.3-flash | ≈ $0.02 |
| Variance | best cheap model, 1 repeat | ≈ $0.004 |
| 2a | gemini-3.8-flash on failed fields only | ≈ $0.01 |
| 2b | sonnet / gpt-6.1-sol / gemini-pro (and cheap + reasoning low) on fields 2a still fails | ≈ $0.03–0.12 |
| **Total** | | **≈ $0.06–0.16** (was ≈ $0.6) |

### 13.4 Known limits of the oracle design
- It overstates accuracy relative to a real pipeline (the right text is always present). That is fine for
  *ranking models*; E2 measures the pipeline loss.
- Null discipline is easier with less text to be tempted by. E2 re-checks hallucinations at full context.
- Field descriptions are used as given; rewriting is an E2 ablation.

### 13.5 Revision: four fixes added before building (agreed)
1. Cheap models also run on **full documents** (measures the oracle → full gap).
2. Escalation judged against **production-available signals** (cross-model disagreement, null split, citation not in text), not the answer key.
3. luna run **with and without** reasoning for extraction.
4. **lease_003** oracle slices added back (only sample with renewal-option lists).

## 14. E1b round 1 results ($0.0961, 38 calls incl. 3 rate-limit retries)

| Arm | Penalised, oracle (4 docs) | Penalised, full (3 docs) | Same 3 docs excl. free text: oracle / full | Hallucinations | $/doc (list) oracle / full | s/doc | Hidden reasoning tokens |
|---|---|---|---|---|---|---|---|
| gpt-6-luna, reasoning off | 0.662 | 0.670 | 0.831 / 0.835 | 0 | 0.0018 / 0.0025 | ~12.5 | 0 |
| gpt-6-luna | 0.678 | 0.651 | 0.835 / 0.812 | 1 | 0.0025 / 0.0033 | ~24 | ~1.4k/doc |
| claude-haiku-5.5 | 0.685 | 0.743 | 0.859 / **0.906** | 0 | 0.0038 / 0.0048 | 16–21 | ~2k/doc |
| deepseek-v3.2 | 0.691 | 0.739 | 0.847 / 0.900 | 1 (full) | 0.0031 / 0.0051 | **44–49** | 0 |
| glm-5.3-flash | **0.718** | **0.743** | **0.882** / 0.882 | 0 | 0.0028 / 0.0038 (billed $0) | 13–15 | ~1k/doc |

Per field kind: numbers 1.00 and should-be-null 1.00 for every model; dates, lists, booleans and computed 0.6–1.0;
strings 0.43–0.78; **free text 0.00–0.25 under the strict matcher**.

**Findings**
1. **No long-context penalty for cheap models:** full documents score the same or better than oracle slices on the
   same docs. The Locator's accuracy argument is weak at these document sizes (E2 decides).
2. **Most non-free-text errors are shared by every model**: interpretation problems, not capability. premises given as
   "Second Floor, West Section, Suite 214W" (field asks for the suite only); landlord entity or a building alias given
   as property_name; "… submarket" suffix; extra columns in unit_types; total base rent net vs gross of free months;
   rent commencement date before vs after abatement. These point at **description rewriting / programmatic hints**
   (E2), not at stronger models.
3. **Free text is mostly correct paraphrase** that the strict matcher fails (e.g. late fee "greater of $75.00 or 6% of
   the unpaid rent"); a few are genuine errors (holdover "175%" dropped). Their scorer uses an LLM judge on free text,
   so ours needs one to rank models on these fields.
4. **Hallucination is near zero:** 19 should-be-null cells, all models correct; 2 invented values in 35 runs.
5. **Escalation signal:** cross-model disagreement flags ~24% of cells and catches **91–93%** of the bad ones
   (precision 0.56–0.69). Citation-not-in-text is weak (recall ≤ 0.41).
6. **Reasoning:** luna with reasoning on is *not* better than off for extraction, and costs ~40% more and twice the time.
7. **deepseek is the slowest** (~45 s/doc) without being more accurate.

## 15. E1b follow-ups (A judge, B noise, C round 2)

**A. Free-text judge ($0.0011, 4 calls):** 106 distinct (truth, prediction) pairs judged by gpt-6-luna (reasoning off);
50 judged equivalent. Spot check: correctly strict on dropped numbers ("175%", "2%"), but noisy both ways (accepted a
sublet answer that adds recapture terms; rejected one of two near-identical "150% of the Annual Rent" paraphrases).
Good enough for ranking models, not for exact scores. Bias: a cheap model partly judging its own outputs.

**Re-scored (penalised, α = 1, free text judged):**

| Arm | Oracle (4 docs) | Full (3 docs) | $/doc list, full | s/doc |
|---|---|---|---|---|
| glm-5.3-flash | **0.812** | **0.853** | 0.0038 (billed $0) | 13 |
| claude-haiku-5.5 | 0.779 | 0.835 | 0.0048 | 17 |
| deepseek-v3.2 | 0.732 | 0.794 | 0.0051 | 44 |
| gpt-6-luna, reasoning off | 0.769 | 0.743 | **0.0025** | 13 |
| gpt-6-luna | 0.772 | 0.725 | 0.0033 | 24 |

**B. Noise check ($0.0130, 6 calls):** same prompt repeated at temperature 0 on lease_002 and om_001: 0–4 values change
per document and 0–1 fields flip correctness; about **±0.02–0.03** on a 3-document average. glm vs haiku is a tie;
luna (reasoning off) is ~0.1 lower, beyond noise.

**C. Round 2a, gemini-3.8-flash on luna-off's genuine failures ($0.0618, 4 calls; estimate was $0.018 because
gemini-flash spent 3–4k hidden reasoning tokens per call, now included in the estimator):** fixed 8 of 17 failing
fields (computed $/RSF list, early termination, late fee, holdover, total base rent, escalation, submarket, one
property name); did **not** fix the shared interpretation errors (premises "Second Floor, West Section, Suite 214W",
lease_001 property name, rent commencement date, building address). Two wrong values became nulls (better under α = 1).
**Round 2b (strong models) skipped:** the remaining failures are interpretation conventions, the job of description
rewriting in E2, and it would exceed the quoted E1b range.

**E1b total: $0.1720** (round 1 $0.0961 + judge $0.0011 + noise $0.0130 + round 2a $0.0618).

**Takeaways for later decisions:** glm and haiku lead on full documents (tie within noise); the cheapest default
(luna, reasoning off) is ~0.1 behind; escalation by cross-model disagreement catches >90% of errors; the biggest
remaining lever is field-description rewriting, not model strength.
