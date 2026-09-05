"""
Deterministic Contradiction Detection — Unit Tests (offline, zero tokens)
=========================================================================
Covers Phase C (ADR-007):
- extract_metric_mentions: money/percent/count spaces; metric-family gating
  (stray numbers ignored); period detection; scale normalization ($40.111B
  == $40,111M == 40111000000).
- detect_contradictions: same (family, company, period, unit) with disagreeing
  values flags; rounding does NOT flag (precision-aware slack); different
  period/company never compares; tolerance honored.
- Graph wiring: cross_check routes to sharpen on first contradiction, to
  synthesis on retry/survived conflict; sharpen builds a deterministic query.
- Synthesis contract: the CONSISTENCY ALERT block instructs BOTH figures,
  never averaging.

Run:  pytest tests/test_contradictions.py -v
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


# ============================== mention extraction =========================
def test_extracts_money_with_scales():
    from adaptive_rag import extract_metric_mentions
    text = ("Apple Services revenue was $22,314 million in Q4 2023. "
            "Products revenue reached $43.8 billion.")
    m = extract_metric_mentions(text, "apple")
    vals = sorted(m["value"] for m in m if m["unit"] == "$")
    assert 22_314_000_000.0 in vals and 43_800_000_000.0 in vals


def test_extracts_percent_independent_of_money():
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions("Operating margin was 91.7 percent.", "meta")
    assert any(x["unit"] == "%" and x["value"] == 91.7 for x in m)


def test_stray_numbers_without_metric_family_are_ignored():
    from adaptive_rag import extract_metric_mentions
    # No metric family term -> the 99,999 must not become a mention.
    m = extract_metric_mentions("The report cites 99,999 items and 42 pages.", "meta")
    assert m == []


def test_period_gating_attribute():
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions("Revenue was $10.0B in Q4 2022.", "tesla")
    assert m and m[0]["period"] == "Q4-2022"


def test_family_matching_prefers_earliest_sentence_hit():
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions("Net income grew 25% year-over-year.", "meta")
    assert m and m[0]["family"] in ("growth", "net_income")


# ============================== contradiction detection ====================
def _mention(family, value, company="apple", period="Q4-2023", unit="$",
             precision=3, context="ctx"):
    return {"family": family, "value": value, "unit": unit,
            "precision": precision, "period": period, "company": company,
            "context": context}


def test_disagreeing_values_flag():
    from adaptive_rag import detect_contradictions
    c = detect_contradictions([
        _mention("revenue", 22_314e6, context="Services revenue was $22,314M"),
        _mention("revenue", 23_100e6, context="Services revenue was $23,100M"),
    ])
    assert len(c) == 1
    assert c[0]["values"] == [22_314e6, 23_100e6]
    assert c[0]["company"] == "apple" and c[0]["period"] == "Q4-2023"


def test_rounding_does_not_flag():
    from adaptive_rag import detect_contradictions
    # 91.7 (1dp) vs 91.65 (2dp): slack 0.05 absorbs the 0.05 gap.
    c = detect_contradictions([
        _mention("margin", 91.7, unit="%", precision=1),
        _mention("margin", 91.65, unit="%", precision=2),
    ])
    assert c == []


def test_real_gap_beyond_precision_slack_flags():
    from adaptive_rag import detect_contradictions
    # 91.7 vs 95.2: 3.5pp gap, slack 0.05 — a genuine conflict.
    c = detect_contradictions([
        _mention("margin", 91.7, unit="%", precision=1),
        _mention("margin", 95.2, unit="%", precision=1),
    ])
    assert len(c) == 1


def test_different_period_never_compares():
    from adaptive_rag import detect_contradictions
    c = detect_contradictions([
        _mention("revenue", 22_314e6, period="Q4-2023"),
        _mention("revenue", 40_111e6, period="Q4-2022"),  # different year
    ])
    assert c == []


def test_different_company_never_compares():
    from adaptive_rag import detect_contradictions
    c = detect_contradictions([
        _mention("revenue", 22_314e6, company="apple"),
        _mention("revenue", 40_111e6, company="meta"),
    ])
    assert c == []


def test_within_tolerance_not_flagged():
    from adaptive_rag import detect_contradictions
    # 1.5% gap < 2% tolerance -> same figure, different rounding convention.
    c = detect_contradictions([
        _mention("revenue", 40_111e6, precision=0),
        _mention("revenue", 40_700e6, precision=0),
    ])
    assert c == []


def test_full_text_end_to_end_detection():
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    financial = ("Apple Services revenue was $22,314 million in Q4 2023, "
                 "a record quarter for the segment.")
    product = ("Apple Services revenue reached $23.1 billion in Q4 2023 "
               "according to the segment table.")
    mentions = (extract_metric_mentions(financial, "apple")
                + extract_metric_mentions(product, "apple"))
    c = detect_contradictions(mentions)
    assert len(c) == 1 and c[0]["family"] == "revenue"


# ============================== graph wiring ==============================
def _mk_state(**over):
    base = {
        "original_question": "What was Apple services revenue in Q4 2023?",
        "search_query": "apple services revenue",
        "documents": ["Apple | a | Page 1\nx"],
        "evidence_records": [],
        "financial_report": "", "risk_report": "", "product_report": "",
        "final_executive_report": "", "retry_count": 0, "route": "vectorstore",
        "grounded": False, "outcome": "", "cached_hit": False,
        "degraded_agents": [], "run_id": "t", "tenant_id": "default",
        "contradictions": [], "contradiction_retry": 0,
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    base.update(over)
    return base


def test_cross_check_finds_contradiction_across_specialists():
    from adaptive_rag import cross_check_specialists
    state = _mk_state(
        financial_report="Apple Services revenue was $22,314 million in Q4 2023.",
        product_report="Apple Services revenue reached $23.1 billion in Q4 2023.",
    )
    upd = asyncio.run(cross_check_specialists(state))
    assert upd["contradictions"], "specialists disagree -> must flag"


def test_cross_check_clean_reports_no_contradictions():
    from adaptive_rag import cross_check_specialists
    state = _mk_state(
        financial_report="Apple Services revenue was $22,314 million in Q4 2023.",
        product_report="Apple Services revenue was $22.3 billion in Q4 2023.",
    )
    upd = asyncio.run(cross_check_specialists(state))
    assert upd["contradictions"] == []


def test_cross_check_degraded_fleet_skips():
    from adaptive_rag import cross_check_specialists
    state = _mk_state(degraded_agents=["financial"],
                      financial_report="Revenue $10B.",
                      product_report="Revenue $99B.")
    upd = asyncio.run(cross_check_specialists(state))
    assert upd["contradictions"] == []


def test_route_after_cross_check_bounded_retry():
    from adaptive_rag import route_after_cross_check
    contr = [{"family": "revenue", "company": "apple", "period": "Q4-2023",
              "unit": "$", "values": [1, 2], "rel_gap": 0.4, "contexts": []}]
    # First sighting -> sharpen
    assert route_after_cross_check(
        _mk_state(contradictions=contr, contradiction_retry=0)) == "sharpen"
    # Retry already spent -> synthesis (surface, don't loop)
    assert route_after_cross_check(
        _mk_state(contradictions=contr, contradiction_retry=1)) == "csuite_synth"
    # No contradictions -> straight to synthesis
    assert route_after_cross_check(_mk_state()) == "csuite_synth"


# ============================== synthesis contract ========================
def test_sharpen_state_query_visible():
    # sharpen_retrieval writes state["search_query"] AND returns only the
    # retry counter; the graph merges both. Verify the node's state-mutation
    # pattern explicitly (LangGraph nodes mutate+return partials).
    from adaptive_rag import sharpen_retrieval
    contr = [{"family": "margin", "company": "meta", "period": "Q4-2023",
              "unit": "%", "values": [80, 91], "rel_gap": 0.13, "contexts": []}]
    state = _mk_state(contradictions=contr)
    upd = asyncio.run(sharpen_retrieval(state))
    assert upd["contradiction_retry"] == 1
    assert "margin" in state["search_query"]  # mutated for next fleet run


# ============================== synthesis contract ========================
def test_synthesis_gets_consistency_alert_on_conflict(monkeypatch):
    """The synthesis prompt must instruct BOTH-figures reporting. Verified by
    capturing the human prompt through a mocked _llm_call."""
    import adaptive_rag as ar

    contr = [{"family": "revenue", "company": "apple", "period": "Q4-2023",
              "unit": "$", "values": [22_314e6, 23_100e6], "rel_gap": 0.034,
              "contexts": ["a", "b"]}]

    captured = {}

    class _Resp:
        content = "brief"

    async def _spy_llm(runnable, messages, stage):
        captured["messages"] = messages
        return _Resp(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    state = _mk_state(contradictions=contr,
                      financial_report="rev $22,314M",
                      product_report="rev $23.1B")
    upd = asyncio.run(ar.synthesize_csuite_report(state))
    assert upd["final_executive_report"] == "brief"
    human = captured["messages"][1][1]
    assert "SOURCE CONSISTENCY ALERT" in human
    assert "do NOT average" in human or "do NOT average," in human
    assert "22314000000" in human or "22,314" in human or "23100000000" in human


def test_synthesis_clean_run_notes_no_contradictions(monkeypatch):
    import adaptive_rag as ar

    captured = {}

    class _Resp:
        content = "brief"

    async def _spy_llm(runnable, messages, stage):
        captured["messages"] = messages
        return _Resp(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    upd = asyncio.run(ar.synthesize_csuite_report(_mk_state(
        financial_report="rev $22,314M")))
    human = captured["messages"][1][1]
    assert "No cross-specialist contradictions detected" in human


# ============================== adversarial-review fixes ===================
def test_percent_uses_absolute_tolerance_not_relative():
    """Gemini 3.8 review: relative gaps on small margins (1.0% vs 1.03%) are
    noise; 50% vs 50.9% is also noise. Percent space = ABSOLUTE pp > 0.5."""
    from adaptive_rag import detect_contradictions
    # 1.0 vs 1.03: 3% RELATIVE but 0.03pp absolute -> NOT a conflict.
    assert detect_contradictions([
        _mention("margin", 1.0, unit="%", precision=2),
        _mention("margin", 1.03, unit="%", precision=2),
    ]) == []
    # 50.0 vs 50.9: 1.8% relative but 0.9pp absolute -> IS a conflict.
    c = detect_contradictions([
        _mention("margin", 50.0, unit="%", precision=1),
        _mention("margin", 50.9, unit="%", precision=1),
    ])
    assert len(c) == 1


def test_direction_conflict_flagged_even_when_magnitudes_agree():
    """'grew 25%' vs 'declined 25%': identical magnitude, opposite polarity —
    the most dangerous silent wrongness. Must flag (kind='direction')."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    a = extract_metric_mentions("Revenue grew 25% year-over-year in Q4 2023.", "meta")
    b = extract_metric_mentions("Revenue declined 25% year-over-year in Q4 2023.", "meta")
    c = detect_contradictions(a + b)
    assert any(x.get("kind") == "direction" for x in c)


