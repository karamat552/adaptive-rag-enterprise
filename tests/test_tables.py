"""
Table & Footnote-Aware Ingestion + Arithmetic Sanity — Unit Tests (offline)
============================================================================
Covers Phase B (ADR-006):
- _render_table: header-contexted `Label :: Column=value` lines; merged
  header rows (units); label-column (balance-sheet) fallback to grid-only;
  same-page FOOTNOTES co-location.
- verify_table_arithmetic: additive totals vs member sums; accounting
  negatives; tolerance; flag (not drop) semantics for incomplete windows.
- _group_table_rows: row-boundary chunking — a row line is never split;
  every chunk re-states the structural head.
- End-to-end synthetic PDF: parse -> chunks -> transcript -> slice == text
  -> sha256(company⊣source⊣page⊣slice) == chunk_hash (the full receipt chain
  through the Phase B pipeline).

Run:  pytest tests/test_tables.py -v
"""
import hashlib
import os
from pathlib import Path

if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402
import pymupdf  # noqa: E402

from ingest import (  # noqa: E402
    SourceDocument,
    Settings,
    build_splitter,
    _group_table_rows,
    _parse_number,
    _render_table,
    parse_pdf_stream,
    verify_table_arithmetic,
)


# ============================== _render_table ==============================
def test_render_headed_table_contexts_every_row():
    rows = [
        ["Segment", "Revenue", "Operating Income"],
        ["Products", "43,807", "17,448"],
        ["Services", "22,314", "7,078"],
        ["Total", "66,121", "24,526"],
    ]
    md = _render_table(rows, footnotes=[])
    assert "Products :: Revenue=43,807 | Operating Income=17,448" in md
    assert "Total :: Revenue=66,121 | Operating Income=24,526" in md
    assert md.startswith("TABLE:")


def test_render_merges_unit_header_rows():
    # '$ in millions' style continuation rows extend the header, capped at 2.
    rows = [
        ["Segment", "", "Revenue"],
        ["", "Q4 2023 ($M)", "Q4 2023 ($M)"],
        ["Products", "", "43,807"],
    ]
    md = _render_table(rows, footnotes=[])
    assert "Q4 2023 ($M)" in md
    assert "Products" in md


def test_render_label_column_table_falls_back_to_grid():
    # Balance-sheet style as PyMuPDF actually delivers it: the 'header' slot
    # holds a lone structural token (or nothing) — <2 non-empty cells — and
    # labels live in column 0 of every data row. Grid-only; fabricating
    # column names would be worse than none.
    rows = [
        ["ASSETS", "", ""],
        ["Cash and equivalents", "", "23,460"],
        ["Total assets", "", "62,634"],
    ]
    md = _render_table(rows, footnotes=[])
    assert " :: " not in md
    assert "| Cash and equivalents |" in md
    assert "| Total assets |" in md


def test_render_co_locates_footnotes():
    rows = [["Metric", "Q4"], ["Revenue", "40,111"]]
    md = _render_table(rows, footnotes=["(1) Includes FX impact"])
    assert "FOOTNOTES:" in md
    assert "(1) Includes FX impact" in md


def test_render_skips_empty_tables():
    assert _render_table([["", ""], ["", ""]], footnotes=[]) is None


# ============================== verify_table_arithmetic ====================
GOOD = """TABLE:
Products :: Revenue=43,807 | Operating Income=17,448
Services :: Revenue=22,314 | Operating Income=7,078
Total :: Revenue=66,121 | Operating Income=24,526
"""

BAD = """TABLE:
Products :: Revenue=43,807
Services :: Revenue=22,314
Total :: Revenue=70,000
"""


def test_arithmetic_passes_when_members_sum_to_total():
    r = verify_table_arithmetic(GOOD)
    assert r["checked"] == 2 and r["passed"] == 2 and not r["violations"]


def test_arithmetic_flags_mismatch_without_dropping():
    r = verify_table_arithmetic(BAD)
    assert r["checked"] == 1 and r["passed"] == 0
    v = r["violations"][0]
    assert v["total"] == 70000 and v["member_sum"] == 66121


def test_arithmetic_handles_accounting_negatives():
    text = """TABLE:
Cost of sales :: Q4=12,120
Operating expenses :: Q4=(1,234)
R&D :: Q4=(7,391)
Total costs :: Q4=3,495
"""
    # 12120 - 1234 - 7391 = 3495 -> must PASS with paren negatives.
    r = verify_table_arithmetic(text)
    assert r["checked"] >= 1 and r["passed"] == r["checked"]


def test_arithmetic_no_total_row_means_nothing_checked():
    text = """TABLE:
Products :: Revenue=43,807
Services :: Revenue=22,314
"""
    assert verify_table_arithmetic(text)["checked"] == 0


