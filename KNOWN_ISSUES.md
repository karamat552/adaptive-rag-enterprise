# Known Issues & Honest Limits

**Last updated:** 2026-09-12 · **Suite:** 297/297 offline green · **Invariant:** zero fabricated certified answers across every battery, ever. · **Receipts:** 73 grounded, every one crypto-verified.

This document is the project's own list of its open problems — written by
its maintainer, with status, evidence, and fixes' commit hashes. If you
are reviewing this repo: finding one of these items is finding something
we already found. The interesting question is never "is there a
problem?" — every system has them — it's "which ones, and who found
them first?"

---

## Open problems (real, named, scheduled)

### 1. Live-run recall is 50–67% — every miss documented, none hidden
Measured across four full battery runs on three providers. Every miss
decomposes to a named cause: capacity starvation (TPD walls, NIM 503s),
endpoint drafting variance, or auditor strictness. **The invariant that
matters holds: 100% gold accuracy on certified answers, 100% wrong-premise
refusals, 100% adversarial blocks, 0 fabrications — in every single run.**
The scorecard is `coverage_report.json` (generated, never hand-edited).

### 2. Wall-time A/B latency is confounded by quota weather
Token/call reductions (−31.3% / −46%) are cleanly measured; the −91%
wall-time number from the walled run is asterisked (429-backoff storms hit
one arm unevenly despite per-question interleaving). Clean re-measure needs
one calm window. Under walled conditions the abort-on-hint fix provably
turns 5-minute doomed retry loops into 30–90s refusals.

### 3. Single worker, in-process rate buckets
`uvicorn --workers 1` is documented as intentional: rate buckets and
metrics are in-process state. Horizontal scale means Redis (named path,
deliberately deferred — see the Redis discussion ledger). RLS and the
connection pool are already multi-worker-safe.

### 4. Corpus is 3 filings; ingestion is DELETE-and-replace
Re-ingestion replaces a source's pages wholesale and bumps the corpus
epoch (cache evicts correctly — verified). No PDF-version diffing. Receipts
from prior epochs still verify against their own epoch's transcripts.
Incremental ingestion is a roadmap item, not an oversight.

### 5. Evaluation gold set is small and hand-curated
12 battery questions + 6 XBRL facts + ~30 gold figures. The contribution
is the *shape*: recall@answerable, fabrication detection, and refusal
correctness as first-class metrics, with a battery that grows by one row
per live incident (that is exactly how it grew to 12).

### 6. The lexicons are English/SEC-specific
Metric families, clause breakers, % -Change patterns, COLUMN-KEY year
headers — tuned to US SEC prose. The *architecture* (deterministic gates,
tamper-evident receipts, fail-closed audit) generalizes; the lexicons are
re-derived per domain by design.

### 7. Model-catalog rotation can break a session before boot smoke runs
Groq rotated two models mid-project. The boot smoke test now names dead
models at startup — but it runs at service start, not at import time.

---

## Epistemic limits (never "solved" — narrowed and exposed)

### 8. The retrieval blind spot
The audit verifies drafts against the chunks retrieval found; text that was
never retrieved cannot contradict a draft. Mitigations: multi-query
expansion (conditional), per-entity sub-retrieval, and — uniquely — the
receipt shows exactly which chunks were seen, per answer, so a human can
audit the audit. Every RAG system has this hole; ours is the one where you
can see it.

### 9. The audit audits the LLM's draft against the LLM's evidence
"Circularity" is mitigated by five deterministic zero-token gates, XBRL
ground truth, cross-specialist extraction, and the receipt chain — but the
LLM auditor is the final authority on qualitative claims, and two of its
catches this arc (a wrong direction word, an interpretive claim) prove its
value. We do not bypass it; see ADR-015's rejected-proposals section.

---

## Fixed (recent, live-caught — kept here because they teach the classes)

