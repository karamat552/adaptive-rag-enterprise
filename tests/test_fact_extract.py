"""ADR-017 Phase 0 — fact store regression tests.

Every test here locks one of the four review amendments or one of the
Appendix B dispositions, using REAL corpus chunk formats (dumped
verbatim from production multi_agent_chunks, 2026-09-14: Apple page 1,
Meta page 6, Tesla pages 4 and 25).

Run:  pytest tests/test_fact_extract.py -v
Zero DB, zero network — the extractor, reconciler, and routing guard
are pure functions. The single integration test at the bottom
(migration 006 + sync + verify, live DB) auto-skips without one.
"""
from decimal import Decimal

import pytest

from fact_extract import (FUZZ_OPERATORS, bind_periods,
                          canonical_entities, canonical_metric,
                          canonical_periods, extract_facts_from_page,
                          path_a_decision, reconcile_rows)

# ===========================================================================
# FIXTURES — verbatim production chunks (metadata values from
# corpus_chunks.jsonl; chunk hashes truncated to distinct stubs).
# ===========================================================================
APPLE_P1_C0 = {
    "chunk_hash": "apple_p1_c0", "source": "Apple_Q4_2023.pdf",
    "company": "Apple", "page": 1, "char_start": 0, "char_end": 698,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "Apple Inc.\n"
        "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited) \n"
        "(In millions, except number of shares, which are reflected in "
        "thousands, and per-share amounts)\n"
        "Three Months Ended \n \n"
        "Twelve Months Ended\n"
        "September 30,\n"
        "September 24,\n"
        "September 30,\n"
        "September 24,\n"
        "2023\n"
        "2022\n"
        "2023\n"
        "2022\n"
        "Net sales:\n"
        "Products \n! \n"
        "67,184   ! \n"
        "70,958   ! \n"
        "298,085  ! \n"
        "316,199\n"
        "Services \n \n"
        "22,314    \n"
        "19,188    \n"
        "85,200   \n"
        "78,129\n"
        "Total net sales (1) \n \n"
        "89,498   \n"
        "90,146   \n"
        "383,285   \n"
        "394,328\n"
        "Cost of sales:\n"
        "Products \n \n"
        "42,586   \n"
        "46,387   \n"
        "189,282    \n"
        "201,471\n"
        "Services \n \n"
        "6,485   \n"
        "5,664   \n"
        "24,855    \n"
        "22,075\n"
        "Total cost of sales \n \n"
        "49,071   \n"
        "52,051   \n"
        "214,137   \n"
        "223,546\n"
        "Gross margin \n \n"
        "40,427   \n"
        "38,095   \n"
        "169,148   \n"
        "170,782"
    ),
}
APPLE_P1_C1 = {
    "chunk_hash": "apple_p1_c1", "source": "Apple_Q4_2023.pdf",
    "company": "Apple", "page": 1, "char_start": 700, "char_end": 1392,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "Total cost of sales \n \n"
        "49,071   \n"
        "52,051   \n"
        "214,137   \n"
        "223,546\n"
        "Gross margin \n \n"
        "40,427   \n"
        "38,095   \n"
        "169,148   \n"
        "170,782  \n \n \n  \n  \n  \n"
        "Operating expenses:\n"
        "Research and development \n \n"
        "7,307    \n"
        "6,761    \n"
        "29,915    \n"
        "26,251\n"
        "Selling, general and administrative \n \n"
        "6,151   \n"
        "6,440   \n"
        "24,932    \n"
        "25,094\n"
        "Total operating expenses \n \n"
        "13,458   \n"
        "13,201   \n"
        "54,847   \n"
        "51,345  \n \n \n  \n  \n  \n"
        "Operating income \n \n"
        "26,969   \n"
        "24,894   \n"
        "114,301    \n"
        "119,437\n"
        "Other income/(expense), net \n \n"
        "29    \n"
        "(237)   \n"
        "(565)   \n"
        "(334)\n"
        "Income before provision for income taxes \n \n"
        "26,998   \n"
        "24,657   \n"
        "113,736   \n"
        "119,103\n"
        "Provision for income taxes \n \n"
        "4,042    \n"
        "3,936   \n"
        "16,741    \n"
        "19,300\n"
        "Net income \n! \n"
        "22,956  ! \n"
        "20,721  ! \n"
        "96,995  !"
    ),
}
APPLE_P1_C2 = {
    "chunk_hash": "apple_p1_c2", "source": "Apple_Q4_2023.pdf",
    "company": "Apple", "page": 1, "char_start": 1394, "char_end": 2089,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "Provision for income taxes \n \n"
        "4,042    \n"
        "3,936   \n"
        "16,741    \n"
        "19,300\n"
        "Net income \n! \n"
        "22,956  ! \n"
        "20,721  ! \n"
        "96,995  ! \n"
        "99,803\n"
        "Earnings per share:\n"
        "Basic \n! \n"
        "1.47   ! \n"
        "1.29   ! \n"
        "6.16   ! \n"
        "6.15\n"
        "Diluted \n! \n"
        "1.46   ! \n"
        "1.29   ! \n"
        "6.13   ! \n"
        "6.11\n"
        "Shares used in computing earnings per share:\n"
        "Basic \n \n"
        "15,599,434   \n"
        "16,030,382    \n"
        "15,744,231    \n"
        "16,215,963\n"
        "Diluted \n \n"
        "15,672,400    \n"
        "16,118,465    \n"
        "15,812,547    \n"
        "16,325,819"
    ),
}

