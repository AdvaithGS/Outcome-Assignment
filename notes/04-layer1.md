# Step 4: Layer 1, the extraction pipeline

Status: **IDEATION.** A proposal assembled from the user's architecture (notes/02 §10–11), the table-plan idea, and
the evidence from E0, E1a and E1b. Every open choice is listed in §8 for revision.

---

## 1. Shape of the pipeline

```
           ┌──────────────────────────── once per new field list (cached by schema hash) ───────────────────┐
schema ───►│ 0. Schema analysis: shape, field types, groups, computed formulas, absent defaults, rewritten    │
           │    descriptions + programmatic hints                                                           │
           └────────────────────────────────────────────────────────────────────────────────────────────────┘
file ────► 1. Triage + ingestion (per type, local)  ──►  2. Route
                                                           ├─ single-entry document ─► 3a. Prose extractor
                                                           └─ table document ───────► 3b. Table plan + executor
                                                                                         │
           4. Post-processing (deterministic): normalise, absent defaults, computed fields, citations → pages /
              cells, grounding check, confidence  ──►  5. build_result (contract-valid by construction)
```
The user's Locator and Collator survive as the **long-document path** of 3a (see §4); E1b showed they are not
needed for accuracy at the sample sizes.

## 2. Stage 0: schema analysis (online, first time a schema hash is seen)

| Output per field | How | Why (evidence) |
|---|---|---|
| Type: number / date / percent / bool / list / text / free text | Description heuristics ("as a number", "YYYY-MM-DD", "list of", "true if"), confirmed by one LLM call | Drives normalisation and validation |
| Scope (tables): document-level vs per row | Heuristics ("Same for every row") + LLM | Document-level values are extracted once and repeated |
| Absent value: null / 0 / false / [] | Heuristics on "0 if none", "false otherwise", "empty list if none" | Nulling these is wrong (E1b: `renewal_options_count=0`) |
| Computed: formula in a small safe expression language + operand fields; "stated value wins" flag | LLM, validated by parsing | Code does the arithmetic; works for unseen schemas |
| Rewritten description + programmatic hints | LLM rewrite, generic rules (no document-specific hints) | **E1b: most remaining errors are interpretation errors shared by every model** (premises with floor text, landlord entity as property name, "… submarket" suffix, net vs gross rent) |
| Field groups | LLM | Locator and per-group calls (long docs) |

One cheap call per new schema (≈ $0.001), cached in the artifact cache, single-flight across concurrent jobs.
Fallback if the call fails: heuristics only, descriptions as given.

## 3. Stage 1: triage and ingestion (decided by E0/E1a, pending sign-off)

| Input | Path |
|---|---|
| Digital PDF | pdfplumber text per page, `[Page N]` anchors |
| Scanned page (per-page detection) | **Vision transcription** (proposal: gpt-6-luna, reasoning off), local OCR as fallback. Rationale: E1a: vision reads handwriting and garbled names that local OCR cannot, at ~$0.0006/page, and it is **faster** than RapidOCR (≈7 s per page in parallel vs ≈27 s CPU per page) |
| DOCX | Own XML parser, section anchors, embedded images transcribed (same vision path) |
| XLSX / CSV | **New:** grid model with cell references (openpyxl / csv), merged cells filled, empty rows/columns dropped, all sheets kept with names |
| PDF tables | pdfplumber words → rebuilt grid (columns from x-positions, wrapped lines joined) → same table path |

## 4. Stage 3a: single-entry documents (leases, OMs, summaries)

**Default: whole-document extraction**, one call (or parallel calls per field group with a shared document
prefix, which provider prompt caching makes cheap; E2 decides), using the E1b prompt plus the stage-0 descriptions.
- Evidence (E1b): full documents score the same or better than the oracle slices; 40 fields in one call is fine;
  ~$0.0025–0.005/doc.
- **Long-document path (user's Locator design):** above a size threshold (proposal ~60k tokens), the Locator maps
  field groups to anchors (pages for PDF, sections for DOCX), the Collator slices deterministically, and per-group
  extraction runs on the slices.

**Model:** the user picked the cheapest (gpt-6-luna, reasoning off) for now. By the agreed decision rule (cheapest
within noise of the best), E1b points to **glm-5.3-flash** (0.853, $0.0038 list, billed $0 now, looped once:
needs a token cap and a fallback) or **claude-haiku-5.5** (0.835, $0.0048). luna (0.743, $0.0025) is ~0.1 behind,
beyond the ±0.03 noise.

**Escalation (later, E4):** run a second cheap model; fields where they disagree (E1b: flags ~24% of fields,
catches >90% of errors) go to a stronger model with only their slices (gemini-flash fixed ~half in round 2a).

## 5. Stage 3b: table documents (the user's plan design)

1. **Preview** (fixed ~3–6k tokens): sheet names and sizes, header rows, first and last ~15 rows, samples where the
   structure changes, with row numbers and column letters.
2. **Plan** (one cheap call): the user's spec (`first_data_row`, `last_data_row`, `stop_at`, `columns`,
   `constants`, `block_fields`, `row_aggregates`, `value_maps`, `null_values`, `blank_defaults`, `missing`) plus
   **row rules** (what counts as a data row; skip "Total"; repeated page headers) and a **section rule** (carry
   the nearest header down).