| Class | Live case | Fix |
|---|---|---|
| Echo-leak certification | receipt e8650748687f: nemotron rule-check deliberation certified grounded | 7 new markers, stripper+guard, `20fd15f` |
| Self-pacing blindness | 16-min hints announced, 0 pauses fired | quota_hint_s threaded to results, `7782921` |
| Cross-company XBRL judging | Meta's $40,111M judged vs Apple's $22,956M gold | sentence-named attribution, `28dd632` |
| Figure-level gate ownership | assets figure judged vs revenue fact | decoy anchors, `a8dd0bf` |
| Timeout starvation | 429-storm backoff burned the timeout budget silently | type-based timeout cooldown + failover, `a8dd0bf` |
| Column-counting errors | 2022 column quoted for 2023 questions | COLUMN-KEY narration, `efdb44a` |
| Per-share/basis collisions | $2.27 GAAP EPS vs $0.71 non-GAAP = "conflict" | unit-space + basis split, `a8dd0bf` |
| Bullet-claim fidelity | `- Revenue grew [2]\n- EPS rose [3]` merged into ONE claim citing [2,3] | atomic bullet units, `77a78a7` |
| Injection-echo guard gap | draft echoing "IGNORE ALL PREVIOUS INSTRUCTIONS" passed the echo-guard | 7 injection markers added, `77a78a7` |
| Unprovable cache replays | legacy provenance-less entries self-pointed, /verify 404'd | provenance-less entries = miss (self-heal), `73c938e` |
| Graph channel drops | 6 state keys (provenance, quota_hint, echo_reject, xbrl_issues, quota_aborted, _premise_fast_path) silently discarded at merge | all declared in MultiAgentState + channel introspection test, `fc77992` |
| Synthesis headroom deficit | GLM reasoning burned 9,230 chars before content — 1800-token cap returned empty draft | RAG_REASONING_HEADROOM env floor in cap binder, `0f61ae8` |
| Instant PoolError on burst | 24 concurrent _db_calls vs pool_max=15 → ~9 immediate failures | bounded-wait checkout (DB_POOL_WAIT_S=5s), `e3d6e8a` |
| Dead transcript sync | sync_page_transcripts defined but never called — re-ingest left transcripts stale | wired into db.py entry after migrate, `dea6f2c` |
| Schema-aware peer rescue | peer-rescued audit returned raw text where GroundingCheck object expected | peer_schema threaded + repair validation, `155a69f` |
| Circuit ownership violation | quota 429s counted toward 5-failure global circuit | quota = cooldown, non-quota = circuit, `155a69f` |

## Operational posture

- `/query` auth: **QUERY_API_KEYS** now enforces tenant identity when set
  (open mode when unset — demo posture) — `99989df`.
- Failover: primary (Groq) → NIM per-stage, executive PINNED + PEER-RESCUE (ADR-008 amendment: NIM 120b → Gemini 3.5-flash).
- Quota economics: a full battery fits one 200K window post-ADR-015.
- ModelScope second backup: **PARKED — external constraint** (2026-09-10). API-Inference requires Alibaba Cloud account binding + real-name verification (KYC); verified live (token lists 46 models, inference 401s until KYC completes). The failover stack is complete without it: Groq primary → NIM 120b (lane 1) → TokenRouter GLM-5.3-free (lane 2), with NIM/gemini-3.5-flash as executive peers. Revisit only if the active lanes prove insufficient.
- k6 load test: scheduled — 5-10 concurrent users, p95 latency + error rates for the resume baseline
- Redis Stage 1: trigger-gated on k6 results (p95 degradation or second worker); ADR-016 has the 9-play roadmap ready
- Incremental PDF diffing: deferred (SHA-256 comparison is correct for 3 filings; page-level diffing is a roadmap item for >20 sources)
- Suite determinism: live-LLM tests (test_main.py auth, test_answer_accuracy.py) are the known flake class; offline suite is deterministic 297/297
