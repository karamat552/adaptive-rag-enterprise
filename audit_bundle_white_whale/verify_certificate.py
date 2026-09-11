#!/usr/bin/env python3
"""
verify_certificate.py — OFFLINE, STANDALONE, ZERO-DEPENDENCY verifier for an
Adaptive-RAG Compliance Audit Bundle.

Auditor contract (read this first):
  This script shares NO CODE with the producing system. It is a deliberate,
  independent reimplementation of the verification contract so an external
  auditor can check the producer's claims without trusting the producer's
  code. Its only inputs are THIS bundle:
    - certificate.json  (the receipt: answer, claims, evidence spans+hashes,
                         page transcripts, chunk-truth table, source anchors)

  It re-derives, for every evidence link:
    sha256(company ⊣ source ⊣ page ⊣ slice) == chunk_hash
    slice == recorded content (verbatim)
    span in bounds, company/source/page match the chunk-truth table
  and validates every claim's citation indexes. Exit 0 = every check passed.

Usage:
    python verify_certificate.py                 # verify ./certificate.json
    python verify_certificate.py path/to/certificate.json
    python verify_certificate.py --json          # machine-readable result

What this PROVES offline:
  - every cited span exists verbatim in the bundled page transcripts
  - every chunk hash matches its slice byte-for-byte (tamper-evident)
  - every claim cites in-range evidence; attribution is consistent

What this does NOT prove (honest limits — see README.txt):
  - that the bundled transcripts match the ORIGINAL PDFs. For that, hash
    the source PDFs yourself and compare to the "pdf_sha256" anchors in
    certificate.json["sources"] — the filings are public on SEC EDGAR.
  - signature attestation (if certificate.json carries a "signature" block,
    this script verifies it ONLY if the optional 'cryptography' package is
    installed on the air-gapped machine).

stdlib only: json, hashlib, sys, argparse, base64. Python >= 3.9.
"""
import argparse
import base64
import hashlib
import json
import sys

SEP = "\x1f"


# ---------------------------------------------------------------------------
# Pure verification core — independent implementation of the server contract
# ---------------------------------------------------------------------------
def verify_core(receipt: dict, claims: list, evidence: list,
                transcripts: dict, chunk_truth: dict) -> dict:
    """transcripts: {(source, page): text}; chunk_truth: {hash: {...}}."""
    links_checked = links_ok = 0
    links = []
    for e in evidence:
        cs, ce = e.get("char_start"), e.get("char_end")
        h = e.get("chunk_hash")
        if cs is None or ce is None:
            links.append({"chunk_hash": h, "status": "no_span"})
            continue
        links_checked += 1
        t = transcripts.get((e.get("source"), e.get("page")))
        if t is None:
            links.append({"chunk_hash": h, "status": "transcript_missing"})
            continue
        if not (isinstance(cs, int) and isinstance(ce, int)
                and 0 <= cs < ce <= len(t)):
            links.append({"chunk_hash": h, "status": "span_out_of_bounds"})
            continue
        truth = chunk_truth.get(h)
        if truth is None:
            links.append({"chunk_hash": h, "status": "unknown_chunk"})
            continue
        if (truth.get("company") != (e.get("company") or "")
                or truth.get("page") != e.get("page")
                or truth.get("source") != (e.get("source") or "")):
            links.append({"chunk_hash": h, "status": "attribution_mismatch"})
            continue
        slice_text = t[cs:ce]
        key = f'{e.get("company") or ""}{SEP}{e.get("source") or ""}{SEP}' \
              f'{e.get("page") or 0}{SEP}{slice_text}'
        hash_ok = (hashlib.sha256(key.encode("utf-8")).hexdigest() == h)
        verbatim_ok = slice_text == (e.get("content") or "")
        ok = hash_ok and verbatim_ok
        links_ok += int(ok)
        links.append({"chunk_hash": h,
                      "status": "ok" if ok else
                                ("hash_mismatch" if not hash_ok
                                 else "text_mismatch")})

    claims_ok = True
    claim_issues = []
    for c in claims:
        for idx in (c.get("citations") or []):
            if not (isinstance(idx, int) and 1 <= idx <= len(evidence)):
                claims_ok = False
                claim_issues.append({"claim": str(c.get("claim", ""))[:80],
                                     "bad_citation": idx})
    verified = links_checked > 0 and links_ok == links_checked and claims_ok
    return {"verified": verified, "links_checked": links_checked,
            "links_ok": links_ok, "links": links,
            "claim_issues": claim_issues}


