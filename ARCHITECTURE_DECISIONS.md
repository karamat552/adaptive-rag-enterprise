# Architecture Decision Records

Lightweight ADRs for decisions that shaped — or were deliberately deferred from — this
platform. Each records the context, the decision, and the reason it holds at our current
scale, so future maintainers (and interviewers) can see the *judgment*, not just the code.

---

## ADR-001: Dynamic Selective Specialist Dispatch — DEFERRED

**Status:** Deferred (telemetry-gated) · **Date:** 2026-08 · **Context:** [v4.0 Roadmap]

**Context.** All vectorstore queries dispatch the full 3-specialist fleet (financial,
risk, product). For hyper-specific single-metric queries this burns ~800–1,600 avoidable
extraction tokens. Tempting fix: extend `RouteDecision` with
`target_analysts: List[...]` so the router picks 1–2 specialists.

**Decision.** Do not implement yet. Reasons:

1. **The router becomes load-bearing.** Our tiering principle puts cheap models only
   where failures are backstopped (`adaptive_rag.py` provider defaults). The 8B router
   makes a coarse, backstopped 3-way single-label call today; multi-label dispatch is a
   fine-grained decision with no backstop, and small models are flakiest exactly there
   (label collapse).
2. **Misroutes fail worse, not safer.** The audit checks claims against *retrieved
   docs*, not question coverage. A misroute yields either a refusal loop (all 3 retry
   attempts repeat the same wrong dispatch) or, worse, a *grounded-but-thin* answer that
   caches — poison the canary cannot see.
3. **The eval has a blind spot.** Gold suites treat refusals as honest skips, so a
   systematic misclassifier converting answers to refusals would pass every gate.
4. **The savings are thinner than the headline.** The semantic cache already absorbs
   repeats; the realistic query universe (3 companies × 1 quarter) caches out fast.

**Telemetry gate (implement when justified):** `queries_total{analysts=...}` dispatch
labels + a refusal-rate budget in `eval.py`. Guarded design if built: `financial`
always dispatches; escalate to full fleet when `retry_count > 0` so a misroute
self-heals on attempt 2.

---

## ADR-002: Router Upgrade vs. Cascade — REJECTED (upgrades), DEFERRED (cascade)

**Status:** Deferred · **Date:** 2026-08

**Context.** Three alternatives were evaluated for multi-label routing fidelity:
a 70B router, a pure embedding router, or few-shot prompting of the existing 8B.

**Decision.** Reject the model upgrade on quota economics alone: the router is the
highest-frequency uncached-path call (and the rewriter draws from the same slot), so
moving it into the constrained 70B pool puts our hottest call in the same bucket the
unbackstopped audit depends on. Expected savings from improved precision (~300–500
tokens per avoided misroute) do not cover the premium paid on *every* cache miss.
Also rejected for now: a cross-provider router behind the process-wide circuit breaker
(a second provider's outage would trip the shared breaker and take down healthy paths).

**If routing ever needs upgrading, the design is a confidence-gated cascade:**
deterministic scope regex → corpus-derived embedding prior (kNN over the 186 labeled
chunks, CI-testable, zero tokens) → few-shot 8B only on embedding-margin ambiguity →
escalate-on-retry full dispatch. Same philosophy as our quota tiering: cheap
deterministic signals first, LLM judgment only where ambiguity is demonstrated.

---

## ADR-003: Deterministic Citation Pre-Audit — ADOPTED

**Status:** Accepted · **Date:** 2026-08 · **Supersedes part of:** ADR-001 discussion

**Context.** The probabilistic 120B auditor catches fabricated citations (the canary
proved it), but only *after* tokens are spent.

**Decision.** `citation_pre_audit()` (pure regex, zero tokens) rejects any `[n]`
outside the evidence range 1..len(documents) — fabricated *by construction* — before
the auditor is invoked. Number substantiation is deliberately **not** checked
deterministically: derived metrics and legitimate rounding make naive numeric matching
false-positive-prone, so it stays with the LLM auditor. This is the one check with
zero false-positive risk. Guarded by `tests/test_guard_preaudit.py` (8 tests,
including spy-based proof the auditor is never invoked on a bad-citation draft).

---

## ADR-004: In-Process Rate Limiting & Metrics — Single Replica, Documented

**Status:** Accepted (trade-off) · **Date:** 2026-08 · **Context:** [v4.0 Roadmap]

**Context.** Rate buckets, Prometheus counters, and the run semaphore are in-process.

**Decision.** Intentional: one uvicorn worker / one replica. Multi-replica needs
externalized state (Postgres- or Redis-backed buckets) and LangGraph checkpointing —
real engineering that solves a traffic problem we do not have while adding a new
failure domain (Redis down = gateway degraded). Our state is already in Postgres, so
Redis's complexity is not earned yet.

**Gate:** revisit when p95 latency degrades under real load or a second replica is
actually required. Until then this is the documented, honest trade-off.

---

## v4.0 Roadmap (evaluated, ordered, telemetry-gated)

1. **SEC EDGAR continuous ingestion + XBRL ground truth** — the flagship: the corpus
   pipeline (`ingest.py` → `migrate_from_manifest` → `bump_corpus_epoch`) is already
   ~80% of the plumbing. Key wrinkles: EDGAR filings are inline-XBRL HTML (needs an
   HTML parse path, not PyMuPDF-as-is), and fair-access policy (≤10 req/s, declared
   User-Agent). XBRL companyfacts provide exact structured figures.
2. **Deterministic derived-metric math** — pair with #1: structured
   `{metric, value, unit, evidence_idx}` extraction + a whitelisted arithmetic
   evaluator (~30 lines, no `exec`, no sandbox) for growth/margins/deltas; teach the
   auditor to trust values tagged `computed_deterministically`. Prerequisite: the
   structured extraction, not the evaluator.
3. **HITL review queue for verified refusals** — store refusal payloads for compliance
   review; every resolved item becomes a gold-set eval case. Guarded: force-publish
   only with a permanently-carried `overridden_by` provenance flag (never silent).
4. **Distributed rate limiting / checkpointing** — strictly traffic-gated (see ADR-004).
