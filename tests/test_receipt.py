"""
Verification Receipt Layer — Unit Tests (offline, zero tokens)
================================================================
Covers Phase A (ADR-005): span lineage mapping (ingest), claim extraction,
receipt evidence projection, and the DETERMINISTIC hash-chain verifier
(db.verify_receipt_chain with transcript lookups monkeypatched — no DB).

The chain under test: claim [n] -> evidence[n-1] -> (source, page, span) ->
page transcript slice -> sha256(company⊣source⊣page⊣slice) == chunk_hash.
A tampered span, content, or citation must break the chain by construction.

Run:  pytest tests/test_receipt.py -v
"""
import asyncio
import hashlib
import os
from pathlib import Path

# Same dummy-guard pattern as tests/test_db.py for bare CI environments.
if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402


# ============================== span mapping (ingest) ======================
# v2.2 replaced find()-based span mapping with construction-exact offsets:
# the page transcript is BUILT from the emitted chunks, so
# transcript[char_start:char_end] == chunk.text holds by construction.
# The end-to-end proof lives in tests/test_tables.py (synthetic PDF through
# parse_pdf_stream -> transcript slice -> hash chain).


# ============================== chunk metadata contract ====================
def test_chunk_metadata_accepts_spans():
    from ingest import ChunkMetadata
    m = ChunkMetadata(company="Apple", source="Apple_Q4_2023.pdf", page=3,
                      year=2023, quarter="Q4", category="financial",
                      section_title="RESULTS", char_start=10, char_end=700,
                      transcript_version=1)
    assert m.char_start == 10 and m.char_end == 700


def test_chunk_metadata_spans_optional():
    from ingest import ChunkMetadata
    m = ChunkMetadata(company="Apple", source="Apple_Q4_2023.pdf", page=3,
                      year=2023, quarter="Q4", category="financial",
                      section_title="RESULTS")
    assert m.char_start is None and m.char_end is None, \
        "pre-2.1 rows and not-locatable chunks stay valid"


# ============================== claim extraction ===========================
def test_extract_claims_sentence_split_with_citations():
    from adaptive_rag import extract_claims
    draft = ("Apple Services revenue was $22.3B [1]. Operating margin held at "
             "91.7% [1][2]. The quarter saw no major headwinds.")
    claims = extract_claims(draft, 2)
    assert len(claims) == 3
    assert claims[0]["citations"] == [1]
    assert claims[1]["citations"] == [1, 2]
    assert claims[2]["citations"] == [], "uncited claims are recorded, visible"


def test_extract_claims_strips_markdown_headers():
    from adaptive_rag import extract_claims
    draft = "### Executive Summary\nRevenue rose [1].\n### Ledger\nDone."
    claims = extract_claims(draft, 1)
    assert all(not c["claim"].startswith("#") for c in claims)
    assert any("Revenue rose" in c["claim"] for c in claims)


def test_extract_claims_ignores_out_of_range():
    from adaptive_rag import extract_claims
    claims = extract_claims("Margin [99] held.", 2)
    assert claims[0]["citations"] == [], "pre-audit rejects [99] before this runs"


# ============================== evidence projection ========================
def test_build_receipt_evidence_keeps_lineage_fields():
    from adaptive_rag import build_receipt_evidence
    records = [{"chunk_hash": "a" * 64, "company": "Apple",
                "source": "Apple_Q4_2023.pdf", "page": 3,
                "content": "Services revenue...", "char_start": 0,
                "char_end": 50, "transcript_version": 1,
                "contains_table": True, "arithmetic_ok": False}]
    ev = build_receipt_evidence(records)
    assert ev == [{"chunk_hash": "a" * 64, "company": "Apple",
                   "source": "Apple_Q4_2023.pdf", "page": 3,
                   "content": "Services revenue...", "char_start": 0,
                   "char_end": 50, "transcript_version": 1,
                   "contains_table": True, "arithmetic_ok": False}]


def test_build_receipt_evidence_survives_spanless_records():
    from adaptive_rag import build_receipt_evidence
    ev = build_receipt_evidence([{"chunk_hash": "b" * 64, "content": "legacy",
                                  "company": None, "source": None, "page": None}])
    assert ev[0]["char_start"] is None and ev[0]["chunk_hash"] == "b" * 64


