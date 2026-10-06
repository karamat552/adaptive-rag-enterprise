"""Token dig: per-node token + call-count attribution for one question.

The B real run measured ~178K tokens / 49.7 LLM calls per ANSWERED question.
This dig attributes usage to NODES: the graph's state updates carry
CUMULATIVE usage (usage_in/usage_out/llm_calls grow monotonically), so the
delta between consecutive node updates is that node's own spend + call
count. Run with RAG_FACT_FASTPATH=1 (production-matching: the owner
verified the flag is ON on Render) against the seeded disposable stack.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

USAGE_KEYS = ("usage_in", "usage_out", "llm_calls")


def dig_one(adaptive_rag, question: str, qid: str) -> Dict:
    async def _stream():
        graph = adaptive_rag.get_graph()
        state = {"original_question": question, "retry_count": 0,
                 "run_id": f"dig-{time.strftime('%H%M%S')}",
                 "tenant_id": "default"}
        rows = []
        prev = {k: 0 for k in USAGE_KEYS}
        t_prev = time.monotonic()
        outcome = None
        async for update in graph.astream(
                state, config={"recursion_limit": 25},
                stream_mode="updates"):
            now = time.monotonic()
            if not isinstance(update, dict):
                continue
            for node, payload in update.items():
                if not isinstance(payload, dict):
                    continue
                row = {"node": node, "ms": int((now - t_prev) * 1000)}
                for k in USAGE_KEYS:
                    cur_val = int(payload.get(k) or 0)
                    row[k] = cur_val - prev[k]
                    prev[k] = cur_val
                if payload.get("outcome"):
                    outcome = payload["outcome"]
                rows.append(row)
            t_prev = now
        return rows, outcome

    rows, outcome = asyncio.run(_stream())
    print(f"\n===== {qid}: {question[:60]}")
    print(f"outcome={outcome}")
    print(f"{'node':22s} {'ms':>7s} {'tok_in':>8s} {'tok_out':>8s} {'calls':>6s}")
    tot = {k: 0 for k in USAGE_KEYS}
    for r in rows:
        if any(r[k] for k in USAGE_KEYS) or r["ms"] > 100:
            print(f"{r['node']:22s} {r['ms']:7d} {r['usage_in']:8d} "
                  f"{r['usage_out']:8d} {r['llm_calls']:6d}")
        for k in USAGE_KEYS:
            tot[k] += r[k]
    print(f"{'TOTAL':22s} {'':7s} {tot['usage_in']:8d} "
          f"{tot['usage_out']:8d} {tot['llm_calls']:6d}")
    return {"qid": qid, "question": question, "outcome": outcome,
            "nodes": rows, "total": tot}


def main() -> None:
    import adaptive_rag
    spec = json.loads((REPO / "tests" / "battery_phase1_preregistered.json")
                      .read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in spec["questions"]}
    targets = sys.argv[1:] or ["A01", "A02"]
    out = []
    for qid in targets:
        out.append(dig_one(adaptive_rag, by_id[qid]["q"], qid))
    (REPO / "eval_out" / "token_dig.json").parent.mkdir(exist_ok=True)
    (REPO / "eval_out" / "token_dig.json").write_text(
        json.dumps(out, indent=1, default=str), encoding="utf-8")
    print("\nJSON written: eval_out/token_dig.json")


if __name__ == "__main__":
    main()
