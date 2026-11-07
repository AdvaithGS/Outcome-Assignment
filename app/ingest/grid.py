"""Grid model for tabular documents: XLSX, CSV, and (later) tables rebuilt from PDFs.

A Grid is a set of named sheets; each sheet maps (row, col) → value, 1-based like spreadsheets, so every value has
an exact address ("Sheet!F12") for citations and `source_locations`. Merged cells are filled with the merged
value. Formulas are read as their cached values.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


def col_letter(c: int) -> str:
    s = ""
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.strip().upper():
        if not ch.isalpha():
            raise ValueError(f"bad column {letters!r}")
        n = n * 26 + (ord(ch) - 64)
    return n


def split_ref(ref: str) -> tuple[str | None, int, int]:
    """'Sheet!B12' or 'B12' → (sheet, row, col)."""
    sheet, _, cell = ref.rpartition("!")
    m = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", cell.strip())
    if not m:
        raise ValueError(f"bad cell reference {ref!r}")
    return (sheet.strip("'") or None), int(m.group(2)), col_index(m.group(1))


def _clean(v):
    if isinstance(v, str):
        v = v.replace(" ", " ")
        return v if v.strip() else None
    if isinstance(v, datetime):
        return v.date() if (v.hour, v.minute, v.second) == (0, 0, 0) else v
    return v


@dataclass
class Sheet:
    name: str
    cells: dict[tuple[int, int], object] = field(default_factory=dict)
    max_row: int = 0
    max_col: int = 0

    def get(self, r: int, c: int):
        return self.cells.get((r, c))

    _rows: dict[int, dict[int, object]] | None = field(default=None, repr=False)
    row_pages: dict[int, int] | None = None     # for grids rebuilt from PDFs: row → page

    def _index(self) -> dict[int, dict[int, object]]:
        if self._rows is None:
            rows: dict[int, dict[int, object]] = {}
            for (r, c), v in self.cells.items():
                rows.setdefault(r, {})[c] = v
            self._rows = rows
        return self._rows

    def row(self, r: int) -> dict[int, object]:
        return self._index().get(r, {})

    def nonempty_rows(self) -> list[int]:
        return sorted(self._index())

    def ref(self, r: int, c: int) -> str:
        return f"{self.name}!{col_letter(c)}{r}"


@dataclass
class Grid:
    source: str
    kind: str                      # xlsx | csv | pdf
    sheets: list[Sheet]

    def sheet(self, name: str | None) -> Sheet:
        if name is None:
            return self.sheets[0]
        for s in self.sheets:
            if s.name == name:
                return s
        for s in self.sheets:      # tolerate case / whitespace differences in a plan
            if s.name.strip().lower() == str(name).strip().lower():
                return s
        raise KeyError(f"no sheet {name!r}; sheets: {[s.name for s in self.sheets]}")


def _index_rows(sheet: Sheet) -> Sheet:
    sheet._rows = None
    if sheet.cells:
        sheet.max_row = max(r for r, _ in sheet.cells)
        sheet.max_col = max(c for _, c in sheet.cells)
    return sheet


def load_xlsx(path: str | Path) -> Grid:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    sheets = []
    for ws in wb.worksheets:
        s = Sheet(ws.title)
        for row in ws.iter_rows():
            for c in row:
                v = _clean(c.value)
                if v is not None:
                    s.cells[(c.row, c.column)] = v
        for rng in ws.merged_cells.ranges:
            v = s.cells.get((rng.min_row, rng.min_col))
            if v is None:
                continue
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    s.cells.setdefault((r, c), v)
        sheets.append(_index_rows(s))
    return Grid(source=Path(path).name, kind="xlsx", sheets=sheets)


def load_csv(path: str | Path) -> Grid:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    s = Sheet(Path(path).stem)
    for r, row in enumerate(csv.reader(io.StringIO(text), dialect), start=1):
        for c, v in enumerate(row, start=1):
            v = _clean(v.strip() if isinstance(v, str) else v)
            if v is not None:
                s.cells[(r, c)] = v
    return Grid(source=Path(path).name, kind="csv", sheets=[_index_rows(s)])


def fmt(v, limit: int = 60) -> str:
    if isinstance(v, (date, datetime)):
        s = v.isoformat()
    elif isinstance(v, float) and v.is_integer():
        s = str(int(v))
    else:
        s = str(v)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def preview(grid: Grid, *, head: int = 45, tail: int = 20, samples: int = 15, max_cells: int = 24,
            full_sheets: int = 3, brief_rows: int = 6) -> str:
    """Fixed-size view: first and last non-empty rows plus evenly spaced samples in between, each cell with its
    address; long cells truncated. The first `full_sheets` sheets are shown this way, further sheets only by name,
    size and first rows, so the preview does not grow with the number of sheets either."""
    out = []
    for n, s in enumerate(grid.sheets):
        if n >= full_sheets:
            rows = s.nonempty_rows()
            out.append(f"### Sheet {s.name!r}: rows 1–{s.max_row}, columns A–{col_letter(max(1, s.max_col))}, "
                       f"{len(rows)} non-empty rows (first rows only)")
            for r in rows[:brief_rows]:
                out.append(f"  row {r}: " + " | ".join(f"{col_letter(c)}={fmt(v)}" for c, v in sorted(s.row(r).items())[:max_cells]))
            continue
        rows = s.nonempty_rows()
        out.append(f"### Sheet {s.name!r}: rows 1–{s.max_row}, columns A–{col_letter(max(1, s.max_col))}, "
                   f"{len(rows)} non-empty rows")
        if len(rows) <= head + tail + samples:
            shown = rows
        else:
            middle = rows[head:-tail]
            step = max(1, len(middle) // samples)
            shown = sorted(set(rows[:head]) | set(rows[-tail:]) | set(middle[::step][:samples]))
        prev = None
        for r in shown:
            if prev is not None and r != prev + 1:
                out.append("  …")
            cells = sorted(s.row(r).items())[:max_cells]
            out.append(f"  row {r}: " + " | ".join(f"{col_letter(c)}={fmt(v)}" for c, v in cells))
            prev = r
    return "\n".join(out)


def load_grid(path: str | Path, file_type: str) -> Grid:
    return load_csv(path) if file_type == "csv" else load_xlsx(path)


def grid_document(grid: Grid):
    """A spreadsheet as an anchored text Document (for single-entry field lists over spreadsheets)."""
    from app.ingest.document import MarkdownBuilder
    b = MarkdownBuilder()
    for s in grid.sheets:
        b.heading(f"Sheet {s.name}", force=True)
        b.text("\n".join(f"[{s.name}!row {r}] " + " | ".join(f"{col_letter(c)}{r}={fmt(v)}" for c, v in sorted(s.row(r).items()))
                          for r in s.nonempty_rows()))
    doc = b.build(source=grid.source, kind=grid.kind, variant="grid-text")
    doc.meta["grid"] = grid          # lets pointer mode read exact cells
    return doc


_NUMERIC = re.compile(r"^\(?-?\$?[\d,]+(\.\d+)?%?\)?$")


def grid_from_pdf(path: str | Path, min_table_lines: int = 5):
    """Rebuild a table grid from a PDF's text layer.

    Words → lines (by vertical position) → fragments (words closer than ~1.5 character widths) → columns: the
    x-ranges that are never empty on number-bearing lines become column gutters, and every fragment is placed in the
    column under its centre. Rows are numbered continuously across pages; each row remembers its page, so citations
    and page_numbers point at the page. Returns None when the PDF has no text layer or no table-like lines."""
    import pdfplumber
    lines = []   # (page, [(x0, x1, text)])
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=1.5, y_tolerance=3, keep_blank_chars=False)
            words.sort(key=lambda w: (round(w["top"]), w["x0"]))
            cur, cur_top = [], None
            for w in words:
                if cur_top is not None and abs(w["top"] - cur_top) > 3:
                    lines.append((pno, cur))
                    cur = []
                cur_top = w["top"] if not cur else cur_top
                cur.append(w)
            if cur:
                lines.append((pno, cur))
    rows = []
    for pno, ws in lines:
        ws.sort(key=lambda w: w["x0"])
        frags = []
        for w in ws:
            cw = (w["x1"] - w["x0"]) / max(1, len(w["text"]))
            if frags and w["x0"] - frags[-1][1] < 1.5 * cw and not (_NUMERIC.match(w["text"]) or _NUMERIC.match(frags[-1][2])):
                frags[-1] = (frags[-1][0], w["x1"], frags[-1][2] + " " + w["text"])
            else:
                frags.append((w["x0"], w["x1"], w["text"]))
        rows.append((pno, frags))
    table = [f for _, f in rows if sum(1 for _, _, t in f if _NUMERIC.match(t)) >= 3]
    if len(table) < min_table_lines:
        return None
    width = int(max(x1 for f in table for _, x1, _ in f)) + 2
    occ = [0] * width
    for f in table:
        for x0, x1, _ in f:
            for x in range(int(x0), min(width, int(x1) + 1)):
                occ[x] += 1
    spans, start = [], None
    for x in range(width):
        if occ[x] and start is None:
            start = x
        elif not occ[x] and start is not None:
            spans.append((start, x - 1))
            start = None
    if start is not None:
        spans.append((start, width - 1))

    def col_of(x0, x1):
        mid = (x0 + x1) / 2
        for i, (a, b) in enumerate(spans):
            if a - 1 <= mid <= b + 1:
                return i + 1
        if mid > spans[-1][1]:
            return len(spans) + 1          # text right of the table (e.g. commentary) gets its own column
        return min(range(len(spans)), key=lambda i: min(abs(mid - spans[i][0]), abs(mid - spans[i][1]))) + 1

    # page headers/footers: a line whose text (digits ignored, e.g. "Page: 3 of 8") recurs on 3+ pages is kept once
    def sig(frags):
        return re.sub(r"\d+", "#", " ".join(t for _, _, t in frags)).strip().lower()
    pages_of: dict[str, set[int]] = {}
    for pno, frags in rows:
        pages_of.setdefault(sig(frags), set()).add(pno)
    seen_sig: set[str] = set()
    kept = []
    for pno, frags in rows:
        k = sig(frags)
        if len(pages_of[k]) >= 3:
            if k in seen_sig:
                continue
            seen_sig.add(k)
        kept.append((pno, frags))
    rows = kept

    sheet = Sheet(Path(path).stem)
    sheet.row_pages = {}
    for r, (pno, frags) in enumerate(rows, start=1):
        sheet.row_pages[r] = pno
        for x0, x1, text in frags:
            c = col_of(x0, x1)
            prev = sheet.cells.get((r, c))
            sheet.cells[(r, c)] = f"{prev} {text}" if prev is not None else text
    return Grid(source=Path(path).name, kind="pdf", sheets=[_index_rows(sheet)])


def _drop_repeated(rows: list[tuple[int, list[str]]], min_pages: int = 3) -> list[tuple[int, list[str]]]:
    """Page headers/footers/repeated column headers: a row whose text (digits ignored) recurs on min_pages+ pages is
    kept only once."""
    def sig(cells):
        return re.sub(r"\d+", "#", " ".join(cells)).strip().lower()
    pages_of: dict[str, set[int]] = {}
    for pno, cells in rows:
        pages_of.setdefault(sig(cells), set()).add(pno)
    seen, kept = set(), []
    for pno, cells in rows:
        k = sig(cells)
        if len(pages_of[k]) >= min_pages:
            if k in seen:
                continue
            seen.add(k)
        kept.append((pno, cells))
    return kept


def _sheet_from_rows(name: str, rows: list[tuple[int, list[str]]], kind: str, source: str) -> "Grid":
    sheet = Sheet(name)
    sheet.row_pages = {}
    for r, (pno, cells) in enumerate(rows, start=1):
        if pno:
            sheet.row_pages[r] = pno
        for c, v in enumerate(cells, start=1):
            v = _clean(v.strip() if isinstance(v, str) else v)
            if v is not None:
                sheet.cells[(r, c)] = v
    if not sheet.row_pages:
        sheet.row_pages = None
    return Grid(source=source, kind=kind, sheets=[_index_rows(sheet)])


_MD_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def grid_from_markdown_pages(pages: list[str], source: str, min_table_rows: int = 3) -> "Grid | None":
    """Grid from per-page Markdown (vision transcriptions of scanned pages): every Markdown table row becomes a grid
    row (cells in order), every other non-empty line a one-cell row (titles, dates, notes stay visible to the plan).
    Rows keep their page; repeated page headers are dropped."""
    rows: list[tuple[int, list[str]]] = []
    n_table = 0
    for pno, text in enumerate(pages, start=1):
        for line in text.splitlines():
            t = line.strip()
            if not t or _MD_SEP.match(t):
                continue
            if t.startswith("|") and t.count("|") >= 2:
                cells = [re.sub(r"(\*\*|__)(.*?)\1", r"\2", c).strip() for c in t.strip("|").split("|")]
                rows.append((pno, cells))
                n_table += 1
            else:
                rows.append((pno, [re.sub(r"^#+\s*|\*\*", "", t).strip()]))
    if n_table < min_table_rows:
        return None
    return _sheet_from_rows(Path(source).stem, _drop_repeated(rows), "pdf", Path(source).name)


def grid_from_docx(path: str | Path) -> "Grid | None":
    """Grid from a DOCX: each Word table row becomes a grid row (merged cells padded), each paragraph a one-cell row,
    in document order. Returns None when the document has no table."""
    import zipfile
    from xml.etree import ElementTree as ET
    from app.ingest.docx import _DocxParser, _w
    rows: list[tuple[int, list[str]]] = []
    n_table = 0
    with zipfile.ZipFile(path) as z:
        parser = _DocxParser(z, ocr_engine=None, min_image_bytes=10 ** 12, cache=None)
        body = ET.fromstring(z.read("word/document.xml")).find(_w("body"))

        def walk(container):
            nonlocal n_table
            for el in container:
                if el.tag == _w("p"):
                    text = parser.para(el)[0]
                    if text:
                        rows.append((0, [text]))
                elif el.tag == _w("tbl"):
                    for tr in el.findall(_w("tr")):
                        cells = []
                        for tc in tr.findall(_w("tc")):
                            pr = tc.find(_w("tcPr"))
                            span = pr.find(_w("gridSpan")) if pr is not None else None
                            span = int(span.get(f"{{{'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}}}val") or 1) if span is not None else 1
                            cells.append(parser.cell_text(tc))
                            cells.extend([""] * (span - 1))
                        rows.append((0, cells))
                        n_table += 1
                elif el.tag == _w("sdt"):
                    content = el.find(_w("sdtContent"))
                    if content is not None:
                        walk(content)
        walk(body)
    if n_table == 0:
        return None
    return _sheet_from_rows(Path(path).stem, rows, "docx", Path(path).name)
