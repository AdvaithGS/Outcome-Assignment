"""Unit tests for layer 1's deterministic parts: formula language, normalisation, heuristics, post-processing."""
from __future__ import annotations

import pytest

from app.ingest.document import MarkdownBuilder, PageInfo
from app.pipeline import formula as F
from app.pipeline.normalise import normalise, to_date, to_month, to_number
from app.pipeline.postprocess import postprocess
from app.pipeline.schema_analysis import Computed, heuristic_spec, merge_llm


# ------------------------------------------------------------------ formula language

def test_lease_formulas():
    v = {"start": "1993-05-01", "end": "1998-04-30", "rent": [1932, 1990, 2050, 2111, 2174], "rsf": 1892,
         "starts": ["1993-05-01", "1994-05-01"], "ends": ["1994-04-30", "1995-04-30"], "m": [1000, 1100]}
    assert F.evaluate("months_between(start, end)", v) == 60
    assert F.evaluate("round_each(div(mul(rent, 12), rsf), 2)", v) == [12.25, 12.62, 13.0, 13.39, 13.79]  # = truth
    assert F.evaluate("sum(mul(m, months_each(starts, ends)))", v) == 12 * 1000 + 12 * 1100
    assert F.evaluate("round(div(a, b))", {"a": 41_500_000, "b": 188}) == 220745
    assert F.evaluate("round(2.5)", {}) == 3                    # half-up, not banker's rounding
    assert F.evaluate("round_each(div(mul(rent, 12), rsf), digits=2)", v)[1] == 12.62   # named digits= allowed
    assert F.evaluate("round(div(a, b), digits=0)", {"a": 10, "b": 4}) == 3


def test_months_between_partial_and_short_terms():
    assert F.months_between("2025-10-01", "2027-04-30") == 19
    assert F.months_between("2024-01-15", "2024-02-14") == 1
    assert F.months_between("2024-01-15", "2024-02-13") == 0


@pytest.mark.parametrize("bad", ["__import__('os')", "a.b", "[x for x in y]", "open('f')", "lambda: 1", "a if b else c",
                                 "sum(x, start=1)", "round(x, ndigits=2)"])
def test_formula_language_rejects_anything_else(bad):
    with pytest.raises(F.FormulaError):
        F.parse(bad)


def test_formula_missing_operand_and_length_mismatch():
    with pytest.raises(F.FormulaError):
        F.evaluate("sum(x)", {})
    with pytest.raises(F.FormulaError):
        F.evaluate("mul(a, b)", {"a": [1, 2], "b": [1]})
    assert F.names("round(div(asking_price, units))") == {"asking_price", "units"}


# ------------------------------------------------------------------ normalisation

@pytest.mark.parametrize("raw,want", [("$4,250.00", 4250), ("(1,234)", -1234), ("-46.78%", -46.78), (2150.0, 2150.0),
                                      ("1,892 RSF", 1892), ("n/a", None), ("about five", None), (True, None)])
def test_numbers(raw, want):
    assert to_number(raw) == want


@pytest.mark.parametrize("raw,want", [("March 5, 19 93", "1993-03-05"), ("the 21st day of August, 2025", "2025-08-21"),
                                      ("first day of October, 2025", "2025-10-01"), ("05/01/93", "1993-05-01"),
                                      ("2026-01-01", "2026-01-01"), ("Feb. 9, 2025", "2025-02-09"), ("13/45/2020", None)])
def test_dates(raw, want):
    assert to_date(raw) == want


def test_month_bool_list_and_scalar_from_list():
    assert to_month("February 2025") == "2025-02" and to_month("02/2025") == "2025-02"
    assert normalise("Yes", "bool") is True and normalise("false", "bool") is False
    assert normalise(["$1,932", "1,990"], "list", "number") == [1932, 1990]
    assert normalise(["Suite 310"], "text") == "Suite 310" and normalise(["a", "b"], "text") is None
    assert normalise("  Suite 214W ", "text") == "Suite 214W" and normalise("N/A", "text") is None


# ------------------------------------------------------------------ heuristics

