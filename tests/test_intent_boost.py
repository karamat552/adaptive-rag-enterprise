"""THE INTENT BOOST (KNOWN_ISSUES #8, narrowed) — when the query asks for
a TOTAL figure, the consolidated-line chunk is promoted above the segment
breakdowns. Pinned: the boost fires only when the intent keyword is
present, promotes the right chunks, and is a no-op otherwise.

Offline: the pure function — zero tokens, zero network, zero DB.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from adaptive_rag import _intent_boost


def _chunk(text: str) -> dict:
    return {"content": text, "company": "Apple", "source": "Apple_Q4_2023.pdf",
            "page": 1, "chunk_hash": f"h_{hash(text) % 10000}"}


def test_total_query_promotes_consolidated_line():
    """A 'total' query: the consolidated-line chunk jumps to the front,
    above the segment-table chunks that textually outrank it."""
    query = "What was Apple's total net sales in Q4 2023?"
    chunks = [
        _chunk("Americas segment net sales: $40,115 million"),
        _chunk("Total net sales: $89,498 million"),
        _chunk("Europe segment net sales: $22,463 million"),
    ]
    result = _intent_boost(query, chunks)
    assert "Total net sales" in result[0]["content"], \
        "the consolidated-line chunk must be promoted to the front"


def test_intent_boost_noop_without_total():
    """Without the 'total' intent keyword, the chunks pass through
    unchanged — the boost is targeted, not a blanket reorder."""
    query = "What was Apple's operating income in Q4 2023?"
    chunks = [
        _chunk("Operating income: $26,969 million"),
        _chunk("Total net sales: $89,498 million"),
    ]
    result = _intent_boost(query, chunks)
    assert result == chunks


def test_intent_boost_noop_without_matching_chunks():
    """The intent keyword is present but no chunk contains the
    consolidated-line phrase — the boost is a no-op (nothing to promote)."""
    query = "What was Apple's total revenue by region in Q4 2023?"
    chunks = [
        _chunk("Americas segment: $40,115 million"),
        _chunk("Europe segment: $22,463 million"),
    ]
    result = _intent_boost(query, chunks)
    assert result == chunks


def test_multiple_consolidated_chunks_all_promoted():
    """Multiple matching chunks: all are promoted together, preserving
    their relative order among themselves."""
    query = "What was Apple's total revenue in Q4 2023?"
    chunks = [
        _chunk("Total revenues: $89,498 million"),
        _chunk("Americas segment: $40,115 million"),
        _chunk("Total revenue from products: $69,367 million"),
    ]
    result = _intent_boost(query, chunks)
    promoted = [c for c in result if "total revenue" in c.get("content", "").lower()
                or "total revenues" in c.get("content", "").lower()]
    assert len(promoted) == 2, "both consolidated chunks must be promoted"
    assert all(result.index(c) < result.index(rest)
               for c in promoted for rest in result if rest not in promoted)