def test_comparative_clause_binds_figure_to_its_own_period():
    """'$10B, compared to $8B in Q4-2022' — the $8B belongs to Q4-2022, the
    $10B to the sentence head period. Mis-binding both to one period was a
    false-positive factory (Gemini review #3)."""
    from adaptive_rag import extract_metric_mentions
    text = ("Meta revenue was $40.1 billion in Q4 2023, compared to "
            "$32.2 billion in Q4 2022.")
    m = extract_metric_mentions(text, "meta")
    by_value = {round(x["value"] / 1e9, 2): x["period"] for x in m}
    assert by_value.get(40.1) == "Q4-2023"
    assert by_value.get(32.2) == "Q4-2022", \
        "the comparative figure must bind to ITS clause period, not the head's"


def test_company_attribution_per_sentence():
    """Live-found defect (2026-09-04): a report sentence about APPLE must
    never attribute to META just because the question scoped both companies.
    Sentences naming no company inherit the context company instead."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Apple operating cash flow was $110.5 billion in Q4 2023. "
            "Meta operating cash flow was $19.2 billion in Q4 2023.")
    m = extract_metric_mentions(text, "apple")  # context company: apple
    by_comp = {x["company"] for x in m if x["family"] == "cash"}
    assert by_comp == {"apple", "meta"}, \
        "each sentence must attribute ONLY to the company it names"
    # And no cross-company contradiction gets flagged:
    assert detect_contradictions(m) == []


def test_unnamed_sentence_inherits_context_company():
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions("Net income grew to $25.0 billion in Q4 2023.", "meta")
    assert all(x["company"] == "meta" for x in m)


def test_ambiguous_comparison_sentence_never_votes():
    """Benchmark Q2 defect (2026-09-04): '$14B versus $40B' sentences with no
    company name put Meta's revenue in Apple's conflict group (186% 'gap').
    Ambiguous sentences stay citable but company=None — never vote."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Revenue grew to $14,017 million versus $40,110 million in Q4 2023. "
            "Apple revenue reached $14,017 million in Q4 2023.")
    m = extract_metric_mentions(text, "apple")
    ambiguous = [x for x in m if x["company"] is None]
    assert ambiguous, "the versus-sentence must be recognized as ambiguous"
    assert all(x["company"] != "meta" for x in m), \
        "no company name in the sentence -> never attributed to a wrong company"
    assert detect_contradictions(m) == []


