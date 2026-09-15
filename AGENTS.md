# Adaptive RAG — Workspace Instructions

Financial-doc RAG pipeline. Serving layer `main.py` (FastAPI, PORT 8000),
UX client `app.py` (Streamlit), orchestration `adaptive_rag.py` (env `RAG_`
prefix, provider-agnostic seam), persistence `db.py` (env `DB_` prefix;
least-privilege runtime role + separate admin identity). Architecture ledger:
`ARCHITECTURE_DECISIONS.md` (ADR-NNN) — check it before touching decided
ground. ADR-017 shadow mode (`fact_shadow.py`, `fact_templates.py`) is live:
Path A (V2) answers are built and compared, never served.

## Multi-model review convention (ADR-006 / ADR-013)

The session model is the primary reviewer. Before committing non-trivial
code, new tests, or architecture changes, cross-check with independent
models via the `/consult` command, or directly:

    python scripts/consult.py --prompt-file BRIEF.md --models "<lanes>"

Lanes: `gemini-<model>`, `groq`, `nim`, `claude[:<model>]`,
`openai[:<model>]` (paid lanes default to claude-3-7-sonnet-latest / gpt-4o).
Fail-soft: an unset key prints `[UNAVAILABLE]` and never blocks other lanes.
Consult lanes are review-only — never assign them to RAG stages (the 16-point
benchmark rule, ADR-008).

## Hard rules

- Never commit `.env` or real API keys; `.env.example` is the template.
- The operational posture is fail-closed (auth, eviction, refusal paths) —
  do not weaken it to make tests pass.
- Reference ADR numbers in commit messages when touching decided ground.
