# V3 ROADMAP — Deterministic Depth over Infrastructure Width

**Author:** IX Alpha (build agent), synthesizing the fourth external review
(Claude, CRO-at-a-$50B-quant-fund lens, 2026-09-14)
**Status:** DOCUMENT OF RECORD for the V3 horizon. Supersedes the "V3
revisit list" sketch in ADR-017 §A.5 and extends B.6.3.
**Entry condition:** V2 Phases 1-2 (shadow mode + Path A live) have
passed the A.2 gate. Nothing here starts before that.

---

## 0. The review's shape (and why it was good)

The CRO lens evaluated our three V3 horizon ideas — Figure-DAG,
cross-filing corroboration, bi-temporal infra — and attacked each at
its weakest joint. All three critiques were accepted in substance; two
were corrected in remedy. The pattern of the corrections is consistent:
**Claude kept proposing LLM-shaped fixes; this system's moat is that
the critical path is LLM-free.** Every disposition below keeps the
deterministic layer first and confines the LLM to the residue the
deterministic layer cannot resolve — the same inversion that defines
V2 (§2.4 segmented audit), applied to V3 scope.

---

## 1. Symbolic Figure-DAG — "The Calculator Fallacy"

**The critique (Claude, recorded):** proving the division is the easy
part; the danger is OPERAND MISBINDING — dividing GAAP operating income
by non-GAAP revenue, or Q4 by FY, and certifying the ratio because the
arithmetic is correct. A DAG that certifies math without certifying
BINDING is a machine for laundering wrong answers into trusted ones.
Demands: strict Basis Compatibility checks on every edge, and Lineage
Taint Propagation — derived values inherit the worst warning flag of
their inputs.

**Builder disposition: ACCEPTED in full — and Phase 0 already proved
the point three times.** Operand misbinding is not hypothetical here;
it is the live failure class of the Q4-2023 corpus alone:

- Tesla's cash-flow "Net income" (7,943, NCI-inclusive) sits within
  the 0.5% reconciliation tolerance of the attributable XBRL figure
  (7,928) — a basis-class misbinding that would have FALSELY
  reconciled if the page had been admitted (closed by the ALL-CAPS
  section-marker eligibility rule).
- The chunk-boundary bleed: a trailing row label + the next chunk's
  re-stated values emitted correctly-parsed numbers bound to the wrong
  row (closed by the row-completes-within-one-chunk rule).
- Tesla prints GAAP and non-GAAP net income on the same page, five
  values apart (4,106 vs 3,687) — an adjacent-operand trap any DAG
  consumer must survive.

**Corrections to the remedy (both strengthen it):**

1. **Basis compatibility is a generalization of an existing gate, not
   new machinery.** Gate 4 already declines to judge non-GAAP sentences
   against GAAP facts (`_NON_GAAP_RE`), and Phase 0 stores `basis=GAAP`
   per fact row. The DAG's basis check is that same rule applied to
   edges: an operand edge is legal only when `basis`, `period` (no
   Q4/FY mixing — the "full-year decline" rule already exists), and
   `company` all match. Nothing new to invent; wire the existing
   predicates into the edge validator.
2. **Taint propagation is a two-line extension of `verifier_class`.**
   A derived node's class = the WORST of its operands'
   (`unreconciled_fact` poisons everything downstream; only
   `span_xbrl_reconciled` operands can produce a reconciled
   derivative). The column already exists on every fact row.

