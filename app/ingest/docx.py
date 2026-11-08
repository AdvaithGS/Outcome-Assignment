"""DOCX branch: walk the WordprocessingML body in document order and build anchored Markdown.

- Headings: Word heading styles when present, otherwise heuristics (short all-caps lines, bold
  short lines, ARTICLE/SECTION n). Most real leases use no heading styles.
- Auto-numbering is reconstructed from numbering.xml, so "28. REAL ESTATE BROKER." keeps its number.
- Tables → `[Table ID: NNN]` + Markdown table (gridSpan padded, vMerge continuations blank).
- Tabs inside a line → " | " (keeps tab-aligned pseudo-tables readable); leading tabs dropped.
- Tracked deletions (w:delText) are skipped; insertions are kept. Struck-through runs are marked ~~like this~~
  (a formatted redline does not apply); hidden runs (w:vanish) are skipped.
- Embedded images above a size threshold → `[Image ID: NNN]` + OCR transcription (signature pages, scanned exhibits).

Baselines for E0: `ingest_docx_baseline(path, "python-docx" | "mammoth")`.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from app.artifacts import ArtifactCache
from app.ingest.document import Document, MarkdownBuilder, feed_lines, looks_like_heading, run_in_heading
from app.ingest.ocr import run_ocr

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
V = "urn:schemas-microsoft-com:vml"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _attr(el, name: str, ns: str = W) -> str | None:
    return el.get(f"{{{ns}}}{name}") if el is not None else None


def _on(rpr, prop: str) -> bool:
    """Is a run property (b, strike, dstrike, vanish) switched on? <w:strike/> or val true/1/on."""
    el = rpr.find(_w(prop)) if rpr is not None else None
    return el is not None and (_attr(el, "val") or "true").lower() not in ("0", "false", "off", "none")


def _join_marked(pieces: list[str], struck: list[bool]) -> str:
    """Join run texts, wrapping each stretch of struck-through text in ~~ ~~ (whitespace kept outside the marks),
    the same convention the vision transcription uses, so the extraction prompt treats it as not applying."""
    out, i = [], 0
    while i < len(pieces):
        if not struck[i]:
            out.append(pieces[i])
            i += 1
            continue
        j = i
        while j < len(pieces) and (struck[j] or (not pieces[j].strip() and j + 1 < len(pieces) and struck[j + 1])):
            j += 1
        seg = "".join(pieces[i:j])
        lead, core, trail = seg[:len(seg) - len(seg.lstrip())], seg.strip(), seg[len(seg.rstrip()):]
        out.append(f"{lead}~~{core}~~{trail}")
        i = j
    return "".join(out)


# ---------------------------------------------------------------------------- numbering

def _roman(n: int) -> str:
    vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
            (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _letters(n: int) -> str:
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def _fmt(n: int, fmt: str) -> str:
    return {"lowerLetter": _letters(n), "upperLetter": _letters(n).upper(), "lowerRoman": _roman(n),
            "upperRoman": _roman(n).upper(), "bullet": "", "none": "",
            "decimalZero": f"{n:02d}"}.get(fmt, str(n))


class Numbering:
    def __init__(self, xml: bytes | None):
        self.levels: dict[str, dict[int, tuple[str, str, int]]] = {}
        self.num_to_abs: dict[str, str] = {}
        self.counters: dict[str, dict[int, int]] = {}
        if not xml:
            return
        root = ET.fromstring(xml)
        for an in root.findall(_w("abstractNum")):
            aid = _attr(an, "abstractNumId")
            lv = {}
            for l in an.findall(_w("lvl")):
                ilvl = int(_attr(l, "ilvl") or 0)
                fmt = _attr(l.find(_w("numFmt")), "val") or "decimal"
                text = _attr(l.find(_w("lvlText")), "val") or ""
                start = int(_attr(l.find(_w("start")), "val") or 1)
                lv[ilvl] = (fmt, text, start)
            self.levels[aid] = lv
        for n in root.findall(_w("num")):
            self.num_to_abs[_attr(n, "numId")] = _attr(n.find(_w("abstractNumId")), "val")

    def label(self, num_id: str, ilvl: int) -> str:
        aid = self.num_to_abs.get(num_id)
        lv = self.levels.get(aid or "", {})
        if ilvl not in lv:
            return ""
        c = self.counters.setdefault(aid, {})
        c[ilvl] = c.get(ilvl, lv[ilvl][2] - 1) + 1
        for deeper in [k for k in c if k > ilvl]:
            del c[deeper]
        fmt, text, _ = lv[ilvl]
        if fmt in ("bullet", "none"):
            return ""

        def sub(m):
            lvl = int(m.group(1)) - 1
            n = c.get(lvl, lv.get(lvl, ("decimal", "", 1))[2])
            return _fmt(n, lv.get(lvl, ("decimal",))[0])

        return re.sub(r"%(\d)", sub, text).strip()


# ---------------------------------------------------------------------------- parser

class _DocxParser:
    def __init__(self, z: zipfile.ZipFile, *, ocr_engine: str | None, min_image_bytes: int, cache: ArtifactCache | None):
        self.z = z
        self.ocr_engine = ocr_engine
        self.min_image_bytes = min_image_bytes
        self.cache = cache
        names = set(z.namelist())
        self.numbering = Numbering(z.read("word/numbering.xml") if "word/numbering.xml" in names else None)
        self.styles: dict[str, tuple[str, str | None, tuple | None]] = {}   # id -> (name, basedOn, numPr)
        if "word/styles.xml" in names:
            for s in ET.fromstring(z.read("word/styles.xml")).findall(_w("style")):
                sid = _attr(s, "styleId")
                name = _attr(s.find(_w("name")), "val") or sid
                based = _attr(s.find(_w("basedOn")), "val")
                np_ = s.find(f"{_w('pPr')}/{_w('numPr')}")
                num = (_attr(np_.find(_w("numId")), "val"), int(_attr(np_.find(_w("ilvl")), "val") or 0)) if np_ is not None and np_.find(_w("numId")) is not None else None
                self.styles[sid] = (name, based, num)
        self.rels: dict[str, str] = {}
        if "word/_rels/document.xml.rels" in names:
            for r in ET.fromstring(z.read("word/_rels/document.xml.rels")).findall(f"{{{REL}}}Relationship"):
                self.rels[r.get("Id")] = r.get("Target")
        self.images_seen = 0
        self.images_ocrd = 0
        self.ocr_seconds = 0.0

    def style_name(self, sid: str | None) -> str | None:
        return self.styles.get(sid, (sid, None, None))[0] if sid else None

    def para_numpr(self, p, sid: str | None):
        np_ = p.find(f"{_w('pPr')}/{_w('numPr')}")
        if np_ is not None and np_.find(_w("numId")) is not None:
            return _attr(np_.find(_w("numId")), "val"), int(_attr(np_.find(_w("ilvl")), "val") or 0)
        seen = set()
        while sid and sid not in seen:          # numbering inherited from the style chain
            seen.add(sid)
            name, based, num = self.styles.get(sid, (None, None, None))
            if num:
                return num
            sid = based
        return None

    def para(self, p) -> tuple[str, bool, str | None, list[str]]:
        """-> (text, mostly_bold, style_name, image_rel_ids)"""
        sid = _attr(p.find(f"{_w('pPr')}/{_w('pStyle')}"), "val")
        pieces: list[str] = []
        images: list[str] = []
        bold_chars = total_chars = 0
        struck: list[bool] = []                 # per piece: struck-through text (a redline that does not apply)
        for r in p.iter(_w("r")):
            rpr = r.find(_w("rPr"))
            if _on(rpr, "vanish"):
                continue                        # hidden text is not part of the document a reader sees
            is_bold = _on(rpr, "b")
            is_struck = _on(rpr, "strike") or _on(rpr, "dstrike")
            for el in r:
                if el.tag == _w("t"):
                    pieces.append(el.text or "")
                    struck.append(is_struck and bool((el.text or "").strip()))
                    n = len((el.text or "").strip())
                    total_chars += n
                    bold_chars += n if is_bold else 0
                elif el.tag == _w("tab"):
                    pieces.append("\t")
                    struck.append(False)
                elif el.tag in (_w("br"), _w("cr")):
                    pieces.append("\n")
                    struck.append(False)
            for blip in r.iter(f"{{{A}}}blip"):
                if blip.get(f"{{{R}}}embed"):
                    images.append(blip.get(f"{{{R}}}embed"))
            for im in r.iter(f"{{{V}}}imagedata"):
                if im.get(f"{{{R}}}id"):
                    images.append(im.get(f"{{{R}}}id"))
        images = list(dict.fromkeys(images))   # mc:AlternateContent repeats an image as Choice + Fallback
        text = _join_marked(pieces, struck)
        text = "\n".join(re.sub(r"\t+", " | ", ln.lstrip("\t")).rstrip(" |") for ln in text.split("\n"))
        num = self.para_numpr(p, sid)
        if num and text.strip():
            label = self.numbering.label(*num)
            if label:
                text = f"{label} {text.strip()}"
        mostly_bold = total_chars > 0 and bold_chars / total_chars > 0.6
        return text.strip(), mostly_bold, self.style_name(sid), images

    def cell_text(self, tc) -> str:
        return " ".join(t for t in (self.para(p)[0] for p in tc.iter(_w("p"))) if t).replace("|", "/")

    def table(self, tbl) -> str:
        rows = []
        for tr in tbl.findall(_w("tr")):
            cells = []
            for tc in tr.findall(_w("tc")):
                pr = tc.find(_w("tcPr"))
                span = int(_attr(pr.find(_w("gridSpan")) if pr is not None else None, "val") or 1)
                vm = pr.find(_w("vMerge")) if pr is not None else None
                txt = "" if (vm is not None and (_attr(vm, "val") or "continue") == "continue") else self.cell_text(tc)
                cells.append(txt)
                cells.extend([""] * (span - 1))
            rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(md)

    def image_text(self, rid: str) -> tuple[str, str] | None:
        target = self.rels.get(rid)
        if not target:
            return None
        path = target if target.startswith("word/") else "word/" + target.lstrip("/").removeprefix("word/")
        try:
            data = self.z.read(path)
        except KeyError:
            return None
        self.images_seen += 1
        ext = path.rsplit(".", 1)[-1].lower()
        if len(data) < self.min_image_bytes or ext not in ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif"):
            return None
        if not self.ocr_engine:
            return "(image not transcribed)", path
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("L").save(buf, format="PNG")
        r = run_ocr(self.ocr_engine, buf.getvalue(), cache=self.cache)
        self.images_ocrd += 1
        self.ocr_seconds += r.seconds
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "n/a"
        return r.text, f"{path.split('/')[-1]}, ocr={self.ocr_engine}, conf={conf}"

    def walk(self, container, b: MarkdownBuilder) -> None:
        for el in container:
            if el.tag == _w("p"):
                text, bold, style, images = self.para(el)
                if text:
                    ri = run_in_heading(text) if "\n" not in text[:80] else None
                    if "\n" not in text and looks_like_heading(text, bold=bold, style=style):
                        b.heading(text)
                    elif ri:
                        b.run_in(ri[0], text, ri[1])
                    else:
                        b.text(text)
                for rid in images:
                    got = self.image_text(rid)
                    if got:
                        b.image(got[0], note=got[1])
            elif el.tag == _w("tbl"):
                md = self.table(el)
                if md:
                    b.table(md)
            elif el.tag == _w("sdt"):
                content = el.find(_w("sdtContent"))
                if content is not None:
                    self.walk(content, b)


def ingest_docx(path: str | Path, *, ocr_engine: str | None = "tesseract", min_image_bytes: int = 30_000,
                cache: ArtifactCache | None = None, variant: str | None = None) -> Document:
    path = Path(path)
    t0 = time.perf_counter()
    with zipfile.ZipFile(path) as z:
        parser = _DocxParser(z, ocr_engine=ocr_engine, min_image_bytes=min_image_bytes, cache=cache)
        body = ET.fromstring(z.read("word/document.xml")).find(_w("body"))
        b = MarkdownBuilder()
        parser.walk(body, b)
    return b.build(source=path.name, kind="docx", variant=variant or f"docx-xml+{ocr_engine or 'no-ocr'}",
                   meta={"seconds": round(time.perf_counter() - t0, 2), "images_seen": parser.images_seen,
                         "images_ocrd": parser.images_ocrd, "ocr_seconds": round(parser.ocr_seconds, 2)})


def ingest_docx_baseline(path: str | Path, lib: str) -> Document:
    """Library baselines for E0 (text only, same heading heuristics, no image OCR)."""
    path = Path(path)
    t0 = time.perf_counter()
    if lib == "python-docx":
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            parts += [" | ".join(c.text for c in row.cells) for row in t.rows]
        text = "\n\n".join(parts)
    elif lib == "mammoth":
        import mammoth
        with open(path, "rb") as f:
            text = mammoth.extract_raw_text(f).value
    else:
        raise ValueError(lib)
    b = MarkdownBuilder()
    feed_lines(b, text)
    return b.build(source=path.name, kind="docx", variant=lib, meta={"seconds": round(time.perf_counter() - t0, 2)})