3. **Executor** (code): reads the grid, applies the rules, fills rows, keeps the cell of every value
   (`source_locations`, citations).
4. **Checks:** row count sensible, primary keys unique and non-empty, values parse as their types, column sums
   match a printed totals row where one exists.
5. **Repair:** failed checks go back once with the errors; still failing → fallback, the LLM writes rows in chunks.
6. **Classified columns** (e.g. T12 `category`): the LLM labels the *distinct* values (≈10 section names), and code
   maps them to the rows.

Cost: ≈ $0.001–0.003 per table document whatever its length; hallucination nearly impossible (values copied
from cells).

## 6. Stage 4: post-processing (deterministic)

- Normalise by type: numbers (`$`, thousands separators, parentheses negatives, `%` kept as printed), dates to
  ISO (many input formats, including "19 93"), booleans, lists; reject values that do not parse.
- Absent defaults applied; computed fields evaluated from operands; a stated value wins; disagreement between
  stated and computed lowers confidence.
- Citations: verify the snippet exists in the source text; fill `page_numbers` from `[Page N]` and
  `source_locations` from cells.
- **Null policy (evidence-based):** E1b showed "citation not found" is a weak error signal (precision 0.06–0.24),
  so it lowers confidence instead of nulling. Values that fail type validation are nulled.
- Confidence: grounding + type validity + (later) cross-model agreement.

## 7. Expected cost and latency per document (estimates)

| Document | $ | Latency |
|---|---|---|
| Digital lease / OM | 0.003–0.005 | ~15 s |
| Scanned lease (25 pages) | +0.015 vision | ~30–45 s |
| Table (any length) | 0.001–0.003 | ~10–20 s |
| Escalation (if adopted) | +0.005–0.015 | +10–20 s |

## 8. Build order and remaining experiments

1. Stage 0 (heuristics + LLM), stage 4, the stage 3a whole-document extractor, scanned-page vision, and
   `run_samples` (all samples → `outputs/` → validator). This gives the first real scores for leases and the OM.
2. Spreadsheet grid + table plan executor + PDF-table grid → scores for boxscore, t12, rentroll, budget.
3. **E2** (≈ $0.05–0.08): original vs rewritten descriptions; single call vs per-group calls; Locator vs whole
   document on the leases; table plan vs LLM rows on the small tables.
