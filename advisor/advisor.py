"""DEV ADVISOR — a standalone second opinion for development decisions.

The owner's tool (2026-09-17): a free, strong reviewer (OpenRouter
z-ai/glm-5.2:free — pricing 0/0, 32K context; the original stealth/
union-alpha retired 2026-09-18 -> unbiased/pareto, paid) to DEBATE with
while building: architecture calls, bug hypotheses, review of diffs and docs.

HARD BOUNDARY (ADR-008 stands): this is a DEVELOPMENT aid only.
 - It is NEVER imported by the pipeline (no module in the project
   references advisor/).
 - It reads its key from advisor/key.txt — NOT from the project's .env —
   so it stays outside the runtime environment entirely.
 - Review models never serve RAG stages; nothing here is wired into any
   lane, gate, or workflow. A lane in the failover chain is a separate,
   live-probed decision under ADR-008's rules.

Usage (from the repo root, with the project venv):
  python advisor/advisor.py "question or decision to debate"
  python advisor/advisor.py --file adaptive_rag.py --file fact_claims.py \
      "review these two modules for missed edge cases"
  python advisor/advisor.py --system "you are a skeptical CTO" "is X wise?"
The advisor answers; the session agent (and the owner) decide.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
KEY_FILE = HERE / "key.txt"
DEFAULT_MODEL = "z-ai/glm-5.2:free"   # stealth/union-alpha retired 2026-09-18 -> unbiased/pareto (paid)
DEFAULT_SYSTEM = (
    "You are a skeptical senior systems reviewer for a financial-doc "
    "RAG platform whose moat is deterministic verification (byte-span "
    "receipts, five zero-token gates, fail-closed refusals, ADR-gated "
    "rollouts). Be adversarial and specific: name the failure mode, the "
    "edge case, the assumption that will not hold. Prefer concrete "
    "engineering remedies over platitudes. Disagree when warranted.")


def _key() -> str:
    if KEY_FILE.exists():
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    raise SystemExit(
        "advisor key missing: put the OpenRouter key in advisor/key.txt "
        "(gitignored — never commit it, never move it into .env)")


def consult(question: str, context_files: list, system: str,
            model: str) -> dict:
    parts = []
    for f in context_files:
        p = Path(f)
        if not p.exists():
            raise SystemExit(f"context file not found: {f}")
        body = p.read_text(encoding="utf-8", errors="replace")
        parts.append(f"=== FILE: {f} ===\n{body[:60000]}")
    user = "\n\n".join(parts + [question])
    t0 = time.perf_counter()
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": "Bearer " + _key(),
                 "Content-Type": "application/json"},
        json={"model": model,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}],
              "temperature": 0.4, "max_tokens": 4000},
        timeout=180)
    r.raise_for_status()
    d = r.json()
    usage = d.get("usage") or {}
    return {"reply": d["choices"][0]["message"]["content"],
            "latency_s": round(time.perf_counter() - t0, 2),
            "cost_usd": usage.get("cost"),
            "model": d.get("model", model)}


def main() -> int:
    ap = argparse.ArgumentParser(description="standalone dev advisor")
    ap.add_argument("question")
    ap.add_argument("--file", action="append", default=[],
                    help="attach a file as context (repeatable)")
    ap.add_argument("--system", default=DEFAULT_SYSTEM)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ns = ap.parse_args()
    out = consult(ns.question, ns.file, ns.system, ns.model)
    print(f"[{out['model']} · {out['latency_s']}s · cost ${out['cost_usd']}]")
    print(out["reply"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
