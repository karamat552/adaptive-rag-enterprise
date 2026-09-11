"""
ELITE-2 as a PERMANENT CI HARNESS — seeded receipt-chain fuzzing (offline)
==========================================================================
Institutional-audit remediation (2026-09-10): the 1,000-mutation fuzz ran
as a one-off session artifact; an auditor requires reproducible evidence.
This file makes it a committed, deterministic, zero-network pytest.

Method: generate ONE canonical verified receipt (claims + evidence with
construction-exact spans + transcripts + chunk truth), then apply SEEDED
mutations across the realistic forgery classes and assert EVERY mutation
either (a) fails the chain with a NAMED status, or (b) leaves verification
correctly True because the mutation was semantically neutral (whitespace
in a non-span field). Zero false accepts means: NO mutation may turn a
broken receipt True, and NO mutation may turn a sound receipt True for a
different reason than integrity.

The mutations attack the PRODUCTION verify path (db.verify_receipt_chain)
through its two fetch layers (transcript fetch + chunk-truth fetch),
monkeypatched to in-memory dicts — the algorithm under test is the
shipped one, not a copy.

Run:  pytest tests/test_fuzz.py -q          (~1,000+ seeds, seconds)
      FUZZ_N=5000 pytest tests/test_fuzz.py # scale up for hardening runs
"""
import hashlib
import json
import os
import random
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import adaptive_rag as ar  # noqa: E402
import db  # noqa: E402


# ---------------------------------------------------------------------------
# Canonical fixture — a MINIATURE but structurally exact certified receipt.
# ---------------------------------------------------------------------------
def _canonical_receipt(seed: int = 42) -> dict:
    """One sound receipt: 2 evidence links, 2 claims, exact spans."""
    rng = random.Random(seed)
    transcripts, evidence = {}, []
    for i in (1, 2):
        page_text = (
            f"Meta Q4 2023 revenue was $40,111 million. "
            f"Segment detail row {i} with filler text to span. " * 4
        )
        source, page = "Meta_Q4_2023.pdf", 3 + i
        transcripts[(source, page)] = page_text
        cs = rng.randrange(0, 40)
        ce = cs + rng.randrange(80, 140)
        if ce > len(page_text):
            ce = len(page_text)
        slice_text = page_text[cs:ce]
        chunk_hash = hashlib.sha256(
            f"Meta\x1f{source}\x1f{page}\x1f{slice_text}".encode("utf-8")
        ).hexdigest()
        evidence.append({
            "chunk_hash": chunk_hash, "company": "Meta", "source": source,
            "page": page, "content": slice_text,
            "char_start": cs, "char_end": ce, "transcript_version": 1,
        })
    claims = [
        {"claim": "Meta revenue was $40,111 million in Q4 2023.",
         "citations": [1]},
        {"claim": "Segment detail was disclosed in the filing.",
         "citations": [2]},
    ]
    return {
        "run_id": f"fuzz-{uuid.UUID(int=seed).hex[:12]}",
        "question": "Meta revenue?", "answer": "Meta revenue...",
        "claims_json": claims, "evidence_json": evidence,
        "n_claims": 2, "n_evidence": 2, "audit_verdict": "grounded",
        "corpus_epoch": 9, "tenant_id": "default",
    }, transcripts


def _chain(receipt, transcripts, monkeypatch):
    """Drive the PRODUCTION verify_receipt_chain with in-memory fetches."""
    tid = receipt.get("tenant_id", "default")
    needed = bool(receipt.get("evidence_json"))

    def _fake_conn(*a, **k):
        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                self._rows = []
                sql_l = sql.lower()
                if "from multi_agent_chunks" in sql_l:
                    hashes = params[0]
                    truth = {}
                    for e in receipt["evidence_json"]:
                        if e["chunk_hash"] in hashes:
                            truth[e["chunk_hash"]] = {
                                "company": e["company"], "source": e["source"],
                                "page": e["page"]}
                    self._rows = [(None, {"chunk_hash": h, "company": t["company"],
                                          "source": t["source"], "page": t["page"]})
                                  for h, t in truth.items()]
                elif "from page_transcripts" in sql_l:
                    epoch, tenant = params[0], params[1]
                    self._rows = [(None, {"source": s, "page": p,
                                          "transcript": t})
                                  for (s, p), t in transcripts.items()
                                  if str(epoch) == str(receipt["corpus_epoch"])]
            def fetchall(self):
                return [r[1] for r in self._rows]

            def fetchone(self):
                return self._rows[0][1] if self._rows else None

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self, cursor_factory=None):
                return _Cur()

        return _Conn()

    monkeypatch.setattr(db, "get_db_connection", _fake_conn)
    return db.verify_receipt_chain(receipt)