# ============================== deterministic chain verify ================
def _make_evidence(company: str, source: str, page: int, text: str,
                   tamper_span=None, tamper_content=None):
    key = f"{company}\x1f{source}\x1f{page}\x1f{text}"
    chunk_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    rec = {"chunk_hash": chunk_hash, "company": company, "source": source,
           "page": page, "content": tamper_content or text,
           "char_start": 0, "char_end": len(text), "transcript_version": 1}
    if tamper_span is not None:
        rec["char_end"] = tamper_span
    return rec


@pytest.fixture()
def _patch_transcripts(monkeypatch):
    """Intercepts BOTH DB fetches inside verify_receipt_chain (the page
    transcripts AND the chunk-table attribution truth); everything else is
    pure hashing."""
    import db
    store = {"transcripts": {}, "chunks": {}}
    monkeypatch.setattr(
        db, "get_db_connection",
        lambda *a, **k: _FakeConn(store))
    return store


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, cursor_factory=None):
        return _FakeCur(self._store)


class _FakeCur:
    def __init__(self, store):
        self._store = store
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "multi_agent_chunks" in sql:
            hashes = params[0] if params else []
            self.rows = [{"chunk_hash": h, **self._store["chunks"][h]}
                         for h in hashes if h in self._store["chunks"]]
        else:  # page_transcripts fetch
            self.rows = [
                {"source": s, "page": p, "transcript": t}
                for (s, p), t in self._store["transcripts"].items()
            ]

    def fetchall(self):
        return self.rows


def _register(store, evidence):
    """Registers a receipt-evidence record with the fake DB: its page
    transcript AND its chunk-table attribution row (the verifier now
    cross-checks company/source/page against the chunk table)."""
    store["transcripts"][(evidence["source"], evidence["page"])] = \
        evidence["content"]
    store["chunks"][evidence["chunk_hash"]] = {
        "company": evidence["company"], "source": evidence["source"],
        "page": evidence["page"]}


def test_chain_verifies_clean_receipt(_patch_transcripts):
    import db
    text = "Services revenue was $22,314 million."
    ev = _make_evidence("Apple", "Apple_Q4_2023.pdf", 3, text)
    _register(_patch_transcripts, ev)
    receipt = {"evidence_json": [ev],
               "claims_json": [{"claim": "Services revenue $22.3B", "citations": [1]}],
               "tenant_id": "default", "corpus_epoch": 1}
    result = db.verify_receipt_chain(receipt)
    assert result["verified"] is True
    assert result["links_ok"] == result["links_checked"] == 1


def test_chain_catches_tampered_span(_patch_transcripts):
    import db
    text = "Services revenue was $22,314 million."
    ev = _make_evidence("Apple", "Apple_Q4_2023.pdf", 3, text)
    _register(_patch_transcripts, ev)
    # Forged span points at the wrong slice -> hash AND text break.
    receipt = {"evidence_json": [_make_evidence("Apple", "Apple_Q4_2023.pdf", 3,
                                                 text, tamper_span=10)],
               "claims_json": [], "tenant_id": "default", "corpus_epoch": 1}
    result = db.verify_receipt_chain(receipt)
    assert result["verified"] is False
    assert result["links_ok"] == 0


def test_chain_catches_tampered_content(_patch_transcripts):
    import db
    text = "Services revenue was $22,314 million."
    ev = _make_evidence("Apple", "Apple_Q4_2023.pdf", 3, text)
    _register(_patch_transcripts, ev)
    # Content field altered post-hoc (the lie) while span/hash stay original.
    receipt = {"evidence_json": [_make_evidence("Apple", "Apple_Q4_2023.pdf", 3,
                                                 text, tamper_content="Revenue was $99B.")],
               "claims_json": [], "tenant_id": "default", "corpus_epoch": 1}
    result = db.verify_receipt_chain(receipt)
    assert result["verified"] is False
    assert result["links"][0]["status"] == "text_mismatch"


def test_chain_catches_bad_claim_citation(_patch_transcripts):
    import db
    text = "Services revenue was $22,314 million."
    ev = _make_evidence("Apple", "Apple_Q4_2023.pdf", 3, text)
    _register(_patch_transcripts, ev)
    receipt = {"evidence_json": [ev],
               "claims_json": [{"claim": "X", "citations": [7]}],  # out of range
               "tenant_id": "default", "corpus_epoch": 1}
    result = db.verify_receipt_chain(receipt)
    assert result["verified"] is False
    assert result["claim_issues"][0]["bad_citation"] == 7


