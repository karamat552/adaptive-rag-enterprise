"""
Unit & Scale Assertion Engine — Tests (offline, zero tokens)
=============================================================
Roadmap #3 (consult consensus: the most catastrophic financial hallucination
class is a right number at the wrong scale — "$40.111 billion" over an
'in millions' table is a 1000× lie that reads fluently).

Covers:
- parse_declared_units over REAL corpus table-chunk phrasing ('($ in
  millions, except percentages and per share data)').
- assert_claim_scales: honest re-scales pass ($89.5B over 89,498 in-millions),
  derived sums pass (segment totals), and off-by-1000 lies FAIL with the
  direction recorded.
- Guard wiring: fact_checker_guard fail-closes on a scale lie BEFORE the
  LLM auditor (spy proves zero auditor tokens spent) and records the issue.

Run:  pytest tests/test_units.py -v
"""
import asyncio
import os
from pathlib import Path

if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402


# Real corpus table-chunk shape (Tesla_Q4_2023.pdf p4, verbatim structure).
TESLA_TABLE = """TABLE:
Total automotive revenues :: Q4-2022=21,307 | Q4-2023=21,563 | YoY=1%
Energy generation and storage revenue :: Q4-2022=1,310 | Q4-2023=1,438 | YoY=10%
| ($ in millions, except percentages and per share data) | Q4-2022 | Q4-2023 | YoY |
| Total automotive revenues | 21,307 | 21,563 | 1% |
| Energy generation and storage revenue | 1,310 | 1,438 | 10% |"""

APPLE_TABLE = "TABLE:\nProducts :: Q4=43,807\nServices :: Q4=22,314\n| ($ in millions) |"


def _ev(text, **over):
    e = {"chunk_hash": "h" * 64, "company": "Tesla", "source": "Tesla_Q4_2023.pdf",
         "page": 4, "content": text, "char_start": 0, "char_end": len(text),
         "transcript_version": 1, "contains_table": True, "arithmetic_ok": None}
    e.update(over)
    return e


# ============================== units declaration =========================
def test_parse_in_millions_declaration():
    from adaptive_rag import parse_declared_units
    u = parse_declared_units(TESLA_TABLE)
    assert u == {"money_scale": 1e6, "per_share_exception": True}


def test_parse_plain_in_millions():
    from adaptive_rag import parse_declared_units
    assert parse_declared_units(APPLE_TABLE)["money_scale"] == 1e6
    assert parse_declared_units(
        "TABLE:\n| ($ in billions) | x |")["money_scale"] == 1e9
    assert parse_declared_units(
        "TABLE:\n| ($ in thousands) | x |")["money_scale"] == 1e3


def test_parse_no_declaration_declines_to_judge():
    from adaptive_rag import parse_declared_units
    # Prose evidence / pre-2.1 rows: no declared scale -> None -> the engine
    # never judges (declining is not certifying).
    assert parse_declared_units(
        "Revenue grew strongly this quarter across all segments.")["money_scale"] is None


# ============================== honest claims pass =========================
def test_exact_table_figure_passes():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    assert assert_claim_scales(
        "Total automotive revenues reached $21,563 million in Q4 2023 [1].",
        ev) == []


def test_legitimate_rescale_to_billions_passes():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    # 21,563 in-millions rendered as $21.6B (rounded) — legitimate.
    assert assert_claim_scales(
        "Total automotive revenues reached $21.6 billion in Q4 2023 [1].",
        ev) == []


def test_derived_segment_sum_passes():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(APPLE_TABLE)]
    # 43,807 + 22,314 = 66,121 — a total derived from cited member rows.
    assert assert_claim_scales(
        "Total net sales reached $66,121 million in Q4 2023 [1].", ev) == []


def test_plain_dollar_figure_over_millions_table_passes():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    # '$21,563' with no suffix: table-unit figure quoted verbatim.
    assert assert_claim_scales(
        "Total automotive revenues were $21,563 in Q4 2023 [1].", ev) == []


# ============================== scale lies fail ===========================
def test_off_by_1000_billions_lie_fails():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    issues = assert_claim_scales(
        "Total automotive revenues reached $21,563 billion in Q4 2023 [1].",
        ev)
    assert len(issues) == 1, "a 1000x lie must never pass the scale gate"
    assert issues[0]["claimed"] > issues[0]["nearest_evidence"], \
        "direction: claimed exceeds every reconstructable evidence value"


def test_inverted_scale_lie_fails():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    # '$21.6 million' for a 21,563-million figure — orders of magnitude
    # too small; still caught (any non-reconstructable value fails).
    issues = assert_claim_scales(
        "Total automotive revenues were only $21.6 million in Q4 2023 [1].",
        ev)
    assert len(issues) == 1, "the 1000x-too-small lie must also be caught"


def test_unrelated_magnitude_fails():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    issues = assert_claim_scales(
        "Total automotive revenues reached $990 billion in Q4 2023 [1].", ev)
    assert len(issues) == 1
    assert issues[0]["suspected"] == "scale mismatch"