def test_heuristic_types_and_absent_values():
    schema = {"name": "lease", "fields": {
        "free_rent_months": {"name": "Free Rent", "description": "Total months of abatement, as a number. 0 if none."},
        "loc": {"name": "LOC", "description": "true if the lease requires a letter of credit, false otherwise."},
        "brokers": {"name": "Brokers", "description": "List of brokers named. Empty list if none are named."},
        "start": {"name": "Commencement Date", "description": "The date the lease term begins."},
        "share": {"name": "Pro Rata Share", "description": "Share as a percentage number exactly as stated."},
        "period": {"name": "Report Period", "description": "The month and year, as YYYY-MM. Same for every row."},
    }}
    s = heuristic_spec(schema).fields
    assert (s["free_rent_months"].type, s["free_rent_months"].absent) == ("number", 0)
    assert (s["loc"].type, s["loc"].absent) == ("bool", False)
    assert (s["brokers"].type, s["brokers"].absent) == ("list", [])
    assert s["start"].type == "date" and s["share"].type == "percent" and s["period"].type == "month"


def test_merge_llm_rejects_bad_formulas_and_keeps_heuristics():
    schema = {"fields": {"a": {"name": "A", "description": "a number"}, "b": {"name": "B", "description": "a number"},
                         "t": {"name": "T", "description": "total"}}}
    spec = merge_llm(heuristic_spec(schema), {"fields": {
        "t": {"type": "number", "computed": {"formula": "add(a, b)", "operands": {"a": "field", "b": "field"}}},
        "a": {"type": "number", "computed": {"formula": "__import__('os')", "operands": {}}},
        "b": {"type": "bogus", "description": "x"}}})
    assert spec.fields["t"].computed.formula == "add(a, b)"
    assert spec.fields["a"].computed is None                  # unsafe formula rejected
    assert spec.fields["b"].type == "number"                  # invalid type ignored, heuristic kept
    assert spec.fields["b"].description == "a number"         # too-short rewrite ignored


# ------------------------------------------------------------------ post-processing

def _doc():
    b = MarkdownBuilder()
    b.page(1)
    b.text("TERM: commencing May 1, 1993 and ending April 30, 1998. Total Base Rent $ 123,084")
    b.page(2)
    b.text("SECURITY DEPOSIT: ~~2,400~~ [handwritten: 2150]")
    return b.build(source="x.pdf", kind="pdf", variant="t", pages=[PageInfo(1, "text", 10), PageInfo(2, "text", 10)])


def _spec():
    schema = {"fields": {
        "start": {"name": "Start", "description": "Commencement date, YYYY-MM-DD"},
        "end": {"name": "End", "description": "Expiration date, YYYY-MM-DD"},
        "term": {"name": "Term", "description": "Length in whole months. Compute it if only dates are given."},
        "total": {"name": "Total", "description": "Total base rent as a number. Compute if not stated."},
        "deposit": {"name": "Deposit", "description": "Security deposit as a number"},
        "free": {"name": "Free", "description": "Months of free rent as a number. 0 if none."},
    }}
    spec = heuristic_spec(schema)
    spec.fields["term"].type = "integer"
    spec.fields["term"].computed = Computed("months_between(start, end)", {"start": "field", "end": "field"}, True)
    spec.fields["total"].computed = Computed("mul(monthly, 60)", {"monthly": "monthly rent"}, True)
    return spec


RAW = {
    "start": {"value": "May 1, 1993", "citation": "[Page 1] commencing May 1, 1993"},
    "end": {"value": "1998-04-30", "citation": "[Page 1] ending April 30, 1998"},
    "term": {"value": 59, "stated": None, "citation": "[Page 1] commencing May 1, 1993 and ending April 30, 1998"},
    "total": {"value": 123084, "stated": "$ 123,084", "citation": "[Page 1] Total Base Rent $ 123,084"},
    "deposit": {"value": 2150, "citation": "[Page 2] SECURITY DEPOSIT: ~~2,400~~ [handwritten: 2150]"},
    "free": {"value": None, "citation": ""},
    "_helpers": {"monthly": {"value": "$2,000", "citation": "[Page 9] not in the doc"}},
}


def test_computed_field_strategies_from_one_response():
    doc, spec = _doc(), _spec()
    m, c, h = (postprocess(spec, RAW, doc, mode) for mode in ("M", "C", "H"))
    assert m["term"]["value"] == 59                  # the model's own (wrong) arithmetic
    assert c["term"]["value"] == 60 and h["term"]["value"] == 60
    assert m["total"]["value"] == 123084 and c["total"]["value"] == 120000
    assert h["total"]["value"] == 123084             # the printed figure wins
    assert h["total"]["confidence"] < 0.9            # printed vs computed disagree → lower confidence


