"""Task 4 — ADR-022 capture fix: every shadow question emits a SURVIVABLE
stdout record (SHADOW-ROW json) so a timeout-killed nightly still leaves
per-question outcome/token data in the Actions log (the report file never
survives a killed job — runs #15/#16 uploaded no artifacts).
Offline: arun_query is monkeypatched; zero tokens.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import battery_phase1


def test_shadow_pass_emits_per_question_records(monkeypatch, capsys):
    import adaptive_rag

    async def _fake_run(q):
        return {"outcome": "vectorstore", "grounded": True, "cached": False,
                "quota_hint_s": None, "degraded_agents": [], "answer": "a",
                "run_id": "r-" + q[:4], "per_model": {"groq/x":
                {"input": 100, "output": 50}}, "latency_s": 1.0}

    monkeypatch.setattr(adaptive_rag, "arun_query", _fake_run)
    monkeypatch.setattr(battery_phase1, "_fetch_ledger_row",
                        lambda rid, q: {"agreement_class": "agree",
                                        "agreement_detail": "d"})
    rows = asyncio.run(battery_phase1.shadow_pass(limit=3))
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("SHADOW-ROW ")]
    assert len(lines) == 3 == len(rows), f"want 3 SHADOW-ROW lines, got {len(lines)}"
    parsed = [json.loads(ln[len("SHADOW-ROW "):]) for ln in lines]
    assert all(r["tokens"] == 150 for r in parsed), \
        [r["tokens"] for r in parsed]
    assert all("outcome" in r and "q" in r for r in parsed)
