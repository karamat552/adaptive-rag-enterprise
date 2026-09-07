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


# ============== year-binding: real YoY prose shapes (white-whale live) =====
def test_respectively_construction_binds_positionally():
    """'...$32,165M and $40,111M in Q4 2022 and Q4 2023, respectively' —
    nearest-token binding gave BOTH figures 2022 (the first year)."""
    from adaptive_rag import _figure_year
    s = ("Meta revenue was $32,165 million and $40,111 million in "
         "Q4 2022 and Q4 2023, respectively.")
    import re as _re
    figs = [m.start() for m in _re.finditer(r"\$\d", s)]
    assert _figure_year(s, figs[0]) == "2022"
    assert _figure_year(s, figs[1]) == "2023"


def test_up_from_clause_binds_its_own_year():
    """'$40,111M in Q4 2023, up from $32,165M in Q4 2022' — the current
    figure must not steal the prior year (nearest-token bug)."""
    from adaptive_rag import _figure_year
    import re as _re
    s = ("Meta revenue was $40,111 million in Q4 2023, up from "
         "$32,165 million in Q4 2022.")
    figs = [m.start() for m in _re.finditer(r"\$\d", s)]
    assert _figure_year(s, figs[0]) == "2023"
    assert _figure_year(s, figs[1]) == "2022"


def test_cross_clause_year_never_leaks():
    """'Meta $40,111M in Q4 2023, while Apple $89,498M' — Apple's figure
    has no year in ITS clause: un-anchored (declined), never borrows 2023."""
    from adaptive_rag import _figure_year
    import re as _re
    s = ("Meta revenue was $40,111 million in Q4 2023, while Apple "
         "revenue was $89,498 million.")
    figs = [m.start() for m in _re.finditer(r"\$\d", s)]
    assert _figure_year(s, figs[0]) == "2023"
    assert _figure_year(s, figs[1]) is None


# ================= live lessons 2026-09-06 (coverage battery day-2) =======
def test_xbrl_per_share_eps_never_judged_against_absolute():
    """Q8 'Tesla diluted EPS': $2.27 per-share must NOT be rejected against
    the $7,928M absolute net-income fact — the fact carries no per-share
    dimension. The eps anchor owns the figure; the gate declines it."""
    from adaptive_rag import check_xbrl_figures
    facts = [{"company": "tesla", "metric": "net_income",
              "value": 7928000000.0, "period": "Q4-2023", "unit": "USD"}]
    ev = [{"company": "Tesla"}]
    draft = ("Tesla diluted EPS was $2.27 in Q4 2023, while non-GAAP EPS "
             "was $0.71 per share【1】.")
    assert check_xbrl_figures(draft, ev, facts) == []


def test_xbrl_figure_anchored_to_decoy_metric_declined():
    """Q3 iter-1 'Meta total revenue': 'revenue grew to $40.1B while total
    assets reached $229.6B' — the ASSETS figure must never be judged against
    the revenue fact. Figure-level ownership, not sentence-level."""
    from adaptive_rag import check_xbrl_figures
    facts = [{"company": "meta", "metric": "revenue",
              "value": 40111000000.0, "period": "Q4-2023", "unit": "USD"}]
    ev = [{"company": "Meta"}]
    draft = ("Meta revenue grew to $40.1 billion while total assets reached "
             "$229.6 billion in Q4 2023【1】.")
    assert check_xbrl_figures(draft, ev, facts) == []


def test_xbrl_wrong_total_still_caught():
    """The figure-level anchoring must not open a hole: a genuinely wrong
    TOTAL revenue figure anchored to the revenue term is still rejected."""
    from adaptive_rag import check_xbrl_figures
    facts = [{"company": "meta", "metric": "revenue",
              "value": 40111000000.0, "period": "Q4-2023", "unit": "USD"}]
    ev = [{"company": "Meta"}]
    draft = "Meta total revenue was $38.7 billion in Q4 2023【1】."
    issues = check_xbrl_figures(draft, ev, facts)
    assert len(issues) == 1 and issues[0]["metric"] == "revenue"


def test_gaap_non_gaap_basis_never_contradicts():
    """Q8's second false conflict: '$2.27 GAAP EPS' vs '$0.71 non-GAAP EPS'
    are two accounting bases of one metric — never a contradiction."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Tesla diluted EPS was $2.27 in Q4 2023. Tesla non-GAAP diluted "
            "EPS was $0.71 per share in Q4 2023.")
    m = extract_metric_mentions(text, "tesla")
    assert detect_contradictions(m) == []


def test_per_share_never_groups_with_absolute():
    """Q8's first false conflict: '$2.27 diluted EPS' and '$7.9B net
    income' share family+period+unit '$' but not scale — per-share figures
    live in their own unit space."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Tesla diluted EPS was $2.27 in Q4 2023. Tesla net income was "
            "$7.9 billion in Q4 2023.")
    m = extract_metric_mentions(text, "tesla")
    assert detect_contradictions(m) == []


