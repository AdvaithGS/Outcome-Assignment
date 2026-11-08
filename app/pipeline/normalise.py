"""Turn raw extracted values into the contract's formats, by inferred field type.

    number / integer / percent / year → JSON number ($, thousands separators, "(1,234)" negatives, "%" handled;
                                         percentages stay as printed: "5.25%" → 5.25)
    date  → "YYYY-MM-DD" (ISO, US numeric, "March 5, 1993", "5th day of March, 1993", "March 5, 19 93", ...)
    month → "YYYY-MM"
    bool  → true / false
    list  → list of normalised items
    text / freetext → trimmed string
A value that does not parse as its type becomes None: a wrong-typed value is worse than a null.
"""
from __future__ import annotations

import re
from datetime import date, datetime

MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"])}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
WORD_ORD = {w: i + 1 for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth "
    "fifteenth sixteenth seventeenth eighteenth nineteenth twentieth twenty-first twenty-second twenty-third "
    "twenty-fourth twenty-fifth twenty-sixth twenty-seventh twenty-eighth twenty-ninth thirtieth thirty-first".split())}
NULL_STRINGS = {"", "null", "none", "n/a", "na", "-", "--", "not stated", "not found", "unknown"}


_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")
_MULT_WORD = re.compile(r"^\s*(thousand|million|mil|mm|billion|bn)\b", re.I)
_MULT_LETTER = re.compile(r"^(k|m|b)\b", re.I)        # "$12.5M": only directly after a currency amount
_MULTS = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mil": 1e6, "mm": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}


def to_number(v):
    """One number from a value: "$4,250.00 per month" → 4250.0, "$1.2 million" → 1200000, "(1,234)" → -1234.
    A string holding several figures ("1,200-1,500", "2019-2020", "Year 1: $25") is not one number → None,
    never the digits run together."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() and not isinstance(v, float) else v
    if not isinstance(v, str):
        return None
    s = v.strip()
    if s.lower() in NULL_STRINGS:
        return None
    toks = list(_NUM_TOKEN.finditer(s))
    if len(toks) != 1:
        return None
    tok = toks[0]
    digits = tok.group(0).replace(",", "")
    if not re.fullmatch(r"\d*\.?\d+", digits) or len(s.split()) > 8:     # long prose is not a value
        return None
    before, after = s[:tok.start()], s[tok.end():]
    neg = bool(re.fullmatch(r"\s*[-−–]\s*[$€£₹]?\s*|\s*[$€£₹]\s*[-−–]\s*", before)) or (
        "(" in before and ")" in after and before.rstrip().endswith(("(", "($", "(€", "(£", "(₹")))
    mult = 1.0
    m = _MULT_WORD.match(after)
    if m:
        mult = _MULTS[m.group(1).lower()]
    elif re.search(r"[$€£₹]\s*$", before):
        m = _MULT_LETTER.match(after)
        if m:
            mult = _MULTS[m.group(1).lower()]
    x = float(digits) * mult
    x = -x if neg else x
    return int(x) if x.is_integer() and ("." not in digits or mult != 1.0) else x


def to_date(v) -> str | None:
    if isinstance(v, datetime):
        return v.date().isoformat()                                 # a spreadsheet date with a time of day
    if isinstance(v, date):
        return v.isoformat()
    if not isinstance(v, str):
        return None
    s = " ".join(v.strip().lower().replace(",", " , ").split())
    s = re.sub(r"\b(19|20) (\d{2})\b", r"\1\2", s)                 # "19 93" from filled-in forms
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", s)   # US numeric; day first only when it must be
    if m:
        y = int(m.group(3))
        y += (1900 if y >= 50 else 2000) if y < 100 else 0
        a, b = int(m.group(1)), int(m.group(2))
        return _mk(y, b, a) if a > 12 >= b else _mk(y, a, b)
    mon = r"(january|february|march|april|may|june|july|august|september|sept|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.?"
    m = re.search(rf"\b{mon} (\d{{1,2}})(?:st|nd|rd|th)? ?,? ?(\d{{4}})\b", s)
    if m:
        return _mk(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:day )?(?:of )?{mon} ?,? ?(\d{{4}})\b", s)
    if m:
        return _mk(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    ords = "|".join(sorted(WORD_ORD, key=len, reverse=True))
    m = re.search(rf"\b({ords}) (?:day )?(?:of )?{mon} ?,? ?(\d{{4}})\b", s)
    if m:
        return _mk(int(m.group(3)), MONTHS[m.group(2)], WORD_ORD[m.group(1)])
    return None


def _mk(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def to_month(v) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    m = re.search(r"\b(\d{4})-(\d{1,2})\b", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{4})\b", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    m = re.search(r"\b([a-z]+)\.? ?,? ?(\d{4})\b", s)
    if m and m.group(1) in MONTHS:
        return f"{int(m.group(2)):04d}-{MONTHS[m.group(1)]:02d}"
    d = to_date(v)
    return d[:7] if d else None


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "y"):
            return True
        if s in ("false", "no", "n", "none"):
            return False
    return None


def normalise(value, ftype: str, item_type: str | None = None):
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in NULL_STRINGS and ftype not in ("bool",):
        return None
    if ftype == "list":
        items = value if isinstance(value, list) else [value]
        out = [normalise(x, item_type or "text") for x in items]
        return [x for x in out if x is not None]
    if isinstance(value, list):                      # a scalar field given a list: take a single item only
        value = value[0] if len(value) == 1 else None
        if value is None:
            return None
    if ftype in ("number", "percent"):
        return to_number(value)
    if ftype in ("integer", "year"):
        x = to_number(value)
        return int(round(x)) if isinstance(x, (int, float)) else None
    if ftype == "date":
        return to_date(value)
    if ftype == "month":
        return to_month(value)
    if ftype == "bool":
        return to_bool(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)                           # 1205.0 in a text field reads "1205"
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    return s or None