**Scope ruling (the builder's addendum Claude did not make): the DAG
ships as a CHECKER, never a GENERATOR.** It never computes and serves
a derived answer; it only verifies that a claim's arithmetic
reconstructs from a basis-compatible, untainted operand set — and
declines to judge when it cannot. This is the same posture as all five
V1 gates: the machinery judges, it does not author. A V3 ADR will
make this ruling binding before any DAG code lands.

**V3 item 1 — Figure-DAG (checker-only):** pure Python over
`fact_rows`; edge validation = basis/period/company compatibility;
taint = worst-of-operands `verifier_class`; output is
certify/decline, never a served number.

---

## 2. Cross-Filing Temporal Corroboration — "Alert Fatigue"

**The critique (Claude, recorded):** flagging every YoY numeric diff
between the prior-year comparative column and the original filing
generates massive alert noise, because legitimate differences are
constant in real filings — segment reclassifications (ASU 2023-07),
discontinued operations (ASC 205-20), five-year plan re-bases. A pure
math diff is the wrong instrument; scope it as a disclosure
classifier: check whether the diff was LEGALLY EXPLAINED in the
footnotes (or an Item 4.02 8-K non-reliance filing) before surfacing
it.

**Builder disposition: the fatigue concern is ACCEPTED and is the
strongest of the three critiques; the remedy is REFRAMED for this
architecture.** Two corrections:

1. **In this system, "alerts" are not read by humans — they are
   verifier-class downgrades consumed by the routing fabric.** An
   over-flagged fact becomes `unreconciled_fact` → the query demotes
   to the fleet (Amendment 2). So the true cost of false positives is
   COVERAGE SHRINKAGE on Path A, not operator fatigue. That changes
   the engineering target: precision-first is still right (a false
   flag burns the fast path's value proposition), but the metric to
   hold ourselves to is the Path-A coverage rate at zero missed
   restatements — measured nightly, like every other gate.
2. **An LLM classifier in the critical path is the one shape we must
   not build — and we don't need to, because Phase 0 already shipped
   half the instrument.** The B.1.3 context-marker machinery detects
   "as previously reported / restated / pro-forma" VERBATIM in filing
   text — a deterministic explanation-presence check. The V3 pipeline
   is tiered, Claude's classifier demoted to the final tier:

   ```
   Tier 1 (pure Python): the numeric diff itself — prior-year
     comparative column vs the original filing's own figure,
     both already span-anchored in fact_rows.
   Tier 2 (pure Python): explanation-marker presence in the NEWER
     filing's text — restatement / reclassification / discontinued-
     operations / pro-forma markers (B.1.3 vocabulary, extended).
     Explained diffs close here: flagged `restated_explained`,
     served by the fleet only, never silently served by Path A.
   Tier 3 (one small-context LLM call, the segmented-audit pattern):
     ONLY the unexplained residue — "is there an ASC 205-20 / ASU
     2023-07 / Item 4.02 disclosure anywhere in this filing that
     Tier 2's vocabulary missed?" Output recorded on the row as
     `llm_classified`; never a silent pass. Classifier failures
     fail CLOSED (unexplained).
   ```

   Item 4.02 8-Ks themselves are a free tier-2 signal: they carry the
   phrase "should no longer be relied upon" — a deterministic marker,
   no model required.

**Hard data dependency, stated honestly: this item requires a
multi-year corpus.** The current corpus is one filing per company
(Q4-2023). Cross-filing corroboration is ranked behind corpus
expansion (V3 item 4) for exactly this reason — B.6.3's ranking
("ahead of segment-dimension XBRL") survives, but its *precondition*
is two-plus years of filings per company.

**V3 item 2 — tiered cross-filing diff:** Python diff → marker
presence → LLM classifier on residue only; classifier fails closed;
coverage rate measured against the fatique bound.

---

## 3. Bi-Temporal Infrastructure — "The Time Travel Paradox"

**The critique (Claude, recorded):** bi-temporal is the industry
standard, but timestamping the database alone does not deliver
point-in-time truth: the REASONING MODEL has lookahead bias — a 2026
model querying 2023 data "knows" what happened in 2024 and will leak
it into synthesis and audit. True bi-temporal therefore requires
version-locking the exact LLM weights and prompts — and since that is
impractical with hosted providers, defer the whole item as premature
optimization now.

**Builder disposition: the lookahead-bias mechanism is ACCEPTED and
is the deepest observation in the review; the deferral is ACCEPTED
with one carve-out and one correction.**

1. **The critique is half-true for THIS architecture, and the half it
   misses is the moat.** Lookahead bias afflicts the LLM paths —
   fleet, synthesis, audit — which no amount of DB timestamping can
   cure. It does NOT afflict Path A's deterministic layer: a SQL
   lookup over epoch-stamped `fact_rows` plus a template fill has no
   weights, no training cutoff, no subconscious knowledge. A
   bi-temporal `fact_rows` query is lookahead-free BY CONSTRUCTION.
   So the deferral applies to the full engine, but the deterministic
   layer may adopt bi-temporal semantics whenever multi-year corpus
   arrives, at zero risk of the paradox.
2. **Version-locking hosted-model weights is not "hard" — it is
   impossible, and the honest middle is attribution, not freezing.**
   Providers rotate weights under fixed model IDs; no API contract
   pins them. What we CAN do, cheaply, is record lineage per receipt:
   (model id, prompt hash, corpus epoch, verifier class per claim).
   Any answer becomes attributable — "this claim was produced by
   model M under prompt P against corpus epoch E" — which is the
   same discipline as B.1.4's per-claim verifier stamp, extended one
   field. **This ships in Phase 1 (shadow mode), not V3** — it is
   required for the shadow study to be meaningful at all (V1-vs-V2
   disagreement forensics need to name the models that disagreed).
3. **The claim-tier honesty rule that follows (binding):** docs and
   receipts must never claim point-in-time purity for LLM-certified
   claims. Receipts for the deterministic layer may claim as-of
   replay; receipts for the fleet/audit layer claim attribution
   only. A compliance reader is told exactly which guarantee each
   claim carries — the B.1.6 tiering discipline, applied to time.

**V3 item 3 — bi-temporal, deterministic-layer-first:** as-of queries
over `fact_rows` when multi-year corpus lands (filing_date valid-time
+ ingested_at transaction-time columns already exist from Phase 0);
LLM-layer as-of replay is declared OUT OF SCOPE honestly rather than
approximated.

---

## 4. The sequenced V3 roadmap

Ordered by dependency, not by glamour. Every item keeps the V2
invariant: zero fabricated certified answers, measured nightly.

| # | Item | Type | Depends on | Deterministic core | LLM role |
|---|---|---|---|---|---|
| 0 | **Receipt lineage** — (model id, prompt hash, epoch) per receipt + per-claim verifier | V2 Phase 1 addition | nothing | stamp at write time | none (records the fleet, doesn't judge) |
| 1 | **Figure-DAG, checker-only** — basis/period/company edge validation, worst-of-operands taint | V3 | fact_rows (shipped) | the entire item | none |
| 2 | **Corpus expansion** — 2+ years of filings per company | data acquisition | infra budget | ingest + epoch machinery (exists) | none |
| 3 | **Tiered cross-filing diff** — Python diff → marker presence → LLM classifier on residue, fail-closed | V3 | item 2 | tiers 1-2 | tier 3 residue only |
| 4 | **Bi-temporal deterministic layer** — as-of fact_rows queries; LLM layer declared attribution-only | V3 | item 2 | the entire claim | none |
| 5 | **Segment-dimension XBRL** — dimensional facts ingested; Products/Services etc. gain XBRL twins | V3 | item 2 + XBRL map curation | reconcile-at-ingest | none |
| 6 | **Warehouse migration** (kdb+/ClickHouse-class) — ONLY if corpus grows toward S&P-500 scale | conditional | corpus scale trigger | — | — |
| 7 | **arelle as SECONDARY reconciler** — never primary (A.3.1 ruling stands: PDF-primary) | V3 | item 5 | cross-check only | none |

**Explicitly rejected for V3 (recorded with reasons, as ever):**

- **LLM classifiers in the critical path** — the CRO's instinct for a
  classifier is right; its placement is wrong for a system whose moat
  is that the critical path is LLM-free. Tier-3 residue only.
- **DAG-generated derived answers** — checker-only, per §1. A DAG
  that authors numbers re-creates V1's extraction risk with better
  arithmetic.
- **Model-weight version-locking claims** — impossible with hosted
  providers; we record attribution instead and say so plainly.
- **Full bi-temporal LLM replay** — out of scope, stated honestly
  (§3.3), not approximated with weasel wording.

---

## 5. What this roadmap does NOT change

Phase 1 and Phase 2 of V2 are untouched: the A.2 gate (permuted
battery, corrupted-claim injections, ≥98% agreement, 7 green nights),
the `RAG_FACT_FASTPATH` kill-switch, and the zero-fabrication
invariant all stand exactly as locked in ADR-017. V3 is the horizon
AFTER the fast path is live and trusted — the CRO review shapes that
horizon; it does not open it early.

**Review disposition summary: 3/3 critiques accepted in substance;
2 remedies corrected (classifier → tiered with LLM last; weight-
freezing → attribution stamps); 0 rejections. The architecture is
capped.**
