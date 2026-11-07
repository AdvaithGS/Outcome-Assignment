"""Decide the document type from the file's content, not its name. PDF, DOCX, XLSX or CSV; anything else is None."""
from __future__ import annotations

import zipfile
from pathlib import Path


def detect(path: Path, filename: str = "") -> str | None:
    with open(path, "rb") as f:
        head = f.read(8192)
    if head.lstrip()[:5] == b"%PDF-" or b"%PDF-" in head[:1024]:
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return None
    if b"\x00" in head:
        return None                      # binary we do not support (e.g. legacy .doc/.xls)
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = head.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    if filename.lower().endswith((".csv", ".tsv", ".txt")) or any(c in text for c in (",", ";", "\t")):
        return "csv"
    return None
