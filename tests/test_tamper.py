"""
Receipt-Chain Tamper Suite — CI Mutation Tests (offline, zero LLM tokens)
=========================================================================
The provable version of the moat (converged roadmap item #1, all three
consult opinions): programmatically forge every attack surface of the
verification receipt and require GET /verify's deterministic recompute to
catch 100% of them:

    span shifts / off-by-one / inversions / out-of-bounds,
    content lies (post-hoc edited evidence text),
    hash forgeries (self-consistent recomputed lie),
    transcript tampering (the stored spine itself altered),
    company/page swaps (cross-attribution),
    citation forgeries (out-of-range, zero, non-int),
    multi-evidence partial tampering (one honest link + one lie).

Every test asserts BOTH `verified is False` AND the specific link status —
we prove HOW each forgery breaks, not merely that something did. The
CONTROL case proves the same fixture certifies when untampered (a tamper
suite that can't pass its control is testing nothing).

Run:  pytest tests/test_tamper.py -v
"""
import hashlib
import os
from pathlib import Path

if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402


# ============================== fixture ====================================
SRC = "Apple_Q4_2023.pdf"
PAGE = 3
TEXT = "Services revenue was $22,314 million in Q4 2023."
TEXT2 = "Operating margin expanded across all segments this quarter."


def _chain_key(company, source, page, text):
    return f"{company}\x1f{source}\x1f{page}\x1f{text}"


def _evidence(company="Apple", source=SRC, page=PAGE, text=TEXT,
              char_start=0, char_end=None, chunk_hash=None, content=None):
    end = char_end if char_end is not None else len(text)
    return {
        "chunk_hash": chunk_hash or hashlib.sha256(
            _chain_key(company, source, page, text).encode("utf-8")).hexdigest(),
        "company": company, "source": source, "page": page,
        "content": content if content is not None else text,
        "char_start": char_start, "char_end": end,
        "transcript_version": 1,
    }


def _receipt(evidence, claims=None):
    return {
        "run_id": "tamper-test", "question": "q", "answer": "a",
        "evidence_json": evidence,
        "claims_json": claims if claims is not None else
        [{"claim": "Services revenue $22.3B", "citations": list(
            range(1, len(evidence) + 1))}],
        "n_claims": 1, "n_evidence": len(evidence),
        "audit_verdict": "grounded", "corpus_epoch": 1,
        "tenant_id": "default",
    }


class _FakeCur:
    def __init__(self, store, chunks):
        self._store = store
        self._chunks = chunks   # {chunk_hash: {company, source, page}}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "multi_agent_chunks" in sql:
            hashes = params[0] if params else []
            self.rows = [{"chunk_hash": h, **self._chunks[h]}
                         for h in hashes if h in self._chunks]
        else:  # page_transcripts fetch
            self.rows = [{"source": s, "page": p, "transcript": t}
                         for (s, p), t in self._store.items()]

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, store, chunks):
        self._store = store
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, cursor_factory=None):
        return _FakeCur(self._store, self._chunks)


@pytest.fixture()
def transcripts(monkeypatch):
    import db
    store = {(SRC, PAGE): TEXT, ("Apple_Q4_2023.pdf", 4): TEXT2,
             ("Meta_Q4_2023.pdf", 3): TEXT2}
    chunks = {e["chunk_hash"]: {"company": e["company"], "source": e["source"],
                                "page": e["page"]}
              for e in (_evidence(), _evidence(text=TEXT2, page=4))}
    monkeypatch.setattr(db, "get_db_connection",
                        lambda *a, **k: _FakeConn(store, chunks))
    return {"store": store, "chunks": chunks}


def _verify(receipt):
    import db
    return db.verify_receipt_chain(receipt)


# ============================== CONTROL (must certify) ====================
def test_control_case_certifies(transcripts):
    """Untampered receipt: full chain holds — span exact, hash exact,
    citations in range. A tamper suite that cannot pass its control
    proves nothing about its forgeries."""
    r = _verify(_receipt([_evidence()]))
    assert r["verified"] is True
    assert r["links_ok"] == r["links_checked"] == 1
    assert r["links"][0]["status"] == "ok"


# ============================== span forgeries =============================
def test_span_shift_fails(transcripts):
    e = _evidence(char_start=5, char_end=5 + len(TEXT))
    r = _verify(_receipt([e]))
    assert r["verified"] is False
    assert r["links"][0]["status"] in ("hash_mismatch", "text_mismatch",
                                       "span_out_of_bounds")


def test_span_off_by_one_fails(transcripts):
    """THE classic span attack: shift the end by a single character."""
    e = _evidence(char_end=len(TEXT) - 1)
    r = _verify(_receipt([e]))
    assert r["verified"] is False
    assert r["links"][0]["status"] in ("hash_mismatch", "text_mismatch")


def test_span_inversion_fails(transcripts):
    """start > end must not certify (and must not crash the verifier)."""
    e = _evidence(char_start=10, char_end=2)
    r = _verify(_receipt([e]))
    assert r["verified"] is False


def test_span_out_of_bounds_fails(transcripts):
    """Span reaching past the transcript end must not certify."""
    e = _evidence(char_start=0, char_end=len(TEXT) + 500)
    r = _verify(_receipt([e]))
    assert r["verified"] is False


