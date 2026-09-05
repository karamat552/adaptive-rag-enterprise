"""
Growth-Claim Consistency (#4) + XBRL Figure Crosscheck (#5) — Tests
====================================================================
Offline, zero tokens. Both are deterministic pre-audit gates in
fact_checker_guard (ADR-010 / ADR-011).

Growth gate: the draft's 'X grew/declined N%' claims must agree in DIRECTION
with the company's own comparative columns (Phase B pair rows). Magnitude
checks deliberately stay with the LLM audit (restatements).

XBRL gate: consolidated $-figures vs SEC-published structured facts.
Curated-concept only; segment claims are skipped; missing facts = judge-not.

Run:  pytest tests/test_consistency.py tests/test_xbrl.py -v
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


# Real corpus table-chunk shape (Tesla p4, verbatim structure).
TESLA_TABLE = """TABLE:
Total automotive revenues :: Q4-2022=21,307 | Q4-2023=21,563 | YoY=1%
Energy generation and storage revenue :: Q4-2022=1,310 | Q4-2023=1,438 | YoY=10%
| ($ in millions, except percentages and per share data) | Q4-2022 | Q4-2023 | YoY |
| Total automotive revenues | 21,307 | 21,563 | 1% |"""


def _ev(text, company="Tesla", **over):
    e = {"chunk_hash": "h" * 64, "company": company,
         "source": "Tesla_Q4_2023.pdf", "page": 4, "content": text,
         "char_start": 0, "char_end": len(text), "transcript_version": 1,
         "contains_table": True, "arithmetic_ok": None}
    e.update(over)
    return e


# ============================== comparative pairs =========================
def test_comparative_pairs_parse_from_pair_rows():
    from adaptive_rag import find_comparative_pairs
    pairs = find_comparative_pairs([_ev(TESLA_TABLE)])
    labels = {p["label"] for p in pairs}
    assert "total automotive revenues" in labels
    auto = next(p for p in pairs if "automotive" in p["label"])
    assert auto["prior"] == 21307.0 and auto["current"] == 21563.0


def test_grid_only_evidence_yields_no_pairs():
    # Balance-sheet (grid-only) chunks have no pair rows -> judge-not.
    from adaptive_rag import find_comparative_pairs
    assert find_comparative_pairs([_ev("TABLE:\n| Cash | 23,460 |")]) == []


# ============================== growth direction ==========================
def test_honest_growth_claim_passes():
    from adaptive_rag import check_growth_claims
    # Automotive 21,307 -> 21,563 IS up; 'grew 1%' agrees.
    assert check_growth_claims(
        "Total automotive revenues grew 1 percent in Q4 2023 [1].",
        [_ev(TESLA_TABLE)]) == []


def test_direction_lie_fails():
    from adaptive_rag import check_growth_claims
    # The evidence shows automotive UP; claiming 'declined' is a lie about
    # the company's own numbers.
    issues = check_growth_claims(
        "Total automotive revenues declined 2 percent in Q4 2023 [1].",
        [_ev(TESLA_TABLE)])
    assert len(issues) == 1
    assert issues[0]["claimed_direction"] == "down"
    assert issues[0]["evidence_direction"] == "up"
    assert issues[0]["witness_pair"]["prior"] == 21307.0


def test_honest_decline_claim_passes():
    from adaptive_rag import check_growth_claims
    # Energy 1,310 -> 1,438 is up; craft an honest 'energy grew' claim.
    assert check_growth_claims(
        "Energy generation and storage revenue grew 10 percent [1].",
        [_ev(TESLA_TABLE)]) == []


def test_no_matching_pair_declines_to_judge():
    from adaptive_rag import check_growth_claims
    # 'Headcount grew 5%' with no headcount comparative pair -> judge-not.
    assert check_growth_claims(
        "Global headcount grew 5 percent in Q4 2023 [1].",
        [_ev(TESLA_TABLE)]) == []


def test_non_metric_subject_ignored():
    from adaptive_rag import check_growth_claims
    # 'the team grew 40%' — not a financial metric; not judged.
    assert check_growth_claims(
        "the engineering team grew 40 percent this year [1].",
        [_ev(TESLA_TABLE)]) == []


# ============================== guard wiring (#4) =========================
def test_guard_fail_closes_on_direction_lie(monkeypatch):
    import adaptive_rag as ar
    auditor_calls = {"n": 0}

    class _Audit:
        grounded = True

    async def _spy_llm(runnable, messages, stage):
        auditor_calls["n"] += 1
        return _Audit(), ar.UsageCollector()

    async def _no_xbrl(*a, **k):
        return []

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "get_xbrl_facts", _no_xbrl)
    monkeypatch.setattr(ar, "save_verification_receipt", lambda *a, **k: True)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)

    state = {
        "original_question": "How did Tesla automotive revenue trend?",
        "search_query": "tesla automotive revenue",
        "documents": ["Tesla | Tesla_Q4_2023.pdf | Page 4\n" + TESLA_TABLE],
        "evidence_records": [_ev(TESLA_TABLE)],
        "final_executive_report":
            "Total automotive revenues declined 2 percent in Q4 2023 [1].",
        "degraded_agents": [], "retry_count": 0, "run_id": "growth-t",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is False
    assert upd["outcome"] == "unverified_system"
    assert upd["growth_issues"], "the finding must ride the state"
    assert auditor_calls["n"] == 0, "direction lies fail before the auditor"


# ============================== XBRL crosscheck ===========================
def _fact(company, metric, value, unit="USD", derivation="FY_minus_9mo"):
    return {"company": company, "metric": metric, "period": "Q4-2023",
            "value": value, "unit": unit, "derivation": derivation,
            "derived_from": None, "payload_sha256": "f" * 64,
            "source_form": "10-K/10-Q", "corpus_epoch": 10}


def test_correct_consolidated_figure_passes():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Apple", "revenue", 89_498_000_000.0)]
    ev = [_ev("Apple total net sales were strong.", company="Apple")]
    assert check_xbrl_figures(
        "Apple total net sales reached $89,498 million in Q4 2023 [1].",
        ev, facts) == []


def test_legitimate_billions_rendering_passes():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Apple", "revenue", 89_498_000_000.0)]
    ev = [_ev("Apple total net sales were strong.", company="Apple")]
    assert check_xbrl_figures(
        "Apple total net sales reached $89.5 billion in Q4 2023 [1].",
        ev, facts) == []


def test_wrong_consolidated_figure_fails():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Apple", "revenue", 89_498_000_000.0)]
    ev = [_ev("Apple total net sales were strong.", company="Apple")]
    issues = check_xbrl_figures(
        "Apple total net sales reached $99,999 million in Q4 2023 [1].",
        ev, facts)
    assert len(issues) == 1
    assert issues[0]["official"] == 89_498_000_000.0
    assert issues[0]["claimed"] == 99_999_000_000.0


def test_segment_claims_skipped():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Apple", "revenue", 89_498_000_000.0)]
    ev = [_ev("Apple services segment.", company="Apple")]
    # Services segment revenue is NOT the consolidated fact — must be
    # skipped, not misattributed (ADR-011).
    assert check_xbrl_figures(
        "Apple Services revenue reached $99,999 million in Q4 2023 [1].",
        ev, facts) == []


def test_no_matching_fact_declines_to_judge():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Tesla", "revenue", 25_167_000_000.0)]
    ev = [_ev("Apple filings.", company="Apple")]
    assert check_xbrl_figures(
        "Apple total revenue reached $99,999 million in Q4 2023 [1].",
        ev, facts) == []


def test_no_facts_no_judgment():
    from adaptive_rag import check_xbrl_figures
    ev = [_ev("Apple filings.", company="Apple")]
    assert check_xbrl_figures(
        "Apple total revenue reached $99,999 million [1].", ev, []) == []


# ============================== guard wiring (#5) =========================
def test_guard_fail_closes_on_xbrl_mismatch(monkeypatch):
    import adaptive_rag as ar
    auditor_calls = {"n": 0}

    class _Audit:
        grounded = True

    async def _spy_llm(runnable, messages, stage):
        auditor_calls["n"] += 1
        return _Audit(), ar.UsageCollector()

    async def _facts(*a, **k):
        return [_fact("Apple", "revenue", 89_498_000_000.0)]

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "get_xbrl_facts", _facts)
    monkeypatch.setattr(ar, "save_verification_receipt", lambda *a, **k: True)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)

    state = {
        "original_question": "What was Apple total revenue?",
        "search_query": "apple total revenue",
        "documents": ["Apple | Apple_Q4_2023.pdf | Page 1\nApple total net sales"],
        "evidence_records": [_ev("Apple total net sales.", company="Apple")],
        "final_executive_report":
            "Apple total net sales reached $99,999 million in Q4 2023 [1].",
        "degraded_agents": [], "retry_count": 0, "run_id": "xbrl-t",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is False
    assert upd["outcome"] == "unverified_system"
    assert upd["xbrl_issues"][0]["official"] == 89_498_000_000.0
    assert auditor_calls["n"] == 0, "XBRL mismatches fail before the auditor"


def test_guard_certifies_when_xbrl_agrees(monkeypatch):
    import adaptive_rag as ar

    class _Audit:
        grounded = True

    async def _spy_llm(runnable, messages, stage):
        return _Audit(), ar.UsageCollector()

    async def _facts(*a, **k):
        return [_fact("Apple", "revenue", 89_498_000_000.0)]

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "get_xbrl_facts", _facts)
    monkeypatch.setattr(ar, "save_verification_receipt", lambda *a, **k: True)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)

    state = {
        "original_question": "What was Apple total revenue?",
        "search_query": "apple total revenue",
        "documents": ["Apple | Apple_Q4_2023.pdf | Page 1\nApple total net sales"],
        "evidence_records": [_ev("Apple total net sales.", company="Apple")],
        "final_executive_report":
            "Apple total net sales reached $89,498 million in Q4 2023 [1].",
        "degraded_agents": [], "retry_count": 0, "run_id": "xbrl-ok",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is True


# ============================== Q2 live FP: year binding ==================
def test_yoy_comparison_both_years_pass():
    """Live-found FP (2026-09-05, NIM run): 'grew from $32.2B in 2022 to
    $40.1B in 2023' — the 2022 figure was flagged against the 2023 XBRL
    fact. A YoY sentence legitimately contains BOTH years; figures bound to
    a year the facts don't cover must be declined, never flagged."""
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Meta", "revenue", 40_111_000_000.0)]
    ev = [_ev("Meta total revenue.", company="Meta")]
    assert check_xbrl_figures(
        "Meta revenue grew from $32.165 billion in 2022 to $40.111 billion "
        "in 2023 [1].", ev, facts) == []


