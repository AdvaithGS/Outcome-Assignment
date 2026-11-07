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