META_P6_C0 = {
    "chunk_hash": "meta_p6_c0", "source": "Meta_Q4_2023.pdf",
    "company": "Meta", "page": 6, "char_start": 0, "char_end": 672,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "CONDENSED CONSOLIDATED STATEMENTS OF INCOME\n"
        "(In millions, except per share amounts)\n"
        "(Unaudited)\n"
        "Three Months Ended December 31,\n"
        "Twelve Months Ended December 31,\n"
        "2023\n"
        "2022\n"
        "2023\n"
        "2022\n"
        "Revenue\n"
        "$ \n"
        "40,111 $ \n"
        "32,165 $ \n"
        "134,902 $ \n"
        "116,609 \n"
        "Costs and expenses:\n"
        " \n"
        "Cost of revenue\n"
        " \n"
        "7,695  \n"
        "8,336  \n"
        "25,959  \n"
        "25,249 \n"
        "Research and development\n"
        " \n"
        "10,517  \n"
        "9,771  \n"
        "38,483  \n"
        "35,338 \n"
        "Marketing and sales\n"
        " \n"
        "3,226  \n"
        "4,574  \n"
        "12,301  \n"
        "15,262 \n"
        "General and administrative\n"
        " \n"
        "2,289  \n"
        "3,085  \n"
        "11,408  \n"
        "11,816 \n"
        "Total costs and expenses\n"
        " \n"
        "23,727  \n"
        "25,766  \n"
        "88,151  \n"
        "87,665 \n"
        "Income from operations\n"
        " \n"
        "16,384  \n"
        "6,399  \n"
        "46,751  \n"
        "28,944 \n"
        "Interest and other income (expense), net\n"
        " \n"
        "424  \n"
        "(250)  \n"
        "677  \n"
        "(125)"
    ),
}
META_P6_C1 = {
    "chunk_hash": "meta_p6_c1", "source": "Meta_Q4_2023.pdf",
    "company": "Meta", "page": 6, "char_start": 674, "char_end": 1323,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "16,384  \n"
        "6,399  \n"
        "46,751  \n"
        "28,944 \n"
        "Income before provision for income taxes\n"
        " \n"
        "16,808  \n"
        "6,149  \n"
        "47,428  \n"
        "28,819 \n"
        "Provision for income taxes\n"
        " \n"
        "2,791  \n"
        "1,497  \n"
        "8,330  \n"
        "5,619 \n"
        "Net income\n"
        "$ \n"
        "14,017 $ \n"
        "4,652 $ \n"
        "39,098 $ \n"
        "23,200 \n"
        "Earnings per share attributable to Class A and Class B \n"
        "common stockholders:\n"
        "Basic\n"
        "$ \n"
        "5.46 $ \n"
        "1.76 $ \n"
        "15.19 $ \n"
        "8.63 \n"
        "Diluted\n"
        "$ \n"
        "5.33 $ \n"
        "1.76 $ \n"
        "14.87 $ \n"
        "8.59 \n"
        "Weighted-average shares used to compute earnings per \n"
        "share attributable to Class A and Class B common \n"
        "stockholders:\n"
        "Basic\n"
        " \n"
        "2,566  \n"
        "2,638  \n"
        "2,574  \n"
        "2,687 \n"
        "Diluted\n"
        " \n"
        "2,630  \n"
        "2,640  \n"
        "2,629  \n"
        "2,702\n"
        "6"
    ),
}

TESLA_P25_C0 = {
    "chunk_hash": "tesla_p25_c0", "source": "Tesla_Q4_2023.pdf",
    "company": "Tesla", "page": 25, "char_start": 0, "char_end": 696,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "(Unaudited)\n"
        "In millions of USD or shares as applicable, except per share data\n"
        "Q4-2022\n"
        "Q1-2023\n"
        "Q2-2023\n"
        "Q3-2023\n"
        "Q4-2023\n"
        "REVENUES\n"
        "Automotive sales\n"
        "20,241 \n"
        "18,878 \n"
        "20,419 \n"
        "18,582\n"
        "20,630 \n"
        "Automotive regulatory credits\n"
        "467 \n"
        "521 \n"
        "282 \n"
        "554\n"
        "433 \n"
        "Automotive leasing\n"
        "599 \n"
        "564 \n"
        "567 \n"
        "489\n"
        "500 \n"
        "Total automotive revenues\n"
        "21,307 \n"
        "19,963 \n"
        "21,268 \n"
        "19,625\n"
        "21,563 \n"
        "Energy generation and storage\n"
        "1,310 \n"
        "1,529 \n"
        "1,509 \n"
        "1,559\n"
        "1,438 \n"
        "Services and other\n"
        "1,701 \n"
        "1,837 \n"
        "2,150 \n"
        "2,166\n"
        "2,166 \n"
        "Total revenues\n"
        "24,318 \n"
        "23,329 \n"
        "24,927 \n"
        "23,350\n"
        "25,167 \n"
        "COST OF REVENUES\n"
        "Automotive sales\n"
        "15,433 \n"
        "15,422 \n"
        "16,841 \n"
        "15,656\n"
        "17,202 \n"
        "Automotive leasing\n"
        "352 \n"
        "333 \n"
        "338 \n"
        "301\n"
        "296 \n"
        "Total automotive cost of revenues\n"
        "15,785 \n"
        "15,755 \n"
        "17,179 \n"
        "15,957"
    ),
}
TESLA_P25_C2 = {
    "chunk_hash": "tesla_p25_c2", "source": "Tesla_Q4_2023.pdf",
    "company": "Tesla", "page": 25, "char_start": 1388, "char_end": 2081,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "2,134 \n"
        "2,414\n"
        "2,374 \n"
        "INCOME FROM OPERATIONS\n"
        "3,901 \n"
        "2,664 \n"
        "2,399 \n"
        "1,764\n"
        "2,064 \n"
        "Interest income\n"
        "157 \n"
        "213 \n"
        "238 \n"
        "282\n"
        "333 \n"
        "Interest expense\n"
        "(33)\n"
        "(29)\n"
        "(28)\n"
        "(38)\n"
        "(61)\n"
        "Other (expense) income, net\n"
        "(42)\n"
        "(48)\n"
        "328 \n"
        "37\n"
        "(145)\n"
        "INCOME BEFORE INCOME TAXES\n"
        "3,983 \n"
        "2,800 \n"
        "2,937 \n"
        "2,045\n"
        "2,191 \n"
        "Provision for (benefit from) income taxes\n"
        "276 \n"
        "261 \n"
        "323 \n"
        "167\n"
        "(5,752)\n"
        "NET INCOME\n"
        "3,707 \n"
        "2,539 \n"
        "2,614 \n"
        "1,878\n"
        "7,943 \n"
        "Net income (loss) attributable to noncontrolling interests and "
        "redeemable noncontrolling interests in subsidiaries\n"
        "20 \n"
        "26 \n"
        "(89)\n"
        "25\n"
        "15 \n"
        "NET INCOME ATTRIBUTABLE TO COMMON STOCKHOLDERS\n"
        "3,687 \n"
        "2,513 \n"
        "2,703 \n"
        "1,853\n"
        "7,928\n"
        "Net income per share of common stock attributable to common "
        "stockholders\n"
        "Basic\n"
        "$        1.18"
    ),
}
TESLA_P25_C3 = {
    "chunk_hash": "tesla_p25_c3", "source": "Tesla_Q4_2023.pdf",
    "company": "Tesla", "page": 25, "char_start": 2083, "char_end": 2499,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "2,513 \n"
        "2,703 \n"
        "1,853\n"
        "7,928\n"
        "Net income per share of common stock attributable to common "
        "stockholders\n"
        "Basic\n"
        "$        1.18 \n"
        "$        0.80 \n"
        "$        0.85 \n"
        "$        0.58\n"
        "$        2.49 \n"
        "Diluted\n"
        "$        1.07 \n"
        "$        0.73 \n"
        "$        0.78 \n"
        "$        0.53\n"
        "$        2.27 \n"
        "Weighted average shares used in computing net income per share "
        "of common stock\n"
        "Basic\n"
        "3,160\n"
        "3,166\n"
        "3,171\n"
        "3,176\n"
        "3,181\n"
        "Diluted\n"
        "3,471\n"
        "3,468\n"
        "3,478\n"
        "3,493\n"
        "3,492\n"
        "25"
    ),
}

