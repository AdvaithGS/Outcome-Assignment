"""Stage 3a: single-entry documents, whole-document extraction (path A).

One prompt template: fixed rules → field list (from stage 0) → document text with anchors. For computed fields
the model returns its own answer *and* the figure as printed (if any), and the helper quantities the formulas need,
so one cached response supports all three computed-field strategies (M / C / H, see postprocess.py).

Unusable output (not JSON) → one repair retry with the error appended → the fallback model → nulls (PARTIAL).
"""
from __future__ import annotations

import json
import logging

from app.ingest.document import Document
from app.llm import LLMClient, UsageTracker
from app.pipeline.schema_analysis import SchemaSpec, parse_json

log = logging.getLogger("extract")
PROMPT_VERSION = "single-v1"

SYSTEM = """You extract fields from a commercial real estate document.

Return ONE JSON object whose keys are exactly the field keys listed (plus "_helpers" if helper values are requested).
Each field value is an object: {"value": <value>, "citation": "<short verbatim snippet from the document, prefixed with its location tag exactly as it appears, e.g. [Page 3] or [Section 012: BASE RENT.]>"}

Rules for "value":
- Numbers as JSON numbers: no currency symbols, no thousands separators, no units.
- Dates as "YYYY-MM-DD". Percentages exactly as the document prints them (5.25 for "5.25%").
- Booleans as true / false. Lists as JSON arrays in document order; aligned lists must have the same length.
- Text exactly as written unless the field asks for a short phrase.
- Text marked ~~like this~~ is struck out and does not apply; [handwritten: ...] marks handwriting, which governs.
- If the document does not state the value, use null with an empty citation. Never guess or use market norms.
  A field whose description defines a value for absence (e.g. "0 if none", "false otherwise", "empty list if none") uses that value instead.
- For fields marked COMPUTED also return "stated": the value exactly as the document prints it, or null if the
  document does not print it; "value" is then your own answer (the stated figure if printed, otherwise your computation).
"""


def _absent_text(a) -> str:
    return {None: "null"}.get(a, json.dumps(a)) if not isinstance(a, list) else "[]"


FREETEXT_RULES = {
    # default: phrase fields follow their description
    "describe": "",
    # E2 variant: copy the document's own words
    "verbatim": ("\nFor fields of type freetext (a phrase or sentence about a term), copy the SHORTEST passage of the "
                 "document that fully states the term, word for word; you may leave out words before or after the term, "
                 "but do not paraphrase, summarise or add anything.\n"),
    # E2 variant: short answer in the format of the description's example, keeping every fact
    "concise": ("\nFor fields of type freetext, answer in the length and format of the description's example (e.g. "
                "'base year 1993', '5% of the overdue amount'), using the document's own wording, and keep every number, "
                "percentage, party and condition the document states for that term.\n"),
}


# Generic commercial-real-estate extraction rules (E2-hints). Each targets an error type that every model made in
# E1b/E2; none names a document. Switched by HINTS=on|off.
HINTS = """
General rules for these documents:
- Identifier fields (suite, unit, floor, code, number): give only the identifier itself (e.g. "Suite 214W"), not the
  words around it (floor or section descriptions).
- A property or building name is the name the document uses for the building or property (it may look like an
  address, e.g. "250 South Harbor"); do not substitute the owner's or landlord's company name for it.
- An address is the full address as written, including a building name when it is part of the address line.
- A right or option (renewal, early termination, expansion) counts only if the document grants it to the party the
  field names; rights of the other party, or rights arising only from casualty, condemnation or default, do not count.
- For a phrase field describing a term (escalation, expense structure, fees), when the document provides for no
  such term or no change, answer exactly "none". Any value the document does not state is JSON null, never the word "None".
"""


_QUOTE_CUES = ("as the lease states it", "as the document states it", "as written", "verbatim", "exactly as",
               "as stated in", "as the lease states", "as the document states", "word for word")


def field_style(f, mode: str = "pointer") -> str:
    """How a field is extracted in pointer mode, decided from its type and description only (no field names):
    quote   - free text the description wants as written: the answer IS an exact span of the document
    summary - other free text: a short phrase (concise rule)
    pointer - a value printed in the document (number, date, percentage, name, code): value + exact supporting quote
    value   - judged or derived (booleans, lists, computed): value as answered, quote used for grounding only"""
    d = (f.original + " " + f.description).lower()
    if f.computed or f.type in ("bool", "list"):
        return "value"
    if f.type == "freetext":
        if mode == "hybrid":
            return "summary"                 # hybrid: every free-text field is a concise summary, never a raw quote
        return "quote" if any(c in d for c in _QUOTE_CUES) else "summary"
    return "pointer"


