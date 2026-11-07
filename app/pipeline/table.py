"""Stage 3b: table documents. The LLM writes an extraction PLAN from a fixed-size preview; code executes it.

Plan format: the user's spec (first_data_row, last_data_row, stop_at, columns, constants, block_fields,
require_values, header_fields, row_aggregates, value_maps, null_values, blank_defaults, missing), extended with
record_start / record_end / skip_if (records found by rule, not only by fixed row ranges) and classify (labels
for distinct values, e.g. Revenue / Expense / Other per section, done by one small LLM call).

Flow: preview → plan (1 call) → execute → checks → if checks fail: one repair call with the errors → still failing:
fallback where the LLM writes the rows in chunks. Every executed value keeps its cell address (citation +
source_locations), so values are copied, never invented.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.ingest.grid import Grid, Sheet, col_index, col_letter, fmt, preview, split_ref
from app.llm import LLMClient, UsageTracker
from app.pipeline import formula as F
from app.pipeline.normalise import normalise
from app.pipeline.schema_analysis import SchemaSpec, parse_json

log = logging.getLogger("table")

PLAN_SYSTEM = """You write an extraction PLAN for a table document. You see a preview of a spreadsheet-like grid (every cell
shown with its column letter and row number; long sheets are shown as head, evenly spaced samples and tail) and a
list of fields. Code will execute your plan on the full grid, so describe RULES that hold for every data row, not
just the rows you can see. Return ONE JSON object with these keys (omit keys you do not need):

  "sheet": the sheet holding the table
  "record_start": {"column": "A", "pattern": "<regex the cell must FULLY match>"}  - every row whose cell matches starts
        one output row (one record). Make the pattern specific to real data rows (e.g. "\\\\d{3}-\\\\d{3}" for unit ids
        like 100-101, "\\\\d{3}-\\\\d{3}" for GL codes like 510-500) so headers, totals and summary rows do not match.
  "first_data_row", "last_data_row": optional inclusive row bounds (null = no bound)
  "require_values": ["C"]   - a start row must ALSO have non-empty cells in these columns (e.g. rows that carry amounts)
  "skip_if": [{"column": "B", "pattern": "<regex>"}]   - start rows to ignore (e.g. totals), if the field list says so
  "stop_at": {"column": "A", "pattern": "<regex>"}   - the first row matching this ends the table completely
  "record_end": {"column": "G", "pattern": "<regex>"}   - for multi-row records (a record line followed by detail lines):
        the row that closes a record (inclusive). Without it a record runs until the next start row.
  "columns": {"<field>": "D"}   - value from that column on the record's start row; to take PART of a cell (two
        values printed in one cell, e.g. "6101-0000 Minimum Base Rent") use {"column": "A", "regex": "<pattern with
        ONE capture group>"}, e.g. "^(\\d{4}-\\d{4})" for the code and "^\\d{4}-\\d{4}\\s+(.*)$" for the name
  "block_fields": {"<field>": {"mode": "first|join|sum|value|count", "column": "I",
                               "where": {"column": "H", "equals"|"not_equals"|"matches"|"not_matches": "..."}}}
        - for values spread over a multi-row record: first = first non-empty value in the record; join = all text
          joined; sum = sum of numbers; value = the value on the first row meeting "where"; count = rows meeting "where".
          "where" is optional; rows whose where-column is empty never meet a where condition.
  "row_aggregates": {"<field>": {"op": "sum|count_nonzero", "columns": ["C", "N"]}}   - over the start row, columns C..N
  "header_fields": {"<field>": {"column": "B", "header_row": {"nonempty": ["B"], "empty": ["C"]}}}
        - the nearest row ABOVE the record that satisfies header_row (e.g. a section title row without amounts).
          Section title rows often have an EMPTY code/key column, so do not require the key column in header_row.
  "constants": {"<field>": {"value": <value>, "cell": "A2", "confidence": 0.0-1.0}}   - one value for the whole
        document (e.g. property name, report date), with the cell it comes from; give the value already in the
        field's format (dates as YYYY-MM-DD, numbers as numbers)
  "classify": {"<field>": {"options": ["...", "..."], "inputs": ["<other field>", ...]}}   - for a field whose value
        is a label the document does not print per row; code asks for one label per distinct input combination
  "value_maps": {"<field>": {"<cell text>": "<output value>"}}   - exact remaps of printed values
  "null_values": ["-- Vacant --", "N/A"]   - cell texts that mean "no value"
  "blank_defaults": {"<field>": 0}   - value to use when the cell is empty, only where the field list says so
  "missing": ["<field>"]   - fields that are not in the document at all
  "checks": [{"expr": "add(monthly_rent, monthly_other_charges)", "equals": "monthly_total_charges"}]
        - REQUIRED key: one entry for every relation between fields that a field description states (e.g. "should
          equal A plus B"), as an expression over field keys (functions: add, sub, mul, div, sum, round) that must
          equal another field; an empty list if no description states one. Code verifies each check on every row.
Every field must appear exactly once in columns, block_fields, row_aggregates, header_fields, constants, classify
or missing. Use column letters exactly as shown in the preview."""

CLASSIFY_SYSTEM = """Assign each numbered item exactly one of the allowed labels, following the field description.
Return ONE JSON object mapping each item number (as a string) to its label."""

ROWS_SYSTEM = """You extract table rows from part of a spreadsheet. Return ONE JSON object {"rows": [...]} with one object per
data row in THIS chunk (skip headers, totals and summary rows), each object having exactly the field keys listed.
Values: numbers as JSON numbers, dates as YYYY-MM-DD, text as written, null when empty. Do not invent rows."""


@dataclass
class TableResult:
    rows: list[dict]
    plan: dict | None
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    method: str = "plan"
    partial: bool = False


# ------------------------------------------------------------------------------- prompts

def field_list(spec: SchemaSpec) -> str:
    lines = []
    for f in spec.fields.values():
        t = f.type + (f" of {f.item_type}" if f.type == "list" and f.item_type else "")
        tags = [t, "one value for the whole document" if f.scope == "document" else "per row"]
        if f.primary:
            tags.append("PRIMARY KEY: identifies the row")
        if f.absent is not None:
            tags.append(f"if absent: {json.dumps(f.absent)}")
        lines.append(f'- "{f.key}" ({f.name}) [{"; ".join(tags)}]: {f.description}')
    return "\n".join(lines)


def plan_messages(spec: SchemaSpec, grid: Grid) -> list[dict]:
    user = (f"Document type: {spec.description or spec.name}\n\nFields:\n{field_list(spec)}\n\n"
            f"Grid preview ({grid.source}):\n{preview(grid)}")
    return [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}]


# ------------------------------------------------------------------------------- execution

def _match(pattern: str, value) -> bool:
    return value is not None and re.fullmatch(pattern, fmt(value, 10_000)) is not None


def _cell_ok(sheet: Sheet, r: int, cond: dict | None) -> bool:
    if not cond:
        return True
    c = col_index(cond["column"])
    v = sheet.get(r, c)
    if v is None:
        return False
    s = fmt(v, 10_000).strip().lower()
    if "equals" in cond:
        return s == str(cond["equals"]).strip().lower()
    if "not_equals" in cond:
        return s != str(cond["not_equals"]).strip().lower()
    if "matches" in cond:
        return re.search(cond["matches"], fmt(v, 10_000), re.I) is not None
    if "not_matches" in cond:
        return re.search(cond["not_matches"], fmt(v, 10_000), re.I) is None
    return True


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return normalise(v, "number")


def _cols(spec_cols: list[str]) -> list[int]:
    if len(spec_cols) == 2 and all(isinstance(x, str) and x.isalpha() for x in spec_cols):
        a, b = col_index(spec_cols[0]), col_index(spec_cols[1])
        return list(range(min(a, b), max(a, b) + 1))
    return [col_index(x) for x in spec_cols]


def validate_plan(plan: dict, grid: Grid, spec: SchemaSpec) -> list[str]:
    errs = []
    try:
        sheet = grid.sheet(plan.get("sheet"))
    except KeyError as exc:
        return [str(exc)]
    rs = plan.get("record_start")
    if not isinstance(rs, dict) or "column" not in rs or "pattern" not in rs:
        errs.append("record_start must be {'column': ..., 'pattern': ...}")
    for key in ("record_start", "stop_at", "record_end"):
        cond = plan.get(key)
        if isinstance(cond, dict) and "pattern" in cond:
            try:
                re.compile(cond["pattern"])
                col_index(cond["column"])
            except (re.error, ValueError, KeyError) as exc:
                errs.append(f"{key}: {exc}")
    if not isinstance(plan.get("checks"), list):
        errs.append("'checks' is required: list every relation between fields that a field description states "
                    "(e.g. 'should equal rent plus other charges'), or [] if none")
    covered = set()
    for section in ("columns", "block_fields", "row_aggregates", "header_fields", "constants", "classify"):
        covered |= set((plan.get(section) or {}).keys())
    covered |= set(plan.get("missing") or [])
    unknown = covered - set(spec.fields)
    if unknown:
        errs.append(f"unknown field keys in plan: {sorted(unknown)}")
    absent = [k for k in spec.fields if k not in covered and not spec.fields[k].computed]
    if absent:
        errs.append(f"fields not covered by the plan: {absent}")
    for k in plan.get("missing") or []:
        f = spec.fields.get(k)
        if f and f.scope == "row" and re.search(r"\bcolumns?\b", f.description + " " + f.original, re.I):
            errs.append(f"{k!r} is listed as missing, but its description refers to a column of the table; "
                        f"map it to the matching column (look at the header rows)")
    for k, col in (plan.get("columns") or {}).items():
        try:
            letter = col["column"] if isinstance(col, dict) else col
            if isinstance(col, dict) and col.get("regex"):
                re.compile(col["regex"])
            if col_index(letter) > sheet.max_col + 5:
                errs.append(f"columns.{k}: column {letter} is beyond the sheet")
        except (ValueError, KeyError, TypeError, re.error) as exc:
            errs.append(f"columns.{k}: bad column spec {col!r} ({exc})")
    return errs


def execute(plan: dict, grid: Grid, spec: SchemaSpec, diag: dict | None = None) -> list[dict]:
    """Run a plan on the full grid → list of rows: field → {value, citation, source_locations} (raw values).
    `diag` (optional) receives {"continued_columns": {col: share of records whose later lines use that column}}."""
    sheet = grid.sheet(plan.get("sheet"))
    rows = sheet.nonempty_rows()
    lo = plan.get("first_data_row") or 1
    hi = plan.get("last_data_row") or sheet.max_row
    rs = plan["record_start"]
    rs_col = col_index(rs["column"])
    req = [col_index(c) for c in plan.get("require_values") or []]
    skips = plan.get("skip_if") or []
    stop, end = plan.get("stop_at"), plan.get("record_end")

    starts: list[int] = []
    stop_row = None
    for r in rows:
        if r < lo or r > hi:
            continue
        if stop and r > lo and _match(stop["pattern"], sheet.get(r, col_index(stop["column"]))):
            stop_row = r
            break
        if not _match(rs["pattern"], sheet.get(r, rs_col)):
            continue
        if any(sheet.get(r, c) is None for c in req):
            continue
        if any(_match(s["pattern"], sheet.get(r, col_index(s["column"]))) for s in skips if "pattern" in s):
            continue
        starts.append(r)

    limit = min(hi, stop_row - 1 if stop_row else hi)
    blocks = []
    for i, r in enumerate(starts):
        nxt = starts[i + 1] - 1 if i + 1 < len(starts) else limit
        block = [x for x in rows if r <= x <= nxt]
        if end:
            closing = [x for x in block if x > r and _match(end["pattern"], sheet.get(x, col_index(end["column"])))]
            if closing:
                block = [x for x in block if x <= closing[0]]
            elif i + 1 == len(starts):
                block = [r]                       # last record without a closing row: do not swallow trailing summaries
        blocks.append(block)

    def where(r, c):
        """Location tag for citations: a cell address for spreadsheets, the page for grids rebuilt from PDFs."""
        if sheet.row_pages:
            return f"Page {sheet.row_pages.get(r, 1)}"
        return sheet.ref(r, c)

    def cell(r, c, k=None):
        v = sheet.get(r, c)
        if v is None:
            return {"value": None, "citation": "", "source_locations": []}
        out = {"value": v, "citation": f"[{where(r, c)}] {fmt(v)}"}
        if sheet.row_pages:
            out["page_numbers"] = [sheet.row_pages.get(r, 1)]
        else:
            out["source_locations"] = [sheet.ref(r, c)]
        return out

    if diag is not None and blocks:
        # a "value" block field whose where-condition matches several lines with different values is ambiguous
        amb = {}
        for k, b in (plan.get("block_fields") or {}).items():
            if b.get("mode") != "value" or not b.get("where"):
                continue
            c = col_index(b["column"])
            n = 0
            for block in blocks:
                vals = {fmt(sheet.get(x, c)) for x in block if _cell_ok(sheet, x, b["where"]) and sheet.get(x, c) is not None}
                n += len(vals) > 1
            if n:
                amb[k] = n / len(blocks)
        diag["ambiguous_value_fields"] = amb
        cont: dict[int, int] = {}
        for block in blocks:
            used = {c for x in block[1:] for c in sheet.row(x)}
            for c in used:
                cont[c] = cont.get(c, 0) + 1
        diag["continued_columns"] = {col_letter(c): n / len(blocks) for c, n in cont.items()}
    start_set = set(starts)
    header_rows = {}
    for k, h in (plan.get("header_fields") or {}).items():
        cond = h.get("header_row") or {}
        ne = [col_index(c) for c in cond.get("nonempty", [h["column"]])]
        em = [col_index(c) for c in cond.get("empty", [])]
        header_rows[k] = [x for x in rows if x not in start_set
                          and all(sheet.get(x, c) is not None for c in ne) and all(sheet.get(x, c) is None for c in em)]

    out = []
    for block in blocks:
        r = block[0]
        row = {}
        for k, col in (plan.get("columns") or {}).items():
            if isinstance(col, dict):
                fr = cell(r, col_index(col["column"]))
                if fr["value"] is not None and col.get("regex"):
                    m = re.search(col["regex"], fmt(fr["value"], 10_000))
                    fr["value"] = (m.group(1) if m and m.groups() else m.group(0) if m else None)
                    if fr["value"] is None:
                        fr = {"value": None, "citation": "", "source_locations": []}
                row[k] = fr
            else:
                row[k] = cell(r, col_index(col))
        for k, b in (plan.get("block_fields") or {}).items():
            c = col_index(b["column"])
            hits = [x for x in block if _cell_ok(sheet, x, b.get("where"))]
            mode = b.get("mode", "first")
            if mode == "first":
                found = next((x for x in hits if sheet.get(x, c) is not None), None)
                row[k] = cell(found, c) if found else {"value": None, "citation": "", "source_locations": []}
            elif mode == "value":
                found = hits[0] if hits else None
                row[k] = cell(found, c) if found else {"value": None, "citation": "", "source_locations": []}
            elif mode == "join":
                parts = [(x, sheet.get(x, c)) for x in hits if sheet.get(x, c) is not None]
                text = " ".join(fmt(v, 10_000) for _, v in parts)
                row[k] = {"value": text or None, "citation": f"[{where(parts[0][0], c)}] {text[:120]}" if parts else "",
                          **({"page_numbers": sorted({sheet.row_pages.get(x, 1) for x, _ in parts})} if sheet.row_pages
                             else {"source_locations": [sheet.ref(x, c) for x, _ in parts][:24]})}
            elif mode in ("sum", "count"):
                vals = [(x, _num(sheet.get(x, c))) for x in hits]
                vals = [(x, v) for x, v in vals if v is not None] if mode == "sum" else [(x, 1) for x in hits]
                total = sum(v for _, v in vals)
                total = round(total, 6) if isinstance(total, float) else total
                row[k] = {"value": total, "citation": (f"[{sheet.ref(vals[0][0], c)}] {mode} of {len(vals)} lines"
                                                       if vals else ""),
                          "source_locations": [sheet.ref(x, c) for x, _ in vals][:24]}
        for k, a in (plan.get("row_aggregates") or {}).items():
            cs = _cols(a.get("columns") or [])
            nums = [(c, _num(sheet.get(r, c))) for c in cs]
            if a.get("op") == "count_nonzero":
                v = sum(1 for _, n in nums if n not in (None, 0))
            else:
                v = sum(n for _, n in nums if n is not None)
            row[k] = {"value": v, "citation": f"[{sheet.ref(r, cs[0])}:{col_letter(cs[-1])}{r}] {a.get('op')}" if cs else "",
                      "source_locations": [sheet.ref(r, c) for c, n in nums if n is not None][:24]}
        for k, h in (plan.get("header_fields") or {}).items():
            above = [x for x in header_rows.get(k, []) if x < r]
            row[k] = cell(above[-1], col_index(h["column"])) if above else {"value": None, "citation": "", "source_locations": []}
        for k, cst in (plan.get("constants") or {}).items():
            ref = cst.get("cell") or ""
            fr = {"value": cst.get("value"), "citation": "", "source_locations": [], "confidence": cst.get("confidence")}
            if ref:
                try:
                    _, cr, cc = split_ref(ref)
                except ValueError:
                    cr = None
                if sheet.row_pages and cr is not None:      # grid rebuilt from a PDF: cite the page
                    p = sheet.row_pages.get(cr, 1)
                    fr.update(citation=f"[Page {p}] {fmt(cst.get('value'))}", page_numbers=[p])
                else:
                    fr.update(citation=f"[{sheet.name}!{ref}] {fmt(cst.get('value'))}", source_locations=[f"{sheet.name}!{ref}"])
            row[k] = fr
        for k in plan.get("missing") or []:
            row[k] = {"value": None, "citation": "", "source_locations": []}
        out.append(row)
    return out


def finalise(rows: list[dict], plan: dict, spec: SchemaSpec) -> list[dict]:
    """Apply null_values / value_maps / blank_defaults / stage-0 types / absent defaults / per-row formulas."""
    nulls = {str(x).strip().lower() for x in plan.get("null_values") or []}
    vmaps = plan.get("value_maps") or {}
    blanks = plan.get("blank_defaults") or {}
    out = []
    for row in rows:
        new = {}
        for k, f in spec.fields.items():
            fr = dict(row.get(k) or {"value": None, "citation": "", "source_locations": []})
            v = fr.get("value")
            if isinstance(v, str) and v.strip().lower() in nulls:
                v = None
            if v is not None and k in vmaps and fmt(v, 10_000) in vmaps[k]:
                v = vmaps[k][fmt(v, 10_000)]
            if v is not None:
                v = normalise(v, f.type, f.item_type)
            if v is None and k in blanks:
                v = blanks[k]
            if v is None and f.absent is not None:
                v = list(f.absent) if isinstance(f.absent, list) else f.absent
            fr["value"] = v
            new[k] = fr
        for k, f in spec.fields.items():           # per-row formulas from stage 0, only where the plan gave no value
            if f.computed and new[k]["value"] is None:
                try:
                    v = F.evaluate(f.computed.formula, {kk: vv["value"] for kk, vv in new.items()})
                    new[k] = {"value": normalise(v, f.type, f.item_type or "number"), "citation": "", "source_locations": []}
                except F.FormulaError:
                    pass
        out.append(new)
    return out


def check(rows: list[dict], spec: SchemaSpec, plan: dict, diag: dict | None = None) -> list[str]:
    errs = []
    for chk in plan.get("checks") or []:
        expr, target = chk.get("expr"), chk.get("equals")
        if not isinstance(expr, str) or target not in spec.fields:
            continue
        evaluated = failed = 0
        for r in rows:
            vals = {k: fr.get("value") for k, fr in r.items()}
            try:
                got = F.evaluate(expr, vals)
            except F.FormulaError:
                continue
            want = vals.get(target)
            if not isinstance(want, (int, float)) or not isinstance(got, (int, float)):
                continue
            evaluated += 1
            failed += abs(got - want) > max(0.01, abs(want) * 0.005)
        if evaluated and failed / evaluated > 0.05:
            errs.append(f"check {expr} == {target} fails in {failed} of {evaluated} rows: one of these fields is "
                        f"probably read from the wrong place (e.g. a start-row column instead of a total line)")
    cont = (diag or {}).get("continued_columns", {})
    for k, share in (diag or {}).get("ambiguous_value_fields", {}).items():
        if share >= 0.01:
            errs.append(f"block_fields.{k}: mode 'value' found several matching lines with different values in "
                        f"{share:.0%} of records and would keep only the first; use 'sum' if they add up, or a narrower "
                        f"'where' if only one is meant")
    for k, src in (plan.get("columns") or {}).items():
        letter = src.get("column") if isinstance(src, dict) else src
        f = spec.fields.get(k)
        if f and f.type == "freetext" and cont.get(str(letter).upper(), 0) >= 0.1:
            errs.append(f"{k!r} reads only the record's first line, but column {letter} continues onto following lines "
                        f"in {cont[str(letter).upper()]:.0%} of records; use block_fields with mode 'join' for it")
    if not rows:
        return ["the plan produced no rows: record_start / require_values / bounds match nothing"]
    pk = next((k for k, f in spec.fields.items() if f.primary), None)
    if pk:
        keys = [r[pk]["value"] for r in rows]
        empty = sum(1 for k in keys if k in (None, ""))
        if empty:
            errs.append(f"primary key {pk!r} is empty in {empty} of {len(rows)} rows")
        seen, dups = set(), set()
        for k in keys:
            if k in seen:
                dups.add(k)
            seen.add(k)
        if dups and len(dups) / len(rows) > 0.2:
            errs.append(f"primary key {pk!r} has many duplicates: {sorted(map(str, dups))[:8]} "
                        f"(are header/summary rows matched?)")
    for k, f in spec.fields.items():
        src = (plan.get("columns") or {}).get(k)
        if isinstance(src, dict):
            src = src.get("column")
        if not src or f.type in ("text", "freetext"):
            continue
        raw = [r[k] for r in rows]
        bad = sum(1 for fr in raw if fr.get("value") is None and fr.get("source_locations"))
        filled = sum(1 for fr in raw if fr.get("source_locations"))
        if filled and bad / filled > 0.3:
            errs.append(f"field {k!r} (column {src}): {bad} of {filled} cells do not parse as {f.type}; wrong column?")
    return errs


# ------------------------------------------------------------------------------- orchestration

def dedupe(rows: list[dict], spec: SchemaSpec) -> tuple[list[dict], int]:
    """Keep the first row per primary key (reports often list a record twice, e.g. a unit in the main list and again
    in a 'future residents' section)."""
    pk = next((k for k, f in spec.fields.items() if f.primary), None)
    if not pk:
        return rows, 0
    seen, out = set(), []
    for r in rows:
        k = r[pk]["value"]
        if k is not None and k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, len(rows) - len(out)


def _cite_cells(rows: list[dict]) -> list[dict]:
    for row in rows:
        for fr in row.values():
            if fr.get("confidence") is None:
                fr["confidence"] = 0.9 if fr.get("source_locations") and fr.get("value") is not None else (
                    0.6 if fr.get("value") is None else 0.75)
            if fr.get("value") is None:
                fr["citation"], fr["source_locations"] = "", []
    return rows


async def classify(rows: list[dict], plan: dict, spec: SchemaSpec, client: LLMClient, *, model: str, extra: dict,
                   usage: UsageTracker | None, job_id: str | None, deadline: float | None) -> None:
    for k, c in (plan.get("classify") or {}).items():
        inputs = [i for i in c.get("inputs") or [] if i in spec.fields]
        options = [str(o) for o in c.get("options") or []]
        if not inputs or not options or k not in spec.fields:
            continue
        combos = sorted({tuple(fmt(r[i]["value"]) if r[i]["value"] is not None else "" for i in inputs) for r in rows})
        items = "\n".join(f"{n}. " + " | ".join(f"{i}={v}" for i, v in zip(inputs, combo)) for n, combo in enumerate(combos))
        msgs = [{"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": f"Field: {k}: {spec.fields[k].description}\nAllowed labels: {options}\n\n{items}"}]
        r = await client.chat(model=model, messages=msgs, stage="table_classify", job_id=job_id, usage=usage,
                              temperature=0, max_tokens=4000, timeout_s=90, deadline=deadline,
                              response_format={"type": "json_object"}, **extra)
        got = parse_json(r.content) or {}
        labels = {combo: got.get(str(n)) for n, combo in enumerate(combos)}
        for row in rows:
            combo = tuple(fmt(row[i]["value"]) if row[i]["value"] is not None else "" for i in inputs)
            lab = labels.get(combo)
            row[k] = {"value": lab if lab in options else None, "citation": "", "source_locations": [],
                      "confidence": 0.8 if lab in options else 0.5}


async def rows_fallback(grid: Grid, spec: SchemaSpec, client: LLMClient, *, model: str, extra: dict,
                        usage: UsageTracker | None, job_id: str | None, deadline: float | None,
                        chunk: int = 60) -> list[dict]:
    """Last resort: the LLM writes the rows, chunk by chunk (cost grows with the table's length)."""
    sheet = max(grid.sheets, key=lambda s: len(s.cells))
    rows = sheet.nonempty_rows()
    head = "\n".join(f"row {r}: " + " | ".join(f"{col_letter(c)}={fmt(v)}" for c, v in sorted(sheet.row(r).items()))
                     for r in rows[:12])
    out = []
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        body = "\n".join(f"row {r}: " + " | ".join(f"{col_letter(c)}={fmt(v)}" for c, v in sorted(sheet.row(r).items()))
                         for r in part)
        msgs = [{"role": "system", "content": ROWS_SYSTEM},
                {"role": "user", "content": f"Fields:\n{field_list(spec)}\n\nTop of the sheet:\n{head}\n\nChunk:\n{body}"}]
        r = await client.chat(model=model, messages=msgs, stage="table_rows_fallback", job_id=job_id, usage=usage,
                              temperature=0, max_tokens=12000, timeout_s=150, deadline=deadline,
                              response_format={"type": "json_object"}, **extra)
        got = parse_json(r.content) or {}
        for obj in got.get("rows") or []:
            if isinstance(obj, dict):
                out.append({k: {"value": obj.get(k), "citation": "", "source_locations": [], "confidence": 0.6}
                            for k in spec.fields})
    return out


FALLBACK_MAX_ROWS = 400      # above this the LLM-writes-rows fallback is too slow/expensive; use the best plan instead


async def extract_table(spec: SchemaSpec, grid: Grid, client: LLMClient, *, model: str, extra: dict,
                        usage: UsageTracker | None, job_id: str | None = None,
                        deadline: float | None = None, max_plans: int = 3) -> TableResult:
    msgs = plan_messages(spec, grid)
    errors: list[str] = []
    plan = None
    best = None                                   # (n_errors, rows, plan, errors) of the best failing plan
    for attempt in range(max_plans):
        r = await client.chat(model=model, messages=msgs, stage="table_plan", job_id=job_id, usage=usage,
                              temperature=0, max_tokens=6000, timeout_s=120, deadline=deadline,
                              response_format={"type": "json_object"}, **extra)
        plan = parse_json(r.content)
        rows: list[dict] = []
        if plan is None:
            errors = ["the plan was not a JSON object"]
        else:
            errors = validate_plan(plan, grid, spec)
            if not errors:
                try:
                    diag: dict = {}
                    raw = execute(plan, grid, spec, diag)
                    rows = finalise(raw, plan, spec)
                    errors = check(rows, spec, plan, diag)
                except Exception as exc:  # noqa: BLE001 - a bad plan must not crash the job
                    errors = [f"executing the plan failed: {type(exc).__name__}: {exc}"]
                    rows = []
                if not errors:
                    rows, dropped = dedupe(rows, spec)
                    if plan.get("classify"):
                        await classify(rows, plan, spec, client, model=model, extra=extra, usage=usage,
                                       job_id=job_id, deadline=deadline)
                    notes = [f"plan ok after {attempt + 1} call(s)"] + ([f"{dropped} duplicate rows merged"] if dropped else [])
                    return TableResult(rows=_cite_cells(rows), plan=plan, notes=notes)
                if rows and (best is None or len(errors) < best[0]):
                    best = (len(errors), rows, plan, errors)
        log.info("plan rejected", extra={"job_id": job_id, "attempt": attempt + 1, "errors": errors[:5]})
        if attempt + 1 < max_plans:
            msgs = msgs + [{"role": "assistant", "content": (r.content or "")[:8000]},
                           {"role": "user", "content": "Executing that plan on the full grid found these problems:\n- "
                                                       + "\n- ".join(errors[:10]) + "\nReturn a corrected plan (the "
                                                       "complete JSON object)."}]
    size = max(len(s.nonempty_rows()) for s in grid.sheets)
    if size > FALLBACK_MAX_ROWS and best is not None:
        _, rows, plan, errors = best
        rows, _ = dedupe(rows, spec)
        if plan.get("classify"):
            await classify(rows, plan, spec, client, model=model, extra=extra, usage=usage, job_id=job_id, deadline=deadline)
        return TableResult(rows=_cite_cells(rows), plan=plan, errors=errors, method="best_plan", partial=True,
                           notes=[f"no plan passed all checks after {max_plans} calls; used the best one "
                                  f"({'; '.join(errors[:2])})"])
    rows = await rows_fallback(grid, spec, client, model=model, extra=extra, usage=usage, job_id=job_id, deadline=deadline)
    rows = finalise(rows, {}, spec)
    return TableResult(rows=_cite_cells(rows), plan=plan, errors=errors, method="rows_fallback", partial=True,
                       notes=[f"plans failed ({'; '.join(errors[:3])}); LLM wrote the rows"])