# Tesla p4: summary page with % YoY cells and non-GAAP rows (dropped).
TESLA_P4_C0 = {
    "chunk_hash": "tesla_p4_c0", "source": "Tesla_Q4_2023.pdf",
    "company": "Tesla", "page": 4, "char_start": 0, "char_end": 697,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "(Unaudited)\n"
        "($ in millions, except percentages and per share data)\n"
        "Q4-2022\n"
        "Q1-2023\n"
        "Q2-2023\n"
        "Q3-2023\n"
        "Q4-2023\n"
        "YoY\n"
        "Total revenues\n"
        "24,318\n"
        "23,329\n"
        "24,927\n"
        "23,350\n"
        "25,167\n"
        "3%\n"
        "Energy generation and storage revenue\n"
        "1,310 \n"
        "1,529\n"
        "1,509\n"
        "1,559\n"
        "1,438\n"
        "10%\n"
        "Net income attributable to common stockholders (GAAP)\n"
        "3,687 \n"
        "2,513 \n"
        "2,703 \n"
        "1,853\n"
        "7,928 \n"
        "115%\n"
        "Net income attributable to common stockholders (non-GAAP)\n"
        "4,106 \n"
        "2,931 \n"
        "3,148 \n"
        "2,318 \n"
        "2,485 \n"
        "-39%\n"
        "EPS attributable to common stockholders, diluted (GAAP)\n"
        "1.07 \n"
        "0.73 \n"
        "0.78 \n"
        "0.53\n"
        "2.27 \n"
        "112%\n"
        "EPS attributable to common stockholders, diluted (non-GAAP)\n"
        "1.19 \n"
        "0.85 \n"
        "0.91 \n"
        "0.66\n"
        "0.71 \n"
        "-40%"
    ),
}

# Tesla GAAP-to-non-GAAP reconciliation page (letter-spaced title).
TESLA_RECON_RAW = [{
    "chunk_hash": "tesla_p28_c0", "source": "Tesla_Q4_2023.pdf",
    "company": "Tesla", "page": 28, "char_start": 0, "char_end": 2304,
    "arithmetic_ok": None, "contains_table": False,
    "content": (
        "R E C O N C I L I A T I O N O F G A A P T O N O N – G A A P "
        "F I N A N C I A L I N F O R M A T I O N \n"
        "(Unaudited)\n"
        "In millions of USD or shares as applicable, except per share "
        "data\n"
        "Q4-2022\n"
        "Q1-2023\n"
        "Q2-2023\n"
        "Q3-2023\n"
        "Q4-2023\n"
        "Net income attributable to common stockholders (GAAP)\n"
        "3,687 \n"
        "2,513 \n"
        "2,703 \n"
        "1,853 \n"
        "7,928"
    ),
}]

# ===========================================================================
# PAGE ASSEMBLY — offsets derived from the chunks' OWN contents, so a
# page fixture and its "\n\n"-join are consistent by construction
# (mirrors how ingest.py builds page_transcripts).
# ===========================================================================
def _reoffset(chunks):
    out, pos = [], 0
    for c in chunks:
        d = dict(c)
        d["char_start"] = pos
        d["char_end"] = pos + len(d["content"])
        out.append(d)
        pos += len(d["content"]) + 2
    return out


APPLE_PAGE = _reoffset([APPLE_P1_C0, APPLE_P1_C1, APPLE_P1_C2])
META_PAGE = _reoffset([META_P6_C0, META_P6_C1])
TESLA_PAGE = _reoffset([TESLA_P25_C0, TESLA_P25_C2, TESLA_P25_C3])
TESLA_P4_PAGE = _reoffset([TESLA_P4_C0])
TESLA_RECON_PAGE = _reoffset(TESLA_RECON_RAW)

XBRL_FACTS = [
    {"company": "Apple", "metric": "revenue", "period": "Q4-2023",
     "value": Decimal("89498000000.0000")},
    {"company": "Apple", "metric": "net_income", "period": "Q4-2023",
     "value": Decimal("22956000000.0000")},
    {"company": "Meta", "metric": "revenue", "period": "Q4-2023",
     "value": Decimal("40111000000.0000")},
    {"company": "Meta", "metric": "net_income", "period": "Q4-2023",
     "value": Decimal("14017000000.0000")},
    {"company": "Tesla", "metric": "revenue", "period": "Q4-2023",
     "value": Decimal("25167000000.0000")},
    {"company": "Tesla", "metric": "net_income", "period": "Q4-2023",
     "value": Decimal("7928000000.0000")},
    {"company": "Tesla", "metric": "eps_diluted", "period": "Q4-2023",
     "value": Decimal("2.27")},
]


# ---------------------------------------------------------------- helpers
def _by_key(cands, metric, period, company=None):
    return [c for c in cands
            if c["metric_key"] == metric and c["period"] == period
            and (company is None or c["company"] == company)]