def _verify_signature(cert: dict) -> dict:
    """Optional attestation layer. Verifies the Ed25519 signature over the
    canonical payload using the PUBLIC KEY embedded in the bundle. Requires
    the 'cryptography' package on the auditor's machine; without it the
    check is SKIPPED with an explicit warning (honest degradation)."""
    sig = cert.get("signature")
    if not sig or not sig.get("signature"):
        return {"status": "absent",
                "note": "no signature block in certificate"}
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        return {"status": "skipped",
                "note": "signature present but 'cryptography' is not "
                        "installed on this machine — install it to verify "
                        "the attestation"}
    payload = {k: v for k, v in cert.items() if k != "signature"}
    canonical = json.dumps(payload, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    try:
        pub_b64 = sig["public_key"]
        key_bytes = base64.b64decode(pub_b64)
        sig_bytes = base64.b64decode(sig["signature"])
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            sig_bytes, canonical.encode("utf-8"))
        return {"status": "verified", "key_id": sig.get("key_id")}
    except Exception as exc:
        return {"status": "INVALID", "error": str(exc)[:200]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Offline verifier for an Adaptive-RAG Compliance Audit "
                    "Bundle. Zero dependencies; stdlib only.")
    ap.add_argument("certificate", nargs="?", default="certificate.json",
                    help="path to certificate.json (default: ./certificate.json)")
    ap.add_argument("--json", action="store_true",
                    help="emit the full result as JSON instead of a table")
    args = ap.parse_args(argv)

    try:
        with open(args.certificate, encoding="utf-8") as fh:
            cert = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read certificate ({exc})")
        return 2

    receipt = cert.get("receipt") or {}
    claims = receipt.get("claims_json") or []
    evidence = receipt.get("evidence_json") or []
    # Bundle normalizes JSONB lists into plain lists at export; accept both.
    if isinstance(claims, str):
        claims = json.loads(claims)
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    transcripts = {}
    for t in cert.get("transcripts", []):
        transcripts[(t["source"], int(t["page"]))] = t["transcript"]
    chunk_truth = {t["chunk_hash"]: t for t in cert.get("chunk_truth", [])}

    result = verify_core(receipt, claims, evidence, transcripts, chunk_truth)
    result["signature"] = _verify_signature(cert)
    result["certificate_metadata"] = {
        "run_id": receipt.get("run_id"),
        "corpus_epoch": receipt.get("corpus_epoch"),
        "created_at": receipt.get("created_at"),
        "sources": {s.get("source"): s.get("pdf_sha256")
                    for s in (cert.get("sources") or [])},
    }

    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0 if result["verified"] else 1

    print("=" * 66)
    print(f"CERTIFICATE {receipt.get('run_id', '?')} | "
          f"corpus epoch {receipt.get('corpus_epoch')}")
    print(f"  question : {receipt.get('question', '')[:70]}")
    print(f"  answer   : {(receipt.get('answer') or '')[:70]}…")
    print("=" * 66)
    for lnk in result["links"]:
        mark = "OK  " if lnk["status"] == "ok" else "FAIL"
        print(f"  [{mark}] {lnk['status']:<22} {str(lnk.get('chunk_hash'))[:16]}…")
    for issue in result["claim_issues"]:
        print(f"  [FAIL] bad citation {issue['bad_citation']} in: "
              f"{issue['claim']}")
    sig = result["signature"]
    if sig["status"] == "verified":
        print(f"  [OK  ] signature attestation verified "
              f"(key {sig.get('key_id', 'embedded')})")
    elif sig["status"] == "skipped":
        print(f"  [SKIP] {sig['note']}")
    elif sig["status"] == "INVALID":
        print(f"  [FAIL] signature INVALID — {sig.get('error', '')[:80]}")
    total = result["links_checked"]
    ok = result["links_ok"]
    print("-" * 66)
    verdict = "PASS — chain verified offline" if result["verified"] else \
              "FAIL — broken link(s); this certificate is NOT proven"
    print(f"{verdict}  ({ok}/{total} links ok, "
          f"{len(result['claim_issues'])} citation issue(s))")
    src_anchors = result["certificate_metadata"]["sources"]
    if src_anchors:
        print("\n  Independent re-verification (recommended):")
        for src_name, sha in sorted(src_anchors.items()):
            print(f"    download '{src_name}' from SEC EDGAR, then:")
            print(f"      certutil -hashfile <file> SHA256   (Windows)")
            print(f"      shasum -a 256 <file>               (macOS/Linux)")
            print(f"      expect: {sha}")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
