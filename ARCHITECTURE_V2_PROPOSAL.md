# ARCHITECTURE V2 — The Deterministic-First Refactor (Proposal for External Review)

**Author:** IX Alpha (the build agent with full repo access)
**Status:** PROPOSAL — awaiting second-opinion review before execution
**Baseline:** Adaptive-RAG-Enterprise, main @ 2a7175b · 311 tests green · live on Render + Streamlit
**Reviewer instructions are at the bottom of this document.**

---

## 0. Why this refactor exists (measured evidence, not opinion)

Every number below was measured on the live system or read from the code — this
is not a hypothetical redesign.

| Measured fact | Source |
|---|---|
| A certified numeric answer costs ~20K tokens across 5–7 LLM calls (router → specialist fleet → synthesis → audit) | Render `/metrics`, run `19c2d7e90d54`: 19,893 tokens, 44.6s |
| Groq free tier: 200K TPD/model, 30 RPM — the audit call alone (~4–6K tokens) burns ~3% of the daily ceiling per question | Groq 429 bodies in refusal receipts, 2026-09-13 |
| Pruning (1 specialist, top_k=15, evidence-pool parity) cut a run to 19,893 tokens / 44.6s — already shipped, default-on | commit `df5cca6` |
| The SEC already publishes every figure we ask an LLM to extract, as XBRL tags; we ALREADY ingest them into an `xbrl_facts` table and use them as a *cross-check gate* | `scripts/xbrl.py`, `db.py` |
| Our own failure ledger shows the real extraction risk is **misbinding** (wrong entity/period/basis/column), not number invention — caught live 4 times by our own gates | KNOWN_ISSUES.md classes: cross-company XBRL judging `28dd632`, column-counting `efdb44a`, per-share/basis collisions, pruning evidence-pool shrink (2026-09-13) |

**The structural insight:** we built deterministic verification machinery
(five zero-token gates, construction-exact byte spans, SHA-256 receipt chain,
XBRL reconciliation) — and then still pay an LLM to do the *extraction*
underneath it. The refactor inverts that: numbers are **looked up, not
generated**; the LLM is reserved for interpretation.

---

## 1. Current architecture (V1) — stated precisely

```
auth → semantic cache (0.985 near-exact replay) → premise fast-path
  → router (gpt-oss-20b, structured RouteDecision, failover to NIM/TokenRouter)
  → specialist fleet (qwen3.8-27b; PRUNED to 1 agent @ top_k=15 for
    single-domain questions — evidence-pool parity keeps the union ~11-15 chunks)
  → clause-level cross-check (pure Python contradiction detection)
  → CIO synthesis (gpt-oss-120b) with [n] inline citations
  → 5 deterministic zero-token gates (citation bounds, unit/scale,
    growth direction, XBRL reconciliation, echo guard)
  → grounding audit (gpt-oss-120b again — procedural maker-checker, SAME model
    family on the primary lane, cross-vendor on failover: Groq→NIM→Gemini)
  → certified answer + SHA-256 receipt chain (byte spans) + Ed25519 offline bundle
```

**What V1 gets right (non-negotiable, carried into V2 unchanged):**
- Construction-exact spans: `transcript[char_start:char_end] == chunk.text` by construction; receipts re-verify the sha-256 chain in 0 tokens.
- Fail-closed everything: ungrounded → bounded rewrite → verified refusal; refusals also leave receipts naming the audit objection.
- Epoch-partitioned cache + corpus; RLS multi-tenancy; provenance-threaded cache replays.
- 22-class failure ledger, 311 regression tests, nightly canary battery (capacity-vs-logic classified), 1,000-mutation fuzz + 17-vector tamper suites.

