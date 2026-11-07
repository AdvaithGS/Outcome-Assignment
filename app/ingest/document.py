"""Common document model: Markdown text carrying a coordinate system of anchors.

Every ingestion branch (PDF, DOCX, later spreadsheets) produces a `Document`. Anchors are tags
written inline into the Markdown, so an LLM can name them and code can slice by them:

    [Page 3]                      PDF page start (gives `page_numbers`)
    [Section 012: BASE RENT.]     heading-based semantic section
    [--- CHUNK 004 ---]           fixed-size boundary (~1,000 words without a new section)
    [Table ID: 002]               a table block
    [Image ID: 001]               an embedded image and its transcription

Slicing rules (`Document.slice`):
    section → up to the next section          chunk → up to the next section or chunk
    page    → up to the next page             table / image → the block itself
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

WORDS_PER_CHUNK = 1000
_TOC_RE = re.compile(r"^\s*(table of )?contents\s*$", re.I)
_TOC_EXIT_CHARS = 150


@dataclass
class Anchor:
    id: str            # S012 / C004 / T002 / I001 / P3
    kind: str          # section | chunk | table | image | page
    tag: str           # exact tag text as written in the Markdown
    title: str = ""
    start: int = 0     # offset of the tag in the Markdown
    end: int = 0       # end of the span this anchor owns
    pages: list[int] = field(default_factory=list)


@dataclass
class PageInfo:
    number: int
    method: str                     # "text" | "ocr:<engine>"
    chars: int
    ocr_confidence: float | None = None
    seconds: float = 0.0


@dataclass
class Document:
    source: str
    kind: str                       # pdf | docx
    variant: str                    # which ingestion configuration produced it
    markdown: str
    anchors: list[Anchor]
    pages: list[PageInfo] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    _page_offsets: list[tuple[int, int]] = field(default_factory=list, repr=False)  # (offset, page)

    @property
    def approx_tokens(self) -> int:
        return len(self.markdown) // 4

    def anchor(self, ref: str) -> Anchor | None:
        ref = ref.strip()
        for a in self.anchors:
            if a.id == ref or a.tag == ref:
                return a
        return None

    def slice(self, ref: str) -> str:
        a = self.anchor(ref)
        return self.markdown[a.start:a.end] if a else ""

    def page_at(self, offset: int) -> int | None:
        if not self._page_offsets:
            return None
        i = bisect.bisect_right([o for o, _ in self._page_offsets], offset) - 1
        return self._page_offsets[i][1] if i >= 0 else None

    def anchor_at(self, offset: int, kinds: tuple[str, ...] = ("chunk", "section")) -> Anchor | None:
        """Smallest anchor of the given kinds whose span contains `offset`."""
        best = None
        for a in self.anchors:
            if a.kind in kinds and a.start <= offset < a.end:
                if best is None or (a.end - a.start) < (best.end - best.start):
                    best = a
        return best


# ---------------------------------------------------------------------------- headings

_ARTICLE_RE = re.compile(r"^(article|section|exhibit|schedule|addendum|rider)\s+([0-9]+|[ivxlc]+|[a-z])\b", re.I)


def looks_like_heading(text: str, *, bold: bool = False, style: str | None = None) -> bool:
    """Heuristic heading detector shared by all branches (most real leases use no heading styles)."""
    t = text.strip()
    if not t or len(t) > 90:
        return False
    if style and re.match(r"(heading|title)\b", style, re.I):
        return True
    if _ARTICLE_RE.match(t):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 3 or len(t.split()) > 12 or t.endswith((",", ";")):
        return False
    upper = sum(c.isupper() for c in letters) / len(letters)
    if upper > 0.9:
        return True
    return bold and len(t.split()) <= 8 and t[0].isupper()


# "17. HOLDING OVER. Tenant shall pay ..." / "ARTICLE 4 - RENT: The Tenant ..." / "(b) Late Charges. If ..."
_RUN_IN_RE = re.compile(
    r"^(?P<label>(?:article|section)\s+[\dIVXLC.]+[.:\-\s]*|\(?\d{1,2}(?:\.\d{1,2})*[.)]|\(?[A-Za-z][.)]|[IVXLC]+\.)\s*"
    r"(?P<title>[A-Z][A-Za-z0-9 ,'&/\-()]{2,70}?[.:])\s+(?P<rest>\S.*)$", re.S | re.I)


def run_in_heading(text: str) -> tuple[str, bool] | None:
    """Detect a heading run into the start of a paragraph. -> (title, has_body_text) or None.
    The title must be mostly upper case (or the whole label+title short), so ordinary numbered
    sentences ("1. Tenant shall ...") are not mistaken for headings."""
    m = _RUN_IN_RE.match(text.strip())
    if not m:
        return None
    title = m.group("title")
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 3:
        return None
    upper = sum(c.isupper() for c in letters) / len(letters)
    words_title = len(title.split())
    if upper < 0.85 and not (words_title <= 5 and all(w[0].isupper() for w in title.split() if w[0].isalpha())):
        return None
    rest = m.group("rest")
    has_body = len(re.findall(r"\b[a-z]{2,}\b", rest)) >= 4
    return f"{m.group('label').strip()} {title.strip()}".strip(), has_body


# ---------------------------------------------------------------------------- builder

class MarkdownBuilder:
    def __init__(self, *, words_per_chunk: int = WORDS_PER_CHUNK, detect_toc: bool = True):
        self._parts: list[str] = []
        self._len = 0
        self.anchors: list[Anchor] = []
        self._counts = {"section": 0, "chunk": 0, "table": 0, "image": 0}
        self._page_offsets: list[tuple[int, int]] = []
        self._words_since_anchor = 0
        self.words_per_chunk = words_per_chunk
        self.detect_toc = detect_toc
        self._in_toc = False

    # -- low level
    def _emit(self, s: str) -> int:
        start = self._len
        self._parts.append(s)
        self._len += len(s)
        return start

    def _tag(self, kind: str, tag: str, title: str = "", aid: str | None = None) -> Anchor:
        last = self._parts[-1] if self._parts else ""
        if last and not last.endswith("\n\n"):
            self._emit("\n" if last.endswith("\n") else "\n\n")
        a = Anchor(id=aid or tag, kind=kind, tag=tag, title=title, start=self._len)
        self._emit(tag + "\n")
        self.anchors.append(a)
        return a

    # -- anchors
    def page(self, n: int) -> None:
        self._page_offsets.append((self._len, n))
        self._tag("page", f"[Page {n}]", aid=f"P{n}")

    def heading(self, title: str, *, force: bool = False) -> None:
        title = " ".join(title.split())
        if self.detect_toc and _TOC_RE.match(title):
            self._section(title)
            self._in_toc = True
            return
        if self._in_toc and not force:
            self._emit(title + "\n")      # TOC entries are text, not sections
            return
        self._section(title)

    def run_in(self, title: str, full_text: str, has_body: bool) -> None:
        """A heading run into its paragraph: open a section, keep the full paragraph as text
        (so verbatim citations including the heading still match). A run-in heading followed by
        real prose is body text, never a TOC entry, so it ends TOC mode."""
        if has_body:
            self._in_toc = False
        self.heading(title)
        self.text(full_text)

    def _section(self, title: str) -> None:
        self._counts["section"] += 1
        k = self._counts["section"]
        self._tag("section", f"[Section {k:03d}: {title}]", title=title, aid=f"S{k:03d}")
        self._words_since_anchor = 0

    def _chunk(self) -> None:
        self._counts["chunk"] += 1
        k = self._counts["chunk"]
        self._tag("chunk", f"[--- CHUNK {k:03d} ---]", aid=f"C{k:03d}")
        self._words_since_anchor = 0

    # -- content
    def text(self, para: str) -> None:
        para = para.strip("\n")
        if not para.strip():
            return
        words = len(para.split())
        if self._in_toc and len(para) >= _TOC_EXIT_CHARS:
            self._in_toc = False
        if self._words_since_anchor and self._words_since_anchor + words > self.words_per_chunk:
            self._chunk()
        self._emit(para + "\n\n")
        self._words_since_anchor += words

    def table(self, md: str, title: str = "") -> None:
        self._counts["table"] += 1
        k = self._counts["table"]
        a = self._tag("table", f"[Table ID: {k:03d}]", title=title, aid=f"T{k:03d}")
        self._emit(md.strip("\n") + "\n\n")
        a.end = self._len
        self._words_since_anchor += len(md.split())

    def image(self, transcription: str, note: str = "") -> None:
        self._counts["image"] += 1
        k = self._counts["image"]
        a = self._tag("image", f"[Image ID: {k:03d}]", title=note, aid=f"I{k:03d}")
        body = transcription.strip() or "(no text recognised)"
        self._emit(body + "\n\n")
        a.end = self._len
        self._words_since_anchor += len(body.split())

    # -- finish
    def build(self, *, source: str, kind: str, variant: str, pages: list[PageInfo] | None = None,
              meta: dict | None = None) -> Document:
        md = "".join(self._parts)
        n = len(md)
        by_kind: dict[str, list[Anchor]] = {}
        for a in self.anchors:
            by_kind.setdefault(a.kind, []).append(a)

        def next_start(a: Anchor, kinds: tuple[str, ...]) -> int:
            later = [b.start for b in self.anchors if b.kind in kinds and b.start > a.start]
            return min(later) if later else n

        for a in self.anchors:
            if a.kind == "section":
                a.end = next_start(a, ("section",))
            elif a.kind == "chunk":
                a.end = next_start(a, ("section", "chunk"))
            elif a.kind == "page":
                a.end = next_start(a, ("page",))
            # table/image ends were set when the block was written

        doc = Document(source=source, kind=kind, variant=variant, markdown=md, anchors=self.anchors,
                       pages=pages or [], meta=meta or {}, _page_offsets=list(self._page_offsets))
        if self._page_offsets:
            trailing_page_tag = re.compile(r"\[Page \d+\]\s*$")
            for a in self.anchors:
                content_end = a.end
                while True:   # a span ending in the next page's tag does not cover that page
                    seg = md[a.start:content_end].rstrip()
                    m = trailing_page_tag.search(seg)
                    if not m or a.kind == "page":
                        break
                    content_end = a.start + m.start()
                first, last = doc.page_at(a.start), doc.page_at(max(a.start, content_end - 1))
                if first is not None and last is not None:
                    a.pages = list(range(first, last + 1))
        return doc


# ---------------------------------------------------------------------------- shared helpers

def feed_lines(builder: MarkdownBuilder, text: str) -> None:
    """Feed plain extracted text (PDF/OCR) into the builder: headings become sections, Markdown
    table runs become tables, everything else is grouped into paragraphs at blank lines."""
    para: list[str] = []
    table: list[str] = []

    def flush_para():
        if para:
            builder.text("\n".join(para))
            para.clear()

    def flush_table():
        if table:
            builder.table("\n".join(table))
            table.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            flush_para()
            table.append(stripped)
            continue
        flush_table()
        if not stripped:
            flush_para()
            continue
        md_heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if md_heading or looks_like_heading(stripped):
            flush_para()
            builder.heading(md_heading.group(1).strip("*_ ") if md_heading else stripped)
            continue
        if not para:
            ri = run_in_heading(stripped)
            if ri and ri[1]:
                builder._in_toc = False
                builder.heading(ri[0])
        para.append(line)
    flush_table()
    flush_para()