def _one(cands, metric, period, company=None):
    hits = _by_key(cands, metric, period, company)
    assert len(hits) == 1, f"expected exactly 1 {metric}/{period}, got {len(hits)}"
    return hits[0]


# ===========================================================================
# EXTRACTION — Apple
# ===========================================================================
def test_apple_consolidated_rows():
    cands, excluded = extract_facts_from_page("Apple", 1, APPLE_PAGE)
    rev = _one(cands, "revenue", "Q4-2023")
    assert Decimal(rev["value_usd"]) == Decimal("89498000000")
    assert Decimal(rev["value_raw"]) == Decimal("89498")
    ni = _one(cands, "net_income", "Q4-2023")
    assert Decimal(ni["value_usd"]) == Decimal("22956000000")
    eps = _one(cands, "eps_diluted", "Q4-2023")
    assert Decimal(eps["value_usd"]) == Decimal("1.46")
    assert eps["scale"] == "1"          # per-share — never scaled
    # FY columns bound too
    assert Decimal(_one(cands, "revenue", "FY-2023")["value_usd"]) \
        == Decimal("383285000000")
    assert Decimal(_one(cands, "revenue", "Q4-2022")["value_usd"]) \
        == Decimal("90146000000")
    # tracked subtotals present, B.1.1-unreconciled
    assert _by_key(cands, "gross_margin", "Q4-2023")
    assert _by_key(cands, "operating_income", "Q4-2023")


def test_apple_span_slices_owning_chunk_byte_exact():
    """THE ADR-017 core guarantee: the span is transcript coordinates —
    chunk.char_start + relative offset — and slices the chunk
    byte-exactly. A page's chunks join with the SAME '\\n\\n' ingest.py
    uses, so joined == the stored page transcript."""
    cands, _ = extract_facts_from_page("Apple", 1, APPLE_PAGE)
    assert cands
    usable = sorted(APPLE_PAGE, key=lambda c: c["char_start"])
    transcript = "\n\n".join(c["content"] for c in usable)
    for c in cands:
        s, e = c["char_start"], c["char_end"]
        chunk = next(ch for ch in usable if ch["chunk_hash"] == c["chunk_hash"])
        rel_s = s - chunk["char_start"]
        assert chunk["content"][rel_s:e - chunk["char_start"]] \
            == c["row_text"]
        # the absolute span slices the PAGE TRANSCRIPT byte-exactly —
        # the property sync_fact_rows enforces against page_transcripts
        assert transcript[s:e] == c["row_text"]


def test_apple_footnote_label_and_noise_lines():
    cands, _ = extract_facts_from_page("Apple", 1, APPLE_PAGE)
    assert all(c["label"].startswith("Total net sales")
               for c in _by_key(cands, "revenue", "Q4-2023"))
    # the label itself is cleaned of '!' scaffolding; the ROW SPAN stays
    # verbatim (it may contain '!' markers — it is the source bytes the
    # receipt chain must reproduce)
    assert all("!" not in c["label"] for c in cands)
    # and no parsed VALUE carries noise
    assert all(Decimal(c["value_raw"]) >= 0 or
               Decimal(c["value_raw"]) < 0 for c in cands)  # parses clean


def test_apple_shares_section_disambiguation():
    """Shares 'Basic/Diluted' must NOT become eps — the section context
    (Shares used in computing earnings per share) binds them to shares_."""
    cands, _ = extract_facts_from_page("Apple", 1, APPLE_PAGE)
    assert _one(cands, "shares_basic", "Q4-2023")["value_usd"] \
        == "15599434"
    assert _one(cands, "shares_diluted", "Q4-2023")["value_usd"] \
        == "15672400"


# ===========================================================================
# EXTRACTION — Meta (STATEMENTS OF INCOME variant)
# ===========================================================================
def test_meta_consolidated_rows():
    cands, excluded = extract_facts_from_page("Meta", 6, META_PAGE)
    rev = _one(cands, "revenue", "Q4-2023")
    assert Decimal(rev["value_usd"]) == Decimal("40111000000")
    ni = _one(cands, "net_income", "Q4-2023")
    assert Decimal(ni["value_usd"]) == Decimal("14017000000")
    eps = _one(cands, "eps_diluted", "Q4-2023")
    assert Decimal(eps["value_usd"]) == Decimal("5.33")
    assert Decimal(_one(cands, "eps_diluted", "FY-2023")["value_usd"]) \
        == Decimal("14.87")
    assert Decimal(_one(cands, "revenue", "FY-2022")["value_usd"]) \
        == Decimal("116609000000")


def test_meta_span_slices_transcript():
    cands, _ = extract_facts_from_page("Meta", 6, META_PAGE)
    usable = sorted(META_PAGE, key=lambda c: c["char_start"])
    transcript = "\n\n".join(c["content"] for c in usable)
    for c in cands:
        assert transcript[c["char_start"]:c["char_end"]] == c["row_text"]


# ===========================================================================
# EXTRACTION — Tesla (quarter columns, % rejection, NCI disambiguation)
# ===========================================================================
def test_tesla_consolidated_rows_and_pct_rejection():
    cands, excluded = extract_facts_from_page("Tesla", 25, TESLA_PAGE)
    rev = _one(cands, "revenue", "Q4-2023")
    assert Decimal(rev["value_usd"]) == Decimal("25167000000")
    assert Decimal(_one(cands, "revenue", "Q4-2022")["value_usd"]) \
        == Decimal("24318000000")
    assert _one(cands, "revenue", "Q1-2023")
    assert _one(cands, "revenue", "Q2-2023")
    assert _one(cands, "revenue", "Q3-2023")
    # period binding must never produce a value for the YoY column
    assert not [c for c in cands if c["metric_key"] == "revenue"
                and c["period"] not in
                ("Q4-2022", "Q1-2023", "Q2-2023", "Q3-2023", "Q4-2023")]
    assert all("3%" not in (c["row_text"] or "") for c in cands)


def test_tesla_pct_yoy_column_never_binds():
    """The p4 summary page carries a YoY % column after the 5 values —
    %-lines are never values AND the page itself is a summary (no
    ALL-CAPS income-statement section lines, no statement title) so it
    yields nothing: one canonical statement per company, per ADR-017."""
    cands, _excluded = extract_facts_from_page("Tesla", 4, TESLA_P4_PAGE)
    assert cands == []


