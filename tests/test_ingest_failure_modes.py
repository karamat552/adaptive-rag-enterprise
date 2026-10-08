"""The ingestion engine's safety nets — watched FAILING deliberately.

The honest edge, closed (2026-10-07): the ingestion code's failure modes
were read but never exercised live. These tests feed it deliberate
failures and watch each safety net fire:

  1. the transcript spine on the REAL committed corpus (the invariant:
     transcript[char_start:char_end] == chunk.text — by construction;
     if this ever breaks, the receipt chain's slice-and-hash is broken);
  2. a non-PDF payload -> PermanentDownloadError + the .part cleaned;
  3. HTTP 404 -> PermanentDownloadError (fail fast, no retry);
  4. HTTP 429 -> RetryableDownloadError (retryable class);
  5. a mid-download connection drop -> RetryableDownloadError + .part cleaned;
  6. an oversize table row stays WHOLE (a split row is a wrong row);
  7. a non-additive total is FLAGGED, never silently dropped;
  8. a dead run NEVER clobbers the last good corpus (atomicity).

Offline: fake sessions (no network), temp dirs, the committed corpus.
Zero tokens, no keys.
"""
import json
from pathlib import Path

import pytest

from ingest import (
    PermanentDownloadError,
    RetryableDownloadError,
    Settings,
    _fetch_once,
    _group_table_rows,
    run_ingestion,
    verify_table_arithmetic,
)

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. the transcript spine — on the REAL committed corpus
# ---------------------------------------------------------------------------
def test_transcript_spine_invariant_on_committed_corpus():
    """THE load-bearing invariant, verified on every committed chunk:
    transcript[char_start:char_end] == chunk.text. If this ever breaks,
    /verify's slice-and-hash chain is broken with it."""
    corpus = REPO / "corpus_chunks.jsonl"
    transcripts_path = REPO / "corpus_chunks.transcripts.jsonl"
    assert corpus.exists() and transcripts_path.exists()

    transcripts: dict = {}
    for ln in transcripts_path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        transcripts[(rec["source"], rec["page"])] = rec["transcript"]

    checked = 0
    for ln in corpus.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        chunk = json.loads(ln)
        m = chunk["metadata"]
        t = transcripts.get((m["source"], m["page"]))
        assert t is not None, \
            f"{m['source']} p.{m['page']} has no committed transcript"
        cs, ce = m.get("char_start"), m.get("char_end")
        assert cs is not None and ce is not None, \
            f"chunk {chunk['chunk_hash'][:12]} has no span (None = not locatable)"
        assert t[cs:ce] == chunk["text"], (
            f"SPAN BROKEN for {chunk['chunk_hash'][:12]} "
            f"({m['source']} p.{m['page']} [{cs}:{ce}]) — the receipt "
            f"chain's slice-and-hash would be broken")
        checked += 1
    assert checked >= 200, f"only {checked} chunks checked — corpus shrank?"


# ---------------------------------------------------------------------------
# fake session machinery (no network)
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, chunks, status=200, headers=None):
        self._chunks = chunks
        self.status_code = status   # the REAL requests attribute name
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1024):
        for c in self._chunks:
            yield c


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, stream=True, timeout=None):
        return self._resp


def _fetch(tmp_path, session):
    doc_path = tmp_path / "test.pdf"
    doc = type("D", (), {"filename": "test.pdf", "url": "https://x/y.pdf"})()
    settings = Settings(data_dir=tmp_path)
    _fetch_once(session, doc, doc_path, settings)
    return doc_path


# ---------------------------------------------------------------------------
# 2-5. the download safety nets
# ---------------------------------------------------------------------------
def test_non_pdf_payload_fails_fast_and_cleans_up(tmp_path):
    """A payload that is not a PDF (magic-byte check) must fail fast and
    leave NO .part file behind."""
    with pytest.raises(PermanentDownloadError, match="not a PDF"):
        _fetch(tmp_path, _FakeSession(_Resp([b"<html>error page</html>"])))
    assert not (tmp_path / "test.pdf.part").exists(), "the .part survived a failed fetch"


