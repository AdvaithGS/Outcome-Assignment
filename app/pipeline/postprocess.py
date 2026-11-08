"""Stage 4: deterministic post-processing of a raw extraction into contract field results.

Computed fields, three strategies from the same raw response (E2a compares them at $0):
    M  the model's own answer ("value"; the printed figure if the document prints one)
    C  code only: evaluate the stage-0 formula over the extracted operands; null if it cannot
    H  hybrid (default): printed figure if the document prints one and the field prefers it, else the formula,
       else the model's answer; agreement between them raises confidence, disagreement lowers it
Then: absent defaults, citation grounding (verbatim check against the document), page_numbers from [Page N].
"""
from __future__ import annotations

import re

from app.eval.coverage import normalise as norm_text
from app.ingest.document import Document
from app.pipeline import formula as F
from app.pipeline.normalise import normalise, to_date, to_number
from app.pipeline.schema_analysis import FieldSpec, SchemaSpec

_TAG = re.compile(r"^\s*\[([^\]]+)\]\s*")
_PAGE = re.compile(r"\bpages?\s+(\d+)(?:\s*[-–,]\s*(\d+))?", re.I)


def _same(a, b) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.01, abs(b) * 0.005)
    return str(a).strip().lower() == str(b).strip().lower()


def _helper_value(v):
    if isinstance(v, list):
        return [_helper_value(x) for x in v]
    n = to_number(v) if not isinstance(v, (int, float)) else v
    if n is not None:
        return n
    d = to_date(v) if isinstance(v, str) else None
    return d if d else v


def _page_of_value(value, doc: Document) -> list[int]:
    """Locate the value itself (any common formatting) to find its page, when the citation does not give one."""
    from app.eval.coverage import _find, classify, normalise_with_map
    if not doc._page_offsets or value is None or isinstance(value, (bool, list)):
        return []
    norm, idx = normalise_with_map(doc.markdown)
    status, offs = _find(value, classify(value), norm)
    if status != "found" or not offs:
        return []
    pages = sorted({doc.page_at(idx[o]) for o in offs if doc.page_at(idx[o])})
    return pages if len(pages) <= 2 else []                 # a value found on many pages says nothing


def _value_found(value, doc: Document, memo: dict) -> bool | None:
    """Is the value itself printed in the document (any common formatting)? None when that cannot be checked (yes/no
    judgements). Lists: at least half of the items are found."""
    from app.eval.coverage import _find, classify, normalise_with_map
    if value is None or isinstance(value, bool):
        return None
    if "norm" not in memo:
        memo["norm"] = normalise_with_map(doc.markdown)[0]
    items = value if isinstance(value, list) else [value]
    if not items:
        return None
    found = sum(1 for v in items if v is not None and _find(v, classify(v), memo["norm"])[0] == "found")
    return found * 2 >= len(items)


def confidence(f: FieldSpec, grounded: bool, located: bool | None, conf_bonus: float = 0.0,
               p_conf: float | None = None) -> float:
    """Evidence-based confidence. Checked against the sample answers (notes/05-e2.md): values whose citation and value
    were both found were right 100% of the time, those found by neither 60-75%; free text matched exactly 25% of the
    time (paraphrase); yes/no fields are judgements with no span to check.
        free text      0.7 cited / 0.55 not            yes/no     0.8 cited / 0.65 not
        computed       0.85, +0.1 when the printed figure, the formula and the model agree, -0.25 when they differ
        printed value  0.92 citation and value found / 0.75 one of them / 0.5 neither
    A value read from an exact spreadsheet cell (pointer mode) keeps its cell confidence."""
    if f.type == "freetext":
        return 0.7 if grounded else 0.55
    if f.type == "bool":
        return 0.8 if grounded else 0.65
    if f.computed:
        return 0.85 + conf_bonus
    if p_conf is not None:
        return p_conf
    return 0.92 if (grounded and located) else (0.75 if (grounded or located) else 0.5)


NULL_CONFIDENCE = 0.75        # nulls after a real extraction were right 98-100% of the time on the samples
NO_READ_CONFIDENCE = 0.3      # nothing was extracted (every call failed / no readable text): a null by necessity