# ---------------------------------------------------------------------------
# Sanity: the canonical receipt verifies True (the fuzz baseline).
# ---------------------------------------------------------------------------
def test_canonical_receipt_verifies(monkeypatch):
    receipt, transcripts = _canonical_receipt()
    result = _chain(receipt, transcripts, monkeypatch)
    assert result["verified"] is True, f"baseline must be True: {result}"
    assert result["links_ok"] == result["links_checked"] == 2


# ---------------------------------------------------------------------------
# The mutation operators — each is a realistic forgery/attack class.
# ---------------------------------------------------------------------------
def _mutate(receipt: dict, transcripts: dict, rng: random.Random) -> tuple:
    """Applies one seeded mutation. Returns (receipt, transcripts, class).
    Receipt is deep-copied; transcripts are copied for the mutating ops."""
    r = json.loads(json.dumps(receipt))
    cls = rng.choice(list(MUTATIONS.keys()))
    r2, t2 = MUTATIONS[cls](r, dict(transcripts), rng)
    return r2, t2, cls


MUTATIONS = {}


def _register(fn):
    MUTATIONS[fn.__name__] = fn
    return fn


@_register
def shift_span(receipt, transcripts, rng):
    """The classic off-by-N span drift."""
    e = rng.choice(receipt["evidence_json"])
    e["char_start"] = max(0, e["char_start"] + rng.choice([-5, -1, 1, 5]))
    return receipt, transcripts