# ============================== nearest-anchor family binding =============
def test_multi_metric_sentence_binds_figures_to_nearest_family():
    """Live-found defect (2026-09-05, white-whale trace): 'net income grew
    201% from $4.652 billion while revenue grew 25% to $40.111 billion'
    first-match binding lumped BOTH figures into one 'growth' group ->
    fabricated contradiction between Meta's own net income and revenue."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = ("Meta net income grew 201 percent from $4.652 billion in Q4 2023, "
            "while revenue grew 25 percent to $40.111 billion.")
    m = extract_metric_mentions(text, "meta")
    # 4.652B must bind to net_income; 40.111B must bind to revenue
    fam_by_val = {}
    for x in m:
        if x["unit"] == "$":
            fam_by_val[round(x["value"] / 1e9, 2)] = x["family"]
    assert fam_by_val.get(4.65) == "net_income", \
        f"4.652B binds to {fam_by_val.get(4.65)} — must bind to net_income"
    assert fam_by_val.get(40.11) == "revenue", \
        f"40.111B binds to {fam_by_val.get(40.11)} — must bind to revenue"
    # And no fabricated contradiction within Meta:
    assert detect_contradictions(m) == []


def test_single_metric_sentence_unchanged():
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions(
        "Meta revenue was $40,111 million in Q4 2023.", "meta")
    assert m and m[0]["family"] == "revenue"


def test_growth_percentage_binds_to_growth_word():
    """'grew 201 percent' — the percent figure must bind to the growth
    family nearest it, not to a distant revenue term."""
    from adaptive_rag import extract_metric_mentions
    m = extract_metric_mentions(
        "Meta net income grew 201 percent year-over-year.", "meta")
    assert m and all(x["family"] in ("net_income", "growth") for x in m), \
        "percent figure near 'net income grew' — nearest-anchor binding"


# ============== consult-converged declines (3-model review, live-caught) ===
def test_arithmetic_transcript_sentence_declined():
    """'(14,017-4,652)/4,652 = 2.012 = 201.2% ~201%' — a visible DERIVATION.
    Parsing it as three figures + a percent fabricated a 4-way conflict out
    of one correct calculation (live capture 2026-09-05)."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = "Net income: (14,017-4,652)/4,652 = 9,365/4,652 = 2.012 = 201.2% ~201% as given."
    assert extract_metric_mentions(text, "meta") == []