**What V1 structurally overspends:** every numeric question — the majority —
routes through LLM extraction when a deterministic lookup can answer it with
*stronger* provenance (the number is span-verbatim from the source by
construction, never in a model's mouth).

---

## 2. Target architecture (V2) — deterministic-first, three paths

One new table, one new routing tier, one audit refactor. The fleet, the gates,
and the receipt engine stay; a new fast path joins them, and all three paths
converge on the SAME verification engine.

```
                                  ┌────────────────────────────┐
        user query ──auth──cache──┤  INTENT ROUTER (one small  │
                                  │  structured call, 20B)     │
                                  └──────────┬─────────────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              ▼                              ▼                              ▼
   PATH A: FACT LOOKUP            PATH B: SEGMENTED            PATH C: QUALITATIVE
   (numeric, single or            (numeric w/ interpretive      (risks, strategy,
    comparative, in-coverage)      clauses; or A's fallback)      comparisons, narrative)
   ─────────────────────          ─────────────────────        ─────────────────────
   fact_store SQL lookup          V1 pipeline (pruned)          V1 multi-agent fleet
   template fill from spans       + SEGMENTED AUDIT:            (3 specialists → synthesis)
   0 extraction LLM calls           allow-list triage splits     + SEGMENTED AUDIT
   0-1 audit tokens                 claims: numeric-verbatim    (same as Path B)
                                    → Python-certified;
                                    interpretive clauses →
                                    small-context 120B audit
   ───────────────────────────────┴──────────────────────────────┴────────────────────
                                             │
                                             ▼
                          ┌──────────────────────────────────────────┐
                          │  SAME 5 GATES + SHA-256 RECEIPT CHAIN +   │
                          │  Ed25519 OFFLINE BUNDLE (path-agnostic)   │
                          └──────────────────────────────────────────┘
```

### 2.1 The fact store — span-anchored, NOT tag-anchored (deliberate choice)

New table `fact_rows`, populated **once at ingest** by pure Python over the
page transcripts (reusing the existing `_MONEY_RE`, scale-normalization,
COLUMN-KEY and TABLE-INTEGRITY machinery):

```
fact_rows:
  company · metric_key · period · value · unit · basis
  chunk_hash · char_start · char_end        ← THE ANCHOR
  source (filing) · corpus_epoch · extraction_confidence
```

**Why anchored to PDF spans and not to XBRL tags:**
- Our receipts prove custody to *source PDF bytes*. An XBRL-tag answer can't
  slice to transcript bytes, which would fork the verification engine into
  "strong proof" and "weaker proof" paths. Unacceptable.
- XBRL stays exactly where it is today: the **cross-verification gate** —
  every fact-row value is reconciled against `xbrl_facts` at ingest; rows that
  disagree are flagged, not silently stored.
- Misbinding — the failure class we've caught live 4 times — is attacked at
  *write time*: a fact row exists only with (entity, metric, period, basis,
  column-context) all bound. A lookup can then only return correctly-bound
  rows; it cannot invent a binding the way an LLM extraction can.

**Coverage honesty:** Path A serves only questions whose (metric, entity,
period) exists in `fact_rows`. Anything else — segment dimensions we don't
parse, unusual metrics, multi-company comparisons of *derived ratios* —
falls through to Path B. The fast path must know what it doesn't know.

### 2.2 Intent routing — one structured call, three outcomes

The existing `RouteDecision` schema gains one field (`path_hint: "fact" |
"fleet" | "auto"`). The router already classifies intent and prunes specialists
(today, default-on); this extends the same call — no new LLM cost. A
deterministic post-check can demote `fact` → `fleet` whenever the lookup comes
back empty or low-confidence.

### 2.3 Templated answers with real provenance

3–4 executive-grade templates (single metric, comparison, YoY with
% Change column). Slots are filled **from the cited spans**; every figure in
the output is byte-identical to its `fact_rows` span, so the citation-bounds
gate passes by construction and the receipt chain is identical to today's.

### 2.4 Segmented audit (applies to Paths B and C)

Allow-list triage per claim, **inverted** (route to the LLM by default):
- Python certifies a claim ONLY IF: every figure is span-verbatim AND zero
  hedge markers ("approximately/roughly/nearly/essentially/flat/about...")
  AND zero interpretive connectives ("due to/driven by/reflecting/because...").
- Anything else → one small-context audit call: flagged clauses + cited
  chunks only (~600–900 tokens vs 4–6K today).
- Audit receipts record **which claims were Python-certified vs
  LLM-certified** — the audit itself stays auditable.

### 2.5 What stays byte-identical

Five gates · receipt chain · Ed25519 bundles · `/verify` · fail-closed
refusals · RLS + epochs · cache (0.985, provenance-threaded) · canary ·
failover fabric (quota=cooldown, infra=circuit) · the entire test suite.

---

## 3. Rollout: measured, gated, reversible

| Phase | Action | Exit gate |
|---|---|---|
| 0 | Ship fact-store extractor + `fact_rows` at current epoch (223 chunks, 3 companies) | Extractor re-verifies every row against its own span + XBRL; ingest test green |
| 1 | **Shadow mode**: Path A answers built but NOT served; canary logs V1-vs-V2 disagreements per question | ≥14 consecutive canary nights with **zero disagreements** on Path-A-covered questions |
| 2 | Path A live behind `RAG_FACT_FASTPATH=1` for PATH-A-CLASS questions only | Canary recall ≥ V1 baseline; fabrication count still 0 |
| 3 | Segmented audit live on Paths B/C | Same shadow discipline: disagreement rate measured before the Python-bypass is trusted |
| Flag | `RAG_FACT_FASTPATH=0` restores V1 routing in one env var | — |

**The non-negotiable invariant across all phases: zero fabricated certified
answers — measured nightly, not assumed.**

---

## 4. Expected effect (projections, to be replaced by canary measurements)

| Metric | V1 today | V2 target |
|---|---|---|
| Numeric-question tokens | ~20K | **~1–3K** (router + template; 0 extraction, 0–1 audit) |
| Numeric-question latency | ~45–120s | **<5s** (SQL + template) |
| Qualitative-path audit cost | 4–6K/call | ~0.7K (segmented) |
| Groq 200K-day capacity | ~10 questions | **~60–100 questions** |
| Verification strength | byte-span receipts | unchanged — and stronger for numerics (lookup can't misbind if rows are bound at write) |

---

## 5. Risks we accept explicitly (the honest section)

1. **Template stiffness** — numeric answers read more formulaic. Accepted:
   deterministic surface = auditable surface. Budget for well-designed templates.
2. **Coverage-boundary bugs** — the demote-to-fleet rule must be conservative;
   a wrong coverage guess is a silent wrong answer. Mitigation: Phase-1 shadow
   measurements + canary recall bar.
3. **Combinatorial XBRL tag maps** — canonicalization (e.g. `Revenues` vs
   `RevenueFromContractWithCustomerExcludingAssessedTax`, period durations,
   fiscal-vs-calendar) is the real engineering. We start with the ~20 metrics
   our battery actually asks about.
4. **Interview optics** — "fewer LLMs" must not read as "weaker AI." The pitch
   is the verifier's path-agnosticism, not call-count reduction.
5. **Same-model maker-checker persists in V2** (procedural separation only).
   Model-diverse checking remains a one-line ADR amendment for later; this
   refactor does not touch it.

---

## 6. Reviewer questions (please answer each explicitly)

1. Do you agree **span-anchored fact rows** (PDF bytes + XBRL cross-check at
   ingest) beat **tag-anchored rows** (SQL-from-XBRL) for a system whose proof
   is byte-level receipts? Where would tag-anchoring actually be superior?
2. Is the allow-list triage (Python certifies only span-verbatim,
   hedge-free, connective-free claims; everything else → LLM) the right
   conservativeness boundary? What class of subtle misinterpretation, if any,
   could still slip a Python-certified claim?
3. Is 14 zero-disagreement canary nights a defensible Phase-1 exit gate, or
   would you demand a different measurement (e.g. per-claim disagreement rate,
   adversarial gold set specifically against the fast path)?
4. Any hole in the coverage-fallback design (fact → demote → fleet) that
   could produce a silently-wrong answer rather than a fallthrough?
5. Anything in this plan that a Tier-1 financial platform (Bloomberg/
   FactSet/Citadel-grade review) would reject, and what would they demand
   instead?

---

# APPENDIX A — EXTERNAL REVIEW SYNTHESIS (ADR-017, LOCKED 2026-09-13)

**Reviewer verdict received:** APPROVED WITH 4 MANDATORY AMENDMENTS.
**Build-agent synthesis:** all 4 amendments ACCEPTED (2 with corrections),
the Q3 measurement critique accepted in simplified form, and 2 reviewer
positions REJECTED with reasons. This appendix is the binding spec —
where it and Sections 1-6 disagree, this appendix wins.

## A.1 The Four Amendments — accepted, as they will be implemented

**Amendment 1 — Subject-noun semantic binding (ACCEPTED, generalized
from existing armor).** Every Path-A-certified claim must satisfy a
4th triage condition: the sentence's metric noun must resolve to the
canonical `metric_key` of the cited span. Implementation note: V1
ALREADY has this discipline for the 3 XBRL-metrics via
`_figure_metric_owner` (nearest-anchor figure ownership — Gemini's own
"operating income $40,111M" example is caught by it today and declined
to the LLM audit). The amendment's real work is generalizing this
binding to ALL ~20 fact_rows metrics at lookup time, not just the 3.

**Amendment 2 — Exact-match routing strictness (ACCEPTED as proposed).**
Path A is honored ONLY when the router's parsed (entity, metric,
period) triple maps to an exact canonical `fact_rows` key at high
confidence (≥0.95). Any ambiguity ("ad revenue" vs "total revenue")
demotes the ENTIRE query to Path B before any template is built.

**Amendment 3 — Atomic multi-entity demotion (ACCEPTED as proposed).**
Comparative queries require ALL N (entity, metric, period) triples in
fact_rows; any single miss demotes the whole query. No split-brain
answers mixing template and fleet output.

**Amendment 4 — Dual-key ingest reconciler (ACCEPTED, tolerance
corrected).** Every consolidated-metric fact row is reconciled against
`xbrl_facts` at ingest. Match = exact OR within 0.5% cross-scale
tolerance (the proven rounding-slack rule: $91.7B vs $91,650M is a
match; the reviewer's strict equality would false-flag legitimate
rounding — our own ledger documents this class). Disagreements are
flagged `unreconciled_fact` and blocked from Path A; only the fleet
may serve them, where the discrepancy surfaces through the
contradiction engine.

## A.2 Evaluation gate (Q3) — simplified instrument, same rigor

14 static-battery nights are replaced by:
- **≥40-question permuted battery** (expanding the existing 12-question
  gold set; new questions pre-registered before the phase starts).
- **15 corrupted-claim injections per phase** (swapped metric nouns,
  inverted growth direction, wrong periods — reusing the fuzz/tamper
  operator machinery). The triage must catch **15/15 across the
  phase**; any false-pass certification resets the clock.
- **≥98% agreement with the V1 pipeline** on the shadow stream.
- **Minimum 7 consecutive nights** satisfying all of the above.
- Zero fabricated certified answers — invariant, non-negotiable.

## A.3 Rejected reviewer positions (recorded with reasons)

1. **XBRL-primary ingestion ("PDF-primary is backwards"):** rejected —
   it contradicts the reviewer's own Q1 verdict that span-anchoring
   wins for our byte-level receipt moat. Our design (PDF-primary,
   XBRL reconcile-at-ingest) IS the reviewer's Amendment 4. The valid
   sub-point — dimensional hypercubes flatten badly in PDF parsing —
   is absorbed as a COVERAGE RULE, not an architecture change: segment
   data without clean single-table support is out of Path A coverage,
   and Amendment 3's atomic demotion routes those questions to the
   fleet.
2. **Bi-temporal point-in-time timestamps:** deferred to Version 3,
   not rejected on merit. For a 3-company current-epoch corpus it is
   scope expansion; the honest middle adopted NOW: fact_rows carries
   `filing_date` (valid time) and `ingested_at` (transaction time) as
   plain columns, and the epoch-partitioning already provides
   as-of-corpus semantics. Full bi-temporal query support (as-of-T
   replay across filings) is a Version 3 roadmap item.
3. **Template stiffness:** accepted as a design constraint — Path A
   templates render context-aware comparatives (prior-year value, the
   table's own verbatim % Change), not bare single-slot fill-ins.

## A.4 Phase gate updates

Phase 1's exit criterion is now the A.2 gate (replacing "14 nights of
the static battery"). Phase 0 additionally ships: the dual-key
reconciler, the exact-match router guard (A.1-2), and the
atomic-demotion rule (A.1-3) as regression tests BEFORE the extractor
goes live. The feature flag `RAG_FACT_FASTPATH` remains the single
kill-switch.

**STATUS: ADR-017 LOCKED — both reviewers in agreement. Execution
authorized for Phase 0: the span-anchored fact extractor + fact_rows
schema + dual-key reconciler.**

## A.5 Phase 0 tooling plan — the reuse ledger (locked with ADR-017)

**Principle (the ledger's first lesson):** every new dependency is a new
failure class. Institutional builders reuse canonical infrastructure and
add tools only when the reuse path is measurably inadequate. Phase 0
adds **zero new runtime dependencies** — one new pure-Python module and
one new table.

| Component | Tool | Status | Why this and not the alternative |
|---|---|---|---|
| Fact extraction | Pure Python over the EXISTING `page_transcripts` (reusing `_MONEY_RE`, scale/unit/basis normalization, COLUMN-KEY + TABLE-INTEGRITY machinery) | Reuse — new module `fact_extract.py`, ~0 new deps | A second extraction layer (camelot / pdfplumber / LlamaParse / ABBYY) would FORK provenance: receipts must anchor to exactly one extraction — the construction-exact one we already byte-verify on all 223 chunks |
| Canonical metric map | Extend the existing `scripts/xbrl.py` us-gaap concept map to the ~20 battery metrics | Reuse | It already maps XBRL tags → our metric keys; curated dictionaries ARE the institutional pattern (Bloomberg's ticker/security master is exactly this) |
| Fact storage | Existing PostgreSQL/Neon — new `fact_rows` table via the existing `schema_migrations` pattern | Reuse | Fundamentals are small relational data; Postgres is the institutional choice at this scale. kdb+/ClickHouse/DolphinDB solve tick-store workloads we don't have |
| Dual-key reconciler (A.1-4) | Port the `_gold_hit` cross-scale power-of-1000 matcher from `coverage_eval.py` | Reuse | The 0.5% rounding-slack rule is proven and regression-tested — no new numeric code |
| Intent routing (A.1-2) | The existing structured router call gains `path_hint`; the local ONNX bge-small embedder supplies the ≥0.95 confidence check | Reuse | No new model, no new API; the embedder is already pinned by the embedding-geometry test |
| Answer templates | Pure-Python f-strings in reviewed code | Deliberate no-tool | A template ENGINE (Jinja2 etc.) is a new dependency and a new audit surface; explicit code is auditable by the same reviewer process |
| Amendment tests | Existing pytest + the fuzz-operator pattern (new operators: metric-noun swap, period swap, entity swap, scale swap) | Reuse | The 1,000-mutation harness pattern extends to fact_rows directly |
| Shadow measurement (A.2) | The existing canary workflow + battery writer + fuzz/tamper machinery | Reuse | The instrument A.2 specifies already exists as deployed infrastructure |

**Explicit non-adoptions** — what Tier-1 platforms run at scale, and why
each is wrong for THIS system now:

- **kdb+/ClickHouse/Snowflake/Databricks** — tick-store and warehouse
  scale; 3 companies × ~20 metrics is thousands of rows. Postgres with
  RLS and epochs already does the job with zero new ops surface.
- **arelle / native-XBRL-first ingestion** — rejected in A.3.1; it
  contradicts the span-anchored receipt moat. Revisited only as a
  SECONDARY reconciler in V3.
- **Commercial/LLM table extractors** — provenance fork (above).
- **NLI models in the audit path** — removed by design in V2.
- **Redis (ADR-016)** — unchanged: trigger-gated on k6 load results,
  independent of this refactor.

**V3 revisit list (recorded, not committed):** warehouse migration IF
the corpus grows toward S&P-500 scale; arelle as secondary reconciler;
full bi-temporal query engine (A.3.2).
