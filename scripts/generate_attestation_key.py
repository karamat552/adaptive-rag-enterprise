#!/usr/bin/env python3
"""Generate the Ed25519 attestation keypair for receipt signing.

WHY THIS EXISTS
---------------
db.sign_certificate_payload() documents this script as the way to enable
Ed25519 attestation over certificate bundles, but the file was missing from
the repository — the only documented path to enabling signing dead-ended, so
every deployment (including production) silently shipped the
``{"status": "unsigned"}`` posture.

WHAT IT DOES
------------
Prints a base64 32-byte raw private key and its derived public key. Set the
private key as ``ATTESTATION_PRIVATE_KEY`` in the runtime environment; the
public key is embedded in every signed bundle (``signature.public_key``), so
verifiers need nothing but the bundle itself.

    python scripts/generate_attestation_key.py            # human-readable
    python scripts/generate_attestation_key.py --env      # 'KEY=value' line
    python scripts/generate_attestation_key.py --check    # inspect current env

SECURITY
--------
The private key IS the attestation identity. Anyone holding it can forge a
signature that verifies against the embedded public key. Store it only in
the secret manager of the deployment (Render/Streamlit env vars, etc.),
never in git. Rotating the key changes every future bundle's key_id/public_key
pair — the hash chain inside old bundles stays verifiable offline regardless,
because attestation is an anti-forgery layer, not the integrity mechanism.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    sys.exit("cryptography is not installed — pip install cryptography")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def generate() -> tuple[str, str]:
    """Return (private_b64, public_b64) for a fresh Ed25519 keypair."""
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64(priv), _b64(pub)


def describe_env() -> int:
    """Report the CURRENT attestation posture without printing secrets."""
    priv = os.getenv("ATTESTATION_PRIVATE_KEY", "").strip()
    if not priv:
        print("ATTESTATION_PRIVATE_KEY: NOT SET")
        print("  -> sign_certificate_payload() returns {'status': 'unsigned'}")
        print("  -> receipt hash chains remain fully verifiable offline")
        return 0
    try:
        raw = base64.b64decode(priv)
        key = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        print(f"ATTESTATION_PRIVATE_KEY: INVALID ({type(exc).__name__}: {exc})")
        print("  -> signing will fail; receipts will carry {'status': 'error'}")
        return 1
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    print(f"ATTESTATION_PRIVATE_KEY: valid ({len(raw)} raw bytes)")
    print(f"  public key: {_b64(pub)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", action="store_true",
                    help="emit a single 'ATTESTATION_PRIVATE_KEY=...' line")
    ap.add_argument("--check", action="store_true",
                    help="report the current env posture (no new key)")
    args = ap.parse_args()

    if args.check:
        return describe_env()

    priv, pub = generate()
    if args.env:
        print(f"ATTESTATION_PRIVATE_KEY={priv}")
        return 0

    print("Ed25519 attestation keypair — store the PRIVATE key as a secret.")
    print()
    print(f"ATTESTATION_PRIVATE_KEY={priv}")
    print(f"public key (embedded in every signed bundle): {pub}")
    print()
    print("Next: set ATTESTATION_PRIVATE_KEY in the deployment environment,")
    print("then re-run a query and confirm the receipt's signature block")
    print("carries algorithm=Ed25519 instead of {'status': 'unsigned'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
