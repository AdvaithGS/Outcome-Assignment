"""Table branch: plan execution on synthetic grids (no LLM): blocks, joins, conditional sums, sections,
row aggregates, partial-cell columns, dedupe, checks, PDF-style page citations."""
from __future__ import annotations

from app.ingest.grid import Grid, Sheet, _index_rows, col_index, col_letter, split_ref
from app.pipeline import table as T
from app.pipeline.schema_analysis import heuristic_spec


def sheet(rows: dict[int, dict[str, object]], name="S", row_pages=None) -> Grid:
    s = Sheet(name)
    for r, cells in rows.items():
        for c, v in cells.items():
            s.cells[(r, col_index(c))] = v
    s.row_pages = row_pages
    return Grid(source="t", kind="xlsx", sheets=[_index_rows(s)])


RENT_SCHEMA = {"name": "rr", "fields": {
    "unit": {"name": "Unit", "description": "Unit id", "primary": True},
    "market": {"name": "Market Rent", "description": "Market rent as a number"},
    "rent": {"name": "Rent", "description": "Rent charge as a number. 0 if none."},
    "other": {"name": "Other", "description": "Sum of other charges as a number. 0 if none."},
    "total": {"name": "Total", "description": "Charge total as a number"},
    "prop": {"name": "Property", "description": "Property name. Same for every row."},
}}
RENT_GRID = {
    1: {"A": "Sunny Court"},
    2: {"A": "Unit", "B": "Market", "C": "Code", "D": "Amount"},
    3: {"A": "100-101", "B": 1500, "C": "Rent", "D": 1400},
    4: {"C": "Pet", "D": 25},
    5: {"C": "Parking", "D": 40},
    6: {"C": "Charge Total:", "D": 1465},
    7: {"A": "100-102", "C": "Pet", "D": 10},
    8: {"B": 1600, "C": "Rent", "D": 1550},
    9: {"C": "Charge Total:", "D": 1560},
    10: {"A": "Totals", "B": 3100, "D": 3025},
    11: {"A": "100-101", "B": 1500, "C": "Rent", "D": 1400},     # listed again in a later section
    12: {"C": "Charge Total:", "D": 1400},
}
RENT_PLAN = {
    "sheet": "S", "record_start": {"column": "A", "pattern": r"\d{3}-\d{3}"},
    "record_end": {"column": "C", "pattern": "Charge Total.*"},
    "columns": {"unit": "A"},
    "block_fields": {"market": {"mode": "first", "column": "B"},
                     "rent": {"mode": "sum", "column": "D", "where": {"column": "C", "equals": "Rent"}},
                     "other": {"mode": "sum", "column": "D", "where": {"column": "C", "not_matches": "^(Rent|Charge Total.*)$"}},
                     "total": {"mode": "value", "column": "D", "where": {"column": "C", "matches": "Charge Total"}}},
    "constants": {"prop": {"value": "Sunny Court", "cell": "A1"}}, "checks": [],
}


def run(plan, grid, schema):
    spec = heuristic_spec(schema)
    assert T.validate_plan(plan, grid, spec) == []
    diag: dict = {}
    rows = T.finalise(T.execute(plan, grid, spec, diag), plan, spec)
    return rows, spec, diag


def test_multi_row_records_and_block_fields():
    rows, spec, _ = run(RENT_PLAN, sheet(RENT_GRID), RENT_SCHEMA)
    rows, dropped = T.dedupe(rows, spec)
    assert dropped == 1 and [r["unit"]["value"] for r in rows] == ["100-101", "100-102"]
    a, b = rows
    assert (a["market"]["value"], a["rent"]["value"], a["other"]["value"], a["total"]["value"]) == (1500, 1400, 65, 1465)
    assert (b["market"]["value"], b["rent"]["value"], b["other"]["value"], b["total"]["value"]) == (1600, 1550, 10, 1560)
    assert b["market"]["source_locations"] == ["S!B8"]          # "first" found the value on a later line of the block
    assert a["total"]["citation"] == "[S!D6] 1465"
    assert a["prop"]["value"] == "Sunny Court" and a["prop"]["source_locations"] == ["S!A1"]


def test_last_record_without_closing_row_does_not_swallow_summaries():
    grid = dict(RENT_GRID)
    del grid[12]
    rows, _, _ = run(RENT_PLAN, sheet(grid), RENT_SCHEMA)
    assert rows[-1]["rent"]["value"] == 1400 and rows[-1]["total"]["value"] is None