def test_absent_defaults_grounding_and_pages():
    out = postprocess(_spec(), RAW, _doc(), "H")
    assert out["start"]["value"] == "1993-05-01" and out["start"]["page_numbers"] == [1]
    assert out["deposit"]["value"] == 2150 and out["deposit"]["page_numbers"] == [2]
    assert out["deposit"]["confidence"] >= 0.9                       # citation found in the document
    assert out["free"] == {"value": 0, "citation": "", "confidence": 0.7}   # "0 if none"
    bad = postprocess(_spec(), {**RAW, "deposit": {"value": 2150, "citation": "[Page 2] invented text"}}, _doc(), "H")
    assert bad["deposit"]["value"] == 2150 and bad["deposit"]["confidence"] < 0.9   # ungrounded: lower confidence only


# ------------------------------------------------------------------ pointer mode

def test_field_style_comes_from_type_and_description_only():
    from app.pipeline.single import field_style
    s = heuristic_spec({"fields": {
        "fee": {"name": "Late Fee", "description": "The late charge as the lease states it, in a short phrase."},
        "exp": {"name": "Expenses", "description": "How expenses pass through, a short phrase, e.g. 'base year 1993'."},
        "amt": {"name": "Deposit", "description": "Security deposit as a number"},
        "loc": {"name": "LOC", "description": "true if a letter of credit is required, false otherwise."}}}).fields
    for k in ("fee", "exp"):
        s[k].type = "freetext"
    assert [field_style(s[k]) for k in ("fee", "exp", "amt", "loc")] == ["quote", "summary", "pointer", "value"]
    assert [field_style(s[k], "hybrid") for k in ("fee", "exp", "amt", "loc")] == ["summary", "summary", "pointer", "value"]


def test_pointer_mode_quotes_numbers_and_fallback():
    spec = heuristic_spec({"fields": {
        "fee": {"name": "Late Fee", "description": "The late charge as the lease states it."},
        "deposit": {"name": "Deposit", "description": "Security deposit as a number"},
        "start": {"name": "Start", "description": "Commencement date, YYYY-MM-DD"}}})
    spec.fields["fee"].type = "freetext"
    b = MarkdownBuilder()
    b.page(1)
    b.text("Tenant shall pay a late charge of the greater of $75.00 or 6% of the unpaid rent.")
    b.page(2)
    b.text("SECURITY DEPOSIT: ~~2,400~~ [handwritten: 2150]. Term commencing May 1, 1993.")
    doc = b.build(source="x.pdf", kind="pdf", variant="t", pages=[PageInfo(1, "text", 1), PageInfo(2, "text", 1)])
    raw = {"fee": {"value": "greater of $75 or 6%", "quote": "the greater of $75.00 or 6% of the unpaid rent",
                   "citation": "[Page 1] late charge"},
           "deposit": {"value": 2150, "quote": "SECURITY DEPOSIT: ~~2,400~~ [handwritten: 2150]", "citation": ""},
           "start": {"value": "1993-05-01", "quote": "commencing May 1, 1993 (paraphrased, not in the text)", "citation": ""}}
    out = postprocess(spec, raw, doc, "H", "pointer")
    assert out["fee"]["value"] == "the greater of $75.00 or 6% of the unpaid rent"     # the exact span is the answer
    assert out["fee"]["citation"].startswith("[Page 1] the greater of") and out["fee"]["page_numbers"] == [1]
    assert out["deposit"]["value"] == 2150 and out["deposit"]["page_numbers"] == [2]
    assert out["deposit"]["confidence"] >= 0.9                                          # value found in its quote
    assert out["start"]["value"] == "1993-05-01"                                        # quote not found: model value kept
    assert out["start"]["confidence"] < 0.9