def test_chain_fails_closed_without_spans(_patch_transcripts):
    import db
    receipt = {"evidence_json": [{"chunk_hash": "c" * 64, "company": "Apple",
                                  "source": "s.pdf", "page": 1, "content": "legacy row",
                                  "char_start": None, "char_end": None}],
               "claims_json": [], "tenant_id": "default", "corpus_epoch": 1}
    result = db.verify_receipt_chain(receipt)
    assert result["verified"] is False, "zero locatable spans must never certify"


def test_chain_fails_closed_on_missing_transcript(_patch_transcripts):
    import db
    text = "Services revenue was $22,314 million."
    # No transcript loaded for this page -> transcript_missing -> not verified.
    receipt = {"evidence_json": [_make_evidence("Apple", "Apple_Q4_2023.pdf", 3, text)],
               "claims_json": [], "tenant_id": "default", "corpus_epoch": 1}
    result = db.verify_receipt_chain(receipt)
    assert result["verified"] is False
    assert result["links"][0]["status"] == "transcript_missing"


# ============================== guard saves receipt ========================
def test_guard_saves_receipt_on_certified(monkeypatch):
    """fact_checker_guard must persist the receipt when the audit certifies —
    captured through a spy on save_verification_receipt."""
    import adaptive_rag as ar

    saved = {}

    class _Audit:
        grounded = True
        explanation = None

    async def _spy_llm(runnable, messages, stage):
        return _Audit(), ar.UsageCollector()

    def _spy_save(run_id, question, answer, claims, evidence, audit_verdict,
                  contradictions=None, tenant_id=None):
        saved.update(run_id=run_id, claims=claims, evidence=evidence,
                     verdict=audit_verdict, contradictions=contradictions)
        return True

    async def _spy_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(ar, "_llm_call", _spy_llm)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "_db_call", _spy_db)
    monkeypatch.setattr(ar, "save_verification_receipt", _spy_save)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)

    state = {
        "original_question": "What was Apple services revenue?",
        "search_query": "apple services revenue",
        "documents": ["Apple | aapl | Page 5\nServices: 22,314",
                      "Apple | aapl | Page 6\nMore"],
        "evidence_records": [
            {"chunk_hash": "d" * 64, "company": "Apple", "source": "aapl",
             "page": 5, "content": "Services: 22,314", "char_start": 0,
             "char_end": 16, "transcript_version": 1},
            {"chunk_hash": "e" * 64, "company": "Apple", "source": "aapl",
             "page": 6, "content": "More", "char_start": 0, "char_end": 4,
             "transcript_version": 1}],
        "final_executive_report": "Services revenue grew per [1] and [2].",
        "degraded_agents": [], "retry_count": 0, "run_id": "run-t1",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is True
    assert saved["run_id"] == "run-t1"
    assert saved["verdict"] == "grounded"
    assert len(saved["evidence"]) == 2
    assert saved["claims"][0]["citations"] == [1, 2]


# ============================== full-width citation drift ===================
def test_extract_claims_handles_fullwidth_brackets():
    """Groq llama-4-scout drifts to 【1】 citations; receipts must still
    attribute claims (regression for the silent 'all claims uncited' bug)."""
    from adaptive_rag import extract_claims
    claims = extract_claims("Total revenue was $40.111 billion \u30101\u3011. "
                            "Free cash flow reached $11.5B \u30102\u3011.", 2)
    assert claims[0]["citations"] == [1]
    assert claims[1]["citations"] == [2]


def test_pre_audit_catches_out_of_range_fullwidth():
    from adaptive_rag import citation_pre_audit
    assert citation_pre_audit("margin \u301099\u3011 held", 5) == "\u301099\u3011"
    assert citation_pre_audit("margin \u30102\u3011 held", 5) is None


# ============================== reasoning-leak guard (bug-hunt) ============
def test_strip_reasoning_cuts_prompt_echo():
    from adaptive_rag import _strip_reasoning
    leaked = ('We need to answer: "What was EPS?" Provide executive brief.\n'
              '### Executive Summary\nTesla delivered diluted EPS of $2.27 '
              'for Q4 2023 [1].')
    out = _strip_reasoning(leaked)
    assert "We need to answer" not in out
    assert "### Executive Summary" in out and "$2.27" in out


def test_strip_reasoning_pure_deliberation_yields_empty():
    from adaptive_rag import _strip_reasoning
    leaked = ("We need to answer the question. Let's check evidence [12]. "
              "However we need Q4 data. The question asks for EPS.")
    assert _strip_reasoning(leaked) == "", \
        "deliberation-only text must quarantine, not surface"


def test_strip_reasoning_clean_draft_untouched():
    from adaptive_rag import _strip_reasoning
    clean = "### Executive Summary\nServices revenue was $22,314 million [1]."
    assert _strip_reasoning(clean) == clean


def test_guard_rejects_leaked_draft_pre_audit(monkeypatch):
    """The audit must NEVER certify a draft containing prompt-echo."""
    import adaptive_rag as ar
    auditor_calls = {"n": 0}

    class _Audit:
        grounded = True

    async def _spy(runnable, messages, stage):
        auditor_calls["n"] += 1
        return _Audit(), ar.UsageCollector()

    monkeypatch.setattr(ar, "_llm_call", _spy)
    monkeypatch.setattr(ar, "_get_checker", lambda: object())
    monkeypatch.setattr(ar, "save_verification_receipt", lambda *a, **k: True)
    monkeypatch.setattr(ar.get_settings(), "disable_cache_writes", True)
    import asyncio
    state = {
        "original_question": "EPS?", "search_query": "eps",
        "documents": ["Tesla | t.pdf | Page 1\nEPS 2.27"],
        "evidence_records": [],
        "final_executive_report":
            "We need to answer the question. Let's check evidence [1].",
        "degraded_agents": [], "retry_count": 0, "run_id": "t",
        "tenant_id": "default",
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0}
    upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd["grounded"] is False
    assert upd.get("echo_reject") is True
    assert auditor_calls["n"] == 0, "leaked drafts die before the auditor"


# ============================== cache-replay provenance (Gauntlet-4) ======
def test_cache_hit_threads_provenance_run_id():
    """Gauntlet-4 finding (2026-09-05): a cache replay skips the fleet, so
    the replay run_id has NO receipt — the cache payload must carry the
    ORIGINAL certification run_id so /verify can resolve the true proof.
    NOTE: check_semantic_cache is SYNC (called via _db_call/to_thread) —
    async mocks return never-awaited coroutines, the exact confusion class
    from the Gauntlet-4 diagnosis."""
    import asyncio
    import adaptive_rag as ar

    def _cached(*a, **k):
        return {"answer": "certified answer [1]", "provenance_run_id": "orig-run-1"}

    async def _db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(ar, "check_semantic_cache", _cached), \
         mock.patch.object(ar, "_db_call", _db):
        state = {"original_question": "q?", "retry_count": 0,
                 "run_id": "replay-run-9", "tenant_id": "default"}
        upd = asyncio.run(ar.check_cache_node(state))
        assert upd.get("cached_hit") is True
        assert upd.get("provenance_run_id") == "orig-run-1", \
            "the replay must expose the ORIGINAL certification run_id"


def test_cache_miss_has_no_provenance_override():
    import asyncio
    import adaptive_rag as ar

    def _miss(*a, **k):
        return None

    async def _db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(ar, "check_semantic_cache", _miss), \
         mock.patch.object(ar, "_db_call", _db):
        state = {"original_question": "q?", "retry_count": 0,
                 "run_id": "fresh-run-1", "tenant_id": "default"}
        upd = asyncio.run(ar.check_cache_node(state))
        assert upd.get("cached_hit") is False
        assert not upd.get("provenance_run_id")


def test_cache_read_bypass_measure_pipeline_not_cache():
    """ADR-014 measurement integrity: with RAG_DISABLE_CACHE_READ=1 the
    node must NOT consult the cache even when a near-identical certified
    answer exists — coverage runs measure the live pipeline, and recall
    must not drift toward 100% as the cache fills. check_semantic_cache is
    SYNC (async mocks return never-awaited coroutines)."""
    import asyncio
    import adaptive_rag as ar

    def _cached(*a, **k):
        return {"answer": "certified answer [1]", "provenance_run_id": "orig-run-1"}

    calls = {"n": 0}

    def _tracking(*a, **k):
        calls["n"] += 1
        return _cached(*a, **k)

    async def _db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    import unittest.mock as mock
    with mock.patch.dict(os.environ, {"RAG_DISABLE_CACHE_READ": "1"}), \
         mock.patch.object(ar, "check_semantic_cache", _tracking), \
         mock.patch.object(ar, "_db_call", _db):
        state = {"original_question": "q?", "retry_count": 0,
                 "run_id": "fresh-run-2", "tenant_id": "default"}
        upd = asyncio.run(ar.check_cache_node(state))
        assert upd.get("cached_hit") is False, "eval mode must bypass cache reads"
        assert calls["n"] == 0, "cache must never be consulted in eval mode"