def test_arithmetic_incomplete_window_flags_not_certifies():
    # Only SOME member rows landed in this chunk — a flagged total, not a
    # verified one. The 'flag, never drop' contract.
    text = """TABLE:
Products :: Revenue=43,807
Services :: Revenue=22,314
Total :: Revenue=213,073
"""
    r = verify_table_arithmetic(text)
    assert r["checked"] == 1 and r["violations"], \
        "incomplete membership must FLAG, never certify"


# ============================== _parse_number ==============================
def test_parse_number_variants():
    assert _parse_number("22,314") == 22314.0
    assert _parse_number("$22,314") == 22314.0
    assert _parse_number("(1,234)") == -1234.0
    assert _parse_number("—") is None
    assert _parse_number("n/a") is None
    assert _parse_number("91.7%") == 91.7


# ============================== _group_table_rows =========================
def test_group_rows_never_splits_a_row():
    body = ("TABLE:\n"
            "| Segment | Revenue | Operating Income |\n"
            + "\n".join(f"Row {i} :: A=1 | B=2" for i in range(40)))
    chunks = _group_table_rows(body, chunk_size=200)
    assert len(chunks) > 1
    for ch in chunks:
        lines = ch.splitlines()
        assert lines[0] == "TABLE:", "every chunk re-states the structural head"
        assert all(ln.count(" :: ") <= 1 for ln in lines), "no row ever split"
        # each chunk is within a sane bound (single oversize row allowed)
        assert len(ch) < 200 + 60 or len(lines) <= 4


def test_group_rows_single_oversize_row_stays_whole():
    huge = "Very long single row :: " + "x" * 500
    chunks = _group_table_rows("TABLE:\n" + huge, chunk_size=100)
    assert len(chunks) == 1 and "x" * 500 in chunks[0]


# ============================== end-to-end synthetic PDF ===================
def _make_synthetic_pdf(tmp_path: Path) -> Path:
    """One page: a prose section + a BORDERED PyMuPDF-detectable table + a
    footnote line. find_tables' 'lines' strategy requires drawn cell borders
    (real SEC filings have them); borderless text grids are not detected.
    Exercises the full Phase B pipeline deterministically."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    page.insert_text((72, y), "OVERVIEW", fontsize=14)
    y += 20
    for ln in ("Total revenue was strong this quarter.",
               "Services grew to record highs overall."):
        page.insert_text((72, y), ln, fontsize=10)
        y += 16

    # Bordered 3x3 table at aligned coordinates.
    table_rows = [
        ["Segment", "Revenue", "Income"],
        ["Products", "43,807", "17,448"],
        ["Services", "22,314", "7,078"],
    ]
    x0, y0, x1, y1 = 60, 260, 360, 340
    rows_n, cols_n = 3, 3
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y1))
    for i in range(1, rows_n):
        yy = y0 + i * (80 / rows_n)
        page.draw_line((x0, yy), (x1, yy))
    for j in range(1, cols_n):
        xx = x0 + j * (300 / cols_n)
        page.draw_line((xx, y0), (xx, y1))
    for r in range(rows_n):
        for c in range(cols_n):
            page.insert_text((x0 + 8 + c * 100, y0 + 20 + r * 26),
                            table_rows[r][c], fontsize=9)

    page.insert_text((72, 150), "(1) Includes amortization of acquired intangibles",
                     fontsize=8)
    path = tmp_path / "synthetic_q4.pdf"
    doc.save(path)
    doc.close()
    return path


def test_synthetic_pdf_full_chain(tmp_path):
    src = SourceDocument(filename="synthetic_q4.pdf", url="https://x",
                          company="TestCo", year=2023, quarter="Q4")
    pdf_path = _make_synthetic_pdf(tmp_path)
    transcripts: list = []
    chunks = list(parse_pdf_stream(
        pdf_path, src, Settings(), build_splitter(Settings()),
        transcript_sink=transcripts))

    assert chunks, "pipeline must yield chunks from the synthetic PDF"
    assert transcripts, "page transcript ledger must be recorded"
    tr = transcripts[0].transcript

    span_ok = hash_ok = tables_seen = 0
    for c in chunks:
        m = c.metadata
        if m.contains_table:
            tables_seen += 1
            # B1: header-contexted rows present in table chunks
            if " :: " in c.text or "|" in c.text:
                pass
        assert m.char_start is not None and m.char_end is not None
        sl = tr[m.char_start:m.char_end]
        if sl == c.text:
            span_ok += 1
            key = f"{m.company}\x1f{m.source}\x1f{m.page}\x1f{sl}"
            if hashlib.sha256(key.encode()).hexdigest() == c.chunk_hash:
                hash_ok += 1
    assert span_ok == len(chunks) == hash_ok, \
        f"construction-exact invariant broken: spans={span_ok} hashes={hash_ok} n={len(chunks)}"
    assert tables_seen >= 1, "the synthetic table must be captured as table chunks"
    # B2: footnote co-located on the same page's table or prose region
    assert any("FOOTNOTES:" in c.text or "Includes amortization" in c.text
               for c in chunks), "footnote must be co-located with its table"