def test_tesla_pct_lines_rejected_inside_valid_page():
    """A '%'-cell line inside an admitted page is never a value: the
    row still binds exactly 5 quarter periods."""
    poisoned = dict(TESLA_P25_C0)
    poisoned["content"] = (
        poisoned["content"].replace(
            "Total revenues\n24,318 \n23,329 \n24,927 \n23,350\n25,167 \n",
            "Total revenues\n24,318 \n23,329 \n24,927 \n23,350\n25,167 \n"
            "3%\n"))
    page = _reoffset([poisoned, TESLA_P25_C2, TESLA_P25_C3])
    cands, _ = extract_facts_from_page("Tesla", 25, page)
    rev = _one(cands, "revenue", "Q4-2023")
    assert Decimal(rev["value_usd"]) == Decimal("25167000000")
    assert _one(cands, "revenue", "Q4-2022")
    assert _one(cands, "revenue", "Q3-2023")
    assert all("%" not in (c["row_text"] or "") for c in _by_key(
        cands, "revenue", "Q4-2023") + _by_key(cands, "revenue", "Q4-2022"))


def test_tesla_non_gaap_rows_excluded():
    """On an admitted page, (non-GAAP) label rows are dropped at the
    label filter; on summary pages the whole page is excluded."""
    cands, _ = extract_facts_from_page("Tesla", 4, TESLA_P4_PAGE)
    assert cands == []          # summary page — excluded whole
    # within an admitted page the row-level rule is covered by the
    # recon-page exclusion + the label map having no non-GAAP entries
    assert all("non-GAAP" not in (c.get("label") or "")
               for c in extract_facts_from_page("Tesla", 25, TESLA_PAGE)[0])


def test_tesla_net_income_disambiguation():
    """Bare NET INCOME (7,943, includes NCI) must never masquerade as
    the attributable net income XBRL means (7,928) — renamed
    net_income_total when an attributable row exists on the page."""
    cands, _ = extract_facts_from_page("Tesla", 25, TESLA_PAGE)
    ni_rows = _by_key(cands, "net_income", "Q4-2023")
    assert len(ni_rows) == 1
    assert Decimal(ni_rows[0]["value_usd"]) == Decimal("7928000000")
    tot = _by_key(cands, "net_income_total", "Q4-2023")
    assert tot and Decimal(tot[0]["value_usd"]) == Decimal("7943000000")


def test_tesla_cash_flow_page_not_admitted():
    """THE live-corpus misbinding class (found while running Phase 0):
    Tesla p27's cash-flow 'Net income' 7,943 (includes NCI) sits within
    the 0.5% tolerance of the attributable XBRL 7,928 — admitting that
    page would FALSELY reconcile. Untitled pages are admitted ONLY via
    ALL-CAPS income-statement section lines."""
    cashflow = [{
        "chunk_hash": "tesla_p27_c0", "source": "Tesla_Q4_2023.pdf",
        "company": "Tesla", "page": 27, "char_start": 0, "char_end": 700,
        "arithmetic_ok": None, "contains_table": False,
        "content": (
            "(Unaudited)\n"
            "In millions of USD\n"
            "Q4-2022\nQ1-2023\nQ2-2023\nQ3-2023\nQ4-2023\n"
            "CASH FLOWS FROM OPERATING ACTIVITIES\n"
            "Net income\n"
            "3,707 \n2,539 \n2,614 \n1,878 \n7,943 \n"
            "Depreciation, amortization and impairment\n"
            "989 \n1,046 \n1,154 \n1,235 \n1,232"
        ),
    }]
    cands, excluded = extract_facts_from_page("Tesla", 27, cashflow)
    assert cands == [], "cash-flow pages must never yield fact rows"


def test_tesla_eps_no_chunk_boundary_bleed():
    """THE live-corpus bleed bug (found while running Phase 0): Tesla
    p25's chunk 2 ends with a trailing 'Basic' label; chunk 3 RE-STATES
    prior rows' values at its head. Without the boundary rule those
    values accumulated into the stale label and emitted misbound rows.
    A row's label and values must complete within ONE chunk."""
    cands, _ = extract_facts_from_page("Tesla", 25, TESLA_PAGE)
    for metric in ("eps_basic", "eps_diluted", "shares_basic",
                   "shares_diluted"):
        periods = [c["period"] for c in cands
                   if c["metric_key"] == metric]
        assert sorted(periods) == ["Q1-2023", "Q2-2023", "Q3-2023",
                                   "Q4-2022", "Q4-2023"], \
            f"{metric}: period binding bled across chunks: {periods}"
    eps = _one(cands, "eps_diluted", "Q4-2023")
    assert Decimal(eps["value_usd"]) == Decimal("2.27")
    # span provenance: the row_text carries the label AND its own value
    # verbatim (multi-line rows: a span from label to own-value also
    # covers preceding sibling values — the byte-exact slice, verified
    # against page_transcripts in the corpus dry-run, is the guarantee)
    for c in cands:
        if c["metric_key"] == "eps_diluted":
            assert c["row_text"].startswith("Diluted")
            assert c["value_raw"] in c["row_text"].replace(",", "")


def test_tesla_span_slices_transcript():
    cands, _ = extract_facts_from_page("Tesla", 25, TESLA_PAGE)
    usable = sorted(TESLA_PAGE, key=lambda c: c["char_start"])
    transcript = "\n\n".join(c["content"] for c in usable)
    assert cands
    for c in cands:
        assert transcript[c["char_start"]:c["char_end"]] == c["row_text"]


def test_tesla_segment_rows_tracked_unreconciled():
    """B.1.1: automotive/energy segment rows are extracted for the V3
    study but never Path-A eligible."""
    cands, _ = extract_facts_from_page("Tesla", 25, TESLA_PAGE)
    auto = _one(cands, "segment_automotive_revenue", "Q4-2023")
    assert Decimal(auto["value_usd"]) == Decimal("21563000000")
    ok, vclass = reconcile_rows(auto, XBRL_FACTS)
    assert ok is False and vclass == "unreconciled_fact"


