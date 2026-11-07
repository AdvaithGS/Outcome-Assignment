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
