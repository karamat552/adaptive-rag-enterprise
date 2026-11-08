"""THE LATENCY KNOB (RAG_SYNTH_EVIDENCE_MAX_CHUNKS) — the synthesis
prompt's evidence prefix-slice, pinned.

The contract: 0/unset = the full evidence stack (the default behavior,
byte-identical); N = the synthesis prompt carries ONLY the top-N chunks
(the rerank order — a PREFIX slice), so the answer's citation positions
[1..N] stay identical to the full list and the receipt chain (which
carries the FULL evidence) is untouched. The knob exists for the
latency-vs-quality Pareto experiment; the judge scores the answers.

Offline: the synthesis node with a stubbed _llm_call that CAPTURES the
prompt — zero tokens, zero network.
"""
import asyncio

import pytest

import adaptive_rag as ar


def _state(n_docs: int) -> dict:
    return {
        "original_question": "What was Apple's operating income in Q4 2023?",
        "documents": [f"chunk {i}: Apple operating income figures and context."
                      for i in range(n_docs)],
        "evidence_records": [{"company": "Apple", "content": f"chunk {i}",
                              "chunk_hash": f"h{i}", "page": i,
                              "source": "Apple_Q4_2023.pdf"}
                             for i in range(n_docs)],
        "financial_report": "financial specialist report text",
        "risk_report": "risk specialist report text",
        "product_report": "product specialist report text",
        "contradictions": [],
        "run_id": "unit-cap",
        "tenant_id": "default",
        "retry_count": 0,
    }


def _fresh_settings(**kw):
    import adaptive_rag as ar
    base = dict(provider="groq", router_model="openai/gpt-oss-20b",
                fleet_model="qwen/qwen3.8-27b",
                executive_model="openai/gpt-oss-120b",
                synth_evidence_max_chunks=0)
    base.update(kw)
    return ar.RagSettings(**base)


def _run_synth(monkeypatch, settings, n_docs, captured):
    """Run the synthesis node with the _llm_call stubbed to capture the
    prompt and return a fixed draft."""

    class _FakeResponse:
        content = "## Executive Summary\ndraft answer [1]."

    class _FakeUsage:
        def totals(self):
            return (0, 0, 0, 0)

    async def _fake_llm(engine, messages, stage):
        captured["user_prompt"] = messages[1][1]
        captured["system_prompt"] = messages[0][1]
        return _FakeResponse(), _FakeUsage()

    monkeypatch.setattr(ar, "_llm_call", _fake_llm)

    class _FakeEngine:
        max_tokens = None

        def bind(self, **kw):
            return self

    monkeypatch.setattr(ar, "_get_engine", lambda m: _FakeEngine())

    saved_settings = ar._S
    ar._S = settings
    try:
        state = _state(n_docs)
        out = asyncio.run(ar.synthesize_csuite_report(state))
    finally:
        ar._S = saved_settings
    return out, captured


def test_default_cap_zero_keeps_full_evidence(monkeypatch):
    """The default (0) = unlimited: every chunk's evidence block reaches
    the synthesis prompt — the pre-knob behavior preserved."""
    captured: dict = {}
    out, _ = _run_synth(monkeypatch, _fresh_settings(), 10, captured)
    prompt = captured["user_prompt"]
    for i in range(10):
        assert f"Evidence [{i + 1}]:" in prompt, f"evidence {i + 1} missing"
    assert "Evidence [11]:" not in prompt


def test_cap_slices_prompt_to_top_n(monkeypatch):
    """The cap N=4: the synthesis prompt carries EXACTLY the top-4
    evidence blocks — the prefix slice, positions preserved."""
    captured: dict = {}
    out, _ = _run_synth(monkeypatch, _fresh_settings(
        synth_evidence_max_chunks=4), 10, captured)
    prompt = captured["user_prompt"]
    assert "Evidence [4]:" in prompt, "the 4th evidence block must survive"
    assert "Evidence [5]:" not in prompt, "the cap did not slice the prompt"
    assert out.get("final_executive_report") == \
        "## Executive Summary\ndraft answer [1]."


def test_cap_pairs_documents_and_evidence(monkeypatch):
    """The documents list (the prose view) and the evidence records are
    capped TOGETHER — the numbered evidence and the receipt's evidence
    must never disagree about which chunks exist. cap=3 keeps chunks
    0,1,2 (the first three); chunk 3 is the first dropped."""
    captured: dict = {}
    out, _ = _run_synth(monkeypatch, _fresh_settings(
        synth_evidence_max_chunks=3), 10, captured)
    prompt = captured["user_prompt"]
    evidence_section = prompt.split("NUMBERED SOURCE EVIDENCE]")[-1].split(
        "[SPECIALIST")[0]
    assert "chunk 2:" in evidence_section, "the 3rd chunk must survive"
    for absent in ("chunk 3:", "chunk 4:", "chunk 9:"):
        assert absent not in evidence_section, \
            f"{absent} leaked past the cap"
