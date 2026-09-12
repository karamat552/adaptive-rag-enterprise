# Version 2.0 Roadmap — Gap Analysis & Prioritized Engineering Plan

**Authored:** 2026-09-12 · **Context:** Post-arc forensic gap analysis, grade
63/100 on the institutional rubric. This document IS the roadmap to 90+.

**Methodology:** Every gap was identified by tracing the actual code,
dependency tree, deployment configuration, and operational posture —
not by generic checklist. Items are ranked by (impact × confidence) / effort.

---

## What We Built (the foundation this roadmap extends)

The system solves the hard problems most prototypes never get right:
- Cryptographic receipt chain (claim → chunk → transcript → SHA-256)
- Five deterministic pre-audit gates (citation, scale, growth, XBRL, echo)
- Multi-provider failover (Groq → NIM → TokenRouter + executive peers)
- Fail-closed discipline (zero fabricated certified answers, ever)
- 297-test adversarial regression suite (204 → 297 in one arc)
- Offline-verifiable compliance bundles (Ed25519 + EDGAR anchoring)
- Docker containerization (multi-stage build, deployed on Render)

---

## Tier 1: Blocks Production Sign-Off (build first)

### 1. Dependency Pinning & Vulnerability Scanning
**Gap:** 41 unpinned packages in requirements.txt. No `pip-audit`, no
`safety check`, no lock file. A single `langchain` update could break the
entire pipeline (LangChain 1.6 already changed structured-output behavior).
**Impact:** Security + reproducibility + stability.
**Effort:** ~2 hours. `pip-compile` + `pip-audit` + CI gate.
**Files:** requirements.txt → requirements.lock, .github/workflows/ci.yml

### 2. Per-Model Token Telemetry (partially built, needs state threading)
**Gap:** `_MODEL_USAGE` accumulator exists but was NOT in MultiAgentState —
the graph dropped it at merge. The result dict now exposes it, but the
per-question battery rows don't aggregate it yet.
**Impact:** Without per-model attribution, "which model consumed what?"
is unanswerable. This is the prerequisite for cost governance.
**Effort:** ~1 hour. Add `per_model_usage` to MultiAgentState + `_with_usage`
accumulation + coverage_eval per-row breakdown.
**Files:** adaptive_rag.py (state schema + _with_usage), scripts/coverage_eval.py

### 3. Live-LLM Test Isolation
**Gap:** test_main.py auth tests run real pipelines against Groq — they
flake when the TPD wall is active. The offline suite is deterministic but
the full suite is green-95% (the documented flake class).
**Impact:** CI reliability. A red build from quota exhaustion erodes trust
in the test suite.
**Effort:** ~1 hour. Split into `tests/offline/` and `tests/live/` with
pytest markers; CI runs offline always, live on schedule.
**Files:** tests/test_main.py, tests/test_answer_accuracy.py, pytest.ini

---

## Tier 2: First Month of Production (high leverage)

### 4. Prometheus Scraping + Grafana Dashboard
**Gap:** `/metrics` endpoint exposes counters but nothing scrapes them.
No alerting when query_errors spike or auth_rejected_total jumps.
**Impact:** You can't operate what you can't observe.
**Effort:** ~4 hours (Grafana Cloud free tier + scrape config + 3 alert rules).
**Files:** main.py (already exposes /metrics), render.yaml (add Grafana sidecar)

### 5. Error Hierarchy
**Gap:** Every error is RuntimeError/ValueError/Exception. No custom
exception classes. Callers can't catch specific failure types.
**Impact:** Developer experience + operational clarity.
**Effort:** ~2 hours. Define 6 custom exceptions, update raise sites.
**Files:** adaptive_rag.py (new exceptions.py or inline)

### 6. Config Validation at Startup
**Gap:** RAG_FAILOVER_ENDPOINTS validated lazily (first use). Invalid
config doesn't fail until the first request hits that path.
**Impact:** Fail-fast at boot instead of mid-request.
**Effort:** ~30 minutes. Call the validators in the lifespan startup.
**Files:** main.py (lifespan), adaptive_rag.py (expose validators)

