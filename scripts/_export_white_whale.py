"""One-shot: export the white-whale bundle and verify it offline."""
import asyncio
import json
import os
import shutil
import subprocess
import sys

ROOT = r"C:\Users\Karamat\Adaptive-RAG-Project"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))
import db  # noqa: E402

BUNDLE_DIR = os.path.join(ROOT, "audit_bundle_white_whale")


async def main():
    os.makedirs(BUNDLE_DIR, exist_ok=True)
    bundle = await asyncio.to_thread(
        db.build_certificate_bundle, "1efc9fe87875")
    cert_json = json.dumps(bundle, indent=1, ensure_ascii=False)
    cert_path = os.path.join(BUNDLE_DIR, "certificate.json")
    with open(cert_path, "w", encoding="utf-8") as f:
        f.write(cert_json)
    shutil.copy(os.path.join(ROOT, "verify_certificate.py"),
                os.path.join(BUNDLE_DIR, "verify_certificate.py"))
    sig = bundle.get("signature", {})
    with open(os.path.join(BUNDLE_DIR, "PUBLIC_KEY.pem"), "w") as f:
        f.write(sig.get("public_key", ""))
    print("bundle written | transcripts:", len(bundle["transcripts"]),
          "| chunk_truth:", len(bundle["chunk_truth"]),
          "| sources:", len(bundle["sources"]),
          "| signature:", sig.get("status", sig.get("algorithm", "?")))
    return cert_path


cert_path = asyncio.run(main())
r = subprocess.run([sys.executable,
                    os.path.join(BUNDLE_DIR, "verify_certificate.py"),
                    cert_path], capture_output=True, text=True,
                   encoding="utf-8")
print("--- OFFLINE VERIFY ---")
print(r.stdout[-1200:])
print("EXIT:", r.returncode)
sys.exit(r.returncode)
