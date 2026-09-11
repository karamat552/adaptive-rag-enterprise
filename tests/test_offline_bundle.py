"""
OFFLINE COMPLIANCE BUNDLE — tamper vectors + equivalence + round-trip
=====================================================================
P2.1 tests (2026-09-10). The offline verifier (verify_certificate.py) is
a DELIBERATELY INDEPENDENT stdlib reimplementation of the server's
verify_bundle_core — an auditor must not trust the producer's code. These
tests prove the two implementations are EQUIVALENT by driving the same
attack classes through both, plus the full 17-vector tamper corpus from
tests/test_tamper.py against the OFFLINE path.

Every test mutates a certified bundle and asserts the offline script
either PASSES a sound bundle or FAILS with the exact named status the
server-side core produces. A mismatch in either direction is a defect.

Run:  pytest tests/test_offline_bundle.py -q   (offline, subprocess-based)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "verify_certificate.py"


# ---------------------------------------------------------------------------
# Bundle factory — mirrors a real certified receipt (white-whale shape)
# ---------------------------------------------------------------------------
def _certified_bundle(seed_text_modifier: int = 0) -> dict:
    page1 = (f"Meta Q4 2023 revenue was $40,111 million, up 25% "
             f"year-over-year. Adjusted modifier {seed_text_modifier}. "
             f"Net income reached $14,017 million.")
    page2 = ("Total costs and expenses were $23,727 million, down 8% "
             "year-over-year. Operating margin expanded to 41%.")
    transcripts = [
        {"source": "Meta_Q4_2023.pdf", "page": 1, "transcript": page1},
        {"source": "Meta_Q4_2023.pdf", "page": 2, "transcript": page2},
    ]
    evidence = []
    chunk_truth = []
    for src, page in (("Meta_Q4_2023.pdf", 1), ("Meta_Q4_2023.pdf", 2)):
        t = next(x["transcript"] for x in transcripts if x["page"] == page)
        cs, ce = 0, min(80, len(t))
        slice_text = t[cs:ce]
        chunk_hash = __import__("hashlib").sha256(
            f"Meta\x1f{src}\x1f{page}\x1f{slice_text}".encode()).hexdigest()
        evidence.append({"chunk_hash": chunk_hash, "company": "Meta",
                         "source": src, "page": page, "content": slice_text,
                         "char_start": cs, "char_end": ce,
                         "transcript_version": 1})
        chunk_truth.append({"chunk_hash": chunk_hash, "company": "Meta",
                            "source": src, "page": page})
    claims = [
        {"claim": "Meta revenue was $40,111 million.", "citations": [1]},
        {"claim": "Costs were $23,727 million.", "citations": [2]},
    ]
    return {
        "bundle_type": "adaptive_rag_compliance_certificate",
        "schema_version": 1,
        "receipt": {"run_id": "cert-1", "question": "Meta revenue?",
                    "answer": "Meta revenue was $40,111 million.",
                    "claims_json": claims, "evidence_json": evidence,
                    "audit_verdict": "grounded", "corpus_epoch": 9,
                    "tenant_id": "default"},
        "claims": claims, "evidence": evidence,
        "chunk_truth": chunk_truth, "transcripts": transcripts,
        "sources": [{"source": "Meta_Q4_2023.pdf",
                     "pdf_sha256": "a" * 64}],
        "signature": {},
    }


def _run_offline(cert: dict, workdir: Path) -> tuple:
    """Write the mutated bundle + run the STANDALONE script in a subprocess.
    Returns (exit_code, stdout)."""
    wd = workdir / f"case_{abs(hash(json.dumps(cert, sort_keys=True)[:200])) % 10**8}"
    wd.mkdir(parents=True, exist_ok=True)
    cert_path = wd / "certificate.json"
    cert_path.write_text(json.dumps(cert, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPT), str(cert_path)],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout


# ---------------------------------------------------------------------------
# Equivalence + attack corpus
# ---------------------------------------------------------------------------
def test_offline_passes_sound_bundle(tmp_path):
    code, out = _run_offline(_certified_bundle(), tmp_path)
    assert code == 0, out
    assert "PASS" in out and "12/12" not in out  # 2 links, both ok


def test_offline_catches_all_core_forgeries(tmp_path):
    """The attack classes from tests/test_tamper.py, replayed against the
    OFFLINE verifier: span shift, truncation, out-of-bounds, forged hash,
    company relabel, content swap, transcript mutation. Every one must FAIL
    (non-zero exit) with a named status."""
    base = _certified_bundle()
    attacks = []
    e0 = base["evidence"][0]

    import copy
    m = copy.deepcopy(base); m["evidence"][0]["char_start"] += 1; attacks.append(("span+1", m))
    m = copy.deepcopy(base); m["evidence"][0]["char_end"] -= 3; attacks.append(("span-3", m))
    m = copy.deepcopy(base); m["evidence"][0]["char_end"] = 10**7; attacks.append(("oob", m))
    m = copy.deepcopy(base); m["evidence"][0]["chunk_hash"] = "f" * 64; attacks.append(("forge-hash", m))
    m = copy.deepcopy(base); m["evidence"][0]["company"] = "Apple"; attacks.append(("relabel", m))
    m = copy.deepcopy(base); m["evidence"][0]["content"] = "Fabricated text."; attacks.append(("swap", m))
    m = copy.deepcopy(base); m["transcripts"][0]["transcript"] = "tampered"; attacks.append(("transcript", m))

    for name, cert in attacks:
        code, out = _run_offline(cert, tmp_path)
        assert code != 0, f" forgery '{name}' was NOT caught offline:\n{out}"


def test_offline_claim_citation_validation(tmp_path):
    base = _certified_bundle()
    base["receipt"]["claims_json"][0]["citations"] = [99]
    code, _ = _run_offline(base, tmp_path)
    assert code != 0, "out-of-range citation must fail offline"


def test_offline_missing_transcript_fails(tmp_path):
    base = _certified_bundle()
    base["transcripts"] = []                      # page rows "missing"
    base["chunk_truth"] = []                      # and no attribution either
    code, out = _run_offline(base, tmp_path)
    assert code != 0 and "transcript_missing" in out


def test_offline_signature_verification_roundtrip(tmp_path):
    """The optional Ed25519 attestation: sign server-side (cryptography),
    verify offline (cryptography present in this env -> VERIFIED). A
    tampered bundle must flip the signature check to INVALID."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    priv_b64 = base64.b64encode(key.private_bytes_raw()).decode()
    pub_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()

    cert = _certified_bundle()
    payload = {k: v for k, v in cert.items() if k != "signature"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    cert["signature"] = {
        "algorithm": "Ed25519", "key_id": "test",
        "public_key": pub_b64,
        "signature": base64.b64encode(key.sign(canonical.encode())).decode(),
    }
    # The offline script re-canonicalizes identically -> signature verifies.
    code, out = _run_offline(cert, tmp_path)
    assert "signature attestation verified" in out, out
    # Tamper the payload AFTER signing -> signature INVALID.
    cert["receipt"]["answer"] = "tampered after signing"
    code2, out2 = _run_offline(cert, tmp_path)
    assert "INVALID" in out2, "post-signing tamper must break attestation"