@_register
def shrink_span(receipt, transcripts, rng):
    """Truncation attack — span shortened hoping slice still matches."""
    e = rng.choice(receipt["evidence_json"])
    e["char_end"] = e["char_start"] + max(1, (e["char_end"] - e["char_start"]) // 2)
    return receipt, transcripts


@_register
def out_of_bounds_span(receipt, transcripts, rng):
    """Span claims bytes past the transcript end."""
    e = rng.choice(receipt["evidence_json"])
    e["char_end"] = 10**7 + rng.randrange(100)
    return receipt, transcripts


@_register
def forge_hash(receipt, transcripts, rng):
    """Random or borrowed-hash forgery."""
    e = rng.choice(receipt["evidence_json"])
    if len(receipt["evidence_json"]) > 1 and rng.random() < 0.5:
        other = [x for x in receipt["evidence_json"] if x is not e][0]
        e["chunk_hash"] = other["chunk_hash"]      # borrowed — the subtle one
    else:
        e["chunk_hash"] = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()
    return receipt, transcripts


@_register
def relabel_company(receipt, transcripts, rng):
    """Company relabeling — the self-consistent re-hash attack requires the
    DB truth to disagree; against the DB-as-authority join it must fail."""
    e = rng.choice(receipt["evidence_json"])
    e["company"] = rng.choice(["Apple", "Tesla", "Meta Inc", "META"])
    return receipt, transcripts


@_register
def swap_content(receipt, transcripts, rng):
    """Content swapped for plausible-sounding other text."""
    e = rng.choice(receipt["evidence_json"])
    e["content"] = "Revenue was $50,000 million according to the filing."
    return receipt, transcripts


@_register
def drop_transcript_page(receipt, transcripts, rng):
    """Transcript row removed — simulate a page lost from the ledger."""
    key = rng.choice(list(transcripts.keys()))
    del transcripts[key]
    return receipt, transcripts


@_register
def mutate_transcript_text(receipt, transcripts, rng):
    """A single character INSIDE A CITED SPAN is altered — the byte-level
    immutability guarantee must catch it via hash mismatch. Fuzz-oracle
    lesson (first committed run, seed 20260910): 69/1000 early mutations
    flipped bytes OUTSIDE every cited span — the sliced hash is untouched
    and verification CORRECTLY passes; that was an oracle false positive,
    not a chain escape. A transcript mutation is only an attack when it
    lands within [char_start, char_end) of the evidence citing it."""
    by_source = {}
    for e in receipt["evidence_json"]:
        by_source.setdefault((e["source"], e["page"]), []).append(e)
    candidates = [k for k in by_source if k in transcripts]
    if not candidates:
        return receipt, transcripts
    key = rng.choice(candidates)
    ev = rng.choice(by_source[key])
    t = transcripts[key]
    lo = max(0, ev["char_start"])
    hi = min(len(t), ev["char_end"])
    if hi - lo < 2:
        return receipt, transcripts
    pos = rng.randrange(lo, hi)
    transcripts[key] = t[:pos] + chr((ord(t[pos]) + 1) % 128 or 33) + t[pos + 1:]
    return receipt, transcripts


@_register
def bad_citation_index(receipt, transcripts, rng):
    """Claim cites an out-of-range evidence index."""
    c = rng.choice(receipt["claims_json"])
    c["citations"] = [len(receipt["evidence_json"]) + rng.randrange(1, 5)]
    return receipt, transcripts


@_register
def verdict_flip(receipt, transcripts, rng):
    """Forged audit verdict on an otherwise sound chain — claims integrity
    is orthogonal to the verdict field; the CHAIN stays True, which is
    correct behavior (chain verifies custody, not the verdict string)."""
    receipt["audit_verdict"] = rng.choice(["ungrounded", "", "GROUNDED"])
    return receipt, transcripts


# ---------------------------------------------------------------------------
# The fuzz run: N seeded mutations, zero false accepts.
# ---------------------------------------------------------------------------
FUZZ_N = int(os.getenv("FUZZ_N", "1000"))


def test_fuzz_thousand_mutations_zero_false_accepts(monkeypatch):
    """ELITE-2, permanent form. Every mutated receipt must either verify
    False with a named reason (an attack caught) or — for semantically
    neutral mutations — verify with the SAME integrity verdict as its
    class intends. A mutated receipt that verifies True where the class
    demands False is a FALSE ACCEPT: the harness fails the build."""
    rng = random.Random(20260910)          # deterministic seed — CI-stable
    # Classes that MUST break verification when applied non-trivially:
    must_fail = {"shift_span", "shrink_span", "out_of_bounds_span",
                 "forge_hash", "relabel_company", "swap_content",
                 "drop_transcript_page", "mutate_transcript_text",
                 "bad_citation_index"}
    # Classes that are neutral-by-design (documented): verdict_flip touches
    # a field the chain deliberately does not adjudicate.
    neutral = {"verdict_flip"}
    stats = {}
    false_accepts = []
    for i in range(FUZZ_N):
        base, transcripts = _canonical_receipt(seed=i)
        mutated, t_mut, cls = _mutate(base, transcripts, rng)
        result = _chain(mutated, t_mut, monkeypatch)
        caught = result.get("verified") is not True or result.get("claims_ok") is False
        stats[cls] = stats.get(cls, 0) + 1
        if cls in must_fail and not caught:
            false_accepts.append((i, cls, result.get("reason")))
        if cls in neutral and result.get("verified") is not True:
            false_accepts.append((i, cls, f"neutral class broke chain: {result.get('reason')}"))
    assert not false_accepts, (
        f"{len(false_accepts)} FALSE ACCEPTS out of {FUZZ_N} mutations — "
        f"first: {false_accepts[:3]}")
    assert sum(stats.values()) == FUZZ_N
    # Every class must have been exercised.
    assert set(stats.keys()) == set(MUTATIONS.keys()), stats


def test_fuzz_seed_determinism():
    """CI reproducibility: same seed -> same mutation sequence."""
    rng_a = random.Random(7)
    rng_b = random.Random(7)
    receipt, transcripts = _canonical_receipt()
    seq_a = [_mutate(receipt, transcripts, rng_a)[2] for _ in range(20)]
    seq_b = [_mutate(receipt, transcripts, rng_b)[2] for _ in range(20)]
    assert seq_a == seq_b
