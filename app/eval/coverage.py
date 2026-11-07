"""LLM-free metrics for an ingested Document against an answer file.

coverage()        Does each non-null answer value appear in the extracted text, in any common
                  formatting (4,250.00 / 4250, January 1, 1994 / 1st day of January, 1994 / 1/1/94 ...)?
                  And does it appear on a page the answer cites? This is an upper bound on what any
                  extractor can get right from this representation.
anchor_quality()  Can each answer citation be located in the text? Which section/chunk holds it,
                  does its page agree with the answer's page, and how large are the slices the
                  Collator would hand to the extractor?
"""
from __future__ import annotations

import re
import statistics
from datetime import date

from app.ingest.document import Document

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
WORD_ORD = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
            "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
            "eighteenth", "nineteenth", "twentieth", "twenty-first", "twenty-second", "twenty-third",
            "twenty-fourth", "twenty-fifth", "twenty-sixth", "twenty-seventh", "twenty-eighth",
            "twenty-ninth", "thirtieth", "thirty-first"]
_COMPUTED_HINT = re.compile(r"\b(compute|computed|sum of|divided|times|count of|rounded|from commencement to)\b", re.I)
_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        " ": " "})


def normalise_with_map(text: str) -> tuple[str, list[int]]:
    """Lowercase, unify quotes/dashes, turn Markdown punctuation (| * # _ `) into spaces, collapse
    whitespace. Returns the normalised text and, per normalised char, its offset in the original."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text.translate(_TRANS)):
        c = ch.lower()
        if c in "|*#_`" or c.isspace():
            if prev_space:
                continue
            c, prev_space = " ", True
        else:
            prev_space = False
        out.append(c)
        idx.append(i)
    return "".join(out), idx


def normalise(text: str) -> str:
    return normalise_with_map(text)[0].strip()


def _num_pattern(x: float) -> re.Pattern:
    x = abs(float(x))
    forms = set()
    if x.is_integer():
        n = int(x)
        forms |= {str(n), f"{n:,}", f"{n}.00", f"{n:,}.00"}
    else:
        forms |= {f"{x:.2f}", f"{x:,.2f}", format(x, "g"), f"{x:,}"}
        if round(x, 1) == x:
            forms.add(f"{x:.1f}")
    alts = "|".join(sorted((re.escape(f) for f in forms), key=len, reverse=True))
    return re.compile(rf"(?<![\d.,])(?:{alts})(?!\d|[.,]\d)")


def _date_pattern(d: date) -> re.Pattern:
    m, ma, day, y, yy = MONTHS[d.month - 1], MONTHS[d.month - 1][:3], d.day, d.year, f"{d.year % 100:02d}"
    ordn = rf"(?:{day}(?:st|nd|rd|th)?|{WORD_ORD[day - 1]})"
    yr = rf"(?:{y}|'?{yy})"
    alts = [
        rf"{m}\s+{ordn},?\s+{y}", rf"{ma}\.?\s+{ordn},?\s+{y}",
        rf"(?:the\s+)?{ordn}\s+(?:day\s+of\s+)?{m},?\s+{y}", rf"{ordn}\s+(?:day\s+of\s+)?{ma}\.?,?\s+{y}",
        rf"0?{d.month}\s?/\s?0?{day}\s?/\s?{yr}", rf"0?{d.month}-0?{day}-{yr}", rf"{y}-{d.month:02d}-{day:02d}",
        rf"0?{d.month}\.0?{day}\.{yr}",
    ]
    return re.compile(rf"(?<!\d)(?:{'|'.join(alts)})(?!\d)", re.I)


def _month_pattern(y: int, mo: int) -> re.Pattern:
    m = MONTHS[mo - 1]
    return re.compile(rf"(?:{m}|{m[:3]}\.?)\s*,?\s*{y}|(?<!\d)0?{mo}/{y}|{y}-{mo:02d}(?!-\d)", re.I)


def classify(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return "date"
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return "month"
        return "string"
    return "other"


def _find(value, kind: str, norm: str) -> tuple[str, list[int]]:
    """-> (status, offsets in the normalised text). status: found | fuzzy | missing | skipped"""
    if kind == "number":
        offs = [m.start() for m in _num_pattern(value).finditer(norm)]
    elif kind == "date":
        offs = [m.start() for m in _date_pattern(date.fromisoformat(value)).finditer(norm)]
    elif kind == "month":
        y, mo = map(int, value.split("-"))
        offs = [m.start() for m in _month_pattern(y, mo).finditer(norm)]
    elif kind == "string":
        v = normalise(value)
        if not v:
            return "skipped", []
        offs = [m.start() for m in re.finditer(re.escape(v), norm)]
        if not offs:
            toks = [t for t in re.findall(r"[a-z0-9]+", v) if len(t) > 1]
            have = set(re.findall(r"[a-z0-9]+", norm))
            if len(toks) >= 2 and sum(t in have for t in toks) / len(toks) >= 0.8:
                return "fuzzy", []
    else:
        return "skipped", []
    return ("found" if offs else "missing"), offs


def coverage(doc: Document, truth: dict, schema: dict) -> dict:
    norm, idx = normalise_with_map(doc.markdown)
    fields = schema["fields"]
    seen: set = set()
    items = []
    for entry in truth["entries"]:
        for key, fr in entry.items():
            v = fr.get("value") if isinstance(fr, dict) else fr
            pages = (fr.get("page_numbers") or []) if isinstance(fr, dict) else []
            for item in (v if isinstance(v, list) else [v]):
                if item is None:
                    continue
                dedupe = (key, repr(item))
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                kind = classify(item)
                status, offs = _find(item, kind, norm)
                on_page = None
                if offs and pages and doc._page_offsets:
                    on_page = any(doc.page_at(idx[o]) in pages for o in offs)
                computed = bool(_COMPUTED_HINT.search(fields.get(key, {}).get("description", "")))
                items.append({"field": key, "value": item, "kind": kind, "status": status,
                              "on_cited_page": on_page, "computed_hint": computed})
    scored = [i for i in items if i["status"] != "skipped"]
    direct = [i for i in scored if not i["computed_hint"]]

    def rate(xs, pred):
        return round(sum(1 for x in xs if pred(x)) / len(xs), 4) if xs else None

    paged = [i for i in direct if i["on_cited_page"] is not None or (i["status"] == "found" and doc._page_offsets)]
    return {
        "items": len(scored), "direct_items": len(direct),
        "found": rate(scored, lambda i: i["status"] == "found"),
        "found_or_fuzzy": rate(scored, lambda i: i["status"] in ("found", "fuzzy")),
        "direct_found": rate(direct, lambda i: i["status"] == "found"),
        "direct_found_or_fuzzy": rate(direct, lambda i: i["status"] in ("found", "fuzzy")),
        "on_cited_page": rate(paged, lambda i: bool(i["on_cited_page"])),
        "missing": [f"{i['field']}={i['value']!r}" for i in direct if i["status"] == "missing"],
        "detail": items,
    }


def _locate(snippet: str, norm: str) -> int | None:
    s = normalise(re.sub(r"^\s*\[[^\]]*\]\s*", "", snippet))
    if len(s) < 8:
        return None
    for probe in (s, s[:60], s[len(s) // 2 - 30: len(s) // 2 + 30] if len(s) > 60 else s, s[:30], s[-30:]):
        if len(probe) >= 8:
            i = norm.find(probe)
            if i >= 0:
                return i
    return None


def leaf_slices(doc: Document) -> list[tuple[str, int]]:
    """(anchor id, approx tokens) of each section/chunk slice the Collator could hand over."""
    marks = sorted((a for a in doc.anchors if a.kind in ("section", "chunk")), key=lambda a: a.start)
    out = []
    for i, a in enumerate(marks):
        end = marks[i + 1].start if i + 1 < len(marks) else len(doc.markdown)
        out.append((a.id, (end - a.start) // 4))
    return out


def anchor_quality(doc: Document, truth: dict) -> dict:
    norm, idx = normalise_with_map(doc.markdown)
    cites = {}
    for entry in truth["entries"]:
        for fr in entry.values():
            if isinstance(fr, dict) and fr.get("citation") and fr.get("value") is not None:
                cites.setdefault(fr["citation"], fr.get("page_numbers") or [])
    located = page_ok = page_checked = 0
    holders: set[str] = set()
    for cite, pages in cites.items():
        pos = _locate(cite, norm)
        if pos is None:
            continue
        located += 1
        raw = idx[pos]
        a = doc.anchor_at(raw)
        holders.add(a.id if a else "-")
        if pages and doc._page_offsets:
            page_checked += 1
            page_ok += doc.page_at(raw) in pages
    sizes = [t for _, t in leaf_slices(doc)]
    return {
        "citations": len(cites), "located": round(located / len(cites), 4) if cites else None,
        "page_agree": round(page_ok / page_checked, 4) if page_checked else None,
        "anchors_holding_citations": len(holders),
        "slices": len(sizes),
        "slice_tokens_median": int(statistics.median(sizes)) if sizes else 0,
        "slice_tokens_p90": int(sorted(sizes)[int(0.9 * (len(sizes) - 1))]) if sizes else 0,
        "slice_tokens_max": max(sizes) if sizes else 0,
    }
