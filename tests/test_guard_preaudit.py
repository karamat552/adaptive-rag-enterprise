"""
Deterministic Citation Pre-Audit — Unit Tests (offline, zero tokens)
====================================================================
Covers the fact_checker_guard fast path added in v3.3: any [n] footnote
outside the evidence range (1..len(documents)) is fabricated by construction
and must fail-closed WITHOUT invoking the 120B auditor.

Number substantiation is deliberately NOT tested here — derived metrics and
legitimate rounding make naive numeric matching false-positive-prone; that
remains the LLM auditor's job (see adaptive_rag.citation_pre_audit docstring).

Run:  pytest tests/test_guard_preaudit.py -v
"""
import asyncio
import os
from pathlib import Path

# Same dummy-guard pattern as tests/test_db.py for bare CI environments.
if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402


# ============================== pure function ==============================
def test_in_bounds_citations_pass():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("Revenue rose [1]. Margin held at [2] and [3].", 3) is None


def test_out_of_bounds_citation_caught():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("Services margin hit 91.7%, a record [99].", 5) == "[99]"


def test_lower_bound_and_zero_caught():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("see [1] then [0]", 3) == "[0]"
    assert citation_pre_audit("see [2] then [7]", 3) == "[7]"


def test_zero_citations_pass_trivially():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("No footnotes in this draft.", 5) is None


def test_markdown_links_not_confused_with_citations():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("see [report](https://x) and cite [1]", 3) is None


def test_first_offender_reported():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("a [1] b [42] c [43]", 3) == "[42]"


# ============================== guard fast path ============================
def _run(coro):
    return asyncio.run(coro)


def _state(draft: str) -> dict:
    return {"original_question": "What was Apple services revenue?",
            "search_query": "apple services revenue",
            "documents": ["doc one", "doc two"],
            "final_executive_report": draft,
            "degraded_agents": [], "retry_count": 0, "run_id": "t",
            "tenant_id": "default",
            "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0}


@pytest.fixture()
def _spy_auditor(monkeypatch):
    """Replaces the LLM seam; counts auditor invocations, always certifies."""
    import adaptive_rag as ar
    calls = {"n": 0}

    async def _spy(runnable, messages, stage):
        calls["n"] += 1

        class _Audit:
            grounded = True
            explanation = None
        return _Audit(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_llm_call", _spy)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)
    return calls


def test_guard_rejects_bad_citation_without_auditor(_spy_auditor):
    import adaptive_rag as ar
    upd = _run(ar.fact_checker_guard(_state("Services revenue grew [3].")))
    assert upd["grounded"] is False
    assert upd["outcome"] == "unverified_system"
    assert _spy_auditor["n"] == 0, "pre-audit must short-circuit BEFORE the auditor"


def test_guard_still_audits_clean_draft(_spy_auditor):
    import adaptive_rag as ar
    upd = _run(ar.fact_checker_guard(_state("Services revenue grew per [1] and [2].")))
    assert upd["grounded"] is True
    assert upd["outcome"] == "vectorstore"
    assert _spy_auditor["n"] == 1, "in-range citations must reach the LLM auditor"