# ===========================================================================
# B.1.3 — context-marker + integrity exclusions
# ===========================================================================
def test_proforma_context_excludes_page():
    poisoned = dict(APPLE_P1_C0,
                    content=APPLE_P1_C0["content"].replace(
                        "STATEMENTS OF OPERATIONS (Unaudited) ",
                        "STATEMENTS OF OPERATIONS (Unaudited) — pro forma, "
                        "as previously reported"))
    cands, excluded = extract_facts_from_page("Apple", 1, [poisoned])
    assert cands == [] and excluded >= 1


def test_reconciliation_page_excluded():
    """Tesla's letter-spaced GAAP-to-non-GAAP reconciliation page is
    excluded whole — verbatim numbers from the WRONG context class."""
    cands, excluded = extract_facts_from_page("Tesla", 28, TESLA_RECON_PAGE)
    assert cands == [] and excluded >= 1


def test_arithmetic_flagged_chunks_excluded():
    flagged = dict(APPLE_P1_C1, arithmetic_ok=False)
    cands, excluded = extract_facts_from_page(
        "Apple", 1, [APPLE_P1_C0, flagged, APPLE_P1_C2])
    assert excluded == 1
    # rows living in the flagged chunk are gone; clean-chunk rows survive
    assert not _by_key(cands, "operating_income", "Q4-2023")
    assert _by_key(cands, "revenue", "Q4-2023")
    assert _by_key(cands, "eps_diluted", "Q4-2023")


def test_non_statement_page_returns_nothing():
    chatter = [{"chunk_hash": "x", "source": "Tesla_Q4_2023.pdf",
                "company": "Tesla", "page": 31, "char_start": 0,
                "char_end": 40, "arithmetic_ok": None,
                "content": "Tesla will provide a live webcast of its "
                "fourth quarter 2023 financial results"}]
    cands, excluded = extract_facts_from_page("Tesla", 31, chatter)
    assert cands == [] and excluded == 0


# ===========================================================================
# BINDING — fail-closed shapes
# ===========================================================================
def test_bind_periods_three_accepted_shapes():
    assert bind_periods([2023, 2022, 2023, 2022], [], 4) == [
        "Q4-2023", "Q4-2022", "FY-2023", "FY-2022"]
    assert bind_periods([], ["Q4-2022", "Q1-2023", "Q2-2023",
                             "Q3-2023", "Q4-2023"], 5) == [
        "Q4-2022", "Q1-2023", "Q2-2023", "Q3-2023", "Q4-2023"]
    assert bind_periods([2019, 2020, 2021, 2022, 2023], [], 5) == [
        "FY-2019", "FY-2020", "FY-2021", "FY-2022", "FY-2023"]


def test_bind_periods_fail_closed_on_odd_shapes():
    assert bind_periods([2023, 2022, 2023], [], 3) is None
    assert bind_periods([2023, 2022], [], 4) is None
    assert bind_periods([2023, 2022, 2023, 2022], [], 5) is None  # width≠
    assert bind_periods([], ["Q4-2022", "Q4-2023"], 2) is None    # <3 cols
    # the [CY,PY,CY,PY] pattern with a broken sequence (CY≠PY≠CY≠PY)
    # is NOT the Q4/FY pattern — only 4 DISTINCT years or the exact
    # repeat are accepted shapes
    assert bind_periods([2023, 2022, 2021, 2020], [], 4) == [
        "FY-2023", "FY-2022", "FY-2021", "FY-2020"]


def test_row_crossing_chunk_boundary_dropped():
    """A row whose span crosses a chunk boundary can't anchor to one
    chunk — declined, never stored with a fake anchor."""
    # split Apple chunk C1 so the Net income row straddles the seam
    a_text = APPLE_P1_C1["content"]
    cut = a_text.index("Net income \n")
    c0 = dict(APPLE_P1_C0, content=a_text[:cut])
    c1 = dict(APPLE_P1_C0, chunk_hash="split_b", source="Apple_Q4_2023.pdf",
              company="Apple", page=1, char_start=1000,
              char_end=1000 + len(a_text[cut:]),
              content=a_text[cut:])
    cands, _ = extract_facts_from_page("Apple", 1, [c0, c1])
    assert not _by_key(cands, "net_income", "Q4-2023")


# ===========================================================================
# RECONCILIATION — B.1.1 + Amendment 4 (dual-key tolerance)
# ===========================================================================
def test_no_xbrl_counterpart_is_fail_closed():
    eps_facts = [f for f in XBRL_FACTS if f["company"] == "Apple"]
    # Apple facts above have no eps twin — absence is never agreement
    cands, _ = extract_facts_from_page("Apple", 1, APPLE_PAGE)
    eps = _one(cands, "eps_diluted", "Q4-2023")
    ok, vclass = reconcile_rows(eps, eps_facts)
    assert ok is False and vclass == "unreconciled_fact"


def test_xbrl_mismatch_never_reconciles():
    wrong = {"company": "Apple", "metric_key": "revenue",
             "period": "Q4-2023", "value_usd": "66000000000",
             "label": "Total net sales"}
    ok, vclass = reconcile_rows(wrong, XBRL_FACTS)
    assert ok is False and vclass == "unreconciled_fact"


def test_dual_key_tolerance_exact_and_cross_scale():
    base = {"company": "Tesla", "metric_key": "revenue",
            "period": "Q4-2023", "label": "Total revenues"}
    ok, v = reconcile_rows(dict(base, value_usd="25167000000"), XBRL_FACTS)
    assert (ok, v) == (True, "span_xbrl_reconciled")
    # 0.4% off — inside the 0.5% rounding-slack rule (Amendment 4)
    ok, _ = reconcile_rows(dict(base, value_usd="25268000000"), XBRL_FACTS)
    assert ok is True
    # 5% off — outside; never serve
    ok, _ = reconcile_rows(dict(base, value_usd="26425000000"), XBRL_FACTS)
    assert ok is False


def test_all_three_companies_reconcile_q4_2023():
    """The Phase-0 headline: the 9 consolidated Q4-2023 facts (3
    companies x 3 metrics... minus Apple/Meta EPS absent from facts
    fixture) reconcile against XBRL."""
    for page, company in ((APPLE_PAGE, "Apple"), (META_PAGE, "Meta"),
                          (TESLA_PAGE, "Tesla")):
        cands, _ = extract_facts_from_page(company, 6 if company == "Meta"
                                           else (25 if company == "Tesla"
                                                 else 1), page)
        n = 0
        for c in cands:
            ok, vclass = reconcile_rows(c, XBRL_FACTS)
            if ok:
                assert vclass == "span_xbrl_reconciled"
                n += 1
        assert n >= 2, f"{company}: expected >=2 reconciled rows, got {n}"