def test_from_to_transition_pair_no_self_conflict():
    """'Net income increased from $20,721 to $22,956' — prior/current pair
    in one sentence, previously both period=None -> self-conflict. The
    transition binding separates their groups."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = "Net income increased from $20,721 to $22,956, about 10.8% increase."
    m = extract_metric_mentions(text, "apple")
    periods = {x["period"] for x in m if x["unit"] == "$"}
    assert "RELATIVE_PRIOR" in periods and "RELATIVE_CURRENT" in periods, \
        f"transition endpoints must bind to distinct periods, got {periods}"
    assert detect_contradictions(m) == []


def test_multi_named_company_sentence_declined():
    """'Apple revenue reached $22,314M while Meta hit $40,111M' — shared
    comparison sentences can never attribute reliably; company=None so the
    figures never vote in either company's conflict groups."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    text = "Apple revenue reached $22,314 million while Meta revenue hit $40,111 million."
    m = extract_metric_mentions(text, "apple")
    assert all(x["company"] is None for x in m), \
        "multi-named-company sentences must decline attribution entirely"
    assert detect_contradictions(m) == []


def test_single_company_sentences_still_attributed():
    """The declines must not over-trigger: a plain single-company sentence
    still attributes and still detects genuine conflicts."""
    from adaptive_rag import extract_metric_mentions, detect_contradictions
    m = extract_metric_mentions(
        "Apple revenue was $22,314 million in Q4 2023.", "apple")
    assert m and m[0]["company"] == "apple"