def test_pointer_mode_reads_spreadsheet_cells():
    from app.ingest.grid import Grid, Sheet, _index_rows, grid_document
    s = Sheet("Summary")
    s.cells = {(1, 1): "Property", (1, 2): "Willow Creek", (2, 1): "Units", (2, 2): 325.0}
    doc = grid_document(Grid(source="x.xlsx", kind="xlsx", sheets=[_index_rows(s)]))
    spec = heuristic_spec({"fields": {"name": {"name": "Property", "description": "Property name"},
                                      "units": {"name": "Units", "description": "Number of units, as a number"}}})
    raw = {"name": {"value": "Willow Creek Apts", "quote": "Willow Creek", "citation": "[Summary!B1] Willow Creek"},
           "units": {"value": 324, "quote": "325", "citation": "[Summary!B2] 325"}}
    out = postprocess(spec, raw, doc, "H", "pointer")
    assert out["name"]["value"] == "Willow Creek" and out["name"]["source_locations"] == ["Summary!B1"]
    assert out["units"]["value"] == 325 and out["units"]["citation"] == "[Summary!B2] 325"


def test_page_selection_for_vision():
    from app.pipeline.ingest import page_needs_vision
    prose = "\n".join(["The Tenant shall keep the Premises in good order and repair at its own cost."] * 20)
    assert page_needs_vision(prose, 0.92, 0.01) == (False, "OCR adequate")
    assert page_needs_vision(prose, 0.71, 0.01)[0]                                   # poor scan
    assert page_needs_vision(prose, 0.92, 0.08)[0]                                   # many low-confidence words
    table = "\n".join(["05/01/93 - 04/30/94 $ 23,184 $ 1,932 12"] * 5 + ["Rent schedule follows."] * 5)
    assert page_needs_vision(table, 0.92, 0.01)[1].startswith("table-like")
    form = "\n".join(["SECURITY DEPOSIT: $2,400", "TERM OF LEASE: Five years"] + ["general text line"] * 8)
    assert page_needs_vision(form, 0.92, 0.01)[1].startswith("form-like")



def test_terse_field_detection():
    from app.pipeline.schema_analysis import terse_fields
    schema = {"fields": {"a": {"name": "Category", "description": "Category"},
                         "b": {"name": "Months", "description": ""},
                         "c": {"name": "Units", "description": "Total unit count"},
                         "d": {"name": "Rent", "description": "Monthly base rent for the unit as a number"}}}
    assert terse_fields(schema) == ["a", "b", "c"]
    import json as _j
    for name in ("lease_abstraction", "rent_roll_units", "operating_statement_lines", "boxscore", "budget_variance",
                 "offering_memorandum"):
        try:
            sch = _j.load(open(f"sample/schemas/{name}.json"))
        except FileNotFoundError:
            continue
        assert len(terse_fields(sch)) <= 2, name            # real field lists are (almost) never terse



def test_garbage_and_line_level_signals():
    from app.pipeline.ingest import page_needs_vision, uncertain_lines
    clean = "\n".join(["The Tenant shall keep the Premises in good order and repair at its own cost."] * 20)
    garbage = clean + "\n" + " ".join(["MART/N DFLACRO/X 1D#3\"03116 ~~x|y"] * 12)
    assert page_needs_vision(garbage, 0.92, 0.01)[1].startswith("garbled")
    body = [[l, 0] for l in clean.splitlines()]
    # a stamp OCR reads differently on every page is recurring boilerplate; the garbled deposit line is not
    pages = [body + [["8 1D#3 03122 Initial", 2]], body + [["14 D#3\" 83129 lnitial", 2]],
             body + [["ID#3 03118 Initial", 2], ["SECURITY DEPOSIT: $ 2,400 swrsso OM_Telacroie", 3]]]
    unsure = uncertain_lines(pages)
    assert unsure[:2] == [None, None] and unsure[2].startswith("SECURITY DEPOSIT")
    assert page_needs_vision(clean, 0.92, 0.01, unsure[2])[1].startswith("uncertain line")
    assert page_needs_vision(clean, 0.92, 0.01, unsure[0]) == (False, "OCR adequate")



def test_cpu_pool_size_and_run():
    import asyncio
    from app.ingest.cpu import available_cpus, cpu_slots, run_cpu
    assert cpu_slots({"OCR_WORKERS": "3"}) == 3
    assert cpu_slots({}) == max(1, available_cpus() - 2) >= 1
    assert asyncio.run(run_cpu(sum, [1, 2, 3])) == 6


