"""Stage 0: analyse a field list once (per schema hash) into FieldSpecs.

Heuristics run first and always (free, deterministic). One LLM call then confirms types, rewrites descriptions,
groups fields and writes formulas for computed fields. The call is cached by its exact request, so a schema seen
before costs nothing; concurrent jobs with the same new schema share one call (client single-flight). If the call
fails or returns something unusable, the heuristic specs are used with the original descriptions.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field

from app.llm import LLMClient, UsageTracker
from app.pipeline import formula as F

log = logging.getLogger("schema")

TYPES = ("number", "integer", "percent", "date", "month", "year", "bool", "text", "freetext", "list")
ANALYSIS_VERSION = 1


@dataclass
class Computed:
    formula: str
    operands: dict[str, str]          # operand name -> "field" (another key) or a description of a helper quantity
    prefer_stated: bool = True        # the document's own figure wins when it prints one


@dataclass
class FieldSpec:
    key: str
    name: str
    original: str                     # description as given
    description: str                  # rewritten (or original on fallback)
    type: str = "text"
    item_type: str | None = None      # for lists
    absent: object = None             # None | 0 | False | []
    scope: str = "document"           # document | row (tables)
    group: str = "general"
    primary: bool = False
    computed: Computed | None = None


@dataclass
class SchemaSpec:
    name: str
    description: str
    table: bool                       # one entry per row (a field is marked primary)
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    source: str = "heuristic"         # heuristic | llm

    def helpers(self) -> dict[str, str]:
        """Helper quantities the extractor must also return (computed-field operands that are not fields)."""
        out = {}
        for f in self.fields.values():
            if f.computed:
                for name, desc in f.computed.operands.items():
                    if name not in self.fields:
                        out[name] = desc
        return out

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------------- heuristics

def heuristic_field(key: str, f: dict) -> FieldSpec:
    d = f.get("description", "") or ""
    dl = d.lower()
    if re.search(r"\blist of\b|\blist\b.*\bin order\b|aligned item by item|\blist\b", dl) and "list" in dl:
        ftype = "list"
    elif "yyyy-mm-dd" in dl or re.search(r"\bdate\b", (f.get("name") or "").lower()):
        ftype = "date"
    elif "yyyy-mm" in dl:
        ftype = "month"
    elif "four-digit year" in dl or "four-digit number" in dl:
        ftype = "year"
    elif re.search(r"\btrue if\b|\btrue\b.*\bfalse\b", dl):
        ftype = "bool"
    elif re.search(r"percent|percentage|%", dl):
        ftype = "percent"
    elif re.search(r"whole number|number of|count of|\bcount\b", dl):
        ftype = "integer"
    elif re.search(r"as a number|\bnumber\b|numbers only|amount", dl):
        ftype = "number"
    elif re.search(r"short phrase|a sentence|as the lease states it|verbatim|commentary", dl):
        ftype = "freetext"
    else:
        ftype = "text"
    item = None
    if ftype == "list":
        item = ("date" if "date" in dl else "number" if re.search(r"rent|number|months|rsf|amount", dl) else "text")
    absent = None
    if re.search(r"\b0 if (none|the|no)\b|\b0 if\b", dl):
        absent = 0
    elif re.search(r"false otherwise|false if", dl):
        absent = False
    elif re.search(r"empty list if", dl):
        absent = []
    scope = "document" if re.search(r"same for every row", dl) else ("row" if f.get("primary") else "document")
    return FieldSpec(key=key, name=f.get("name") or key, original=d, description=d, type=ftype, item_type=item,
                     absent=absent, scope=scope, primary=f.get("primary") is True)


def heuristic_spec(schema: dict) -> SchemaSpec:
    fields = {k: heuristic_field(k, v) for k, v in schema["fields"].items()}
    table = any(f.primary for f in fields.values())
    if table:
        for f in fields.values():
            if f.scope != "document" or f.primary:
                f.scope = "row"
    return SchemaSpec(name=schema.get("name", ""), description=schema.get("description", ""), table=table, fields=fields)


# ------------------------------------------------------------------------------- LLM analysis

SYSTEM = f"""You prepare a field list for a document-extraction pipeline. You are given the field list exactly as a
client wrote it. For EVERY field key, return a JSON object with:
  "type": one of {list(TYPES)}  ("freetext" = a phrase or sentence summarising terms; "text" = a name, code or short label)
  "item_type": for lists, the type of each item, else null
  "absent": the value to return when the document does not contain it: null, 0, false or [] - only use 0/false/[]
            when the description explicitly says so (e.g. "0 if none", "false otherwise", "empty list if none")
  "scope": "row" if the value differs per table row, "document" if it is the same for the whole document
  "group": a short topic label used to batch related fields (e.g. "parties", "dates", "rent", "financials")
  "description": the description rewritten to be unambiguous for an extraction model: keep every constraint of the
            original, state the exact output format, say what to include and what to exclude, keep the original's
            examples, and resolve ambiguity in the direction the original implies. Do not invent facts about any
            particular document.
  "computed": null, unless the value must be derived arithmetically from other values (sums, ratios, counts,
            differences of dates). Then an object:
            {{"formula": an expression in the formula language below,
              "operands": {{name: "field" if the name is another field key, otherwise a precise description of the
                           helper value to extract from the document}},
              "prefer_stated": true if the description says to use the document's own figure when it states one}}
Formula language (nothing else is allowed):
{F.FUNCTION_DOC}
Return one JSON object: {{"fields": {{<key>: {{...}}}}}}."""


def _coerce_absent(v):
    if v in (None, 0, False) or v == []:
        return v
    if isinstance(v, str):
        return {"null": None, "0": 0, "false": False, "[]": []}.get(v.strip().lower())
    return None


def merge_llm(spec: SchemaSpec, data: dict) -> SchemaSpec:
    got = (data or {}).get("fields") or {}
    for key, f in spec.fields.items():
        g = got.get(key)
        if not isinstance(g, dict):
            continue
        if g.get("type") in TYPES:
            f.type = g["type"]
        f.item_type = g.get("item_type") if f.type == "list" and g.get("item_type") in TYPES else f.item_type
        if "absent" in g:
            f.absent = _coerce_absent(g["absent"])
        if spec.table and g.get("scope") in ("row", "document") and not f.primary:
            f.scope = g["scope"]
        if isinstance(g.get("group"), str) and g["group"].strip():
            f.group = g["group"].strip()[:40]
        if isinstance(g.get("description"), str) and len(g["description"].strip()) >= 10:
            f.description = g["description"].strip()
        c = g.get("computed")
        if isinstance(c, dict) and isinstance(c.get("formula"), str):
            try:
                used = F.names(c["formula"])
                ops = {str(k): str(v) for k, v in (c.get("operands") or {}).items()}
                for n in used:
                    ops.setdefault(n, "field" if n in spec.fields else n.replace("_", " "))
                if key in used:
                    raise F.FormulaError("formula refers to its own field")
                f.computed = Computed(formula=c["formula"], operands={k: v for k, v in ops.items() if k in used},
                                      prefer_stated=bool(c.get("prefer_stated", True)))
            except F.FormulaError as exc:
                log.warning("formula rejected", extra={"field": key, "formula": c.get("formula"), "error": str(exc)})
    spec.source = "llm"
    return spec


def parse_json(content: str | None) -> dict | None:
    if not content:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


async def analyse(schema: dict, client: LLMClient | None, *, model: str, extra: dict | None = None,
                  usage: UsageTracker | None = None, job_id: str | None = None,
                  deadline: float | None = None) -> SchemaSpec:
    spec = heuristic_spec(schema)
    if client is None:
        return spec
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"name": schema.get("name"), "description": schema.get("description"),
                                                    "fields": schema["fields"]}, indent=1, sort_keys=True)}]
    try:
        r = await client.chat(model=model, messages=msgs, stage="schema_analysis", job_id=job_id, usage=usage,
                              temperature=0, max_tokens=12000, timeout_s=120, deadline=deadline,
                              response_format={"type": "json_object"}, **(extra or {}))
        data = parse_json(r.content)
        if data is None:
            log.warning("schema analysis unparseable; using heuristics", extra={"job_id": job_id})
            return spec
        return merge_llm(spec, data)
    except Exception as exc:  # noqa: BLE001 - analysis is an enhancement; never fail the job over it
        log.warning("schema analysis failed; using heuristics", extra={"job_id": job_id, "error": str(exc)})
        return spec
