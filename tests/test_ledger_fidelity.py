"""
Deterministic Sources Ledger — Unit Tests (offline, zero tokens)
================================================================
The citation-ledger fidelity gap, live-observed 2026-10-03: a certified brief
cited 【13】/【14】 in prose while its own 'Verified Sources Ledger' enumerated
only 10 rows. The citations were IN RANGE — the bounds gate was correct and
nothing was fabricated. The defect was provenance: rule 2 of the synthesis
prompt asked the MODEL to enumerate its own sources, making the ledger the one
part of a certified answer that was neither derived from nor verified against
the evidence list.

The fix derives the ledger from `evidence_records` (the same records the
receipt carries), driven by the citation indices the brief actually uses, so
ledger and citations cannot disagree by construction.

These tests pin BOTH halves:
  - the detector fires on the real failure shape (otherwise the check is
    theatre), and
  - the rebuild removes the disagreement and is idempotent.

Run:  pytest tests/test_ledger_fidelity.py -v
"""
import os
from pathlib import Path

# Same dummy-guard pattern as tests/test_guard_preaudit.py (bare CI).
if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"


def _records(n: int):
    return [{"company": f"Company{i}", "source": f"doc{i}.pdf", "page": i,
             "chunk_hash": f"hash{i}"} for i in range(1, n + 1)]


def _observed_failure_draft() -> str:
    """Faithful reconstruction of the live 2026-10-03 output: prose cites
    【13】/【14】, the model's own ledger enumerates rows 1..10."""
    body = "## Executive Summary\n\n" + "\n".join(
        f"- Metric {i} moved in the quarter 【{i}】." for i in range(1, 13))
    body += "\n\nThe two headline figures are 【13】 and 【14】.\n"
    ledger = ("\n### Verified Sources Ledger\n\n"
              "| Footnote | Company | Document & Page |\n|---|---|---|\n"
              + "\n".join(f"| 【{i}】 | Company{i} | doc{i}.pdf, Page {i} |"
                          for i in range(1, 11)))
    return body + "\n" + ledger


# ========================= detector fires on the real shape =================
def test_detector_flags_the_observed_mismatch():
    """The check must go RED on the shape production actually emitted."""
    from adaptive_rag import ledger_fidelity_gap

    gap = ledger_fidelity_gap(_observed_failure_draft())

    assert gap is not None, "mismatch of 14 cited vs 10 listed went undetected"
    assert "14" in gap and "10" in gap, f"gap must name both counts: {gap}"


def test_detector_flags_a_missing_ledger():
    from adaptive_rag import ledger_fidelity_gap

    assert ledger_fidelity_gap("Revenue grew [1]. No ledger follows.") is not None


def test_detector_accepts_a_faithful_ledger():
    from adaptive_rag import ledger_fidelity_gap, rebuild_verified_ledger

    fixed = rebuild_verified_ledger(_observed_failure_draft(), _records(14))

    assert ledger_fidelity_gap(fixed) is None


# ============================ the rebuild itself ============================
def test_rebuild_enumerates_exactly_the_cited_indices():
    from adaptive_rag import ledger_row_indices, rebuild_verified_ledger
    import adaptive_rag as ar

    fixed = rebuild_verified_ledger(_observed_failure_draft(), _records(14))
    body, ledger = ar.split_ledger(fixed)

    assert ledger_row_indices(ledger) == ar.cited_indices(body) == list(range(1, 15))


def test_rebuild_is_idempotent():
    from adaptive_rag import rebuild_verified_ledger

    once = rebuild_verified_ledger(_observed_failure_draft(), _records(14))
    twice = rebuild_verified_ledger(once, _records(14))

    assert once == twice, "a second certify pass must not keep appending"


def test_rebuild_does_not_touch_the_prose_body():
    from adaptive_rag import rebuild_verified_ledger
    import adaptive_rag as ar

    draft = _observed_failure_draft()
    body_before, _ = ar.split_ledger(draft)
    body_after, _ = ar.split_ledger(rebuild_verified_ledger(draft, _records(14)))

    assert body_after.strip() == body_before.strip()


def test_rebuild_carries_company_and_page_from_the_records():
    from adaptive_rag import rebuild_verified_ledger, split_ledger

    _, ledger = split_ledger(
        rebuild_verified_ledger(_observed_failure_draft(), _records(14)))

    assert "Company13" in ledger and "doc13.pdf, Page 13" in ledger
    assert "Company14" in ledger and "doc14.pdf, Page 14" in ledger


def test_rebuild_never_fabricates_a_source_for_a_missing_record():
    """Fewer records than cited indices must degrade to an explicit unknown,
    never to a plausible-looking company/page."""
    from adaptive_rag import rebuild_verified_ledger, split_ledger

    _, ledger = split_ledger(
        rebuild_verified_ledger(_observed_failure_draft(), _records(2)))

    assert "unknown" in ledger.lower(), "missing record must be named as unknown"
    assert "doc13.pdf" not in ledger


def test_rebuild_handles_a_brief_that_cites_nothing():
    from adaptive_rag import (ledger_fidelity_gap, rebuild_verified_ledger,
                              split_ledger)

    fixed = rebuild_verified_ledger("A brief with no citations.", [])

    assert ledger_fidelity_gap(fixed) is None
    assert "No indexed evidence was cited" in split_ledger(fixed)[1]


def test_rebuild_appends_a_ledger_when_the_model_omitted_one():
    from adaptive_rag import ledger_fidelity_gap, rebuild_verified_ledger

    fixed = rebuild_verified_ledger("Revenue grew [1] and [2].", _records(3))

    assert ledger_fidelity_gap(fixed) is None


def test_ascii_citation_brackets_are_honoured():
    """Synthesis emits both [n] and 【n】 depending on the fleet model."""
    from adaptive_rag import ledger_fidelity_gap, rebuild_verified_ledger

    fixed = rebuild_verified_ledger("Revenue grew [3] and [7].", _records(8))

    assert ledger_fidelity_gap(fixed) is None