def test_real_absolute_conflict_still_detected():
    """Guardrail: the basis/unit splits must not mute a REAL conflict — two
    absolute GAAP net-income figures that disagree are still flagged."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Tesla net income was $2.5 billion in Q4 2023. Tesla net income "
            "was $7.9 billion in Q4 2023.")
    m = extract_metric_mentions(text, "tesla")
    contras = detect_contradictions(m)
    assert len(contras) == 1 and contras[0]["family"] == "net_income"


def test_year_column_map_positional_header():
    """Live lesson 2026-09-06 (coverage Q3): prose-fallback income
    statements render columns positionally ('2023 2022 2023 2022' header,
    values far below). The evidence line must narrate the column order so
    models stop quoting the 2022 column for 2023 questions."""
    from adaptive_rag import _year_column_map, _format_record
    c = ("Three Months Ended December 31, Twelve Months Ended December 31, "
         "In millions 2023 2022 2023 2022 Revenue $ 40,111 $ 32,165 "
         "$ 134,902 $ 116,609")
    m = _year_column_map(c)
    assert m is not None and "2023 (quarterly), 2022 (quarterly)" in m \
        and "2023 (full-year), 2022 (full-year)" in m
    rec = {"company": "Meta", "source": "Meta_Q4_2023.pdf", "page": 1,
           "content": c}
    assert "COLUMN-KEY" in _format_record(rec)


def test_year_column_map_declines_ambiguous_shapes():
    """Odd year runs, Q-prefixed pairs, and yearless prose produce NO map —
    a wrong map is worse than none (the mislabel bug this test guards was
    introduced and caught during the 2026-09-06 fix itself)."""
    from adaptive_rag import _year_column_map
    assert _year_column_map("ended 2023 2022 2021 Revenue") is None
    assert _year_column_map("Q4 2023 Q4 2022 FY 2023 FY 2022 Revenue") is None
    assert _year_column_map("revenues increased 19 percent") is None
    # Bare pair without period labels: order only, no basis guessing.
    m = _year_column_map("Three Months Ended 2023 2022 Revenue")
    assert m == "Year columns, left to right: 2023, 2022"


# ============ follow-up live lessons 2026-09-06 (NIM-lane trace) ==========
def test_fy_period_never_collides_with_q4():
    """NIM Q8 trace: '$4.30 FY diluted EPS' vs '$2.27 Q4 EPS' was one false
    conflict — full-year figures are a distinct period family."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Tesla diluted EPS was $4.30 for the full year 2023. "
            "Tesla diluted EPS was $2.27 in Q4 2023.")
    m = extract_metric_mentions(text, "tesla")
    periods = {mn["period"] for mn in m if mn["unit"] == "$/share"}
    assert periods == {"FY-2023", "Q4-2023"}
    assert detect_contradictions(m) == []


def test_fy_requires_explicit_token_not_bare_year():
    """Bare '(in) 2023' must NOT bind FY — that would swallow quarterly
    sentences into FY groups. Only fy/fiscal-year/full-year/twelve-months
    tokens carry the FY period."""
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions(
        "Revenue was $40,111 million in 2023.", "meta")
    assert all(mn["period"] != "FY-2023" for mn in m)
    m2 = extract_metric_mentions(
        "Revenue was $134,902 million for the twelve months ended "
        "December 31, 2023.", "meta")
    assert any(mn["period"] == "FY-2023" for mn in m2)


def test_cash_position_family_split_from_flow():
    """Q3 noise group: cash-and-equivalents ($11.5B), free cash flow
    ($11.5B), and debt ($18.4B) collided inside one 'cash' family.
    Cash POSITION (balance) and cash FLOW are different metrics."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Meta cash and cash equivalents were $11.5 billion in Q4 2023. "
            "Meta free cash flow was $11.5 billion in Q4 2023. "
            "Meta long-term debt was $18.4 billion in Q4 2023.")
    m = extract_metric_mentions(text, "meta")
    fams = {mn["family"] for mn in m}
    assert "cash_position" in fams and "cash" in fams and "debt" in fams
    assert detect_contradictions(m) == []


def test_xbrl_gate_declines_non_gaap_basis():
    """NIM Q8 trace: '$2.5B non-GAAP net income' was rejected against the
    GAAP $7,928M gold — a basis-class false reject. The fact set is GAAP
    ground truth; non-GAAP sentences are declined (audit owns basis)."""
    from adaptive_rag import check_xbrl_figures
    facts = [{"company": "tesla", "metric": "net_income",
              "value": 7928000000.0, "period": "Q4-2023", "unit": "USD"}]
    ev = [{"company": "Tesla"}]
    draft = ("Tesla non-GAAP net income was $2.5 billion in Q4 2023【1】, "
             "while GAAP net income was $7.9 billion【1】.")
    issues = check_xbrl_figures(draft, ev, facts)
    # The GAAP figure must be judged (it reconstructs), non-GAAP declined.
    assert all("non-gaap" not in i["claim_sentence"].lower() for i in issues)
    assert issues == []


def test_count_vs_dollar_rendering_never_conflicts():
    """Day-2 trace group [40111.0, 40111000000.0]: the raw table count
    40,111 and the dollar $40,111M are two renderings of ONE figure. Unit
    spaces keep them in separate groups (per-share split side effect,
    2026-09-06) — a real count conflict (headcount 40,111 vs 38,706)
    must still fire."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    m = extract_metric_mentions(
        "Revenue was 40,111 in Q4 2023. Revenue was $40,111 million "
        "in Q4 2023.", "meta")
    assert detect_contradictions(m) == []
    m2 = extract_metric_mentions(
        "Meta headcount was 40,111 in Q4 2023. Meta headcount was 38,706 "
        "in Q4 2023.", "meta")
    assert len(detect_contradictions(m2)) == 1