def _ground(citation: str, doc: Document, doc_norm: str) -> tuple[bool, list[int]]:
    """Is the cited snippet really in the document, and on which pages?"""
    pages: list[int] = []
    m = _TAG.match(citation or "")
    if m:
        for pm in _PAGE.finditer(m.group(1)):
            a, b = int(pm.group(1)), int(pm.group(2) or pm.group(1))
            pages += list(range(min(a, b), max(a, b) + 1))[:10]
    snippet = norm_text(_TAG.sub("", citation or ""))
    if len(snippet) < 4:
        return False, pages
    probes = [snippet[:40], snippet[-40:], snippet[len(snippet) // 2 - 20: len(snippet) // 2 + 20]]
    grounded = any(len(p) >= 4 and p in doc_norm for p in probes)
    if grounded and not pages and doc._page_offsets:
        # find the page of the snippet in the original text
        raw = doc.markdown.lower().find(_TAG.sub("", citation).strip().lower()[:30])
        if raw >= 0:
            p = doc.page_at(raw)
            pages = [p] if p else []
    valid_pages = {p.number for p in doc.pages}
    return grounded, [p for p in pages if p in valid_pages]


_NUM_IN_TEXT = re.compile(r"\(?-?\$?\s?\d[\d,]*(?:\.\d+)?\s?%?\)?")
_CELL_TAG = re.compile(r"^\s*\[([^\]!]+)!\$?([A-Za-z]{1,3})\$?(\d+)\]")


def _clean_span(text: str) -> str:
    text = re.sub(r"\[handwritten:\s*([^\]]*)\]", r"\1", text)
    text = re.sub(r"~~[^~]*~~", "", text)
    return " ".join(text.replace("**", "").replace("|", " ").split()).strip(" ;,")


def locate_quote(quote: str, doc: Document, norm: str, idx: list[int]) -> tuple[int, int, bool] | None:
    """-> (raw start, raw end, exact) of a quote in the document, tolerant of spacing/markup; None if not found."""
    q = norm_text(quote or "")
    if len(q) < 3:
        return None
    i = norm.find(q)
    if i >= 0:
        return idx[i], idx[i + len(q) - 1] + 1, True
    for probe in (q[:60], q[-60:], q[:30], q[-30:]):
        if len(probe) >= 12:
            j = norm.find(probe)
            if j >= 0:
                return idx[j], min(len(doc.markdown), idx[j] + len(quote) + 20), False
    return None


def _pointer(f: FieldSpec, style: str, value, fr: dict, doc: Document, norm: str, idx: list[int]):
    """Resolve a field's pointer: -> (value, citation, pages, confidence or None, source_locations)."""
    quote = str(fr.get("quote") or "") if isinstance(fr, dict) else ""
    cite = str(fr.get("citation") or "") if isinstance(fr, dict) else ""
    grid = doc.meta.get("grid")
    m = _CELL_TAG.match(cite)
    if grid is not None and m and style in ("pointer", "quote"):            # spreadsheet: read the exact cell
        try:
            sheet = grid.sheet(m.group(1))
            from app.ingest.grid import col_index, fmt
            cell = sheet.get(int(m.group(3)), col_index(m.group(2)))
            if cell is not None:
                v = normalise(cell, f.type, f.item_type) if style == "pointer" else fmt(cell, 10_000)
                if v is not None:
                    ref = f"{sheet.name}!{m.group(2).upper()}{m.group(3)}"
                    return v, f"[{ref}] {fmt(cell)}", [], 0.95, [ref]
        except (KeyError, ValueError):
            pass
    span = locate_quote(quote, doc, norm, idx) if quote else None
    pages = []
    if span and doc._page_offsets:
        a, b = doc.page_at(span[0]), doc.page_at(max(span[0], span[1] - 1))
        pages = sorted({p for p in (a, b) if p})
    conf = None
    if style == "quote":
        text = _clean_span(doc.markdown[span[0]:span[1]]) if span and span[2] else _clean_span(quote)
        if text:
            value = text
        conf = 0.9 if span and span[2] else (0.75 if span else 0.6)
    elif style == "pointer" and value is not None and quote:
        if f.type in ("number", "integer", "percent", "year"):
            cands = [x for x in (to_number(t) for t in _NUM_IN_TEXT.findall(quote)) if x is not None]
            agree = any(_same(value, c) for c in cands)
            conf = 0.95 if agree and span else (0.8 if agree else 0.6)
        elif f.type == "date":
            qd = to_date(quote)
            conf = 0.95 if qd == value and span else (0.6 if qd and qd != value else (0.85 if span else 0.65))
        else:
            inside = norm_text(str(value)) in norm_text(quote) or norm_text(str(value)) in norm
            conf = 0.9 if inside and span else (0.75 if inside or span else 0.6)
    elif style == "pointer" and value is None and quote and f.type in ("number", "integer", "percent", "year"):
        cands = [x for x in (to_number(t) for t in _NUM_IN_TEXT.findall(quote)) if x is not None]
        if len(cands) == 1:
            value, conf = normalise(cands[0], f.type), 0.7
    if span and quote and value is not None:
        tag = f"[Page {pages[0]}] " if pages else (_TAG.match(cite).group(0) if _TAG.match(cite) else "")
        cite = f"{tag}{_clean_span(quote)[:200]}"
    return value, cite, pages, conf, []


def postprocess(spec: SchemaSpec, raw: dict | None, doc: Document, mode: str = "H", style: str = "value") -> dict[str, dict]:
    from app.pipeline.single import field_style
    if style in ("pointer", "hybrid"):
        doc_norm_map = __import__("app.eval.coverage", fromlist=["normalise_with_map"]).normalise_with_map(doc.markdown)
    # absent defaults (0 / false / []) are a reading of the document ("it grants no such right"); with no extraction
    # at all (every call failed, or no readable text) nothing was read, so every field stays null
    extracted = bool(raw)
    raw = raw or {}
    memo: dict = {}
    helpers_raw = raw.get("_helpers") if isinstance(raw.get("_helpers"), dict) else {}
    doc_norm = norm_text(doc.markdown)

    def get(key):
        r = raw.get(key)
        if isinstance(r, dict):
            return r.get("value"), str(r.get("citation") or ""), r.get("stated")
        return r, "", None

    # pass 1: plain fields
    values: dict[str, object] = {}
    cites: dict[str, str] = {}
    for key, f in spec.fields.items():
        v, c, _ = get(key)
        values[key] = normalise(v, f.type, f.item_type)
        cites[key] = c
    operands = dict(values)
    for name, hr in helpers_raw.items():
        operands[name] = _helper_value(hr.get("value") if isinstance(hr, dict) else hr)

    out: dict[str, dict] = {}
    for key, f in spec.fields.items():
        value, conf_bonus = values[key], 0.0
        if f.computed:
            _, _, stated_raw = get(key)
            stated = normalise(stated_raw, f.type, f.item_type)
            model_val = values[key]
            try:
                fv = normalise(F.evaluate(f.computed.formula, operands), f.type, f.item_type or "number")
            except F.FormulaError:
                fv = None
            if mode == "M":
                value = model_val
            elif mode == "C":
                value = fv
            else:
                if f.computed.prefer_stated and stated is not None:
                    value = stated
                elif fv is not None:
                    value = fv
                else:
                    value = model_val
                checks = [x for x in (stated, fv, model_val) if x is not None]
                if len(checks) >= 2:
                    conf_bonus = 0.1 if all(_same(checks[0], x) for x in checks[1:]) else -0.25
        absent_default = False
        if value is None and f.absent is not None and extracted:
            value, absent_default = (list(f.absent) if isinstance(f.absent, list) else f.absent), True
        if value is None or absent_default:
            out[key] = {"value": value, "citation": "",
                        "confidence": 0.7 if absent_default else (NULL_CONFIDENCE if extracted else NO_READ_CONFIDENCE)}
            continue
        p_conf, locs = None, []
        if style in ("pointer", "hybrid"):
            fst = field_style(f, style)
            value, cites[key], p_pages, p_conf, locs = _pointer(f, fst, value, raw.get(key) if isinstance(raw.get(key), dict) else {},
                                                               doc, doc_norm_map[0], doc_norm_map[1])
            if value is None:
                out[key] = {"value": None, "citation": "", "confidence": NULL_CONFIDENCE}
                continue
        grounded, pages = _ground(cites[key], doc, doc_norm)
        if style in ("pointer", "hybrid") and p_pages:
            pages = p_pages
        if not pages:
            pages = _page_of_value(value, doc)
        located = bool(pages) or bool(_value_found(value, doc, memo))
        conf = confidence(f, grounded, located, conf_bonus, p_conf)
        fr = {"value": value, "citation": cites[key], "confidence": round(min(1.0, max(0.05, conf)), 2)}
        if pages:
            fr["page_numbers"] = pages
        if locs:
            fr["source_locations"] = locs
        out[key] = fr
    return out
