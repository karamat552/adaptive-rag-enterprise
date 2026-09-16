# Module Decomposition Plan — adaptive_rag.py

**Status:** staged on `v3-staging` · **Author:** 2026-09-16 session ·
**Scope:** GAP_ANALYSIS item 9 (the 3,500-line orchestrator split into
modules) — the execution plan, per the repo's own discipline.

**The one rule that governs timing:** the nightly A.2 battery measures
the system on `main`. Extraction happens on `v3-staging`; merge to
main either AFTER the 7 green nights, or with the explicit acceptance
that any refactor-caused red night resets the clock. A refactor bug
costs a calendar day; the plan below minimizes that risk mechanically.

## Module map (move order = dependency direction, one commit per module)

| # | Module | Contents (moves out of adaptive_rag.py) | Notes |
|---|---|---|---|
| 1 | `gates.py` | `citation_pre_audit`, `_CITE_RE`, `parse_declared_units`, `assert_claim_scales`, `_is_derived_context`, `find_comparative_pairs`, `check_growth_claims`, `_figure_metric_owner`, `_figure_year`, `check_xbrl_figures`, `_sig_digits`, `_direction_of`, `_NON_GAAP_RE` + the shared lexicon regexes (`_MONEY_RE`, `_SCALE`, `_FAMILY_RE`, `_PERIODS`, `_COMPANY_NAME_RE`) | purest functions; the shared lexicons live HERE and `adaptive_rag` imports them back (single source of truth). segmented_audit + tests import from gates |
| 2 | `failover.py` | `EndpointCooldown`, `CircuitBreaker`, `parse_retry_hint`, `_is_quota_error`, `_is_timeout_error`, `get_failover_endpoints`, `_build_backup_engine`, `_failover_stage_call`, `_exec_peer_fallback` + the peer pool constants | the ADR-008/016 seam; config-driven, engine-agnostic |
| 3 | `telemetry.py` | `UsageCollector`, `_track_model_usage`, `_MODEL_USAGE`, `_with_usage` | small; unblocks GAP_ANALYSIS item 2 (per-model rows in battery reports) |
| 4 | `state.py` | `MultiAgentState`, `RagSettings`, `get_settings`, `get_stage_model`, engine factories (`_build_engine`, `_get_engine`, `_bind_output_cap`, `_repairing_structured`, `_schema_fields_match`, `_repair_json_like`) | the config + wiring tier |
| 5 | `fleet.py` | `_specialist`, `execute_specialist_fleet`, `_multi_query_search`, `_rrf_fuse`, canonicalize/formatting helpers, `premise_fast_path` | retrieval + pruning (ADR-001 territory) |
| 6 | `contradictions.py` | `extract_metric_mentions`, `detect_contradictions`, `cross_check_specialists`, `sharpen_retrieval` | the ADR-007 engine — its own file makes the ADR-019 dimension amendment a clean PR |
| 7 | `synthesis.py` | `synthesize_csuite_report`, prompt constants, `_strip_reasoning`, `extract_text_content` | |
| 8 | `audit.py` | `fact_checker_guard`, `build_receipt_evidence`, `extract_claims`; imports `segmented_audit` + `gates` | Phase 3 already isolated the triage — the guard follows |
| 9 | `routing.py` | `route_question`, `route_cache_check`, `pathing_triage`, `fact_fastpath`, `route_fact_fastpath`, node wiring + `get_graph`, `arun_query`/`run_query` | last: the graph assembly imports everything |

## The shim pattern (non-negotiable)

`adaptive_rag.py` STAYS as the compatibility surface: after every move
it re-exports every moved name (`from gates import citation_pre_audit,
parse_declared_units, ...`) so the graph, the test suite, main.py, and
the fact_* family keep importing `adaptive_rag.<name>` unchanged. The
module shrinks toward a facade; nothing else in the repo edits.

## Verification gate per move (all must hold before the next)

1. `python -c "import adaptive_rag"` — imports clean.
2. `pytest tests/ --noconftest -m "not integration" -q` — full offline
   suite green (note: the local mock `tests/conftest.py` breaks live-DB
   tests under pytest — use `--noconftest` locally; CI has real deps).
3. `python scripts/battery_phase1.py` — routing matrix 42/42.
4. `python scripts/test_fastpath_stress.py` — 1,549 checks, 0 tokens.
5. No logic changes in the SAME commit as a move — mechanical only.

## Estimated effort

1–2 weeks total (GAP_ANALYSIS estimate); each module 0.5–1.5 days.
Highest value early: `gates.py` (cleanest cut, unblocks ADR-019's
detector amendment) and `audit.py` (the Phase-3 seam already exists).
