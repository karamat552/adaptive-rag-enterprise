"""THE ZERO-TOKEN PIN — the cost story's missing test (ADR-023/ADR-024).

The headline claim — a covered question through the FastPath costs 0
tokens and invokes the LLM exactly ZERO times — was MEASURED by the bench
(MEASUREMENTS_2026-10-10: the same 5 questions that cost p50=168s /
178,553 tokens / ~49.7 calls now measure p50=94ms / 0 tokens / 0 calls,
5/5 certified) but was pinned by NO test: a regression that lets the
router or any LLM stage run on covered questions would silently restore
the cost, and nothing would fail. This file counts ACTUAL LLM
invocations with a stub and fails if the count is not exactly 0 — the
same red-pin discipline as the battery's exact counts and the budget's
cap.

CI-SAFE BY CONSTRUCTION: no keys (the LLM is a counting stub); the
flag-off test needs no DB; the DB tests skip without a reachable
database (the established fixture pattern); the receipt save is STUBBED
in both directions (ar's namespace AND db's module) so no test ever
writes a receipt to any database — the node's best-effort wrapper makes
a stubbed receipt a legitimate execution path, not a mocked lie.
"""
import asyncio

import pytest

import adaptive_rag as ar


def _stub_receipt_saves(monkeypatch) -> None:
    """No test writes a receipt — to any database. Both lookup paths are
    patched: the module-level import in adaptive_rag AND db's own
    attribute (a local re-import inside the node sees the patched one)."""
    monkeypatch.setattr(ar, "save_verification_receipt",
                        lambda *a, **k: None)
    monkeypatch.setattr("db.save_verification_receipt",
                        lambda *a, **k: None)


@pytest.fixture(scope="module")
def covered_ready():
    """The pin's data prerequisite: a reachable DB + the reconciled
    (Apple, revenue, Q4-2023) triple. Missing data -> SKIP: this file
    pins the COST property, not the fact store's content."""
    try:
        from db import get_fact_rows
        rows = get_fact_rows(reconciled_only=True)
    except Exception:
        pytest.skip("No reachable database — skipping the zero-LLM pin")
    keys = {(str(r["company"]).lower(), r["metric_key"], r["period"])
            for r in rows}
    if ("apple", "revenue", "Q4-2023") not in keys:
        pytest.skip("No reconciled (Apple, revenue, Q4-2023) triple — "
                    "the zero-LLM pin's data prerequisite is missing")


# ------------------------------------------------------------- the pin
def test_fastpath_covered_question_makes_zero_llm_calls(covered_ready,
                                                        monkeypatch):
    """THE ZERO-TOKEN PIN: a covered question through the FastPath node
    must invoke the LLM exactly ZERO times and serve a certified,
    grounded answer. Any LLM invocation fails this pin."""
    monkeypatch.setenv("RAG_FACT_FASTPATH", "1")
    _stub_receipt_saves(monkeypatch)
    calls = {"n": 0}

    async def _counting_stub(*a, **k):
        calls["n"] += 1
        raise AssertionError("the FastPath must not invoke the LLM")

    monkeypatch.setattr(ar, "_llm_call", _counting_stub)
    state = {"original_question": "What was Apple's revenue in Q4 2023?",
             "run_id": "unit-zerotok-1", "tenant_id": "default"}
    out = asyncio.run(ar.fact_fastpath(state))
    assert out.get("fastpath_served") is True, \
        f"the covered question must SERVE (got reason: " \
        f"{out.get('fastpath_reason')!r})"
    assert out.get("outcome") == "vectorstore"
    assert out.get("grounded") is True
    assert out.get("served_path") == "fact"
    assert calls["n"] == 0, "the FastPath invoked the LLM — the " \
                            "zero-token cost story regressed"
    assert out.get("final_executive_report"), "the served answer is empty"
    assert out.get("evidence_records"), "the served answer has no evidence"


# ------------------------------------------------------- the kill-switch
def test_fastpath_flag_off_is_dormant(monkeypatch):
    """The one-env-var kill-switch (ADR-017 §3): RAG_FACT_FASTPATH unset
    -> the node returns {} and never touches the DB or the LLM — the V1
    pipeline serves. Dormant code, dormant cost."""
    monkeypatch.delenv("RAG_FACT_FASTPATH", raising=False)
    calls = {"n": 0}

    async def _counting_stub(*a, **k):
        calls["n"] += 1
        return None, None

    monkeypatch.setattr(ar, "_llm_call", _counting_stub)
    out = asyncio.run(ar.fact_fastpath({
        "original_question": "What was Apple's revenue in Q4 2023?"}))
    assert out == {}
    assert calls["n"] == 0


# ------------------------------------------------------ the honest half
def test_fastpath_uncovered_question_demotes_without_llm(covered_ready,
                                                         monkeypatch):
    """The honest half of the cost story: an OUT-of-coverage question
    demotes to the fleet WITHOUT invoking the LLM inside the fastpath
    node (the demotion decision is deterministic). The fleet's spend is
    ALLOWED there — bounded by the per-run budget, which
    tests/test_call_budget.py pins."""
    monkeypatch.setenv("RAG_FACT_FASTPATH", "1")
    _stub_receipt_saves(monkeypatch)
    calls = {"n": 0}

    async def _counting_stub(*a, **k):
        calls["n"] += 1
        return None, None

    monkeypatch.setattr(ar, "_llm_call", _counting_stub)
    state = {"original_question": "What was Apple's headcount in Q4 2023?",
             "run_id": "unit-zerotok-2", "tenant_id": "default"}
    out = asyncio.run(ar.fact_fastpath(state))
    assert out.get("fastpath_served") is not True, \
        "an out-of-coverage question must demote, never serve"
    assert calls["n"] == 0, "the demotion decision invoked the LLM"


# ------------------------------------------------- the tracking half
def test_fastpath_serve_records_no_model_usage(covered_ready, monkeypatch):
    """A FastPath serve must record ZERO model usage — the per-model
    telemetry (_MODEL_USAGE) is what the token dig and the bench read;
    a phantom entry would poison the attribution. (The zero-LLM pin
    proves no call happens; this proves no usage is RECORDED either.)"""
    monkeypatch.setenv("RAG_FACT_FASTPATH", "1")
    _stub_receipt_saves(monkeypatch)

    async def _refusing_stub(*a, **k):
        raise AssertionError("the FastPath must not invoke the LLM")

    monkeypatch.setattr(ar, "_llm_call", _refusing_stub)
    saved = dict(getattr(ar, "_MODEL_USAGE", {}) or {})
    try:
        if hasattr(ar, "_MODEL_USAGE"):
            ar._MODEL_USAGE.clear()
        state = {"original_question":
                 "What was Apple's revenue in Q4 2023?",
                 "run_id": "unit-zerotok-3", "tenant_id": "default"}
        out = asyncio.run(ar.fact_fastpath(state))
        assert out.get("fastpath_served") is True
        if hasattr(ar, "_MODEL_USAGE"):
            assert not ar._MODEL_USAGE, \
                "the FastPath recorded model usage — a phantom entry"
    finally:
        if hasattr(ar, "_MODEL_USAGE"):
            ar._MODEL_USAGE.clear()
            ar._MODEL_USAGE.update(saved)
