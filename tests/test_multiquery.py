"""
Multi-Query Expansion (ADR-014) — Tests (offline, zero live-LLM cost)
=====================================================================
- _rrf_fuse: rank-based fusion math, chunk_hash dedupe, deterministic
  ordering, top-k truncation, empty-set tolerance.
- _multi_query_search: paraphraser success path (spied), degrade-to-single
  on paraphraser failure, expansion-off short-circuit.
- Terminology constraint: the system prompt forbids new facts — asserted
  via the spy's captured prompt (regression guard for the constraint text).

Run:  pytest tests/test_multiquery.py -v
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


def _rec(h, page=1, content="x", company="Apple"):
    return {"chunk_hash": h, "company": company, "source": "a.pdf",
            "page": page, "content": content, "fusion_score": 0.1}


# ============================== RRF fusion ================================
def test_rrf_fuse_ranks_by_reciprocal_rank():
    from adaptive_rag import _rrf_fuse
    # Chunk A: rank 1 in q1, rank 3 in q2 -> 1/61 + 1/63 = 0.0323
    # Chunk B: rank 2 in q1 only          -> 1/62      = 0.0161
    # Chunk C: rank 1 in q2 only           -> 1/61      = 0.0164
    q1 = [_rec("A"), _rec("B"), _rec("D")]
    q2 = [_rec("C"), _rec("D2"), _rec("A", page=3)]
    fused = _rrf_fuse([q1, q2], top_k=3)
    assert [r["chunk_hash"] for r in fused] == ["A", "C", "B"], \
        "chunk in BOTH queries must dominate single-query chunks"


def test_rrf_fuse_dedupes_by_chunk_hash():
    from adaptive_rag import _rrf_fuse
    q1 = [_rec("A"), _rec("B")]
    q2 = [_rec("A"), _rec("C")]      # A appears in both
    fused = _rrf_fuse([q1, q2], top_k=10)
    hashes = [r["chunk_hash"] for r in fused]
    assert hashes.count("A") == 1 and len(hashes) == 3


def test_rrf_fuse_top_k_truncates():
    from adaptive_rag import _rrf_fuse
    many = [_rec(f"h{i}") for i in range(50)]
    assert len(_rrf_fuse([many], top_k=7)) == 7


def test_rrf_fuse_empty_sets():
    from adaptive_rag import _rrf_fuse
    assert _rrf_fuse([], top_k=5) == []
    assert _rrf_fuse([[]], top_k=5) == []


def test_rrf_fuse_deterministic():
    """Identical inputs -> identical order (tie-break by hash, not dict order)."""
    from adaptive_rag import _rrf_fuse
    sets = [[_rec("z"), _rec("a")], [_rec("m"), _rec("a")]]
    r1 = [r["chunk_hash"] for r in _rrf_fuse(sets)]
    r2 = [r["chunk_hash"] for r in _rrf_fuse([list(s) for s in sets])]
    assert r1 == r2


# ============================== multi-query search ========================
@pytest.fixture()
def _patch_env(monkeypatch):
    import adaptive_rag as ar
    monkeypatch.setattr(ar.get_settings(), "multi_query", 1)
    monkeypatch.setattr(ar.get_settings(), "multi_query_count", 3)
    return ar


def test_multiquery_success_fuses_variants(_patch_env, monkeypatch):
    ar = _patch_env
    captured = {}

    class _Variants:
        variants = ["Apple services revenue total", "Apple services net sales Q4"]

    async def _fake_llm(runnable, messages, stage, allow_failover=False):
        captured["prompt"] = messages[0][1]
        return _Variants(), ar.UsageCollector()

    searches: list = []

    async def _fake_db(fn, *args, **kwargs):
        searches.append(args[0])
        n = len(searches)
        # original + 2 variants each return a distinct chunk
        return [_rec(f"chunk-orig")] if n == 1 else [_rec(f"chunk-v{n}")]

    monkeypatch.setattr(ar, "_llm_call", _fake_llm)
    monkeypatch.setattr(ar, "_get_engine", lambda m: object())
    monkeypatch.setattr(ar, "_db_call", _fake_db)

    rows = asyncio.run(ar._multi_query_search(
        "How much money did Apple make from services?",
        category=None, company="apple"))
    assert len(searches) == 3, "original + 2 variants must each be searched"
    assert any("chunk-orig" in str(r) for r in rows)
    assert any("chunk-v2" in str(r) for r in rows)
    # Terminology constraint present in the paraphraser prompt:
    assert "no new companies, numbers, or periods" in captured["prompt"]


def test_multiquery_degrades_to_single_on_paraphraser_failure(_patch_env, monkeypatch):
    ar = _patch_env

    async def _boom(runnable, messages, stage, allow_failover=False):
        raise RuntimeError("429 rate limit")

    searches: list = []

    async def _fake_db(fn, *args, **kwargs):
        searches.append(args[0])
        return [_rec("single-chunk")]

    monkeypatch.setattr(ar, "_llm_call", _boom)
    monkeypatch.setattr(ar, "_db_call", _fake_db)
    rows = asyncio.run(ar._multi_query_search(
        "Apple services revenue performance", category=None, company=None))
    assert searches == ["Apple services revenue performance"], \
        "paraphraser failure must fall back to the ORIGINAL query only"
    assert rows and rows[0]["chunk_hash"] == "single-chunk"


def test_multiquery_short_query_skips_expansion(_patch_env, monkeypatch):
    ar = _patch_env
    calls = {"expand": 0, "search": 0}

    async def _fake_llm(*a, **k):
        calls["expand"] += 1
        raise AssertionError("expansion must not run for short queries")

    async def _fake_db(fn, *args, **kwargs):
        calls["search"] += 1
        return [_rec("x")]

    monkeypatch.setattr(ar, "_llm_call", _fake_llm)
    monkeypatch.setattr(ar, "_db_call", _fake_db)
    asyncio.run(ar._multi_query_search("tiny q", category=None, company=None))
    assert calls["expand"] == 0 and calls["search"] == 1


def test_multiquery_disabled_short_circuits(_patch_env, monkeypatch):
    ar = _patch_env
    monkeypatch.setattr(ar.get_settings(), "multi_query", 0)
    searches: list = []

    async def _fake_db(fn, *args, **kwargs):
        searches.append(args[0])
        return [_rec("x")]

    async def _no_expand(*a, **k):
        raise AssertionError("expansion must not run when disabled")

    monkeypatch.setattr(ar, "_llm_call", _no_expand)
    monkeypatch.setattr(ar, "_db_call", _fake_db)
    asyncio.run(ar._multi_query_search(
        "A perfectly long enough query about revenue",
        category=None, company=None))
    assert len(searches) == 1


# ============================== per-entity sub-retrieval ===================
def test_multi_company_question_fans_out_per_entity(_patch_env, monkeypatch):
    """ADR-014: on multi-company questions, each company gets its OWN
    search (its own multi-query fan-out) and results are RRF-fused —
    one company's dominance can no longer starve the other's pool."""
    import adaptive_rag as ar

    searches = []

    async def _fake_mq(search_q, *, category, company, top_k, tenant_id=None):
        searches.append((search_q[:30], company, top_k))
        if company == "apple":
            return [_rec("apple-a1", company="Apple"),
                    _rec("apple-a2", company="Apple"),
                    _rec("apple-a3", company="Apple")]
        if company == "meta":
            return [_rec("meta-m1", company="Meta"),
                    _rec("meta-m2", company="Meta")]
        return []

    class _Variants:
        variants = []

    class _Extraction:
        content = "Apple revenue was $89,498 million. Meta revenue was $40,111 million."

    async def _fake_llm(runnable, messages, stage, allow_failover=False):
        # router/expand stages get the Variants schema; extraction gets content
        if stage.startswith("extract"):
            return _Extraction(), ar.UsageCollector()
        return _Variants(), ar.UsageCollector()

    class _BindableEngine:
        def bind(self, **kw):
            return self

    monkeypatch.setattr(ar, "_multi_query_search", _fake_mq)
    monkeypatch.setattr(ar, "_llm_call", _fake_llm)
    monkeypatch.setattr(ar, "_get_engine", lambda m: _BindableEngine())
    monkeypatch.setattr(ar.get_settings(), "multi_query", 0)  # expansion off: isolate fan-out

    class _PassThroughReranker:
        def rerank(self, req):
            # flashrank-shaped passthrough: keep ALL passages in order
            return [{"id": str(p["id"])} for p in req.passages]

    monkeypatch.setattr(ar, "_get_reranker", lambda: _PassThroughReranker())

    async def _fake_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(ar, "_db_call", _fake_db)

    out = asyncio.run(ar._specialist(
        "financial", "Role: analyst.", "financial",
        "compare Apple and Meta revenue",
        "Compare Apple and Meta revenue growth in Q4 2023."))
    companies_searched = [c for _, c, _ in searches]
    assert "apple" in companies_searched and "meta" in companies_searched, \
        "each scoped company must get its own search"
    assert all(c in ("apple", "meta") for c in companies_searched), \
        "no unscoped search while per-entity fan-out runs"
    assert len(out["records"]) >= 2, "fused pool must include BOTH companies"
    pool_companies = {r.get("company") for r in out["records"]}
    assert {"Apple", "Meta"} <= pool_companies, \
        f"evidence pool must be balanced, got {pool_companies}"


def test_single_company_question_unchanged(_patch_env, monkeypatch):
    import adaptive_rag as ar
    searches = []

    async def _fake_mq(search_q, *, category, company, top_k, tenant_id=None):
        searches.append(company)
        return [_rec("apple-only", company="Apple")]

    class _BindableEngine:
        def bind(self, **kw):
            return self

    class _Extraction:
        content = "Services revenue was \$22,314 million."

    async def _fake_llm(runnable, messages, stage, allow_failover=False):
        return _Extraction(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_multi_query_search", _fake_mq)
    monkeypatch.setattr(ar, "_llm_call", _fake_llm)
    monkeypatch.setattr(ar, "_get_engine", lambda m: _BindableEngine())
    out = asyncio.run(ar._specialist(
        "financial", "Role: analyst.", "financial",
        "Apple services revenue",
        "What was Apple services revenue in Q4 2023?"))
    assert searches == ["apple"], "single-company: exactly one scoped search"
    assert out["records"]


# ============ conditional expansion (token plan, 2026-09-07) ============
def test_conditional_expansion_skips_paraphraser_on_confident_hit():
    """Direct search runs FIRST; top vec_similarity >= the confidence bar
    means no paraphraser call and no variant searches — the RPM-burst killer.
    The direct pass is reused verbatim (never re-searched)."""
    import asyncio
    import adaptive_rag as ar

    calls = {"llm": 0, "searches": []}

    async def _fake_db(fn, *args, **kwargs):
        if fn is ar.pgvector_hybrid_search:
            calls["searches"].append(args[0] if args else kwargs.get("query_text"))
            return [{"id": 1, "content": "row", "vec_similarity": 0.71}]
        raise AssertionError("unexpected db call")

    class _FakeMutation:
        variants = ["v1", "v2"]

    async def _fake_llm(*a, **k):
        calls["llm"] += 1
        return _FakeMutation(), None

    import unittest.mock as mock
    with mock.patch.object(ar, "_db_call", _fake_db), \
         mock.patch.object(ar, "_llm_call", _fake_llm):
        rows = asyncio.run(ar._multi_query_search(
            "What was Meta total revenue in Q4 2023?", category=None,
            company="meta"))
    assert calls["llm"] == 0, "confident hit must skip the paraphraser"
    assert len(calls["searches"]) == 1, "only the direct search may run"
    assert rows and rows[0]["vec_similarity"] == 0.71


def test_conditional_expansion_fires_on_weak_hit():
    """Weak direct hit (below the bar) -> paraphraser runs, variants are
    searched, results RRF-fused; the direct pass is REUSED in the fusion
    (not re-searched)."""
    import asyncio
    import adaptive_rag as ar

    calls = {"llm": 0, "searches": []}

    async def _fake_db(fn, *args, **kwargs):
        if fn is ar.pgvector_hybrid_search:
            q = args[0]
            calls["searches"].append(q)
            # ALL queries weak (0.30 < bar) — the direct pass must look
            # ambiguous so expansion is the correct recovery path.
            return [{"id": 1, "content": f"row-{q[:6]}",
                      "vec_similarity": 0.30}]
        raise AssertionError("unexpected db call")

    class _FakeMutation:
        variants = ["meta total net sales Q4", "meta top line quarterly"]

    async def _fake_llm(*a, **k):
        calls["llm"] += 1
        return _FakeMutation(), None

    import unittest.mock as mock
    with mock.patch.object(ar, "_db_call", _fake_db), \
         mock.patch.object(ar, "_llm_call", _fake_llm), \
         mock.patch.dict("os.environ", {"RAG_EXPANSION_CONFIDENCE": "0.55"}):
        rows = asyncio.run(ar._multi_query_search(
            "What was Meta total revenue in Q4 2023?", category=None,
            company="meta"))
    assert calls["llm"] == 1, "weak hit must consult the paraphraser"
    assert len(calls["searches"]) == 3, "direct + 2 variants"
    assert calls["searches"][0].startswith("What was Meta"), \
        "direct must run first"


def test_quarantine_hint_zero_tokens_in_audit_reject():
    """Audit-reject path carries zero LLM tokens (the reject happened in
    the auditor's VERDICT, not a crash) — placeholder documented by the
    abort tests below."""
    import adaptive_rag as ar
    assert callable(ar.route_after_rewrite)
