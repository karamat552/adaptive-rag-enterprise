"""Step 3 (Part 1 directive, 2026-09-30): the pipeline's RETRIEVAL must bind
to the RUN's tenant.

Failing-first against main: `_specialist` (adaptive_rag.py:1428) and the
premise probe (`premise_fast_path`, :1663) call the search WITHOUT
`tenant_id`, so `db.pgvector_hybrid_search` silently binds the pool default
('default'). Proven live on production 2026-10-01: anonymous run
1b9be0eb24a4 declared tenant 'acme', was served the DEFAULT corpus, and its
receipt was filed under 'acme' — cross-attribution. A future tenant with
private uploads would retrieve the public corpus and never its own.

All offline: search, reranker and LLM are fakes; zero DB, zero tokens.
"""
import asyncio
from types import SimpleNamespace

import adaptive_rag as ar


def _fake_record(marker="retrieval-tenancy-probe"):
    return {"company": "Contoso", "content": f"{marker} services revenue "
            "$12,345 million in Q4 2025.", "source": "contoso_q4.pdf",
            "page": 1, "chunk_hash": "h" * 64, "category": "financial",
            "section_title": "s", "contains_table": False,
            "fusion_score": 0.99}


def test_premise_probe_binds_run_tenant(monkeypatch):
    """The premise probe must scope to the run's tenant, not the pool
    default. FAILS on main: no tenant_id reaches the search."""
    captured = {}

    def fake_search(query, **kw):
        captured.update(kw)
        return [_fake_record()]

    monkeypatch.setattr(ar, "pgvector_hybrid_search", fake_search)
    state = {"original_question":
             "What was Apple services segment revenue in Q4 2025?",
             "route": "vectorstore", "tenant_id": "tenant-x"}
    asyncio.run(ar.premise_fast_path(state))
    assert captured.get("tenant_id") == "tenant-x", (
        f"premise probe did not bind the run's tenant; search kwargs: "
        f"{sorted(captured)}")


def test_specialist_retrieval_binds_run_tenant(monkeypatch):
    """`_specialist` must accept and thread the run's tenant into every
    retrieval call (scoped, fallback and per-company paths)."""
    captured = []

    async def fake_mq_search(search_q, *, category, company, top_k,
                             tenant_id=None):
        captured.append(tenant_id)
        return [_fake_record()]

    monkeypatch.setattr(ar, "_multi_query_search", fake_mq_search)
    monkeypatch.setattr(ar, "_get_reranker",
                        lambda: SimpleNamespace(
                            rerank=lambda req: []))
    async def fake_llm_call(engine, messages, stage, allow_failover=False):
        return SimpleNamespace(content="specialist report [1]"), \
            SimpleNamespace(totals=lambda: (0, 0, 0, 0))
    monkeypatch.setattr(ar, "_llm_call", fake_llm_call)

    out = asyncio.run(ar._specialist(
        "financial", "system prompt", "financial",
        "Apple services segment revenue Q4 2025",
        "What was Apple services segment revenue in Q4 2025?",
        top_k=2, tenant_id="tenant-x"))
    assert captured and all(t == "tenant-x" for t in captured), (
        f"specialist retrieval did not bind tenant-x: {captured}")
    assert out["degraded"] is False


def test_specialist_default_tenant_passthrough_unchanged(monkeypatch):
    """Default parity: with NO tenant in state, the search receives None —
    the db layer then binds the pool default, byte-identical to main."""
    captured = []

    async def fake_mq_search(search_q, *, category, company, top_k,
                             tenant_id=None):
        captured.append(tenant_id)
        return [_fake_record()]

    monkeypatch.setattr(ar, "_multi_query_search", fake_mq_search)
    monkeypatch.setattr(ar, "_get_reranker",
                        lambda: SimpleNamespace(rerank=lambda req: []))
    async def fake_llm_call(engine, messages, stage, allow_failover=False):
        return SimpleNamespace(content="r [1]"), \
            SimpleNamespace(totals=lambda: (0, 0, 0, 0))
    monkeypatch.setattr(ar, "_llm_call", fake_llm_call)

    asyncio.run(ar._specialist(
        "financial", "sys", "financial",
        "Apple services revenue Q4 2025",
        "What was Apple services revenue in Q4 2025?", top_k=2))
    assert captured == [None], (
        f"default path changed: {captured}")