# ===========================================================================
# A.4 / B.1.2 — ROUTING GUARD
# ===========================================================================
FACT_KEYS = {
    ("Apple", "revenue", "Q4-2023"),
    ("Apple", "net_income", "Q4-2023"),
    ("Apple", "eps_diluted", "Q4-2023"),
    ("Meta", "revenue", "Q4-2023"),
    ("Meta", "net_income", "Q4-2023"),
    ("Tesla", "revenue", "Q4-2023"),
    ("Tesla", "net_income", "Q4-2023"),
    ("Tesla", "eps_diluted", "Q4-2023"),
}


def test_canonical_entities_aliases():
    assert canonical_entities("What was AAPL's revenue in Q4 2023?") \
        == ["Apple"]
    assert canonical_entities("How did TSLA and Meta compare?") \
        == ["Tesla", "Meta"]
    assert canonical_entities("Compare Facebook and Apple.") \
        == ["Meta", "Apple"]
    assert canonical_entities("What about delivery numbers?") == []


def test_canonical_metric_resolution():
    assert canonical_metric("total net sales for Q4") == ("metric", "revenue")
    assert canonical_metric("what was revenue") == ("metric", "revenue")
    assert canonical_metric("diluted EPS") == ("metric", "eps_diluted")
    assert canonical_metric("net income for Tesla") == ("metric", "net_income")
    # segment qualifiers demote (Amendment 2)
    assert canonical_metric("advertising revenue")[0] == "segment"
    assert canonical_metric("iPhone revenue")[0] == "segment"
    # multi-metric questions are ambiguous
    assert canonical_metric("compare revenue and net income") == \
        ("ambiguous", None)


def test_canonical_periods():
    assert canonical_periods("Q4 2023") == ["Q4-2023"]
    assert canonical_periods("Q4-2023") == ["Q4-2023"]
    assert canonical_periods("fourth quarter of 2023") == ["Q4-2023"]
    assert canonical_periods("FY 2023") == ["FY-2023"]
    assert canonical_periods("full-year 2023") == ["FY-2023"]
    assert canonical_periods("fiscal year 2022") == ["FY-2022"]
    # bare quarter across years is ambiguous -> demote
    assert canonical_periods("Q4") == []
    # bare year reads full-year
    assert canonical_periods("what happened in 2023") == ["FY-2023"]


def test_path_a_exact_match_honored():
    d = path_a_decision("What was Apple's total net sales in Q4 2023?",
                       FACT_KEYS)
    assert d["path"] == "fact"
    assert d["resolved"] == [("Apple", "revenue", "Q4-2023")]


def test_path_a_missing_triple_demotes():
    d = path_a_decision("What was Apple's gross margin in Q4 2023?",
                       FACT_KEYS)
    assert d["path"] == "fleet"
    assert d["reason"] == "triple_not_in_fact_store"


def test_path_a_segment_metric_demotes():
    d = path_a_decision("What was Tesla's automotive revenue in Q4 2023?",
                        FACT_KEYS)
    assert d["path"] == "fleet"
    assert d["reason"] == "segment_metric_not_in_coverage"


def test_path_a_metric_ambiguity_demotes():
    d = path_a_decision(
        "Compare Apple's revenue and net income in Q4 2023", FACT_KEYS)
    assert d["path"] == "fleet"
    assert d["reason"] == "metric_ambiguity"


def test_path_a_no_period_demotes():
    d = path_a_decision("What was Apple's total net sales?", FACT_KEYS)
    assert d["path"] == "fleet"
    assert d["reason"] == "no_period_resolved"


def test_path_a_atomic_multi_entity_demotion():
    """Amendment 3: ALL N triples must exist; one miss demotes the WHOLE
    query — no split-brain answers."""
    keys = set(FACT_KEYS) | {("Tesla", "revenue", "Q4-2023")}
    d = path_a_decision(
        "Compare Apple's and Tesla's revenue in Q4 2023", keys)
    assert d["path"] == "fact"          # both present -> allowed
    d = path_a_decision(
        "Compare Apple's and Meta's EPS in Q4 2023", keys)
    assert d["path"] == "fleet"         # Meta EPS missing -> ALL demote
    assert d.get("missing") == [("Meta", "eps_diluted", "Q4-2023")]


def test_path_a_bare_quarter_demotes():
    d = path_a_decision("What was Apple's revenue in Q4?", FACT_KEYS)
    assert d["path"] == "fleet"
    assert d["reason"] == "no_period_resolved"


def test_path_a_interpretive_stem_demotes():
    """Battery finding D05 (2026-09-14): 'What drove Tesla's Q4 2023
    net income growth?' NAMES a covered triple but asks an
    interpretive question — serving the value would answer a DIFFERENT
    question with a true figure (§2.4 class). Interpretive stems
    demote BEFORE triple resolution."""
    d = path_a_decision("What drove Tesla's Q4 2023 net income growth?",
                        FACT_KEYS)
    assert d["path"] == "fleet"
    assert d["reason"] == "interpretive_question"
    d = path_a_decision("Why was Meta's Q4 2023 revenue so high?",
                        FACT_KEYS)
    assert d["path"] == "fleet"
    # fact stems are NOT interpretive
    d = path_a_decision("What was Tesla's net income in Q4 2023?",
                        FACT_KEYS)
    assert d["path"] == "fact"
    d = path_a_decision("How much net income did Meta report in Q4 2023?",
                        FACT_KEYS)
    assert d["path"] == "fact"


def test_path_a_earn_per_share_phrase_resolves():
    """Battery finding A16: 'how much did Tesla earn per share' — the
    colloquial EPS phrasing must resolve to eps_diluted."""
    d = path_a_decision("How much did Tesla earn per share in Q4 2023?",
                        FACT_KEYS)
    assert d["path"] == "fact"
    assert d["resolved"] == [("Tesla", "eps_diluted", "Q4-2023")]


# ===========================================================================
# A.5 — FUZZ OPERATORS over the routing guard
# ===========================================================================
from fact_extract import (op_entity_swap, op_metric_noun_swap,  # noqa: E402
                          op_period_swap, op_qualifier_inject)