def test_to_number_ranges_units_multipliers():
    from app.pipeline.normalise import normalise
    for v in ("1,200-1,500", "2019-2020", "5-10", "Year 1: $25.00", "2010-03-01"):
        assert normalise(v, "number") is None, v               # several figures: never run the digits together
    assert normalise("$1.2 million", "number") == 1200000
    assert normalise("$12.5M", "number") == 12500000
    for label in ("Suite 5B", "C5", "PR - P1", "Feb 2023", "Property ID: 417", "12 Month Actual to Budget", "4W"):
        assert normalise(label, "number") is None, label        # labels are not amounts
    assert normalise("$24.50/SF", "number") == 24.5 and normalise("$4,250.00 per month", "number") == 4250.0
    assert normalise("3.5% annually", "percent") == 3.5 and normalise("(1,234)", "number") == -1234
    assert normalise("-$1,234.50", "number") == -1234.5 and normalise("approx. 12,500 rentable square feet", "number") == 12500
    assert normalise("12500SF", "number") == 12500 and normalise("36 months", "integer") == 36


def test_to_date_day_first_ordinals_datetimes():
    from datetime import datetime
    from app.pipeline.normalise import normalise
    assert normalise("13/05/2010", "date") == "2010-05-13" and normalise("05/03/2010", "date") == "2010-05-03"
    assert normalise("5th of March, 1993", "date") == "1993-03-05"
    assert normalise(datetime(2024, 1, 31, 10, 30), "date") == "2024-01-31"


def test_xlsx_percent_cells_as_displayed(tmp_path):
    import openpyxl
    from app.ingest.grid import load_xlsx
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = 0.0525, 0.95
    ws["A1"].number_format, ws["B1"].number_format = "0.00%", "0%"
    ws["C1"] = 1234
    wb.save(tmp_path / "p.xlsx")
    s = load_xlsx(tmp_path / "p.xlsx").sheets[0]
    assert (s.get(1, 1), s.get(1, 2), s.get(1, 3)) == ("5.25%", "95%", 1234)


def test_last_table_record_does_not_swallow_totals():
    from app.ingest.grid import Grid, Sheet
    from app.pipeline.schema_analysis import heuristic_spec
    from app.pipeline.table import execute
    sh = Sheet("S")
    for r, (a, b) in enumerate([("U1", 10), (None, 5), ("U2", 20), (None, 7), ("Total", 42), ("Grand", 42)], start=1):
        if a:
            sh.cells[(r, 1)] = a
        sh.cells[(r, 2)] = b
    sh.max_row, sh.max_col = 6, 2
    spec = heuristic_spec({"fields": {"unit": {"name": "Unit", "description": "unit id", "primary": True},
                                      "charges": {"name": "Charges", "description": "sum of charge amounts"}}})
    plan = {"record_start": {"column": "A", "pattern": r"U\d"}, "columns": {"unit": "A"},
            "block_fields": {"charges": {"mode": "sum", "column": "B"}}, "checks": []}
    rows = execute(plan, Grid("t", "xlsx", [sh]), spec)
    assert [r["charges"]["value"] for r in rows] == [15, 27]     # not 27 + 42 + 42 for the last unit


def test_docx_strike_and_hidden_runs(tmp_path):
    import zipfile
    from app.ingest.docx import ingest_docx
    from app.ingest.grid import grid_from_docx
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    def run(t, props=""):
        return f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{t}</w:t></w:r>'
    para = ("<w:p>" + run("Security deposit: ") + run("$2,400", "<w:strike/>") + run(" $2,150")
            + run(" internal note", "<w:vanish/>") + "</w:p>")
    cell = "<w:tc><w:p>" + run("$2,400", "<w:strike/>") + run(" $2,150") + "</w:p></w:tc>"
    body = para + f"<w:tbl><w:tr><w:tc><w:p>{run('Deposit')}</w:p></w:tc>{cell}</w:tr></w:tbl>"
    with zipfile.ZipFile(tmp_path / "d.docx", "w") as z:
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
    md = ingest_docx(tmp_path / "d.docx").markdown
    assert "~~$2,400~~ $2,150" in md and "internal note" not in md
    g = grid_from_docx(tmp_path / "d.docx")
    assert g.sheets[0].get(2, 2) == "$2,150"


# ---- missing / erroneous / failed inputs (stub client: records each model call's stage, fails or answers)