POINTER_RULES = """
For EVERY field also return "quote": the exact characters copied from the document text (same spelling, punctuation and
spacing), the shortest passage that contains or states the value; "" when the value is null.
- Fields marked QUOTE: "value" must be exactly that quote, the shortest passage that states the term, word for word.
- Fields marked SUMMARY: "value" is a short phrase in the format of the description's example, keeping every number,
  percentage and condition the document states for that term; "quote" is the passage it summarises.
- For spreadsheet documents, put the cell address in the citation, e.g. "[Sheet1!B4] 4250".
"""


def build_messages(spec: SchemaSpec, doc_text: str, freetext_mode: str = "describe", hints: bool = False,
                   style: str = "value") -> list[dict]:
    lines = []
    for f in spec.fields.values():
        t = f.type + (f" of {f.item_type}" if f.type == "list" and f.item_type else "")
        flags = f"{t}; if absent: {_absent_text(f.absent)}" + ("; COMPUTED" if f.computed else "")
        if style in ("pointer", "hybrid") and field_style(f, style) in ("quote", "summary"):
            flags += "; " + field_style(f, style).upper()
        lines.append(f'- "{f.key}" ({f.name}) [{flags}]: {f.description}')
    helpers = spec.helpers()
    helper_block = ""
    if helpers:
        helper_block = ('\n\nAlso return "_helpers": an object with these keys, each {"value": ..., "citation": ...}:\n'
                        + "\n".join(f'- "{k}": {v}' for k, v in helpers.items()))
    user = (f"Document type: {spec.description or spec.name}\n\nFields:\n" + "\n".join(lines) + helper_block
            + f"\n\nDocument text:\n<<<\n{doc_text}\n>>>")
    rules = POINTER_RULES if style in ("pointer", "hybrid") else FREETEXT_RULES.get(freetext_mode, "")
    return [{"role": "system", "content": SYSTEM + rules + (HINTS if hints else "")},
            {"role": "user", "content": user}]


async def extract(spec: SchemaSpec, doc: Document, client: LLMClient, *, model: str, extra: dict,
                  fallback_model: str | None, fallback_extra: dict | None, usage: UsageTracker | None,
                  job_id: str | None = None, doc_id: str | None = None,
                  deadline: float | None = None, freetext_mode: str = "describe",
                  hints: bool = False, style: str = "value") -> tuple[dict | None, list[str]]:
    """-> (raw JSON object or None, notes)."""
    notes: list[str] = []
    msgs = build_messages(spec, doc.markdown, freetext_mode, hints, style)
    attempts = [(model, extra, msgs)]
    for i, (m, ex, ms) in enumerate(attempts):
        try:
            r = await client.chat(model=m, messages=ms, stage="extract_single", job_id=job_id, doc_id=doc_id,
                                  usage=usage, temperature=0, max_tokens=12000, timeout_s=150, deadline=deadline,
                                  response_format={"type": "json_object"}, **ex)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"extraction call failed ({m}): {type(exc).__name__}")
            log.warning("extraction call failed", extra={"job_id": job_id, "model": m, "error": str(exc)[:200]})
            if fallback_model and m != fallback_model and len(attempts) < 3:
                attempts.append((fallback_model, fallback_extra or {}, msgs))
            continue
        data = parse_json(r.content)
        if data is not None and r.finish_reason != "length":
            return data, notes
        why = "output truncated at the token cap" if r.finish_reason == "length" else "output is not a JSON object"
        notes.append(f"unusable output from {m}: {why}")
        if i == 0:   # one repair retry with the error stated (a different request, so not served from cache)
            repair = ms + [{"role": "assistant", "content": (r.content or "")[:4000]},
                           {"role": "user", "content": f"That response was unusable: {why}. Return the complete "
                                                       f"answer again as ONE valid JSON object, nothing else."}]
            attempts.append((m, ex, repair))
        elif fallback_model and m != fallback_model and len(attempts) < 3:
            attempts.append((fallback_model, fallback_extra or {}, msgs))
    return None, notes