def test_sections_row_aggregates_require_values_and_regex_columns():
    schema = {"name": "t12", "fields": {
        "gl": {"name": "GL", "description": "GL code", "primary": True},
        "name": {"name": "Name", "description": "Account name"},
        "section": {"name": "Section", "description": "Nearest section header above"},
        "active": {"name": "Months", "description": "Count of months with a non-zero value, as a number"},
        "total": {"name": "Total", "description": "Total as a number"}}}
    grid = sheet({
        1: {"A": "REVENUE"},
        2: {"A": "510-500 Gross Rent", "B": 100, "C": 0, "D": 120, "E": 220},
        3: {"A": "510-520 Pet Rent", "B": 5, "C": 5, "D": 0, "E": 10},
        4: {"A": "EXPENSES"},
        5: {"A": "600-100 Repairs", "B": 0, "C": 0, "D": 0, "E": 0},
        6: {"A": "600-000 Payroll"},                                   # GL-like but no amounts: not a row
    })
    plan = {"sheet": "S", "record_start": {"column": "A", "pattern": r"\d{3}-\d{3}.*"}, "require_values": ["B"],
            "columns": {"gl": {"column": "A", "regex": r"^(\d{3}-\d{3})"}, "name": {"column": "A", "regex": r"^\S+\s+(.*)$"},
                        "total": "E"},
            "row_aggregates": {"active": {"op": "count_nonzero", "columns": ["B", "D"]}},
            "header_fields": {"section": {"column": "A", "header_row": {"nonempty": ["A"], "empty": ["B"]}}}, "checks": []}
    rows, spec, _ = run(plan, grid, schema)
    got = [(r["gl"]["value"], r["name"]["value"], r["section"]["value"], r["active"]["value"], r["total"]["value"]) for r in rows]
    assert got == [("510-500", "Gross Rent", "REVENUE", 2, 220), ("510-520", "Pet Rent", "REVENUE", 2, 10),
                   ("600-100", "Repairs", "EXPENSES", 0, 0)]


def test_checks_catch_bad_plans():
    spec = heuristic_spec(RENT_SCHEMA)
    grid = sheet(RENT_GRID)
    assert any("not covered" in e for e in T.validate_plan({**RENT_PLAN, "constants": {}}, grid, spec))
    assert any("unknown field" in e for e in T.validate_plan({**RENT_PLAN, "missing": ["nope"]}, grid, spec))
    assert T.check([], spec, RENT_PLAN) and "no rows" in T.check([], spec, RENT_PLAN)[0]
    bad = {**RENT_PLAN, "columns": {"unit": "A"}, "block_fields": {**RENT_PLAN["block_fields"],
                                                                  "market": {"mode": "first", "column": "C"}}}
    rows = T.finalise(T.execute(bad, grid, spec), bad, spec)
    assert rows[0]["market"]["value"] is None                    # text in a number field does not parse → null


def test_freetext_split_over_lines_must_be_joined():
    schema = {"name": "b", "fields": {"gl": {"name": "GL", "description": "GL code", "primary": True},
                                      "why": {"name": "Explanation", "description": "Commentary verbatim, a sentence"}}}
    grid = sheet({1: {"A": "6101", "B": "Rent was lower"}, 2: {"B": "because of the pandemic."},
                  3: {"A": "6102", "B": "No change."}}, row_pages={1: 1, 2: 1, 3: 2})
    first_line = {"sheet": "S", "record_start": {"column": "A", "pattern": r"\d{4}"}, "columns": {"gl": "A", "why": "B"}}
    spec = heuristic_spec(schema)
    diag: dict = {}
    rows = T.finalise(T.execute(first_line, grid, spec, diag), first_line, spec)
    assert any("join" in e for e in T.check(rows, spec, first_line, diag))
    joined = {**first_line, "columns": {"gl": "A"}, "block_fields": {"why": {"mode": "join", "column": "B"}}}
    rows = T.finalise(T.execute(joined, grid, spec), joined, spec)
    assert rows[0]["why"]["value"] == "Rent was lower because of the pandemic."
    assert rows[0]["why"]["page_numbers"] == [1] and rows[0]["why"]["citation"].startswith("[Page 1]")
    assert rows[1]["gl"]["citation"] == "[Page 2] 6102" and "source_locations" not in rows[1]["gl"]


