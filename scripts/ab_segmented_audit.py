"""ADR-017 Phase-3 A/B — the token-slashing demo, LIVE.

One question, two arms, full pipeline each time (cache read disabled so
arm B cannot replay arm A):
  A: RAG_SEGMENTED_AUDIT unset  -> the V1 full-context audit (~4-6K)
  B: RAG_SEGMENTED_AUDIT=1      -> segmented triage (0 or ~0.7K)

The [audit] log lines carry the per-call token counts; the receipts'
claims_json carries the per-claim verifier stamps (B.1.4) — queried at
the end so the proof is in the DB, not the console.

Usage: python scripts/ab_segmented_audit.py ["question"]
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")

os.environ["RAG_DISABLE_CACHE_READ"] = "1"   # arm B must not replay A

QUESTION = sys.argv[1] if len(sys.argv) > 1 else \
    "What was Apple's operating income in Q4 2023?"


async def arm(label: str, segmented: bool) -> dict:
    if segmented:
        os.environ["RAG_SEGMENTED_AUDIT"] = "1"
    else:
        os.environ.pop("RAG_SEGMENTED_AUDIT", None)
    from adaptive_rag import arun_query
    t0 = time.perf_counter()
    r = await arun_query(QUESTION)
    toks = sum(u.get("input", 0) + u.get("output", 0)
               for u in (r.get("per_model") or {}).values())
    print(f"\n[{label}] outcome={r.get('outcome')} grounded="
          f"{r.get('grounded')} latency={r.get('latency_s')}s "
          f"run_tokens={toks} wall={time.perf_counter() - t0:.1f}s")
    return r


def receipt_stamps(question: str) -> None:
    from db import admin_connection
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT run_id, audit_verdict, claims_json, model_id FROM "
            "verification_receipts WHERE question=%s "
            "ORDER BY id DESC LIMIT 2", (question,))
        for rid, verdict, claims, mid in cur.fetchall():
            kinds = {}
            for c in (claims or []):
                v = c.get("verifier", "?")
                kinds[v] = kinds.get(v, 0) + 1
            print(f"RECEIPT {rid}: verdict={verdict} model={mid} "
                  f"verifier classes={kinds}")


async def main() -> None:
    print(f"QUESTION: {QUESTION!r}\n")
    await arm("ARM A: FULL AUDIT ", False)
    await arm("ARM B: SEGMENTED  ", True)
    print("\n--- receipts (proof in the DB) ---")
    try:
        receipt_stamps(QUESTION)
    except Exception as exc:
        print("receipt query failed (non-fatal):", exc)


if __name__ == "__main__":
    asyncio.run(main())
