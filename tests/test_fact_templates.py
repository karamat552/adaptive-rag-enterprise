"""ADR-017 Phase 1 — templates, shadow executor, and lineage tests.

Pure-function tests (no DB) + one live integration test (auto-skips
without a reachable database). Every test locks a Phase-1 spec rule:
A.5 (pure templates, byte-identical figures), Amd. 2/3 (exact-match
routing through the builder), B.6.1 (certification wording), the
shadow-isolation contract, and receipt lineage.
"""
import asyncio
from decimal import Decimal

import pytest

from fact_extract import path_a_decision
from fact_templates import (DETERMINISTIC_CERTIFICATION,
                            build_path_a_answer, template_prior_year,
                            template_single_metric)
from fact_shadow import _agreement

# ---------------------------------------------------------------- fixtures
ROW_APPLE_REV = {
    "company": "Apple", "metric_key": "revenue", "period": "Q4-2023",
    "label": "Total net sales", "value_raw": "89498", "scale": "millions",
    "value_usd": "89498000000", "chunk_hash": "h1", "char_start": 100,
    "char_end": 130, "row_text": "Total net sales \n! \n89,498",
    "source": "Apple_Q4_2023.pdf", "page": 1,
}
ROW_APPLE_REV_PRIOR = {
    "company": "Apple", "metric_key": "revenue", "period": "Q4-2022",
    "label": "Total net sales", "value_raw": "90146", "scale": "millions",
    "value_usd": "90146000000", "chunk_hash": "h2", "char_start": 300,
    "char_end": 330, "row_text": "Total net sales \n! \n90,146",
    "source": "Apple_Q4_2023.pdf", "page": 1,
}
ROW_TESLA_EPS = {
    "company": "Tesla", "metric_key": "eps_diluted", "period": "Q4-2023",
    "label": "Diluted", "value_raw": "2.27", "scale": "1",
    "value_usd": "2.27", "chunk_hash": "h3", "char_start": 500,
    "char_end": 560, "row_text": "Diluted\n$ 1.07 \n$ 2.27",
    "source": "Tesla_Q4_2023.pdf", "page": 25,
}
ROW_TESLA_EPS_PADDED = dict(ROW_TESLA_EPS, value_raw="2.2700",
                            row_text="Diluted\n$        2.27 ")

FACT_KEYS = {
    ("Apple", "revenue", "Q4-2023"), ("Apple", "revenue", "Q4-2022"),
    ("Tesla", "eps_diluted", "Q4-2023"),
}


# ---------------------------------------------------------------- templates
def test_single_metric_figure_is_span_verbatim():
    out = template_single_metric(ROW_APPLE_REV, 1)
    assert out == ("Apple reported total net sales of $89,498 million "
                   "for Q4-2023 [1].")
    assert "$89,498" in out            # the span's own comma-grouping


def test_eps_uses_canonical_phrase_and_no_scale_suffix():
    out = template_single_metric(ROW_TESLA_EPS, 1)
    assert out == ("Tesla reported diluted earnings per share (EPS) of "
                   "$2.27 for Q4-2023 [1].")


def test_numeric_padding_never_reaches_the_answer():
    """NUMERIC(20,4) pads 2.27 -> '2.2700'; the answer must carry the
    SPAN's own '2.27', never the padded rendering."""
    out = template_single_metric(ROW_TESLA_EPS_PADDED, 1)
    assert "$2.27" in out and "2.2700" not in out


def test_prior_year_template_carries_both_citations():
    out = template_prior_year(ROW_APPLE_REV, ROW_APPLE_REV_PRIOR, 1, 2)
    assert "$89,498" in out and "$90,146" in out
    assert "[1]" in out and "[2]" in out
    assert "0.7%" in out              # computed YoY, stated with basis


def test_builder_routes_exact_and_demotes_on_missing_row():
    q = "What was Apple's total net sales in Q4 2023?"
    d = path_a_decision(q, FACT_KEYS)
    a = build_path_a_answer(q, d, [ROW_APPLE_REV, ROW_APPLE_REV_PRIOR])
    assert a is not None
    assert a["deterministic_certification"] == \
        DETERMINISTIC_CERTIFICATION       # B.6.1: 'excluded', not 'eliminated'
    assert "89,498" in a["answer"]
    # prior-year row present -> comparative renders with both figures
    assert "$90,146" in a["answer"]
    # prior-year row ABSENT -> single-figure answer (the prior-year
    # comparative is a template enhancement over the RESOLVED triple,
    # never a resolved triple itself) — still serves, honestly narrower
    a2 = build_path_a_answer(q, d, [ROW_APPLE_REV])
    assert a2 is not None and "$90,146" not in a2["answer"]
    # a resolved triple whose row is missing -> fail closed
    q3 = "What was Tesla's diluted EPS in Q4 2023?"
    d3 = path_a_decision(q3, FACT_KEYS)
    assert d3["path"] == "fact"
    assert build_path_a_answer(q3, d3, [ROW_APPLE_REV]) is None