def test_audit_autofail_logs_reason(caplog):
    """The 'Degraded run []' log with an EMPTY quarantine list means
    synthesis failed — a silent [] sent us hunting phantom specialist bugs
    (NIM trace, 2026-09-06). The reason must be named."""
    import asyncio
    import logging
    import adaptive_rag as ar
    state = {"final_executive_report": "", "documents": [{"x": 1}],
             "degraded_agents": [], "original_question": "q?", "run_id": "t"}
    with caplog.at_level(logging.WARNING, logger="EnterpriseRAG"):
        upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is False
    assert any("synthesis returned an empty draft" in r.message
               for r in caplog.records)
    state2 = {**state, "final_executive_report": "draft text",
              "degraded_agents": ["financial"]}
    with caplog.at_level(logging.WARNING, logger="EnterpriseRAG"):
        asyncio.run(ar.fact_checker_guard(state2))
    assert any("specialists quarantined" in r.message for r in caplog.records)


# ============ live lessons 2026-09-07 (day-3 battery traces) ==============
def test_xbrl_gate_sentence_named_company_attribution():
    """Day-3 white-whale trace: citing multi-company evidence made the gate
    judge Meta's $40,111M revenue against APPLE's $22,956M net-income
    gold (40111/32165 vs 22956 rejects). A sentence naming exactly ONE
    company owns its figures — judged only against that company's facts."""
    from adaptive_rag import check_xbrl_figures
    facts = [
        {"company": "apple", "metric": "net_income", "value": 22956000000.0,
         "period": "Q4-2023", "unit": "USD"},
        {"company": "meta", "metric": "revenue", "value": 40111000000.0,
         "period": "Q4-2023", "unit": "USD"}]
    ev = [{"company": "Apple"}, {"company": "Meta"}]   # both cited
    d = ("Meta revenue was $40,111 million in Q4 2023【1】【2】.")
    assert check_xbrl_figures(d, ev, facts) == []       # Meta judged vs Meta only
    d2 = "Apple net income was $25.1 billion in Q4 2023【1】【2】."
    issues = check_xbrl_figures(d2, ev, facts)
    assert len(issues) == 1 and issues[0]["company"] == "apple"
    d3 = "Meta revenue was $38.7 billion in Q4 2023【1】【2】."
    issues3 = check_xbrl_figures(d3, ev, facts)
    assert len(issues3) == 1 and issues3[0]["company"] == "meta"


def test_xbrl_gate_multi_named_sentence_declined():
    """A sentence naming SEVERAL companies attributes ambiguously — it
    falls back to cited-evidence companies only when no single name
    dominates; the mixed-name comparison sentence must never cross-judge
    (same principle as the detector's multi-named decline)."""
    from adaptive_rag import check_xbrl_figures
    facts = [
        {"company": "apple", "metric": "net_income", "value": 22956000000.0,
         "period": "Q4-2023", "unit": "USD"}]
    ev = [{"company": "Apple"}, {"company": "Meta"}]
    d = ("Compared to Meta revenue of $40.1 billion, Apple net income was "
         "$22.9 billion in Q4 2023【1】【2】.")
    assert check_xbrl_figures(d, ev, facts) == []


def test_decoy_noun_decline_no_family_lending():
    """Day-3 trace group ('cash_position', [76455.0, 229623.0]): 'cash and
    marketable securities were $65.4B while total assets reached $229.6B'
    lent the cash_position family to the ASSETS figure. A clause whose
    noun is a non-family financial term claims its figure — decline, not
    loan."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Meta cash and marketable securities were $65.4 billion while "
            "total assets reached $229.6 billion in Q4 2023.")
    m = extract_metric_mentions(text, "meta")
    assert all(mn["value"] != 229600000000.0 for mn in m), \
        "the assets figure must be declined, never grouped"
    assert any(mn["family"] == "cash_position"
               and abs(mn["value"] - 65400000000.0) < 1e3 for mn in m), \
        "the cash figure must still bind (65.4e9 carries float tail 0.00001)"
    assert detect_contradictions(m) == []


def test_decoy_noun_does_not_block_real_figures():
    """Guardrail: a decoy noun in a sibling clause must never decline a
    figure whose OWN clause carries a real family term — revenue beside a
    total-assets clause still binds revenue."""
    from adaptive_rag import extract_metric_mentions
    text = ("Meta revenue was $40.1 billion in Q4 2023 while total assets "
            "reached $229.6 billion.")
    m = extract_metric_mentions(text, "meta")
    fams = {mn["family"] for mn in m}
    assert "revenue" in fams and "cash_position" not in fams
