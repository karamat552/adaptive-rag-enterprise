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

## ADR-005: Verification Receipts — Tamper-Evident Claim-to-Chunk Chain

**Status:** Accepted · **Date:** 2026-09 · **Context:** "killer feature" review
(internal + Gemini 3.7 Flash second opinion independently ranked this #1)

**Context.** The audit certifies *at answer time*, but nothing proves later that a
served answer actually rested on the corpus. Financial consumers need the answer to
carry its own evidence: every claim traceable to a chunk, and every chunk provably
derived from a source page.

**Decision.** A receipt layer spanning all four layers:
- **Ingest (v2.1)**: every chunk carries `[char_start, char_end)` into a per-page
  transcript (section bodies joined `"\n\n"`, layout pass deterministic per PDF).
  Spans are located by verbatim `find()` — never offset arithmetic — and chunks
  that can't be located carry `NULL` spans ("not locatable", never fabricated).
  Transcripts are persisted per page (`corpus_chunks.transcripts.jsonl`), pinned to
  the source PDF by the manifest's SHA-256.
- **DB (migration 002)**: span columns on chunks, `page_transcripts` (unique per
  epoch) and `verification_receipts` (one per grounded run), both RLS-enforced.
- **Orchestrator**: the audited draft is split into claim sentences with their
  `[n]` citations; canonical evidence records (same ordering the synthesis saw)
  are saved with the receipt when the audit certifies.
- **Gateway**: `GET /verify/{run_id}` recomputes the chain deterministically —
  slice the stored transcript, re-hash `company⊣source⊣page⊣slice`, compare to
  `chunk_hash`, and require slice == chunk content verbatim. Zero LLM tokens.
  Tampering with the span, the content, the transcript, or a citation index
  breaks the chain by construction; unspanned (pre-2.1) evidence fails closed
  as `no_span`, never silently certifies.

**Why this and not the alternatives** (agreed with the second opinion): parallel
fleet speed is bounded by synthesis and cached repeats are already sub-second;
multi-provider routing is config anyone can copy (LiteLLM ships it); open-ended
"self-rewiring agents" are non-deterministic and add failure modes. The receipt
chain is the one feature a wrapper cannot fake — it requires ingest-lineage
discipline, an append-only migration culture, and a tamper-test habit, all of
which already existed here.

**Guardrails.** Receipt saves are best-effort (never block a certified answer);
receipts exist only for audit-certified runs (refusals 404 at /verify); offline
suite covers span mapping, claim extraction, chain verify, tampering (span/
content/citation), and the endpoint (`tests/test_receipt.py`, `tests/test_main.py`).
Live e2e proof: 186/186 corpus chunks span-verified; production round-trip stored
a 15-claim/11-evidence receipt and /verify recomputed 11/11 links with zero tokens.

## ADR-006: Table & Footnote-Aware Ingestion + Deterministic Arithmetic Flags

**Status:** Accepted · **Date:** 2026-09 · **Context:** the "blind spot" identified
by the Gemini 3.7 Flash second opinion during the killer-feature review

**Context.** SEC filings are dense tables bound to footnotes (GAAP vs non-GAAP
restatements, segment definitions, numbers modified by a footnote pages away).
Naive chunking destroys the cell-to-header binding: a row retrieved without its
column headers is a number without semantics — and the downstream audit then
faithfully verifies extracted nonsense.

**Decision.** Ingest v2.2 (schema 2.2.0):
- **Header-contexted rows**: headed tables render every row as a self-describing
  `RowLabel :: Column=value | ...` line — a row can never be retrieved without
  BOTH its row identity and its column semantics. Merged unit-rows extend the
  header (capped at 2). Label-column tables (balance sheets: PyMuPDF finds no
  header) fall back to grid-only — fabricating column names would be worse than
  none.
- **Footnote co-location**: same-page footnote-like lines (`(1) …`) are appended
  to every table block as a FOOTNOTES section. Cross-page footnote binding is
  explicitly OUT of scope for v2.2 (honest gap, revisit with XBRL ingestion).
- **Deterministic arithmetic**: `verify_table_arithmetic` checks total-rows
  against member-row sums per column (accounting negatives, 0.5 abs tolerance),
  zero LLM tokens. Chunks carry `arithmetic_ok` (None = nothing checkable,
  True = verified, False = flagged) — searchable (migration 003), surfaced in
  evidence formatting (`TABLE-INTEGRITY-FLAG` annotation), and recorded in the
  manifest's `table_integrity` block. **A violation is a FLAG, never a drop**:
  chunk windows legitimately contain partial member sets (10 flags on the real
  corpus, all chunk-window membership, zero actual corruption).
- **Construction-exact spans**: the page transcript is now BUILT from the
  emitted chunks (v2.1's find()-mapping replaced), so
  `transcript[char_start:char_end] == chunk.text` holds by construction —
  223/223 verified on the real corpus, plus a synthetic-PDF end-to-end test
  (parse → chunks → transcript slice → sha256 chain) in `tests/test_tables.py`.

**Operational finding (documented honestly):** the accuracy harness caught a
real config trap — per-stage model overrides in `.env` (`RAG_ROUTER_MODEL=…`)
silently break provider switching (a `RAG_PROVIDER=google` run forced to call
Groq model names → 404 → fail-closed). Cleared to provider-aware defaults;
`.env` overrides should only be set for the matching provider. Router prompt
also re-calibrated: company-name questions with obscure/false-premise metrics
(e.g. "Tesla dividend per share") must route to the corpus and refuse honestly
there, never be classified away by the router.

**Cost of the window limitation:** multi-chunk tables repeat their structural
head (TABLE: + header) per chunk; a row's value appears twice within a chunk
(pair-line + grid). Accepted: retrieval robustness and receipt verifiability
outrank token economy at this corpus scale.

**Gate for XBRL ingestion (roadmap #1):** structured `{metric, value, unit}`
extraction replaces heuristic parsing entirely; arithmetic checks then run on
native tables, not text reconstructions.

## ADR-007: Deterministic Contradiction Detection & Bounded Self-Healing

**Status:** Accepted · **Date:** 2026-09 · **Context:** roadmap item from the
converged killer-feature review (both opinions ranked it #2)

**Context.** The specialist fleet extracts in parallel from the same corpus.
When two specialists return different values for the same metric, silent
averaging (or lucky picking) in synthesis is exactly the wrong behavior for
a financial Q&A system. The open-ended "agent rewires its own strategy"
version was rejected by both feature-review opinions as non-deterministic
slop; this is the cheap, checkable shape.

**Decision.** Pure-Python layer, zero LLM tokens (adaptive_rag.py §4b):
- **Mentions**: per-sentence extraction gated on metric-family terms
  (revenue / net_income / margin / growth / cash / headcount / debt); value
  spaces are scale-normalized money ($X.XXB/M/K), percentages, and ≥5-digit
  counts. Periods bind to the figure's OWN clause — a comparative
  ("compared to $8B in Q4-2022") binds that figure to Q4-2022, not the
  sentence head (adversarial-review fix).
- **Grouping + tolerance**: (family, company, period, unit) groups flag
  when money/count disagree by >2% relative AND beyond rounding slack at
  the coarser figure's precision; percent space uses ABSOLUTE >0.5pp
  (relative gaps on small margins are rounding noise — adversarial-review
  fix). Direction conflicts ("grew 25%" vs "declined 25%") flag on polarity
  even at equal magnitude (adversarial-review fix, kind='direction').
- **Bounded retry**: first-seen contradiction triggers ONE deterministic
  sharpened re-search (company + family term + period — no LLM rewrite);
  fleet re-runs; a surviving conflict proceeds to synthesis with a
  CONSISTENCY ALERT instructing BOTH figures with sources, NEVER averaging.
- **Persistence**: contradictions ride the verification receipt
  (migration 004, contradictions_json) and the /query result dict; SSE
  surfaces cross_check/sharpen node transitions.

**Adversarial review** (Gemini 3.8 Flash via scripts/consult.py — the
standing multi-model review practice) accepted 3 of 4 findings (above).
The 4th — grouping lacks GAAP-vs-non-GAAP / segment-vs-consolidated /
quarter-vs-TTM dimensions — is REAL but needs structured extraction to fix
honestly; regex cannot attribute an accounting basis. Documented limitation;
the gate is the XBRL ingestion roadmap item, which replaces heuristic
parsing with native structured facts. Same review noted the retry query
cannot disambiguate segment-vs-consolidated conflicts; accepted — the
surviving-conflict path (surface, don't resolve) is the designed behavior
for exactly that case.

**Known limitation (inherited):** sentence-level regex period/entity binding
misses table-header context (period lives in the header, not the row
sentence) — mitigated today by Phase B header-contexted table rows, which
put column semantics on every row line; fully resolved by XBRL.

**Tests:** tests/test_contradictions.py — 22 cases: extraction spaces,
tolerance both ways (false-positive AND false-negative), period/company
gating, comparative-clause binding, direction conflicts, graph routing
(bounded retry), synthesis both-figures contract. Offline suite: 92 passed.

## ADR-008: Generic OpenAI-Compatible Seam + Stage-Aware Failover

**Status:** Accepted · **Date:** 2026-09 · **Context:** Phase D of the
converged roadmap; "essential plumbing, zero moat" per both feature-review
opinions — but two days of live provider instability proved its worth.

**Decision.**
- **Generic seam** (`RAG_PROVIDER=openai_compatible` + `RAG_BASE_URL` +
  `RAG_API_KEY` + mandatory per-stage models): any Cerebras / SambaNova /
  NVIDIA NIM / vLLM endpoint works with zero glue code; `scripts/list_models.py
  openai_compatible <base_url>` enumerates it. Missing model names fail
  LOUDLY at stage resolution (no silent cross-provider model leaks — the
  .env override trap that 404'd a whole test day).
- **Stage-aware failover**: `RAG_FAILOVER_ENDPOINTS` (JSON list of
  base_url/api_key_env/model). ROUTER and FLEET calls (allow_failover=True)
  transparently retry quota-class failures (429/rate-limit/TPD) on each
  backup in order. **EXECUTIVE (synthesis+audit) is PINNED** — no failover,
  ever: a weaker backup model certifying a financial brief is strictly worse
  than a verified refusal. Guarded by tests/test_failover.py
  (`test_executive_stage_never_touches_backups`).
- **Cooldown honors provider hints**: per-endpoint cooldown parses the
  provider's own retry window ("Please try again in 10m44.544s") — day-capped
  providers (Groq TPD) sleep the real remaining window, not a blind 60s;
  minute-capped providers fall back to the default. Non-quota errors
  (timeouts, 5xx) NEVER switch endpoints — the global circuit breaker owns
  those. `/health` reports endpoint count + cooling-down map + executive
  pin.

**War story 1 — catalog rotation (2026-09-04):** Groq silently retired
`llama-3.1-8b-instant` and `llama-4-scout` overnight; every router/fleet call
404'd, cascading to fail-closed refusals. Lessons encoded: provider-aware
defaults now point at surviving models (gpt-oss-20b / qwen3.8-27b / gpt-oss-120b,
benchmark-validated), and the operational rule stands: after any provider
error storm, re-run `scripts/list_models.py` + the 16-point benchmark before
trusting results. Free-tier catalogs are NOT stable contracts — the seam
exists precisely because providers rotate.

**War story 2 — fail-closed under a TPD wall:** with the day's token budget
exhausted mid-run, the system quarantined walled stages, auto-failed the
audit, and issued verified refusals — exactly the designed behavior, zero
hallucinations. Note: Groq's TPD is a ROLLING window, not calendar-midnight;
benchmark-heavy days consume it. Live gates should be budgeted accordingly
(the accuracy gate costs ~50-60K tokens).

**Phase C defect found & fixed live (same day):** the contradiction detector
attributed every report sentence to EVERY scoped company — comparison
questions generated 10-14 spurious cross-company contradictions that
flooded the synthesis prompt and drove audit refusals. Fix: per-sentence
company attribution (a sentence belongs only to companies it names;
unnamed sentences inherit the question's primary company) + the
CONSISTENCY ALERT is capped to the top-3 most severe conflicts (receipt
keeps all). Regression-tested in tests/test_contradictions.py.

**Adversarial consult (scripts/consult.py, Gemini 3.6 Flash — landed on the
third window after 2 days of Google 503s):** 5 findings, dispositions —
1. **Per-model TPD walls (ACCEPTED, confirmed live the same day):** Groq's
   token budgets are per-MODEL; cooldowns are now keyed
   `primary::provider::model` so a walled gpt-oss-20b router never blocks a
   still-open fleet model. Retry-hint parsing was already in place (the
   finding assumed fixed cooldowns).
2. **Fan-out storm (ACCEPTED):** the 3-specialist fan-out triple-counted
   failures toward the 5-failure circuit. Quota-class failures are now OWNED
   by the cooldown layer exclusively — the circuit records only the terminal
   all-endpoints-exhausted signal (genuinely systemic), once.
3. **Per-endpoint timeouts (ACCEPTED):** optional `timeout_s` per failover
   entry — an ultra-fast Cerebras hang no longer eats the global 45s before
   failover proceeds.
4. **'Zero glue' fallacy (ACCEPTED as documented limitation):** 429 formats
   genuinely vary across "OpenAI-compatible" providers; the string-class
   quota matcher covers observed conventions (Groq/OpenAI/Cerebras) but is
   heuristic, not a contract. A first integration against a new provider
   should verify its 429 shape against _is_quota_error before trusting it.
5. **Mirror-endpoint executive failover (REJECTED):** "identical weights at
   a mirror endpoint" cannot be VERIFIED at runtime from a URL — a financial
   audit certifying on an unverified identity claim is exactly the trade
   this ADR forbids. Verified refusal stays the designed behavior.
   (Recovery jitter + background health probes: noted, deferred — the
   half-open circuit probe already rate-limits recovery herds on primary.)

**War story 3 — per-model TPD (2026-09-04, second window):** probing
gpt-oss-120b showed "quota OK" while gpt-oss-20b sat at 199,574/200,000 —
every router call 429'd and fail-closed to out_of_domain, briefly mimicking
a router-classification bug. Lesson encoded in the cooldown keys and this
ADR: per-model budgets need per-model cooldowns, and never diagnose logic
errors while a quota wall is live.

**War story 4 — NIM catalog ≠ deployment (2026-09, key-day):** the first
configured NIM model (meta/llama-3.3-70b-instruct, from directory data)
wasn't even in the catalog; worse, NIM's /models list is the GLOBAL
catalog, not account-deployable functions — most listed models 404
("Function not found for account") or queue past 45s. Only
nvidia/nemotron-3-super-120b-a12b actually served. Rule: a failover
endpoint's model must be LIVE-PROBED (one real completion) before it
enters RAG_FAILOVER_ENDPOINTS; catalog listings are not deployability.
Related: nemotron-3-super is a REASONING model — with a tight token cap
its output is truncated mid-thought (looks like garbage extraction);
with headroom it lands clean strict JSON (verified: correct figure/unit/
period extraction in 1.3s). Reasoning models in the fleet need
fleet_max_tokens headroom, and any future candidate list should prefer
non-reasoning variants for latency-bound stages.

**Tests:** tests/test_failover.py — 17 cases: hint parsing, quota
classification, registry validation, failover ordering, cooldown skipping
(backups AND primary), per-model key extraction, non-quota no-switch,
all-walled exhaustion, circuit-vs-quota ownership, and the executive-pinned
invariant. Offline suite: 111 passed. (Subsequently extended by the
.env-neutralizing isolation fix when real failover config landed:
160 offline green, failover chain validated live against NIM end-to-end —
429 detected, 645s hint honored, nemotron pickup, correct response.)

---

## ADR-005 Addendum: Tamper Suite — Proving the Moat Provable

**Date:** 2026-09 · **Context:** converged roadmap item #1 (all three consult
models ranked it top-2: "irresponsible not to do this before expanding scope")

**Decision.** `tests/test_tamper.py` — 17 CI mutation tests over the receipt
chain: a control case (untampered receipt certifies) plus forgeries of every
attack surface (span shift/off-by-one/inversion/out-of-bounds, content lies,
self-consistent hash forgeries, transcript tampering, company/page
relabeling, citation forgeries, partial tamper among honest links,
empty/no-span fail-closed). Each test asserts BOTH `verified is False` AND
the specific catch status — we prove HOW each forgery breaks.

**The suite ran BEFORE the fixes — and caught two real escapes:**
1. **Span out-of-bounds silently truncated** (Python `t[0:600]` returns what
   exists) and could accidentally agree with content. Fix: spans must satisfy
   `0 <= start < end <= len(transcript)`; else `span_out_of_bounds`.
2. **Company relabeling certified** — an attacker relabeling Apple evidence
   as Meta's, with the hash recomputed over the lie, passed: company never
   met an authority. Fix: `/verify` now cross-checks each chunk's
   (company, source, page) against the CHUNK TABLE (hash is UNIQUE there —
   the DB is the truth, the receipt is the claim); mismatches yield
   `attribution_mismatch`, absent hashes `unknown_chunk`.

**Test-hygiene bug found & fixed the same session:** `test_guard_preaudit`'s
certifying path wrote run_id "t" receipts to the PRODUCTION DB on every
offline suite run (the receipt-save seam wasn't stubbed). Fixed; leak proven
closed (0 artifacts after a full suite run). Offline tests must never touch
production — added to the suite-review checklist.

**State:** 129 offline tests green; 10 integration green; the real production
receipt (15 claims) re-verified 15/15 under the HARDENED chain.

---

## ADR-009: Unit & Scale Assertion Engine — Deterministic Millions-vs-Billions Guard

**Status:** Accepted · **Date:** 2026-09 · **Context:** converged roadmap item
#3 (all three consult models flagged scale confusion as the most catastrophic
financial hallucination class; we hit its live debugging cost ourselves when
$14,017M vs $14.0B cross-contaminated contradiction groups)

**Context.** A right number at the wrong scale — "$21,563 billion" for an
"(in millions)" table — reads fluently and passes vibe-checks. An LLM auditor
can be talked past it; a regex reading the declared units cannot.

**Decision.** Pure-Python, zero-LLM-token gate wired into fact_checker_guard
BEFORE the LLM audit (same contract as the citation pre-audit):
- **parse_declared_units**: extracts the table's own declaration —
  "($ in millions, except percentages and per share data)" →
  {money_scale: 1e6, per_share_exception: True} — from the verbatim units
  rows Phase B tables already carry. No declaration (prose / pre-2.1
  evidence) → the engine DECLINES TO JUDGE (declining is not certifying;
  those claims still face the LLM audit + hash chain).
- **assert_claim_scales**: for each cited claim sentence, every $-figure
  must be RECONSTRUCTABLE from the cited evidence: raw table-unit values
  ("$21,563" quoted verbatim), declared-scale renderings ("$21.6 billion"
  == 21,563 x 1e6), or pairwise sums (totals derived from cited member
  rows). 2% tolerance for rounding. Anything else fails with the direction
  of error recorded. There is deliberately NO freestanding x1000 branch:
  a value 1000x every reconstructable figure IS the lie, not a re-rendering.
- **Fail-closed**: a scale lie rejects the draft (unverified_system) before
  a single auditor token is spent; findings (claimed, nearest evidence,
  ratio, suspected class) ride the state for receipts/UI.

**Validated against production**: the REAL certified Apple receipt (2,890-char
draft, 15 evidence chunks, "$22.3 billion" over in-millions tables) produces
ZERO false positives; the engine detects the declared scales in evidence and
declines to judge undeclared prose.

**Tests:** tests/test_units.py — 14 cases: declaration parsing (all forms),
honest verbatim/re-scaled/derived-sum claims pass, 1000x-up and
1000x-down lies plus unrelated magnitudes fail with direction, uncited and
undeclared-scale claims are never judged, and the guard fail-closes with a
zero-token spy proving the auditor is never reached on a scale lie.
Offline suite: 143 passed.

**Known limitation (honest):** reconstructability is per-citation-set — a
figure legitimately derived from values spread across MANY chunks beyond
pairwise sums (e.g. 4-segment totals) could false-positive. Accepted for
now: the audit retry path re-runs with different evidence composition, and
XBRL ingestion (roadmap #5) replaces reconstructability with exact
structured facts. Gate: revisit if live runs show false positives.

---

## ADR-010: Intra-Filing Growth-Claim Consistency — Direction Is Deterministic

**Status:** Accepted · **Date:** 2026-09 · **Context:** converged roadmap
item #4 (the cross-filing idea rescued to its correct scope by all three
consult models)

**Context.** The original idea — ingest prior-year filings and diff
narrative vs numbers — was unanimously flagged as a false-positive factory:
ASC-250 retrospective restatements (the prior-year column printed in the
current filing is the RESTATED figure), fiscal-vs-calendar quarter offsets
(Apple's Q4 ends September), and segment realignments. The rescued scope:
compare claims against the comparative columns WITHIN the same filing —
zero restatement exposure, and Phase B's pair rows already carry them.

**Decision.** Zero-LLM gate in fact_checker_guard (pre-audit, after the
scale gate):
- `find_comparative_pairs` parses the header-contexted pair rows
  ('Total automotive revenues :: Q4-2022=21,307 | Q4-2023=21,563 | YoY=1%')
  into (label, prior, current) pairs from the cited evidence.
- `check_growth_claims` extracts 'X grew/declined N%' claims and requires
  DIRECTION agreement with any matching pair. **Direction only** —
  magnitude math stays with the LLM audit (legitimate re-basings make naive
  percent checks false-positive-prone; direction is unambiguous).
- Decline-to-judge everywhere: no matching pair, non-financial subjects,
  grid-only evidence — no finding, never a guess.

**Live-validated:** zero false positives on the real production Apple
receipt (15 evidence chunks); direction lies fail-closed before the auditor
(zero-token spy proof in tests/test_consistency.py).

---

## ADR-011: XBRL Ground Truth — SEC-Published Facts as the Figure Authority

**Status:** Accepted · **Date:** 2026-09 · **Context:** roadmap #1/#5, the
flagship; the consult models' strongest endorsement ("figure-checking stops
being an LLM judgment call entirely")

**Decision.**
- **scripts/xbrl.py**: fetches us-gaap company-concept facts from
  data.sec.gov (declared User-Agent per SEC fair-access policy, ≤10 req/s,
  jittered retries) for the three corpus CIKs and derives fiscal Q4-2023
  figures as **Q4 = FY − 9mo** (the fourth quarter is never a primary XBRL
  fact — 10-Qs cover Q1-Q3, the 10-K covers the year). Fiscal conventions
  handled by period math: Apple's 371-day FY (53-week year) and
  September year-end vs Tesla/Meta calendar years. Every fact carries a
  payload SHA-256 over its source facts (lineage), the derivation method,
  and the source forms.
- **Migration 005**: `xbrl_facts` table (RLS-enforced, unique per
  tenant/company/metric/period/epoch) + `get_xbrl_facts` reader.
- **Guard crosscheck**: for each claim sentence citing company-period
  evidence with a matching fact, every metric-hinted $-figure must
  reconstruct from the official value (exact / rounding / millions-and-
  billions renderings). Mismatches fail-closed with the official value
  recorded. **Curated-concept only** (revenue, net income, EPS): segment
  claims (Products/Services, FoA/Reality Labs) are SKIPPED — the concept
  map has no segment dimension and a misattributed check is worse than
  none. Missing facts = decline-to-judge; a facts-fetch failure NEVER
  blocks a certified answer.

**Gold validation (live)**: all six derived facts match the filings —
Apple 89,498M revenue / 22,956M net income; Meta 40,111M / 14,017M;
Tesla 25,167M / 7,928M. Real-receipt validation: zero false positives on
the production Apple draft. Live e2e through all five gates: forced by
provider exhaustion to be deferred (Groq TPD wall + the Gemini free tier's
20-request/day cap discovered in the process); all deterministic layers
proven offline + against real receipts.

**Session's live-caught defects (the discipline paying for itself):**
1. MemoryError in the scale engine's pairwise-sums loop on real dense
   chunks (appending into the iterated list) — fixed with frozen/capped/
   de-duplicated base sets; regression test added. The 40-cap is
   per-FORM so raw and scaled renderings both survive.
2. langchain-google-genai 4.x schema change: `.bind(max_tokens=)` now
   raises GenerateContentConfig extra_forbidden AT INVOKE TIME — every
   google-provider specialist quarantined silently, surfacing downstream
   as 'Zero documents retrieved'. Fixed with `_bind_output_cap`
   (provider-aware maxOutputTokens). Lesson: provider switchovers should
   be smoke-tested with a live 1-token probe, not just config changes.

**Tests:** tests/test_consistency.py — 16 cases (comparative parsing,
direction agreement, decline-to-judge, guard fail-closed with zero-token
proofs). tests/test_units.py +1 dense-chunk regression. Offline: 160.

---

## ADR-012: Stage-Model Selection Criteria

**Status:** Accepted · **Date:** 2026-09 · **Context:** documented on request
after the provider-rotation week; the criteria below are the ones actually
applied, with their evidence, plus the honest gaps.

**Master principle.** Each stage gets the cheapest model whose failure mode
is survivable (v3.2 tiering): router is backstopped by re-route/refusal,
fleet extraction is backstopped by five deterministic gates + the LLM audit,
and the executive (synthesis+audit) is the un-backstopped stage — strongest
model, PINNED, never fails over (ADR-008 invariant).

**Criteria (in actual decision order):**
1. **Empirical pass-gate** — the 16-point benchmark must pass before any
   stage assignment. Honest scope: it is a good-enough gate, NOT a
   comparative ranking; candidate models have never been A/B'd.
2. **Structured-output fidelity** — router/rewriter/audit ride strict
   Pydantic schemas. This is a MODEL property: live incidents include
   full-width 【1】 citation drift (llama-4-scout), json_validate_failed
   (qwen3.8-27b rewriter), and the Google max_tokens→maxOutputTokens
   schema break.
3. **Quota topology** — budgets are PER-MODEL everywhere we looked:
   Groq 200K TPD/1K RPD per model (three stage models = three budgets =
   deliberate quota multiplexing); Google gemini-3.6-flash ~20 req/day vs
   3.5-flash on the 1,500-RPD tier (the sole reason for the 3.5 default —
   quota-driven selection, not a quality comparison); NIM 40 RPM nominal.
4. **Latency — deliberately last.** Sub-second is a vanity metric
   (consult consensus): wall-clock is bounded by executive synthesis, the
   fleet is parallel, the cache serves repeats. What matters is timeout
   BEHAVIOR under load — hence per-endpoint timeout_s, not speed tuning.
5. **Catalog stability** — Groq silently retired two stage models
   overnight; rule: re-run list_models + benchmark after any provider
   error storm. Free-tier catalogs are not contracts.
6. **Fail-closed compatibility** — every model must degrade
   429 → quarantine → verified refusal. Verified live repeatedly.

**Current assignments:** Groq gpt-oss-20b (router) / qwen3.8-27b (fleet) /
gpt-oss-120b (executive; known weakness: 8K free-tier TPM makes audit
prompts tight). Google: gemini-3.5-flash at all three stages (per-model
quota discovery). Failover: NIM nemotron-3-super-120b-a12b (validated live:
correct structured extraction; note it is a REASONING model needing token
headroom). ModelScope/DeepSeek: documented, pending keys.

**Honest gaps:** no clean 4/4 benchmark on any current stack yet (Q2 was
quota-confounded on Groq; gemini-3.5-flash untested end-to-end); no
model-vs-model extraction-accuracy eval (eval.py tests the pipeline, not
candidates); latency figures are log-derived, never profiled; NIM's
nemotron and any future ModelScope assignment remain provisional until
each passes the benchmark.

---

## ADR-011 Addendum: The Q2 Arc — Two More Live-Caught Gate Defects, Then Certified

**Date:** 2026-09-05 · **Context:** benchmark Q2 (the Apple-vs-Meta
comparison) refused across three days and four model stacks; each refusal
was the fail-closed machinery working correctly over progressively
different underlying faults — and two of them were OUR gate bugs.

**The arc, in order:**
1. **Scale-gate false positive (fixed):** cited sentences legitimately
   contain NON-period figures — "$50B share-repurchase authorization",
   "$969B market cap" — which quarterly comparative rows can never
   reconstruct. Fix: the gate now judges only period-anchored/metric-word
   figures and explicitly declines authorization/market-cap/guidance/
   cumulative contexts. 4 regression tests.
2. **The quarantine mystery (solved):** all attempts showed the
   [EXTRACTION_UNAVAILABLE] sentinel — synthesis itself 429ing on Groq's
   walled gpt-oss-120b (pinned executive, no failover, by design). Q2
   always refused because it burns ~35K tokens across retries and always
   lands deepest into the TPD wall.
3. **XBRL-gate year binding (fixed):** the first NIM-backed run completed
   synthesis cleanly — and the XBRL gate then flagged Meta's Q4-2022
   revenue ($32.165B) against the Q4-2023 fact: YoY comparison sentences
   legitimately contain BOTH years. Fix: per-figure year binding (nearest
   in-sentence year anchor); figures bound to years without facts are
   declined — the gate claims authority only for periods it holds truth
   for. 3 regression tests.
4. **Certified:** with all stages on NVIDIA NIM's
   nvidia/nemotron-3-super-120b-a12b (explicit provider flip — an
   equal-or-better 120B-class choice, within the executive-pin's spirit),
   Q2 certified: vectorstore/grounded, correct figures (Meta +25% revenue,
   +201% net income; Apple −0.7%), 3 contradictions surfaced (never
   averaged), and the receipt chain verified 15/15 links.

**Executive-pin amendment (ADR-008 refinement):** the pin exists to
prevent quality DEGRADATION. An EXPLICIT operator decision to run the
executive on an equal-or-stronger model during primary outage is within
its spirit; what remains forbidden is AUTOMATIC failover to a weaker
model. NIM nemotron-3-super-120b is thereby validated for all three
stages (its ADR-012 provisional tag is removed).

**State after the arc:** 167 offline tests green (164 + 3 year-binding
regressions); Q2's receipt exists and verifies; the accuracy gate is the
remaining validation.

**Accuracy-gate postscript (same day, NIM stack):** 5 passed · 2 skipped
(honest refusals) · 2 failed — and both failures are OMISSIONS on
hard-retrieval figures, never hallucinations: every certified answer that
was produced passed all five gates + the audit. Root cause (verified):
the gold advertising figure ($38,706M total) shares a segment note with its
ex-FX variant ($37,897M) in the corpus — which evidence pool retrieval
composes decides whether the total surfaces. Groq's fleet found it two
days ago; NIM's differently-weighted extraction didn't. Classification:
evidence-composition sensitivity on ambiguous segment notes — the exact
class XBRL ingestion retires (segment-tagged structured facts end the
composition lottery). No gate or model defect; no fix today beyond this
record. The growth-figure failure is the same class (derived YoY depends
on which comparative rows surface).

---

## ADR-013: The Unseen-Question Bug Hunt — Multi-Model Findings & Dispositions

**Date:** 2026-09-05 · **Method:** 8 previously-unseen edge questions (per-
share, negatives, wrong-premise, derived margin, 3-way comparison, no-
company scope, balance sheet, YoY) run live through the full NIM stack;
actual outputs fed to TWO independent adversarial reviewers (nemotron-120b
+ gemini-3.1-flash-lite; gemini-3.5 was 503). Every claim verified against
code/corpus before acceptance — reviewers were WRONG about key facts twice.

**Results pattern:** 5 certified / 3 refused. Q3 (Apple dividends) refusing
was correct-by-design (false premise). Q2 (RL loss) and Q4 (margin) were
real defects. And the reviewers' reviews plus human inspection surfaced a
6th defect NOBODY asked about (see below).

**Triage (10 findings):**
| # | Finding | Verdict |
|---|---|---|
| nemotron-1 | RL loss "not in 10-Q" | REJECTED — our Meta corpus IS the earnings release (Exhibit 99-1); chunks confirmed present. Q2 was retrieval composition, not coverage. |
| nemotron-2 / gemini-2 (converged) | derived metrics (margins/ratios) falsely rejected by scale+XBRL gates | ACCEPTED — fixed: derived-context sentences are now skipped by both gates; the LLM audit owns the arithmetic. |
| nemotron-3 / gemini-3 (converged) | wrong-premise questions burn the full retry loop (384s) | ACCEPTED — fixed: premise_fast_path node: one cheap unscoped retrieval over metric terms; zero hits → immediate specific refusal. DB errors fall through to the full pipeline (never refuse on infrastructure). |
| nemotron-4 / gemini-4 | diluted-vs-basic EPS confusion risk | VERIFIED NOT PRESENT in Q1's answer (states diluted correctly); audit owns the distinction. Watch item, not a defect. |
| nemotron-5 | "over-reliance on XBRL" / certify without XBRL match | REJECTED — misreads the design: XBRL is ONE gate; text-only evidence certifies routinely (Q1/Q5/Q7 did). |
| gemini-1 | negative figures rejected as "out of bounds" | REJECTED — no positive-only constraint exists; accounting negatives are handled (tested). |
| gemini-5 | response-level caching is an "audit risk" | REJECTED as stated — cache is epoch-guarded (corpus change invalidates) and only stores AUDIT-CERTIFIED answers; that is the documented design (cache hit = already-verified). |

**Finding #6 — found by human inspection AFTER the reviews (the reviewers
missed it):** Q1's "certified" EPS answer was RAW REASONING TRANSCRIPT —
nemotron's untagged reasoning channel leaked deliberation + prompt-echo into
content ("We need to answer... Let's check evidence [12]...") and the audit
CERTIFIED it. Two fixes: (a) _strip_reasoning extended with echo-marker
sanitization (cut to the last deliverable section; deliberation-only →
quarantine), (b) an echo-guard in fact_checker_guard rejects any draft
containing prompt-echo markers pre-audit, deterministically. The lesson:
reasoning models on OpenAI-compatible endpoints need leak defenses even
when "working", and a certify-happy auditor is a real threat model.

**Also noted (not fixed):** Q6 (no-company risk question) routed
out_of_domain — defensible for a 3-company corpus, but a 'risk trends
across our covered companies' phrasing would serve better; watch item for
the XBRL-era router prompt.

**State:** 179 offline tests green (12 new bug-hunt regressions); the
derived-scope, premise-fast-path, and leak-guard fixes live.

---

## ADR-014: Knowledge Fine-Tuning vs. Retrieval-First — Proof-First Is Non-Negotiable

**Status:** Accepted · **Date:** 2026-09 · **Context:** owner question —
should we fine-tune models on the filings so answers come from internalized
knowledge, eliminating retrieval misses? Adversarially reviewed by two models
(gemini-3.5-flash, nemotron-120b) before acceptance; both got partially
overruled.

**Decision. No knowledge fine-tuning. The core argument:**
- Fine-tuning teaches style, not reliable fact storage — numbers are
  exactly what it garbles (fluent, plausible, wrong).
- The deeper issue is architectural: every guarantee we built lives in the
  EVIDENCE path (char-span receipts, five gates, /verify). Fine-tuned
  answering gives the audit no evidence to verify against — trust
  collapses to "the model said so," which is the wrapper we are
  structurally differentiated from.
- Refresh: a fine-tuned model doesn't "forget" stale quarterly facts, it
  BLENDS them — the worst failure mode for financial data.

**Proof-first vs. justify-first (the settling argument):** nemotron argued
span-masked fine-tuning could recover verbatim numeric recall with
POST-HOC provenance (string-match answers back to the PDF, mint receipts).
Rejected: it inverts the trust model. We are proof-FIRST (evidence → answer
→ verifiable chain); justify-FIRST (answer → search for support after) is
precisely how hallucinations get laundered — a similar-but-wrong string
"verifies" a lie. And nemotron's own analysis conceded paraphrased/derived
answers fail exact-match, forcing retrieval fallback anyway — so
internalization buys only latency while adding training complexity and a
stale-facts mode. Bad trade for this system.

**Skill fine-tuning also rejected (upgraded position):** gemini flagged
schema-rigidity — fine-tuned extractors catastrophically forget when
companies restructure segments or adopt new standards, hallucinating into
their trained schema. Instead: schema discipline at DECODE time (structured
output today; constrained-decoding grammars — Outlines/Guidance — as the
hardened future), zero weight updates, zero drift.

**Accepted consult idea (the one surprise):** a small model fine-tuned as a
STRUCTURAL INDEX — not to answer, but to know where things live (which
note discloses lease liabilities) and generate routing hints/metadata
filters. Compatible with every guarantee (retrieval-only, never evidence).
Deferred until 100+ companies make it earn its complexity.

**Answerability strategy (replaces "100%"):** 100% answerability is
mathematically impossible under zero-fabrication — filings don't
pre-compute every ratio permutation, and a system that never refuses is a
system that hallucinates. The honest enterprise claim is a MEASURED one:
maximize recall of what the corpus supports (multi-query expansion,
per-entity sub-retrieval, temporal weighting, section-context embeddings,
XBRL segment facts, figure-DAG with the execution trace JOINED TO THE
RECEIPT — derived figures get the same tamper-evident proof as quoted
ones), make refusals fast and specific, and publish recall@answerable +
refusal-precision from a permanent coverage eval suite. "We answer 97% of
answerable questions, measured, misses itemized" is the only claim that
survives contact with an auditor.

---

## ADR-015: Token Efficiency & Doomed-Iteration Elimination — Measure, Then Cut

**Date:** 2026-09-07 → 2026-09-08
**Status:** Accepted (measured)

### Context

Free-tier quotas are the binding operational constraint (Groq gpt-oss-120b:
200K tokens/day; the executive stage lives here; qwen fleet: 30 RPM). Two
distinct failure economics dominated every battery run: (a) per-question
waste — expansion fan-out fired 6+ LLM calls even on confident retrievals;
(b) doomed-iteration waste — after a day-capped 429 announced "try again in
31m39.504s", the bounded retry loop still burned a full fleet re-run +
synthesis before refusing.

Two external proposals were rejected on evidence: a Python-first audit
bypass (the LLM auditor caught two fabrications in one week where every
deterministic check passed — Apple "essentially flat" revenue that actually
declined, and an interpretive RL "investment phase" claim), and fact-card
context compaction (breaks the [n]-citation → chunk → transcript → SHA-256
receipt chain).

### Decision

Six changes, each measured before/after by an interleaved A/B harness
(wrapper-counted LLM calls, per-question token telemetry, per-question arm
interleaving so quota weather hits both arms equally):

1. **Conditional multi-query expansion.** Direct search first; the
   paraphraser + variant searches fire only when the top result's
   vec_similarity < RAG_EXPANSION_CONFIDENCE (0.55). Confident hits skip
   them; the direct pass is reused in fusion, never re-searched.
2. **Abort-on-hint.** csuite_synth AND fact_checker_guard thread
   parse_retry_hint(exc) into state; transform_query aborts when the hint
   ≥ RAG_QUOTA_ABORT_S (900s); a new conditional edge
   (route_after_rewrite) routes aborted runs straight to verified refusal —
   the old unconditional rewrite→exec_db edge burned the doomed re-run even
   after the abort flag was set. RPM-scale hints (<30s) retry normally.
3. **Evidence dedup.** Chunks quoted verbatim in specialist reports collapse
   to stubs in the synthesis evidence list; chunks quoted in the DRAFT
   collapse in the audit context. Slots are preserved — citation indices
   still address identical chunks in receipts and /verify.
4. **Premise-probe company scoping.** The unscoped probe matched Meta's
   dividend-initiation headlines, so Tesla's no-dividend wrong-premise ran
   the full pipeline to reach the refusal it could have reached in seconds.
   The probe now filters to the question's own company; multi-company
   questions stay ineligible.
5. **% -Change verbatim mandate** (synthesis prompt rule 8): YoY percentages
   quoted from the source table's own % Change column, cited to that table —
   never self-computed (the auditor rejected a TRUE 25% claim as
   "calculated, not stated").
6. **Battery self-pacing + telemetry.** coverage_eval reads quota_hint_s
   from each result (threaded through arun_query — the first battery with
   the fix found the hint was computed but never surfaced) and waits out
   announced windows (capped); per-question tokens/wall-time in every
   report.

### Measured results (A/B, live Groq, 3 questions, interleaved)

- **Tokens: 67,004 → 46,006 per 3 questions (−31.3%)**
- **LLM calls: 85 → 46 (−46%)**; expansion calls 33 → 1 (−97%)
- Wall-time −91% on the walled run is *asterisked*: confounded by
  429-backoff weather (the abort fix's honest contribution is that walled
  refusals now take 30–90s instead of full retry loops — a 12-question
  walled battery completed in 14 minutes vs 30+).
- Zero recall change on confident questions; certification outcomes
  identical across arms.
- The white-whale (Apple-vs-Meta comparison) certified on the first
  post-fix fresh window with all four gold figures and a 15/15-link
  cryptographically verified receipt — with expansion skipping on every
  confident retrieval in the same run.

### Consequences

- A full battery now fits comfortably in one 200K window (was marginal).
- Refusals under quota fire faster AND cheaper — the fail-closed posture
  now also has fail-closed economics.
- Every future optimization must show its delta in coverage_report.json —
  no guessed savings. Rejected proposals stay rejected: the audit is not
  bypassed (moat preserved), the executive stays pinned (ADR-008).
- Echo-leak found during validation (receipt e8650748687f: nemotron
  rule-check deliberation certified as grounded) and closed with
  regression-locked markers — efficiency work must never outrun the guard
  suite, and this one nearly did.

---

## ADR-016: Redis as the Coordination Plane — Opinions, not Answers

**Date:** 2026-09-09
**Status:** Accepted (staged, trigger-gated)
**Consults:** Gemini 3.8 Flash (4 proposals, 2026-09-09) — vetted below; all
four accepted in amended form.

### Context

The system runs single-worker (`uvicorn --workers 1`, documented): rate
buckets, metrics, endpoint cooldowns, and the run semaphore are in-process
state. That is the known scale tax (KNOWN_ISSUES.md #3). Meanwhile the
binding operational constraints are provider quota (200K TPD/day at the
executive stage; 30 RPM at the fleet) and WAN latency to Neon
(250–530ms RTT, measured). Two batteries died to RPM storms; two more to
TPD walls — each time, the failure mode was *unshared knowledge*: every
process (and every iteration) re-learned what another already knew.

### The Evaporation Test (the boundary rule)

> Every datum considered for Redis must answer: "if this evaporated right
> now, what breaks?" — and the only acceptable answers are "a cache miss,
> a recompute, a re-counted bucket." Nothing that cannot evaporate may
> live in Redis.

**Never in Redis:** receipts, the SHA-256 chain, XBRL ground truth, corpus
+ embeddings, tenant identity (Postgres/RLS owns it; Redis mirrors at most).
**In Redis:** live beliefs — which providers are open, which runs are in
flight, which endpoints are healthy, what quota remains.

### The plays (in adoption order)

1. **Provider Weather Station** *(Redis TTL = cooldown state)* — the
   unification of EndpointCooldown, abort-on-hint, and battery
   self-pacing as one shared substrate: `SET wx:groq:exec:until <ts> NX
   EX <hint_s>`. One worker's 429 teaches every worker, the API, and any
   running battery. Existing in-process interfaces get a Redis backend
   behind a flag; call sites unchanged.
2. **Single-Flight Query Ledger** — idempotency keys + request coalescing
   (Stripe pattern): `SET lock:{qhash} NX EX`. Duplicate/double-click/
   paraphrase-within-window callers attach to the in-flight run instead of
   burning duplicate ~15K-token pipelines.
3. **Predictive Token Reservation** *(consult #1, amended)* — a shadow
   quota ledger in Redis (`INCRBY` actuals; calibrated from verbatim 429
   `Used` figures — Groq exposes no quota API, so every wall is a free
   ground-truth sync). Before firing the fleet: if `remaining <
   executive_estimate` (~12–15K: synthesis + audit + retry headroom),
   fail-closed EARLY. **Executive-pin amendment (ADR-008, overriding the
   consult's 'divert to NIM'):** the reservation protects the PINNED
   executive by refusing before the specialist burn; it never diverts the
   executive. Fleet stages remain failover-eligible per stage-aware rules.
4. **Ephemeral Read-Through Cache** *(consult #2, reduced)* — page
   transcripts (safe BY CONSTRUCTION: content-addressed, the hash chain
   verifies any copy), XBRL facts (epoch-static), sharpen-retry search
   results (deterministic re-queries). Epoch-keyed prefixes
   (`rt:v{epoch}:...`), 15-minute TTL, read-through on miss. Vector search
   itself stays on pgvector (the query is an embedding, not a hash).
5. **Corpus-Term Fast Refusal** *(consult #3, infrastructure rejected)* —
   the consult proposed RedisBloom; at 223 chunks an in-process EXACT set
   of company:metric pairs (loaded at boot, epoch-keyed) gives the same
   <2ms "definitely not in corpus" refusal with ZERO false positives and
   zero infrastructure. Bloom earns its Redis only at millions of keys.
6. **Global RPM Governor** — a Redis token-bucket per provider-model:
   the fleet fan-out stops being N processes' independent bursts against
   one 30 RPM budget and becomes one smoothed stream.
7. **Adaptive Endpoint Scorecard** — sliding-window success/latency per
   endpoint (`ZADD`/`ZRANGE`); failover order becomes "best recent" over
   "first configured." Boot-smoke probes write into it.
8. **Run Semaphore, system-wide** — the concurrency cap becomes a system
   property, not a per-process one.
9. **Streams + Single-Flight SSE** *(consult #4, tension documented)* —
   Redis Streams decouple live agent telemetry from the HTTP connection
   (XADD per stage; consumers re-attach after refresh). TENSION: the
   ghost-request mitigation deliberately cancels runs on disconnect to
   halt token spend; Streams un-does that save. RESOLUTION: ships WITH
   single-flight — a detached run completes into the semantic cache, so
   the re-asker gets a cache hit (cheaper than cancel + full re-run), and
   a grace timer cancels truly abandoned runs. The cancel-vs-complete
   economics flip only because single-flight makes completion durable.

### Staging (trigger-gated, never speculative)

- **Stage 0 (now):** documented decision. Single-worker is correct for
  current load.
- **Stage 1 — Weather Station + RPM Governor + Run Semaphore + metrics**
  *(trigger: first need for a second worker/instance)*. One day; zero
  compliance semantics; unlocks horizontal scale.
- **Stage 2 — Reservation Engine + Read-Through Cache + Single-Flight**
  *(trigger: concurrent-traffic pain or the load test that precedes it)*.
  2–3 days; reservation needs the 429-calibration path proven live.
- **Stage 3 — Streams/SSE + adaptive scorecard** *(trigger: multi-worker
  UI deployments or endpoint-personality pain exceeding config order)*.
- **Corpus-term refusal (5)** is Stage-independent (in-process, free).

### Rejected

- **Caching /verify verdicts** — the endpoint's entire value is fresh
  recomputation against the hash chain; caching makes tamper-evidence
  performative.
- **Receipts/corpus/XBRL in Redis** — fails the Evaporation Test
  catastrophically.
- **Corpus vectors in Redis vector sets** — retrieval is not the
  bottleneck (once per question, local ONNX embedding) and the move would
  lose hybrid RRF + RLS.
- **LangGraph checkpointer** — deferred with trigger (multi-turn mode);
  single-pass bounded graphs have nothing to checkpoint, and Redis does
  not reduce prompt tokens — history a model needs still enters the
  context window at generation.

### Consequences

- Every play degrades to exactly today's behavior if Redis vanishes (the
  flag flips back; the in-process impls remain). Evaporation-safe by
  construction, rollback by config.
- The quota ledger is a shadow, not truth: it drifts between 429s and is
  corrected by them. Never treated as billing data.
- Single-worker remains the deployment until a trigger fires — Redis is
  the prepared path, not the default tax.

---

## ADR-008 Amendment: Peer-to-Peer Executive Failover (2026-09-10)

**Context.** Two batteries died with their audit stages quota-walled
mid-run: the executive (gpt-oss-120b) hit its 200K TPD ceiling with
hundreds of thousands of specialist tokens already spent. Fail-closed
refusal was CORRECT — but the day-capped-TPD class is a *capacity* event,
not a quality event, and the original ADR-008 language ("never a weaker
model certifying a financial brief") was drafted against quality risk,
not quota clocks.

**Amendment.** With `RAG_EXEC_PEER_FAILOVER=1` (opt-in, default OFF — the
pinned behavior remains the default), a QUOTA-CLASS failure at the
executive stage may escalate to a strictly-vetted **peer pool** of
benchmark-passed, equal-or-better models:

    Groq gpt-oss-120b → (429) → NIM nemotron-3-super-120b-a12b
                                 → (429) → Google gemini-3.5-flash

**The guardrails (all invariant, all regression-locked):**
- **Allowlist only:** the peer pool is an explicit constant — never the
  general failover registry, never 8B/20B fleet models, never community/
  free lanes (a pool test parses parameter counts: sub-100B models are
  structurally barred).
- **Vetted only:** a model enters the pool by passing the 16-point
  benchmark on the live pipeline — the same bar the primary executive
  is held to.
- **Quota-only:** non-quota failures (5xx, garbage output, protocol
  errors) NEVER escalate — fail-closed exactly as before. The peer tier
  answers day-capacity walls, not uncertainty.
- **Loud:** every peer rescue logs which peer certified; receipts name
  the executive.
- **Cooldown-aware:** a peer that 429s is skipped for its server-stated
  window, mirroring the fleet failover semantics.

**Why gemini-3.5-flash qualifies:** it is the 1,500-RPD-tier model
(NOT the tiny 20/day 3.6-flash), benchmark-passed on the live pipeline.

32 failover-suite tests green including the four new invariants.