def test_404_fails_fast_without_retry(tmp_path):
    with pytest.raises(PermanentDownloadError, match="HTTP 404"):
        _fetch(tmp_path, _FakeSession(_Resp([], status=404)))
    assert not (tmp_path / "test.pdf.part").exists()


def test_429_is_retryable_class(tmp_path):
    """A 429 is a TRANSIENT failure (retry with backoff), not permanent —
    the wrong class would give up on a recoverable download."""
    with pytest.raises(RetryableDownloadError, match="HTTP 429"):
        _fetch(tmp_path, _FakeSession(_Resp([], status=429)))
    assert not (tmp_path / "test.pdf.part").exists()


def test_mid_download_drop_cleans_part(tmp_path):
    """A connection drop mid-stream must surface as RetryableDownloadError
    AND remove the partial file — a truncated PDF must never land. The
    fake raises the REAL requests exception class (what a real mid-stream
    drop surfaces as; a bare builtin ConnectionError would skip the
    wrapping but still hit the generic .part cleanup net)."""
    import requests
    drop = _Resp([b"%PDF-1.4 real-start"])
    def _iter(chunk_size=1024):
        yield b"%PDF-1.4 real-start"
        raise requests.exceptions.ConnectionError("connection reset by peer")
    drop.iter_content = _iter
    with pytest.raises(RetryableDownloadError, match="transient network"):
        _fetch(tmp_path, _FakeSession(drop))
    assert not (tmp_path / "test.pdf.part").exists(), "the partial file survived a drop"


# ---------------------------------------------------------------------------
# 6-7. table intelligence
# ---------------------------------------------------------------------------
def test_oversize_row_stays_whole():
    """A single row longer than chunk_size stays WHOLE — oversize is
    honest; a split row is a WRONG row (half a figure is worse than a
    long chunk)."""
    big_row = "A" * 2000 + " 42,000"
    chunks = _group_table_rows(f"TABLE:\n| H1 | H2 |\n{big_row}", chunk_size=700)
    assert any(big_row in c for c in chunks), "the oversize row was split"


def test_non_additive_total_is_flagged_not_dropped():
    """A Total row disagreeing with its members' sum is a FLAG with the
    numbers recorded — surfaced, never silently dropped, and never proof
    of a parse error (legit non-additive totals exist)."""
    table = ("TABLE:\n"
             "Revenue :: 2023=100 | 2022=90\n"
             "Other :: 2023=50 | 2022=40\n"
             "Total :: 2023=999 | 2022=130\n")
    out = verify_table_arithmetic(table)
    assert out["checked"] == 2, out
    assert out["passed"] == 1
    assert len(out["violations"]) == 1
    v = out["violations"][0]
    assert v["total"] == 999 and v["member_sum"] == 150
    assert v["column"] == "2023"


# ---------------------------------------------------------------------------
# 8. atomicity: a dead run never clobbers the last good corpus
# ---------------------------------------------------------------------------
def test_dead_run_never_clobbers_corpus(tmp_path, monkeypatch):
    """THE atomicity guarantee: every source failing must leave the
    existing good corpus byte-identical."""
    good = tmp_path / "corpus.jsonl"
    good.write_text('{"the last good corpus": true}\n', encoding="utf-8")
    before = good.read_bytes()

    settings = Settings(data_dir=tmp_path, output_file=good)

    import ingest

    def _all_fail(session, s):
        return {"Tesla_Q4_2023.pdf": {"status": "failed", "error": "dead"},
                "Apple_Q4_2023.pdf": {"status": "failed", "error": "dead"},
                "Meta_Q4_2023.pdf": {"status": "failed", "error": "dead"}}
    monkeypatch.setattr(ingest, "acquire_sources", _all_fail)

    report = run_ingestion(settings)
    assert report.chunks_written == 0
    assert len(report.failed_sources) == 3
    assert good.read_bytes() == before, \
        "a dead run DESTROYED the last good corpus — the atomic publish failed"
    assert not (tmp_path / "corpus.part").exists()