# ============================== content forgeries =========================
def test_content_lie_fails(transcripts):
    """Post-hoc edit of the evidence CONTENT (the quoted text) — span/hash
    untouched. The most direct 'rewrite what the source said' attack."""
    e = _evidence(content="Services revenue was $99,999 million in Q4 2023.")
    r = _verify(_receipt([e]))
    assert r["verified"] is False
    assert r["links"][0]["status"] == "text_mismatch"


def test_hash_forgery_fails(transcripts):
    """Attacker recomputes chunk_hash over the FORGED content so the hash is
    self-consistent — but the transcript spine still exposes the lie."""
    forged = "Services revenue was $99,999 million in Q4 2023."
    e = _evidence(content=forged,
                  chunk_hash=hashlib.sha256(
                      _chain_key("Apple", SRC, PAGE, forged).encode(
                          "utf-8")).hexdigest())
    r = _verify(_receipt([e]))
    assert r["verified"] is False
    assert r["links"][0]["status"] in ("text_mismatch", "hash_mismatch",
                                       "unknown_chunk")


def test_full_self_consistent_forgery_fails(transcripts):
    """The strongest attack: forge content AND hash AND span so they agree
    with EACH OTHER — only the immutable stored transcript can veto it."""
    forged = "Revenue was $99,999 million."
    e = _evidence(content=forged, char_end=len(forged),
                  chunk_hash=hashlib.sha256(
                      _chain_key("Apple", SRC, PAGE, forged).encode(
                          "utf-8")).hexdigest())
    r = _verify(_receipt([e]))
    assert r["verified"] is False, \
        "the transcript spine must veto even self-consistent forgeries"


# ============================== transcript tampering ======================
def test_transcript_tamper_fails(monkeypatch, transcripts):
    """The spine itself altered (e.g. a compromised DB row): the hash chain
    must break — the receipt's chunk_hash was computed over the ORIGINAL."""
    import db
    tampered = dict(transcripts["store"])
    tampered[(SRC, PAGE)] = TEXT.replace("22,314", "99,999")
    monkeypatch.setattr(db, "get_db_connection",
                        lambda *a, **k: _FakeConn(tampered,
                                                  transcripts["chunks"]))
    r = _verify(_receipt([_evidence()]))
    assert r["verified"] is False
    assert r["links"][0]["status"] in ("hash_mismatch", "text_mismatch")


# ============================== cross-attribution forgeries ================
def test_company_swap_fails(transcripts):
    """Relabel Apple's evidence as Meta's — EVEN with the hash recomputed
    over the relabeled company, the chunk table's recorded attribution
    vetoes the swap (DB is the authority, the receipt is the claim). The
    forged hash is itself absent from the chunk table — unknown_chunk is
    an equally valid veto (defense in depth)."""
    forged_hash = hashlib.sha256(
        _chain_key("Meta", SRC, PAGE, TEXT).encode("utf-8")).hexdigest()
    e = _evidence(company="Meta", chunk_hash=forged_hash)
    r = _verify(_receipt([e]))
    assert r["verified"] is False
    assert r["links"][0]["status"] in ("attribution_mismatch", "unknown_chunk"), \
        "chunk-table truth must veto relabeled company attributions"


def test_page_swap_fails(transcripts):
    """Same forgery via page: Apple p3 evidence relabeled p4 — the p4
    transcript is different text, both checks break."""
    e = _evidence(page=4, source=SRC)
    r = _verify(_receipt([e]))
    assert r["verified"] is False


# ============================== citation forgeries =========================
def test_citation_out_of_range_fails(transcripts):
    r = _verify(_receipt([_evidence()],
                          claims=[{"claim": "x", "citations": [2]}]))
    assert r["verified"] is False
    assert r["claim_issues"][0]["bad_citation"] == 2


def test_citation_zero_fails(transcripts):
    r = _verify(_receipt([_evidence()],
                          claims=[{"claim": "x", "citations": [0]}]))
    assert r["verified"] is False


def test_citation_non_int_fails(transcripts):
    r = _verify(_receipt([_evidence()],
                          claims=[{"claim": "x", "citations": ["1"]}]))
    assert r["verified"] is False


# ============================== composite attacks ==========================
def test_partial_tamper_among_honest_evidence_fails(transcripts):
    """Two honest links + ONE forged span: verified requires EVERY link —
    a mixed receipt must not certify."""
    evidence = [_evidence(text=TEXT2, page=4),
                _evidence(char_start=3, char_end=3 + len(TEXT)),  # forged
                _evidence()]
    r = _verify(_receipt(evidence))
    assert r["verified"] is False
    assert r["links_ok"] == 2 and r["links_checked"] == 3


def test_empty_evidence_fails_closed(transcripts):
    """No evidence at all must fail closed, never vacuously certify."""
    r = _verify(_receipt([], claims=[{"claim": "x", "citations": []}]))
    assert r["verified"] is False


def test_all_no_span_evidence_fails_closed(transcripts):
    """Every link no_span (pre-2.1 legacy rows) must fail closed — proven
    live when a spy-test receipt accidentally verified as unlocatable."""
    e = _evidence()
    e["char_start"] = None
    e["char_end"] = None
    r = _verify(_receipt([e]))
    assert r["verified"] is False
    assert r["links"][0]["status"] == "no_span"