class _StubClient:
    def __init__(self, answers=None):
        self.calls, self.answers = [], dict(answers or {})

    async def chat(self, *, stage, **kw):
        self.calls.append(stage)
        if stage in self.answers and self.answers[stage]:
            from types import SimpleNamespace
            return SimpleNamespace(content=self.answers[stage].pop(0), finish_reason="stop")
        raise RuntimeError("stubbed model call")

    async def aclose(self):
        pass


def _only_stage0(c) -> bool:
    return all(s.startswith("schema_") for s in c.calls)


def _run(path, schema, file_type, client):
    import asyncio, time
    from app.artifacts import ArtifactCache
    from app.llm.usage import UsageTracker
    from app.pipeline.base import JobContext
    from app.pipeline.v1 import PipelineV1
    pipe = PipelineV1(client=client, cache=ArtifactCache(path.parent / "cache"))
    ctx = JobContext(job_id="t", org="", file_name=path.name, file_type=file_type, deadline=time.monotonic() + 600,
                     usage=UsageTracker())
    return asyncio.run(pipe.run(path, schema, ctx))


_LEASE = {"fields": {"tenant": {"name": "Tenant", "description": "Tenant legal name"},
                     "renewal": {"name": "Renewal option", "description": "true if the tenant has a renewal option, false otherwise"},
                     "free_rent": {"name": "Free rent", "description": "Number of free rent months, 0 if none"}}}
_TABLE = {"fields": {"unit": {"name": "Unit", "description": "Unit id", "primary": True},
                     "rent": {"name": "Rent", "description": "Monthly rent as a number"}}}


def test_failed_extraction_gives_nulls_not_absent_defaults():
    from app.pipeline.postprocess import postprocess
    from app.pipeline.schema_analysis import heuristic_spec
    from app.ingest.document import MarkdownBuilder
    b = MarkdownBuilder(); b.text("Some lease text that mentions nothing relevant.")
    doc = b.build(source="x", kind="pdf", variant="t")
    spec = heuristic_spec(_LEASE)
    assert spec.fields["renewal"].absent is False and spec.fields["free_rent"].absent == 0
    assert all(v["value"] is None for v in postprocess(spec, None, doc).values())       # nothing read: no false / 0
    got = postprocess(spec, {"tenant": {"value": "Acme LLC", "citation": "Acme LLC"}}, doc)
    assert got["renewal"]["value"] is False and got["free_rent"]["value"] == 0           # read, not stated: defaults


def test_blank_and_empty_documents_make_no_extraction_calls(tmp_path):
    import zipfile, pymupdf
    d = pymupdf.open(); d.new_page(); d.save(tmp_path / "blank.pdf")
    c = _StubClient(); out = _run(tmp_path / "blank.pdf", _LEASE, "pdf", c)
    assert _only_stage0(c) and out.partial and "no readable text" in " ".join(out.notes)
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    with zipfile.ZipFile(tmp_path / "empty.docx", "w") as z:
        z.writestr("word/document.xml", f"<w:document {W}><w:body></w:body></w:document>")
    c = _StubClient(); out = _run(tmp_path / "empty.docx", _LEASE, "docx", c)
    assert _only_stage0(c) and out.partial


def test_corrupt_spreadsheet_is_unreadable_not_retried(tmp_path):
    import zipfile, pytest
    from app.pipeline.base import DocumentError
    with zipfile.ZipFile(tmp_path / "bad.xlsx", "w") as z:
        z.writestr("xl/workbook.xml", "<<<garbage")
    with pytest.raises(DocumentError):
        _run(tmp_path / "bad.xlsx", _TABLE, "xlsx", _StubClient())
    with pytest.raises(DocumentError):
        _run(tmp_path / "bad.xlsx", _LEASE, "xlsx", _StubClient())


def test_empty_table_makes_no_plan_call(tmp_path):
    (tmp_path / "h.csv").write_text("Unit,Rent\n")
    c = _StubClient(); out = _run(tmp_path / "h.csv", _TABLE, "csv", c)
    assert "table_plan" not in c.calls and out.partial and out.rows == [{}]