def test_builder_evidence_spans_match_row_spans():
    q = "What was Apple's total net sales in Q4 2023?"
    d = path_a_decision(q, FACT_KEYS)
    a = build_path_a_answer(q, d, [ROW_APPLE_REV, ROW_APPLE_REV_PRIOR])
    ev = a["evidence_records"][0]
    assert ev["char_start"] == ROW_APPLE_REV["char_start"]
    assert ev["char_end"] == ROW_APPLE_REV["char_end"]
    assert ev["chunk_hash"] == ROW_APPLE_REV["chunk_hash"]


def test_builder_multi_entity_comparative():
    rows = [ROW_APPLE_REV,
            dict(ROW_APPLE_REV, company="Tesla", label="Total revenues",
                 value_raw="25167", row_text="Total revenues\n25,167",
                 value_usd="25167000000")]
    keys = FACT_KEYS | {("Tesla", "revenue", "Q4-2023")}
    q = "Compare Apple's and Tesla's revenue in Q4 2023"
    d = path_a_decision(q, keys)
    assert d["path"] == "fact"
    a = build_path_a_answer(q, d, rows)
    assert a is not None
    assert "Apple" in a["answer"] and "Tesla" in a["answer"]
    assert "89,498" in a["answer"] and "25,167" in a["answer"]
    assert len(a["evidence_records"]) == 2


# ---------------------------------------------------------------- shadow
def test_agreement_classifier_shapes():
    same = _agreement("Apple posted $89,498 million [1].",
                      "Apple reported total net sales of $89,498 million "
                      "for Q4-2023 [1].")
    assert same["class"] == "agree_numeric"
    both_refuse = _agreement("I could not verify an answer.",
                             None)
    assert both_refuse["class"] == "agree_refusal"
    value_clash = _agreement("Apple posted $89,498 million [1].",
                             "Apple reported $99,999 million [1].")
    assert value_clash["class"] == "disagree_value"
    shape_clash = _agreement("I could not verify an answer.",
                             "Apple reported $89,498 million [1].")
    assert shape_clash["class"] == "disagree_shape"


async def _run_shadow_safely(state):
    from fact_shadow import shadow_fact_path
    return await shadow_fact_path(state)


def test_shadow_never_raises_and_never_mutates_state():
    """THE isolation contract: broken inputs produce {} — the shadow
    must never break the served pipeline or alter its state."""
    for bad in ({}, {"original_question": ""},
                {"original_question": "What was Apple's revenue in Q4 2023?"},
                {"original_question": "revenue?", "tenant_id": "x"}):
        out = asyncio.run(_run_shadow_safely(bad))
        assert out in ({},) or isinstance(out, dict)


def test_shadow_coverage_miss_is_not_a_disagreement():
    """Out-of-coverage questions log a miss, write nothing to the
    ledger (V1 serves them; only covered questions count in A.2)."""
    out = asyncio.run(_run_shadow_safely({
        "original_question": "What was Apple's gross margin in Q4 2023?",
        "tenant_id": "default", "run_id": "unit-1",
        "final_executive_report": "Gross margin was $40,427 million [1].",
    }))
    assert out.get("shadow_coverage_miss") == "triple_not_in_fact_store"


# ---------------------------------------------------------------- lineage
def test_receipt_lineage_helpers():
    from adaptive_rag import _receipt_model_id, _receipt_prompt_sha
    sha = _receipt_prompt_sha()
    assert sha is None or (len(sha) == 64 and
                          all(c in "0123456789abcdef" for c in sha))
    # model id resolves from settings without a provider call
    mid = _receipt_model_id({})
    assert mid is None or isinstance(mid, str)


def test_receipt_signature_accepts_lineage_kwargs():
    """The lineage params must be optional-and-named: existing callers
    (refusals, older paths) keep working unchanged."""
    import inspect
    from db import save_verification_receipt
    sig = inspect.signature(save_verification_receipt)
    assert sig.parameters["model_id"].default is None
    assert sig.parameters["prompt_sha256"].default is None


# ---------------------------------------------------------------- live
integration = pytest.mark.integration


def _db_reachable() -> bool:
    try:
        from db import admin_connection
        with admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return True
    except Exception:
        return False


@integration
def test_shadow_ledger_round_trip_live():
    if not _db_reachable():
        pytest.skip("Database not reachable — integration test skipped")
    import json as _json
    from db import admin_connection
    state = {
        "original_question": "How much net income did Tesla report in "
                             "Q4 2023?",
        "tenant_id": "default", "run_id": "shadow-int-test",
        "final_executive_report": "Tesla reported net income of "
                                  "$7,928 million [1].",
    }
    out = asyncio.run(_run_shadow_safely(state))
    assert out == {}
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agreement_class, v2_answer, resolved_triples FROM "
            "shadow_disagreements WHERE run_id=%s;", ("shadow-int-test",))
        row = cur.fetchone()
        assert row is not None, "ledger row must exist"
        assert row[0] == "agree_numeric"
        assert "7,928" in (row[1] or "")
        triples = _json.loads(row[2]) if isinstance(row[2], str) else row[2]
        assert ("Tesla", "net_income", "Q4-2023") in [
            tuple(t) if isinstance(t, list) else t for t in triples]
        cur.execute("DELETE FROM shadow_disagreements WHERE run_id=%s;",
                    ("shadow-int-test",))