### 7. Data Retention Policy
**Gap:** verification_receipts, page_transcripts, and semantic_cache
accumulate forever. No retention policy, no GDPR deletion endpoint,
no PII scan.
**Impact:** SEC-regulated firms require documented data retention.
**Effort:** ~4 hours. Add retention_days to Settings, a purge job,
and a /privacy/delete endpoint.
**Files:** db.py (purge functions), main.py (endpoint), Settings

### 8. k6 Load Test Execution
**Gap:** scripts/load_test.js exists but has never been run. The p95
latency and error rate numbers are needed for the resume baseline and
the ADR-016 Redis trigger.
**Impact:** Measurement (the audit deduction for missing load data).
**Effort:** ~30 minutes. Install k6, run against Render.
**Files:** scripts/load_test.js (already written)

---

## Tier 3: Architectural Evolution (weeks, not sprints)

### 9. Module Decomposition
**Gap:** 3,200+ lines of adaptive_rag.py containing router, fleet
orchestrator, cross-check engine, synthesis pipeline, audit guard,
5 deterministic gates, failover logic, telemetry, and the benchmark.
No module boundaries, no interface contracts.
**Impact:** Maintainability at scale. Testing in isolation.
**Effort:** ~1-2 weeks. Split into: gates.py, fleet.py, synthesis.py,
audit.py, failover.py, telemetry.py, state.py. Update all imports.
**Risk:** Medium — the test suite (297) catches regressions, but the
module boundaries need careful design.

### 10. Figure-DAG (Symbolic Math Engine)
**Gap:** The LLM computes derived ratios (margins, growth %) inline —
the auditor can't verify computations, only verbatim quotes. The
%-Change mandate (rule 8) is a patch, not a solution.
**Impact:** Recall (eliminates false rejections of derived claims) +
integrity (computation traces joined to receipts).
**Effort:** ~2-3 weeks. Parse formulas from LLM output, execute over
XBRL-verified operands, join execution traces to the receipt chain.
**ADR:** ADR-014 end-state. Highest-impact recall improvement available.

### 11. Domain-Expert Evaluation Corpus
**Gap:** 13 battery questions is not a benchmark. A domain expert needs
to curate 100+ questions with verified answers across multiple difficulty
tiers (extraction, derivation, comparison, multi-document synthesis).
**Impact:** Scientific rigor. The recall number becomes defensible.
**Effort:** ~1-2 weeks of domain expert time. Without this, recall
claims are self-referential.

### 12. Knowledge Graph (the end-state retrieval upgrade)
**Gap:** Vector similarity retrieves chunks — it doesn't model entity
relationships. "How does Meta's Reality Labs loss affect their overall
profitability?" requires connecting entities across documents, which
vector search alone can't do.
**Impact:** Multi-document reasoning. The retrieval blind spot narrows.
**Effort:** ~4-6 weeks. Entity extraction, relation modeling, graph
storage (Neo4j or pgvector+SQL), retrieval strategy.
**Risk:** High — this is a fundamentally different retrieval paradigm.

---

## Not on the Roadmap (deliberate positions)

- **Python-first audit bypass**: the LLM auditor catches fabrications
  that deterministic checks miss (proven: 2 live catches this month).
  The moat is not for sale.
- **Executive unpinning**: gpt-oss-120b is the certification authority.
  Peer rescue (b5034c9) answers capacity events; quality events fail
  closed. Always.
- **Microservice decomposition**: single-worker is correct for current
  load. The ADR-016 coordination plane is the prepared path — build it
  when the trigger fires, not before.
- **Cross-currency equivalence**: deliberately NOT guessed by the
  deterministic layer. The LLM audit owns cross-currency reasoning.

---

## Score Projection

| State | Institutional Score | Blocker |
|---|---|---|
| **Today** | 63/100 | Stale deploy, no fuzz/chaos in CI, open auth |
| **After Tier 1** | ~75/100 | Pinned deps, test isolation, per-model telemetry |
| **After Tier 2** | ~85/100 | Observability, error hierarchy, retention policy, load data |
| **After Tier 3** | ~90+/100 | Module decomposition, Figure-DAG, domain corpus |

The roadmap to 90+ is itemized, scoped, and mostly execution — the
architecture is ready to receive every item.