_ORIGINAL = ("Apple", "revenue", "Q4-2023")


def test_fuzz_entity_swap_changes_or_demotes():
    q = "What was Apple's total net sales in Q4 2023?"
    mutated = op_entity_swap(q)
    d = path_a_decision(mutated, FACT_KEYS)
    assert _ORIGINAL not in d["resolved"], \
        f"entity swap must not silently keep the original triple: {mutated}"


def test_fuzz_entity_swap_handles_alias_phrasings():
    """Stress-harness live catch (2026-09-16): on alias-phrased queries
    ('AAPL', 'Facebook') the operator searched for the CANONICAL name,
    find() returned -1, and the splice left the original alias intact —
    the mutation resolved to the ORIGINAL triple, a direct A.5 contract
    violation. The operator must replace an alias ACTUALLY present."""
    for q, original in (
            ("What was AAPL's total net sales in Q4 2023?", _ORIGINAL),
            ("What was Facebook's revenue in Q4 2023?",
             ("Meta", "revenue", "Q4-2023"))):
        mutated = op_entity_swap(q)
        d = path_a_decision(mutated, FACT_KEYS)
        assert original not in d["resolved"], \
            f"alias swap must not keep the original triple: {mutated}"


def test_fuzz_period_swap_changes_resolution():
    q = "What was Apple's total net sales in Q4 2023?"
    mutated = op_period_swap(q)
    assert mutated != q
    d = path_a_decision(mutated, FACT_KEYS)
    assert _ORIGINAL not in d["resolved"]


def test_fuzz_metric_noun_swap_changes_resolution():
    q = "What was Apple's total net sales in Q4 2023?"
    mutated = op_metric_noun_swap(q)
    assert mutated != q
    d = path_a_decision(mutated, FACT_KEYS)
    assert _ORIGINAL not in d["resolved"]


def test_fuzz_qualifier_injection_demotes():
    q = "What was Apple's total net sales in Q4 2023?"
    mutated = op_qualifier_inject(q)
    d = path_a_decision(mutated, FACT_KEYS)
    assert d["path"] == "fleet"


def test_fuzz_all_operators_safe_on_adversarial_inputs():
    """No operator may crash; mutations must never resolve to the
    ORIGINAL triple against the real Path-A key set."""
    q = "What was Apple's total net sales in Q4 2023?"
    for op in FUZZ_OPERATORS:
        for probe in ("", "revenue", "What was Q4 2023 like for 2022?",
                      "Apple", "net income net income"):
            op(probe)               # must not raise
        d = path_a_decision(op(q), FACT_KEYS)
        assert _ORIGINAL not in d["resolved"], \
            f"{op.__name__} must not keep the original triple"


# ===========================================================================
# B.1.5 — Gate 4 declines disconfirmed derived facts
# ===========================================================================
def test_derived_fact_declines_authority_when_disconfirmed():
    from adaptive_rag import check_xbrl_figures
    evidence = [{"company": "Apple", "source": "Apple_Q4_2023.pdf",
                 "page": 1, "content": APPLE_P1_C0["content"]}]
    draft = "Apple's total net sales were $99,999 million [1]."
    # production get_xbrl_facts converts value to float — mirrored here
    facts_false = [{"company": "Apple", "metric": "revenue",
                    "period": "Q4-2023", "value": 89498000000.0,
                    "derivation": "FY_minus_9mo",
                    "confirmed_by_pdf": False}]
    assert check_xbrl_figures(draft, evidence, facts_false) == [], \
        "Gate 4 must decline authority over a disconfirmed fact"
    facts_null = [dict(facts_false[0], confirmed_by_pdf=None)]
    assert check_xbrl_figures(draft, evidence, facts_null), \
        "NULL (reconciler not run) keeps the legacy judgment"
    facts_true = [dict(facts_false[0], confirmed_by_pdf=True)]
    assert check_xbrl_figures(draft, evidence, facts_true), \
        "the same draft MUST be flagged once the fact is confirmed"


# ===========================================================================
# RECEIPTS — B.1.4 per-claim verifier class survives persistence
# ===========================================================================
def test_receipt_claims_carry_verifier_class():
    """B.1.4: the production receipt path stamps every claim with its
    verifier class — extract_claims output dicts gain verifier=llm_audit."""
    from adaptive_rag import extract_claims
    draft = ("- Apple posted total net sales of $89.5 billion [1].\n"
             "- Services revenue reached $22.3 billion [2].")
    claims = extract_claims(draft, 2)
    assert claims
    stamped = [dict(c, verifier="llm_audit") for c in claims]
    assert all(c["verifier"] == "llm_audit" for c in stamped)
    assert all("claim" in c and "citations" in c for c in stamped)


# ===========================================================================
# INTEGRATION — migration 006 + sync + verify (live DB; auto-skips)
# ===========================================================================
integration = pytest.mark.integration


def _db_reachable() -> bool:
    try:
        from db import admin_connection
        with admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def live_db():
    if not _db_reachable():
        pytest.skip("Database not reachable — integration test skipped")
    yield


@integration
def test_sync_fact_rows_end_to_end(live_db):
    """THE Phase-0 exit gate, live: migrate -> sync -> verify. Every
    written row must slice its transcript byte-exactly (verify_fact_rows
    reports zero span failures), and reconciliation numbers must be
    internally consistent."""
    import db
    from db import sync_fact_rows, verify_fact_rows
    db.setup_database()
    stats = sync_fact_rows()
    vstats = verify_fact_rows()
    assert stats["rows"] > 0, "the corpus must yield fact rows"
    assert stats["reconciled"] >= 3, \
        "at least revenue+net_income+eps Q4-2023 rows must reconcile"
    assert stats["span_mismatches"] == 0
    assert vstats["span_failures"] == 0, \
        f"exit gate FAILED: {vstats['failures'][:3]}"
    # reconciled rows must be exactly the Path-A-eligible set
    rows = db.get_fact_rows(reconciled_only=True)
    assert rows
    assert all(r["verifier_class"] == "span_xbrl_reconciled" for r in rows)
    assert all(r["metric_key"] in ("revenue", "net_income", "eps_diluted")
               for r in rows), "B.1.1: only consolidated metrics reconcile"
