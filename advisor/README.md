# advisor/ — the standalone dev advisor

A free second opinion for development decisions (owner tool, 2026-09-17).
Backed by OpenRouter **stealth/union-alpha** (pricing 0/0 during the
stealth period, 262K context) — live-probed: strict-JSON and [n]-citation
probes pass, 3.6–12s latency.

## Setup (one time)

```bash
echo "sk-or-v1-...your-openrouter-key..." > advisor/key.txt
```

`advisor/key.txt` is **gitignored** and must never be committed or moved
into the project `.env` — this folder is deliberately outside the
pipeline's environment.

## Use

```bash
# debate a decision
python advisor/advisor.py "is the split-battery amendment to A.2 wise?"

# review code with full context
python advisor/advisor.py --file fact_claims.py "review for missed edge cases"

# review a design doc against the codebase
python advisor/advisor.py --file ARCHITECTURE_DECISIONS.md \
    "which ADR is most likely to bite us next, and why?"

# custom persona
python advisor/advisor.py --system "you are a compliance auditor" "..."
```

## The boundary (non-negotiable, ADR-008)

This is a **development aid only**. Nothing in the pipeline imports it;
no workflow calls it; it never serves a RAG stage. The failover-chain
use of union-alpha is a *separate* decision under ADR-008's live-probe
rules (router+fleet lanes only — the executive peer pool forbids
community proxies, and that ruling stands regardless of this folder).

The advisor argues; the session agent and the owner decide.