4. **E3–E5** (≈ $0.08–0.1): scanned path variants, escalation, generalisation holdout (renamed keys, paraphrased
   descriptions, the contract's box score schema, rent roll as CSV).

## 9. Questions for revision

1. **Locator:** keep it as the long-document path only (size switch), given E1b? Or test Locator vs whole document
   in E2 before building either?
2. **Default model:** keep luna (reasoning off, cheapest) or move to glm / haiku, as the decision rule suggests?
3. **Scanned pages:** start with vision transcription of every scanned page (cheap, faster than RapidOCR), and let
   E3 test cheaper variants?
4. **Tables:** adopt the plan design as the primary path, with LLM rows as the fallback? (Adds `openpyxl`.)
5. **Escalation:** build now, or after the first scores?
6. **Null policy:** "citation not found" lowers confidence only (per E1b), never nulls on its own?

---

## 10. Revision → implementation, step 1 (pipeline v1 for single-entry documents)

Defaults used (provisional, all configurable): gpt-6-luna with reasoning off for stage 0, extraction and vision;
vision transcription for every scanned page with Tesseract fallback; Locator deferred (long documents only);
computed fields in mode H; a citation that cannot be found only lowers confidence.

**Built:** `app/pipeline/{formula,normalise,schema_analysis,ingest,single,postprocess,v1}.py`,
`app/eval/run_samples.py`; registered as `PIPELINE=v1`. 87 tests pass.

**Run 1 ($0.0487, 67 calls):** mean 0.833 on the 4 single-entry docs, but it took **80 minutes**: 15 calls hit
network timeouts and some waited the full 300 s read timeout before retrying. Three stage-0 formulas were rejected
(my bug: the documented `round(x, digits=0)` used a named argument that the parser refused).

**Fixes:** per-call timeouts (vision 60 s, extraction 150 s, analysis 120 s; default `LLM_TIMEOUT_S` = 90) with
`asyncio.wait_for` capping the *total* attempt time (httpx's read timeout only bounds gaps between bytes);
deadline-aware retries (no attempt past the job deadline, no retry that cannot finish); `digits=` allowed.

**Run 2 ($0.0135, 6 calls; stage 0 and all 46 vision pages from cache):** latency 39–52 s per document.

| Doc | All-null | M | C | **H** |
|---|---|---|---|---|
| lease_001 | 0.150 | 0.800 | 0.800 | **0.800** |
| lease_002 | 0.300 | 0.775 | 0.800 | **0.800** |
| lease_003 | 0.075 | 0.825 | 0.850 | **0.850** |
| om_001 | 0.172 | 0.931 | 0.931 | **0.931** |
| **Mean** | 0.174 | 0.833 | 0.845 | **0.845** |

**E2a conclusion: code-computed fields (C/H) beat the model's own arithmetic (M).** M was wrong on 4 computed
cells (lease_001 $/RSF list, lease_002 total base rent net of free months, lease_003 total and one $/RSF entry);
the formulas got all of them right. H = C on the samples (the only printed figure, lease_001's total, agrees with
the formula). **Decision: keep H**, which also uses the printed figure when there is one.

**Remaining errors (H):** mostly free text the strict validator rejects (their scorer uses an LLM judge), plus the
interpretation errors seen in E1b (premises with floor text, property name, rent commencement date, submarket).
lease_002's "hallucination" is a validator artefact: the answer "none" normalises to null, so our
"none; fixed monthly Base Rent…" counts as invented. Lesson for descriptions: when the answer is "none", output exactly "none".

## 11. Step 2: table documents (the user's plan design)

**Built:** `app/ingest/grid.py` (XLSX/CSV grids with cell addresses, merged cells filled; fixed-size preview;
PDF table grid from word positions: lines → fragments → column gutters, page headers/footers that recur on 3+ pages
removed, each row keeps its page), `app/pipeline/table.py` (plan prompt, validation, executor, checks, one repair,
distinct-value classification, LLM-writes-rows fallback), wired into pipeline v1 for any schema with a primary field.

**Plan format:** the user's spec plus `record_start` / `require_values` / `skip_if` / `record_end` (records by rule,
multi-row records), block modes `first` / `join` / `sum` / `value` / `count` with `where` conditions, `header_fields`
(nearest section row above), `row_aggregates`, partial-cell columns (`{"column": "A", "regex": "^(\\d{4}-\\d{4})"}`),
and `classify` (labels per distinct input combination, one small call).

**What the data required (found by inspection, all generic):**
- The rent roll lists 5 units twice (main list + "Future Resident Details"): duplicates of the primary key are merged
  (first kept); only a high duplicate share is treated as a bad plan.
- In some rent-roll blocks market rent and deposit sit on a later line: block mode `first`.
- The T12's TOTAL rows with their own GL code are rows; section headers are rows with a label and no amounts.
- The budget PDF prints GL code and name in one cell: partial-cell columns. Wrapped explanations: block `join`.
  Repeated page headers looked like section headers on pages 2–8: removed as boilerplate.

**Executor check with hand-written plans ($0):** boxscore 1.000, rentroll 1.000, t12 0.909 (category left to the
LLM), budget 0.999 after the boilerplate fix.

**With LLM-written plans (gpt-6-luna, reasoning off):**

| Doc | Score | Plan calls | $ |
|---|---|---|---|
| boxscore_001 | 1.000 | 1 | 0.0012 |
| rentroll_001 | 1.000 (325/325 rows, 5 duplicates merged) | 1 | 0.0015 |
| t12_001 | 0.998 | 2 (repair) + classify | 0.0030 |
| budget_001 | 0.999 | 2 (repair) | 0.0017 |

Two generic checks drove the repairs: a per-row field whose description mentions a column cannot be "missing"
(the T12 plan had dropped the first/last month columns), and a free-text field mapped to a column that continues
onto following lines must be joined (the budget plan read only the first line of each explanation).

**All 8 samples, official validator (`tools/validate.py outputs/ …`), replay run ($0): mean 0.922**
(boxscore 1.000, budget 0.999, rentroll 1.000, t12 0.998, om 0.931, lease_003 0.850, lease_001 0.800, lease_002 0.800).
Table-step spend: $0.0074. Remaining gap: the single-entry documents (free text under the strict matcher,
interpretation conventions).

**Reproducibility check (replay mode):** the first full replay failed with a cache miss: the table-plan prompt had
changed (partial-cell columns were added for the budget) after boxscore / t12 / rentroll ran, so their outputs came
from the older prompt. Re-ran those three with the current prompt ($0.0029; all plans now pass on the first call:
boxscore 1.000, t12 0.998, rentroll 0.998) and `run_samples` now reports a per-document error instead of stopping.
A full replay of all 8 documents now succeeds at $0: mean **0.922**.