def test_current_year_figure_still_judged():
    """Year binding must not weaken the core check: a WRONG 2023 figure still
    fails (the lie the gate exists for)."""
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Meta", "revenue", 40_111_000_000.0)]
    ev = [_ev("Meta total revenue.", company="Meta")]
    issues = check_xbrl_figures(
        "Meta revenue reached $99 billion in 2023 [1].", ev, facts)
    assert len(issues) == 1
    assert issues[0]["official"] == 40_111_000_000.0


def test_unanchored_figure_judged_against_2023():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Meta", "revenue", 40_111_000_000.0)]
    ev = [_ev("Meta total revenue.", company="Meta")]
    # No year in the sentence -> judged against the (2023) fact we hold.
    issues = check_xbrl_figures(
        "Meta total revenue was $50 billion [1].", ev, facts)
    assert len(issues) == 1


# ============================== bug-hunt fixes (2026-09-05) ================
def test_margin_claim_not_scale_judged():
    """Bug-hunt fix: operating margin is DERIVED (op income / revenue) —
    never reconstructable from any table figure set. Both consult models
    flagged the guaranteed false rejection."""
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    assert assert_claim_scales(
        "Tesla's operating margin was 7.9 percent in Q4 2023 [1].", ev) == []


def test_margin_claim_not_xbrl_judged():
    from adaptive_rag import check_xbrl_figures
    facts = [_fact("Tesla", "revenue", 25_167_000_000.0)]
    ev = [_ev(TESLA_TABLE)]
    # A margin percentage must never be tested against a revenue fact.
    assert check_xbrl_figures(
        "Tesla's operating margin was 7.9 percent in Q4 2023 [1].",
        ev, facts) == []