def test_no_citation_no_judgment():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    # Uncited figures are the receipt's 'uncited claim' problem, not a scale
    # judgment — assert nothing, judge nothing.
    assert assert_claim_scales(
        "Some number somewhere is $99 billion.", ev) == []


def test_undeclared_scale_evidence_not_judged():
    from adaptive_rag import assert_claim_scales
    ev = [_ev("Cash flow was strong. Revenue grew.")]   # no units declaration
    assert assert_claim_scales(
        "Revenue reached $40 billion in Q4 2023 [1].", ev) == []


# ============================== guard wiring ===============================
def test_guard_fail_closes_on_scale_lie_before_auditor(monkeypatch):
    """A 1000× lie must be rejected by the DETERMINISTIC gate with ZERO
    auditor tokens spent — same contract as the citation pre-audit."""
    import adaptive_rag as ar

    auditor_calls = {"n": 0}

    class _Audit:
        grounded = True
        explanation = None

    async def _spy_llm(runnable, messages, stage, **kw):
        auditor_calls["n"] += 1
        return _Audit(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "save_verification_receipt",
                        lambda *a, **k: True)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)

    state = {
        "original_question": "What was Tesla automotive revenue?",
        "search_query": "tesla automotive revenue",
        "documents": ["Tesla | Tesla_Q4_2023.pdf | Page 4\n" + TESLA_TABLE],
        "evidence_records": [_ev(TESLA_TABLE)],
        "final_executive_report":
            "Total automotive revenues reached $21,563 billion [1].",
        "degraded_agents": [], "retry_count": 0, "run_id": "scale-t",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is False
    assert upd["outcome"] == "unverified_system"
    assert upd["scale_issues"], "the scale finding must ride the state"
    assert upd["scale_issues"][0]["claimed"] > \
        upd["scale_issues"][0]["nearest_evidence"], "direction recorded"
    assert auditor_calls["n"] == 0, \
        "scale lies must fail BEFORE the LLM auditor — zero tokens"


def test_guard_certifies_honest_scale(monkeypatch):
    import adaptive_rag as ar

    class _Audit:
        grounded = True
        explanation = None

    async def _spy_llm(runnable, messages, stage, **kw):
        return _Audit(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "save_verification_receipt",
                        lambda *a, **k: True)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)

    state = {
        "original_question": "What was Tesla automotive revenue?",
        "search_query": "tesla automotive revenue",
        "documents": ["Tesla | Tesla_Q4_2023.pdf | Page 4\n" + TESLA_TABLE],
        "evidence_records": [_ev(TESLA_TABLE)],
        "final_executive_report":
            "Total automotive revenues reached $21,563 million [1].",
        "degraded_agents": [], "retry_count": 0, "run_id": "scale-ok",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is True
    assert "scale_issues" not in upd or not upd.get("scale_issues")


def test_dense_real_chunk_no_memory_explosion():
    """Live-run regression (2026-09): the pairwise-sums loop once appended
    into the list it iterated -> MemoryError on real 40-figure chunks. A
    dense chunk must pass through the engine safely and quickly."""
    import time
    from adaptive_rag import assert_claim_scales
    # 40 comma-grouped figures + declaration, like a dense quarterly table.
    rows = "\n".join(
        f"Line item {i:02d} :: Q4-2023={i * 7123:,} | YoY=5%" for i in range(1, 41))
    dense = f"TABLE:\n{rows}\n| ($ in millions) |"
    ev = [_ev(dense)]
    t0 = time.perf_counter()
    issues = assert_claim_scales(
        "Line item 01 was $7,123 million in Q4 2023 [1].", ev)
    dt = time.perf_counter() - t0
    assert dt < 5.0, f"dense chunk must not explode (took {dt:.1f}s)"
    assert issues == [], f"verbatim table figure must reconstruct: {issues[:1]}"


# ============================== Q2 live false-positive regressions ========
def test_buyback_authorization_not_judged():
    """Live-found FP (2026-09-05): '$50B increase to share-repurchase
    authorization' cited against an in-millions table — legitimate
    non-period figure, must not be scale-judged."""
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    assert assert_claim_scales(
        "The board approved a $50 billion increase to the share-repurchase "
        "authorization [1].", ev) == []


def test_market_cap_context_not_judged():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    assert assert_claim_scales(
        "At a market cap of $969 billion, Meta trades at a premium [1].",
        ev) == []


def test_period_figure_still_judged_after_scope_fix():
    """The scope restriction must NOT weaken the core protection: a period-
    anchored figure is still judged (the lie still fails)."""
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    issues = assert_claim_scales(
        "Total automotive revenues reached $21,563 billion in Q4 2023 [1].", ev)
    assert len(issues) == 1, "period-anchored lies must still be caught"


def test_free_floating_figure_not_judged():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    assert assert_claim_scales(
        "The company also mentioned a $99 billion figure somewhere [1].",
        ev) == []
