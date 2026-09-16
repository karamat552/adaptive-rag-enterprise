# Adaptive RAG — Workspace Instructions

Financial-doc RAG pipeline. Serving layer `main.py` (FastAPI, PORT 8000),
UX client `app.py` (Streamlit), orchestration `adaptive_rag.py` (env `RAG_`
prefix, provider-agnostic seam), persistence `db.py` (env `DB_` prefix;
least-privilege runtime role + separate admin identity). Architecture ledger:
`ARCHITECTURE_DECISIONS.md` (ADR-NNN) — check it before touching decided
ground. ADR-017 shadow mode (`fact_shadow.py`, `fact_templates.py`) is live:
Path A (V2) answers are built and compared, never served.

## Review convention

Multi-model consults (ADR-006/ADR-013, `scripts/consult.py`) are RETIRED
from the active workflow by owner decision (2026-09-16): the session model
is the sole reviewer. The script and its ADR history stay for the record;
do not wire consult lanes into pipelines or gates (the ADR-008 rule —
review models never serve RAG stages — still stands).

## Hard rules

- Never commit `.env` or real API keys; `.env.example` is the template.
- The operational posture is fail-closed (auth, eviction, refusal paths) —
  do not weaken it to make tests pass.
- Reference ADR numbers in commit messages when touching decided ground.
