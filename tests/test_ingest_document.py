from app.eval.coverage import _date_pattern, _num_pattern, classify, normalise
from app.ingest.document import MarkdownBuilder, feed_lines, looks_like_heading, run_in_heading
from datetime import date


def test_sections_chunks_and_slicing():
    b = MarkdownBuilder(words_per_chunk=10)
    b.page(1)
    b.heading("ARTICLE 1 - TERM")
    b.text("The term is five years beginning on the Commencement Date.")   # 10 words
    b.text("It ends on the Termination Date.")                              # forces a chunk
    b.page(2)
    b.heading("BASE RENT.")
    b.table("| Period | Rent |\n|---|---|\n| Year 1 | $4,250.00 |")
    doc = b.build(source="x.pdf", kind="pdf", variant="t")
    s1 = doc.anchor("S001")
    assert s1.tag == "[Section 001: ARTICLE 1 - TERM]"
    assert "Termination Date" in doc.slice("S001")          # section slice runs to the next section
    assert "BASE RENT" not in doc.slice("S001")
    assert "Termination Date" in doc.slice("C001") and "five years" not in doc.slice("C001")
    assert doc.slice("T001").strip().endswith("$4,250.00 |")
    assert doc.anchor("S002").pages == [2] and s1.pages == [1]
    assert doc.page_at(doc.markdown.index("$4,250.00")) == 2


def test_toc_entries_are_not_sections_and_body_ends_toc():
    b = MarkdownBuilder()
    b.heading("TABLE OF CONTENTS")
    b.heading("1. BASE RENT.")
    b.heading("2. ADDITIONAL RENT.")
    b.run_in("1. BASE RENT.", "1. BASE RENT. Tenant shall pay to Landlord the monthly base rent set out below.", True)
    doc = b.build(source="x.docx", kind="docx", variant="t")
    titles = [a.title for a in doc.anchors if a.kind == "section"]
    assert titles == ["TABLE OF CONTENTS", "1. BASE RENT."]
    assert "Tenant shall pay" in doc.slice("S002")


def test_heading_heuristics():
    assert looks_like_heading("BASE RENT.")
    assert looks_like_heading("ARTICLE IV")
    assert looks_like_heading("Rent Revenue", bold=True)
    assert not looks_like_heading("Rent Revenue")
    assert not looks_like_heading("KESTREL ANALYTICS USA LLC, a Delaware limited liability company, as tenant,")
    assert run_in_heading("17. HOLDING OVER. Tenant shall pay to Landlord double the Base Rent then in effect") == (
        "17. HOLDING OVER.", True)
    assert run_in_heading("1. Tenant shall pay rent monthly in advance on the first day.") is None
    assert run_in_heading("17. HOLDING OVER ........ 12") is None


def test_feed_lines_groups_paragraphs_tables_and_headings():
    b = MarkdownBuilder()
    feed_lines(b, "SECURITY DEPOSIT\nTenant deposits $2,150.\nwith Landlord.\n\n| a | b | c |\n| 1 | 2 | 3 |\n")
    doc = b.build(source="x", kind="pdf", variant="t")
    assert [a.kind for a in doc.anchors] == ["section", "table"]
    assert "Tenant deposits $2,150.\nwith Landlord." in doc.markdown


def test_value_matching_variants():
    text = normalise("Made as of the 21st day of August, 2025. Deposit: $2,150.00 | Rent 4250 | 8/21/25")
    assert _num_pattern(2150).search(text) and _num_pattern(4250).search(text)
    assert not _num_pattern(215).search(text)
    assert len(list(_date_pattern(date(2025, 8, 21)).finditer(text))) == 2
    assert _date_pattern(date(1994, 1, 1)).search(normalise("commencing on the first day of January, 1994"))
    assert classify("2025-08-21") == "date" and classify("2025-08") == "month" and classify(3.5) == "number"