def test_cell_reference_helpers():
    assert col_letter(1) == "A" and col_letter(28) == "AB" and col_index("AB") == 28
    assert split_ref("Willow Creek!F12") == ("Willow Creek", 12, 6) and split_ref("B3") == (None, 3, 2)


def test_ambiguous_value_mode_is_flagged():
    spec = heuristic_spec(RENT_SCHEMA)
    grid = dict(RENT_GRID)
    grid[5] = {"C": "Rent", "D": 40}                      # a second, different Rent line in the first record
    plan = {**RENT_PLAN, "block_fields": {**RENT_PLAN["block_fields"],
                                          "rent": {"mode": "value", "column": "D", "where": {"column": "C", "equals": "Rent"}}}}
    diag: dict = {}
    rows = T.finalise(T.execute(plan, sheet(grid), spec, diag), plan, spec)
    assert any("several matching lines" in e for e in T.check(rows, spec, plan, diag))


def test_cross_field_check_catches_a_misread_total():
    spec = heuristic_spec(RENT_SCHEMA)
    wrong_total = {**RENT_PLAN, "columns": {"unit": "A", "total": "D"},       # start-row amount, not the total line
                   "block_fields": {k: v for k, v in RENT_PLAN["block_fields"].items() if k != "total"},
                   "checks": [{"expr": "add(rent, other)", "equals": "total"}]}
    rows = T.finalise(T.execute(wrong_total, sheet(RENT_GRID), spec), wrong_total, spec)
    assert any("check add(rent, other) == total" in e for e in T.check(rows, spec, wrong_total))
    good = {**RENT_PLAN, "checks": [{"expr": "add(rent, other)", "equals": "total"}]}
    rows = T.finalise(T.execute(good, sheet(RENT_GRID), spec), good, spec)
    assert not any(e.startswith("check") for e in T.check(rows, spec, good))


def test_grid_from_transcribed_markdown_pages():
    from app.ingest.grid import grid_from_markdown_pages
    head = "| Code | Name | Amount |\n|---|---|---|\n"
    pages = ["# Lakeview Tower\nBudget 2021\n" + head + "| 6101 | Base Rent | 7,481,754 |\n| 6102 | Abatement | -487,261 |",
             head + "| 6105 | Parking | 216,185 |",
             head + "| 6108 | Utilities | 27,000 |"]
    g = grid_from_markdown_pages(pages, "budget_scan.pdf")
    s = g.sheets[0]
    rows = [[v for _, v in sorted(s.row(r).items())] for r in s.nonempty_rows()]
    assert rows[0] == ["Lakeview Tower"] and rows[1] == ["Budget 2021"]
    assert sum(1 for r in rows if r[0] == "Code") == 1                 # repeated column header kept once
    assert ["6105", "Parking", "216,185"] in rows
    r6105 = next(r for r in s.nonempty_rows() if s.get(r, 1) == "6105")
    assert s.row_pages[r6105] == 2


def test_grid_from_docx_tables_and_paragraphs(tmp_path):
    import docx
    from app.ingest.grid import grid_from_docx
    d = docx.Document()
    d.add_paragraph("Sunny Court Rent Roll")
    t = d.add_table(rows=3, cols=3)
    for r, vals in enumerate([("Unit", "Rent", "Status"), ("101", "1,400", "Occupied"), ("102", "1,550", "Vacant")]):
        for c, v in enumerate(vals):
            t.cell(r, c).text = v
    p = tmp_path / "rr.docx"
    d.save(p)
    g = grid_from_docx(p)
    s = g.sheets[0]
    rows = [[v for _, v in sorted(s.row(r).items())] for r in s.nonempty_rows()]
    assert rows == [["Sunny Court Rent Roll"], ["Unit", "Rent", "Status"], ["101", "1,400", "Occupied"],
                    ["102", "1,550", "Vacant"]]
    spec = heuristic_spec({"name": "rr", "fields": {"unit": {"name": "Unit", "description": "Unit", "primary": True},
                                                    "rent": {"name": "Rent", "description": "Rent as a number"}}})
    plan = {"sheet": s.name, "record_start": {"column": "A", "pattern": r"\d{3}"}, "columns": {"unit": "A", "rent": "B"},
            "checks": []}
    out = T.finalise(T.execute(plan, g, spec), plan, spec)
    assert [(r["unit"]["value"], r["rent"]["value"]) for r in out] == [("101", 1400), ("102", 1550)]