def test_digital_pdf_without_table_lines_never_uses_vision(tmp_path):
    import pymupdf
    d = pymupdf.open()
    for i in range(3):
        d.new_page().insert_text((72, 72), f"Page {i + 1} of a narrative memo with no tables at all, only prose.")
        d[-1].insert_text((72, 100), "The parties agree to meet again next quarter to review progress.")
    d.save(tmp_path / "prose.pdf")
    c = _StubClient(); out = _run(tmp_path / "prose.pdf", _TABLE, "pdf", c)
    assert "vision_transcribe" not in c.calls and "table_plan" in c.calls
    assert out.partial


def test_failed_plan_call_moves_to_next_attempt():
    import asyncio, json
    from app.ingest.grid import Grid, Sheet
    from app.pipeline.schema_analysis import heuristic_spec
    from app.pipeline.table import extract_table
    sh = Sheet("S")
    for r, (u, rent) in enumerate([("Unit", "Rent"), ("101", 1000), ("102", 1100)], start=1):
        sh.cells[(r, 1)], sh.cells[(r, 2)] = u, rent
    sh.max_row, sh.max_col = 3, 2
    plan = {"record_start": {"column": "A", "pattern": r"\d{3}"}, "columns": {"unit": "A", "rent": "B"}, "checks": []}

    class FailOnce(_StubClient):
        async def chat(self, *, stage, **kw):
            if not self.calls:
                self.calls.append(stage)
                raise RuntimeError("provider error")
            return await super().chat(stage=stage, **kw)
    c = FailOnce({"table_plan": [json.dumps(plan)]})
    res = asyncio.run(extract_table(heuristic_spec(_TABLE), Grid("t", "csv", [sh]), c, model="m", extra={},
                                    usage=None, repair_model="r"))
    assert c.calls == ["table_plan", "table_plan"] and [r["unit"]["value"] for r in res.rows] == ["101", "102"]



def test_plan_constants_as_printed_only_for_text():
    from app.pipeline.table import printed_value
    assert printed_value("417", "Property =  417 .417") == "417 .417"                  # text: the full printed value
    assert printed_value(2021, "File: 2021.cm3", "year") == 2021                       # typed: the plan's typed value
    assert printed_value(339050, "Property RSF: 339,050", "number") == 339050
    assert printed_value("Oak Ridge", "Property: Harbor Point") == "Oak Ridge"          # not part of the cell: plan's
    assert printed_value(None, "Property: Harbor Point  Period: Jan 2024") == "Harbor Point"   # per-sheet constant



def test_large_field_lists_are_extracted_in_parallel_batches():
    import asyncio, json, re
    from types import SimpleNamespace
    from app.ingest.document import MarkdownBuilder
    from app.pipeline.schema_analysis import heuristic_spec
    from app.pipeline.single import BATCH_SIZE, extract, field_batches
    schema = {"fields": {f"f{i:02d}": {"name": f"Field {i}", "description": f"Value number {i} as a number"}
                         for i in range(80)}}
    spec = heuristic_spec(schema)
    for i, f in enumerate(spec.fields.values()):
        f.group = ["parties", "rent", "dates"][i % 3]
    batches = field_batches(spec)
    assert sum(len(b) for b in batches) == 80 and max(len(b) for b in batches) <= BATCH_SIZE
    assert sorted(k for b in batches for k in b) == sorted(spec.fields)

    class Echo:
        def __init__(self): self.calls = []
        async def chat(self, *, messages, **kw):
            keys = re.findall(r'^- "(f\d\d)"', messages[1]["content"], re.M)
            self.calls.append(keys)
            return SimpleNamespace(content=json.dumps({k: {"value": int(k[1:]), "citation": ""} for k in keys}),
                                   finish_reason="stop")
    b = MarkdownBuilder(); b.text("A document."); doc = b.build(source="x", kind="pdf", variant="t")
    c = Echo()
    raw, notes = asyncio.run(extract(spec, doc, c, model="m", extra={}, fallback_model=None, fallback_extra=None, usage=None))
    assert len(c.calls) == len(batches) and all(len(k) <= BATCH_SIZE for k in c.calls)
    assert {k: v["value"] for k, v in raw.items()} == {k: int(k[1:]) for k in spec.fields}
    small = heuristic_spec({"fields": dict(list(schema["fields"].items())[:40])})
    c = Echo(); asyncio.run(extract(small, doc, c, model="m", extra={}, fallback_model=None, fallback_extra=None, usage=None))
    assert len(c.calls) == 1                                    # 40 fields: one call, as before