def test_growth_percentage_not_scale_judged():
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    # 'grew by 40%' is a derived change — even with a $-figure co-occurring.
    assert assert_claim_scales(
        "Revenue grew by 40 percent, representing $9 billion of new "
        "revenue [1].", ev) == []


def test_plain_figure_still_judged_after_derived_scope():
    """The derived-context skip must not weaken the core: a quotable period
    figure in a non-derived sentence is still judged."""
    from adaptive_rag import assert_claim_scales
    ev = [_ev(TESLA_TABLE)]
    issues = assert_claim_scales(
        "Total automotive revenues reached $21,563 billion in Q4 2023 [1].", ev)
    assert len(issues) == 1


def test_premise_node_no_hits_refuses(monkeypatch):
    """The premise node: zero retrieval hits -> immediate refusal state."""
    import asyncio
    import adaptive_rag as ar

    async def _no_hits(*a, **k):
        return []

    monkeypatch.setattr(ar, "_db_call", _no_hits)
    state = {"original_question": "How much did Apple pay in dividends per share in Q4 2023?",
             "route": "vectorstore", "retry_count": 0, "run_id": "t",
             "tenant_id": "default"}
    upd = asyncio.run(ar.premise_fast_path(state))
    assert upd.get("_premise_fast_path") is True
    assert upd["outcome"] == "verified_refusal"
    assert "dividends" in upd["final_executive_report"]


def test_premise_node_hits_continue(monkeypatch):
    import asyncio
    import adaptive_rag as ar

    async def _hits(*a, **k):
        return [{"company": "Apple", "content": "dividend-like chunk"}]

    monkeypatch.setattr(ar, "_db_call", _hits)
    state = {"original_question": "What was Apple services revenue in Q4 2023?",
             "route": "vectorstore", "retry_count": 0, "run_id": "t",
             "tenant_id": "default"}
    upd = asyncio.run(ar.premise_fast_path(state))
    assert upd == {}, "corpus-supported questions must continue to the fleet"


def test_premise_node_db_error_continues(monkeypatch):
    """Infrastructure failure must NEVER trigger the fast-path refusal."""
    import asyncio
    import adaptive_rag as ar

    async def _boom(*a, **k):
        raise ConnectionError("neon cold start")

    monkeypatch.setattr(ar, "_db_call", _boom)
    state = {"original_question": "What was Apple services revenue in Q4 2023?",
             "route": "vectorstore", "retry_count": 0, "run_id": "t",
             "tenant_id": "default"}
    upd = asyncio.run(ar.premise_fast_path(state))
    assert upd == {}, "DB errors fall through to the normal pipeline"
