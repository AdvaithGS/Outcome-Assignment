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


def postprocess(spec: SchemaSpec, raw: dict | None, doc: Document, mode: str = "H") -> dict[str, dict]:
    raw = raw or {}
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
        if value is None and f.absent is not None:
            value, absent_default = (list(f.absent) if isinstance(f.absent, list) else f.absent), True
        if value is None or absent_default:
            out[key] = {"value": value, "citation": "", "confidence": 0.7 if absent_default else 0.6}
            continue
        grounded, pages = _ground(cites[key], doc, doc_norm)
        conf = (0.9 if grounded else 0.65) + conf_bonus
        fr = {"value": value, "citation": cites[key], "confidence": round(min(1.0, max(0.05, conf)), 2)}
        if pages:
            fr["page_numbers"] = pages
        out[key] = fr
    return out
