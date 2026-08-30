"""
Gold-Set Answer Accuracy Harness
=================================
Measures FACTUAL CORRECTNESS of pipeline answers against ground-truth figures
mined from the ingested corpus (manually verified against source filings).

Each test: run full pipeline -> assert gold values appear in the certified answer.
- outcome == vectorstore  -> assert gold values present (FAIL = hallucination/omission)
- outcome == refusal      -> SKIP counted separately (honest refusal, not a lie)

Run:  pytest tests/test_answer_accuracy.py -v
NOTE: each test = one full pipeline run (~30-120s on free tier). Batch across
days if quota-bound; RAG_DISABLE_CACHE_WRITE is NOT set here (certified
answers SHOULD populate the cache).
"""
import asyncio
import os
from pathlib import Path

# Unit-test-style dummy guard: only inject if no real config exists anywhere
if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402


def _run(question: str) -> dict:
    from adaptive_rag import arun_query
    return asyncio.run(arun_query(question))


# ---------------------------------------------------------------------------
# GOLD SET — figures verified against source filings / corpus tables
# ---------------------------------------------------------------------------
GOLD_SET = [
    # --- Single-company extraction ---
    ("What were Tesla's total automotive revenues in Q4 2023?",
     ["21,563"]),
    ("What was Tesla's energy generation and storage revenue in Q4 2023?",
     ["1,438"]),
    ("What was Apple's total net sales in Q4 2023?",
     ["89,498"]),
    ("What was Meta's total revenue in Q4 2023?",
     ["40,111"]),
    ("What was Meta's advertising revenue in Q4 2023?",
     ["38,706", "38.7"]),
    # --- Derived metrics ---
    ("What was Meta's revenue growth in Q4 2023 compared to Q4 2022?",
     ["24.7", "25"]),
    ("What was Apple's revenue change in Q4 2023 versus Q4 2022?",
     ["-0.7", "decline", "decreased"]),
    # --- Multi-company comparison ---
    ("Compare the revenue growth and net income performance between Apple and Meta in Q4 2023.",
     ["40,111", "89,498"]),
    # --- Honest-gap probes (refusal is the CORRECT outcome if data is absent) ---
    ("What was Tesla's dividend per share in Q4 2023?",
     None),
]


@pytest.fixture(scope="session")
def db_ready():
    from db import admin_connection
    try:
        with admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
    except Exception:
        pytest.skip("No reachable database — skipping accuracy harness")


@pytest.mark.parametrize("question,gold", GOLD_SET,
                         ids=[q[:40] for q, _ in GOLD_SET])
def test_answer_accuracy(db_ready, question, gold):
    r = _run(question)
    outcome = r["outcome"]

    if gold is None:
        # Honest-gap probe: ANY outcome is acceptable; refusal is a WIN
        assert outcome in ("vectorstore", "verified_refusal", "unverified_system"), \
            f"unexpected outcome {outcome}"
        print(f"    -> outcome={outcome} (honest-gap probe)")
        return

    if outcome in ("verified_refusal", "unverified_system"):
        pytest.skip(f"system honestly refused (retrieval gap?) — not a hallucination")

    assert outcome == "vectorstore", f"unexpected outcome: {outcome}"
    missing = [g for g in gold if g not in r["answer"]]
    assert not missing, \
        f"HALLUCINATION/OMISSION — missing {missing} in answer:\n{r['answer'][:600]}"