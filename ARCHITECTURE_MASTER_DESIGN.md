# ARCHITECTURE_MASTER_DESIGN — The Complete Pipeline, Start to End

**Purpose:** the single document that explains every stage of the system —
what happens, where, why, what it costs, and how it fails. Written for the
maintainer and the senior interviewer. Grounded in the actual code; every
component names its file.

**Companions:** `ARCHITECTURE_V2_PROPOSAL.md` (ADR-017, the locked refactor
spec) · `KNOWN_ISSUES.md` (the 22-class failure ledger) ·
`ARCHITECTURE_DECISIONS.md` (ADRs 005–016) · `README.md` (the demo face).

---

## 0. The one-sentence thesis

> An AI system that answers financial questions about SEC filings, where
> **every number in every certified answer traces cryptographically to exact
> bytes of the source PDF**, and where the system **refuses rather than
> guesses** — enforced by deterministic gates first and a separate LLM audit
> second.

---

## 1. The two lifecycles

The system has two distinct loops. Almost all confusion about RAG systems
comes from mixing them; we keep them architecturally separate.

**A. INGESTION (offline, per filing, deterministic — zero LLM tokens)**
`ingest.py`, `db.py`, `scripts/xbrl.py`

**B. QUERY (online, per question, LLM-involved but bounded)**
`main.py` → `adaptive_rag.py` (LangGraph) → `db.py`

```
INGESTION                          QUERY
────────────                       ────────────
PDF → text+tables                  question → auth → cache → route
  → header-contextualized rows       → [Path A | B | C]
  → chunks with byte spans           → gates → synthesis → audit
  → page transcripts                 → receipt → certified answer
  → vector embed + index                or verified refusal
  → fact_rows (V2 Phase 0)
  → XBRL facts fetch (EDGAR)
  → corpus epoch bump
```

---

## 2. LIFECYCLE A — INGESTION (build the ground truth once)

### 2.1 PDF → header-contextualized text (`ingest.py`)
Raw filing PDFs are parsed so that **table rows never lose their column
context**: a row is serialized as `Segment=Products | Revenue=$67,184 |
Period=Q4 2023` — the semantics travel WITH the number, so a chunk boundary
can never sever "67,184" from "Products revenue, 2023 quarterly column."
Footnotes are co-located beneath their tables, never floated into prose.

### 2.2 Construction-exact character spans (the foundation of ALL proof)
Chunks are emitted while a running offset counter builds the page
transcript from the SAME pieces. The invariant, true by construction and
verified on every chunk (223/223):

```
transcript[char_start:char_end] == chunk.text    (byte-exact)
chunk_hash = sha256(company | source | page | transcript_slice)
```

There is no `find()` search step anywhere — spans cannot be ambiguous.

### 2.3 Vectors + transcripts + epoch (`db.py`)
Each chunk is embedded (local ONNX `bge-small-en-v1.5` — no API), stored in
`multi_agent_chunks` with pgvector HNSW + trigram keyword indexes (hybrid
RRF later), and the page transcript is persisted with its chunk→span map.
Re-ingestion bumps `corpus_state.epoch` — every cache read filters on the
current epoch, so stale answers become invisible instantly, no deletes.

### 2.4 The XBRL ground-truth fetch (`scripts/xbrl.py`)
SEC EDGAR's machine-readable facts are fetched (fair-access pacing).
Fiscal Q4 never appears as a primary XBRL fact (companies file 10-Qs for
Q1–Q3 and a 10-K for the full year), so it is **derived**:
`Q4 = Full-Year(10-K) − 9-Month(10-Q)`, stored in `xbrl_facts` with the
payload's SHA-256 for lineage. These are the *official* numbers our Gate 4
will reconcile drafts against.

### 2.5 V2 Phase 0 — `fact_rows` (the deterministic fact store)
Pure Python sweeps the page transcripts ONCE, reusing the money/scale/unit
regexes and COLUMN-KEY machinery, producing:

```
company · metric_key · period · value · unit · basis
chunk_hash · char_start · char_end      ← span anchor (receipt-grade)
filing_date · ingested_at                ← A.3.2 compromise columns
reconciled flag                          ← Amendment 4: value must match
                                           xbrl_facts exactly OR within
                                           0.5% cross-scale tolerance,
                                           else 'unreconciled_fact',
                                           blocked from Path A forever
```

Rows are only written when (entity, metric, period, basis, column) are all
bound — **misbinding is attacked at write time**, so a later lookup cannot
invent a binding the way an LLM extraction can.

---

## 3. LIFECYCLE B — QUERY: the twelve stages

Follow one real question through:
**"What were Apple's Products revenue versus Services revenue in Q4 2023?"**

### STAGE 1 — Gateway & security (`main.py`) · 0 tokens · ~ms
- `X-API-Key` auth → tenant identity (401 missing, 403 wrong tenant/
  wrong key). Open mode if `QUERY_API_KEYS` unset.
- Per-IP token-bucket rate limit (15/min).
- Request-ID == LangGraph run_id, threaded through every JSON log line.

### STAGE 2 — Semantic cache (`check_cache_node` → `db.py`) · 0 tokens · ~3s
- pgvector cosine, **near-exact only** (0.985): identical re-asks hit
  (distance ~0); paraphrases and *any* different question miss — a cached
  answer can never be replayed for a question it doesn't answer.
- Exact on: tenant, filters, embed model, **current corpus epoch**.
- A hit returns the answer + the ORIGINAL `provenance_run_id` — the replay
  never mints fake proof; "Prove it" on a cached answer resolves to the
  run that earned the certification.
- Provenance-less legacy entries are **evicted**, not served — an
  unverifiable certified answer is a contradiction we refuse to ship.
- *Our question:* miss (first ask). Cache hit would end the pipeline here.

### STAGE 3 — Premise fast-path (`premise_fast_path`) · 0 tokens · <1s
Company-scoped SQL probe: does this corpus have ANY retrieval support for
this metric for this company? ("Tesla's dividend" → zero hits → clean
refusal in <1s instead of a doomed 142s pipeline run.) Multi-company
questions never fast-path (the other company might support them).

### STAGE 4 — Intent router (`route_question`) · gpt-oss-20b · 1 call · ~1s
One structured `RouteDecision` (with the `_repairing_structured` JSON
repair backstop): `vectorstore | general_knowledge | out_of_domain`,
plus `active_specialists` (pruning) and — **V2** — `path_hint`.
- Fail-closed: router unavailable or returns garbage → refusal, never crash
  (the structured-failover lesson).
- **V2 routing guard (Amendment 2):** `path_hint="fact"` is honored ONLY if
  (entity, metric, period) maps to an exact canonical `fact_rows` key at
  ≥0.95 confidence. Any ambiguity demotes the whole query to the fleet.

*Our question:* `vectorstore`, financial-only → the branching point.

### STAGE 5 — THE THREE PATHS (V2; today only Path B/C exist)

**PATH A — Fact lookup (V2 Phase 2+, deterministic) · 0 LLM calls · <3s**
Numeric single/comparative questions whose triples ALL exist in fact_rows:
- **Amendment 3 — atomic demotion:** a comparison needs every (entity,
  metric, period) present; ONE miss demotes the ENTIRE query to the fleet.
  No split-brain answers.
- Template fill FROM THE SPANS: every figure in the output is
  byte-identical to its `fact_rows` span (prior-year value + the table's
  own verbatim % Change included — not bare Mad-Libs).
- **Amendment 1 — subject-noun binding:** the template's metric noun must
  resolve to the span's canonical `metric_key` (generalized
  `_figure_metric_owner` armor, now covering all ~20 metrics).
- Citations and receipt chain are IDENTICAL to fleet answers — the
  verifier cannot tell which path produced the answer (that is the point).

**PATH B — Pruned fleet (today, for single-domain questions)**
`execute_specialist_fleet` · qwen3.8-27b · 1 call (pruning default-on)
- One specialist (financial for us), retrieval fan-out via conditional
  multi-query expansion (only when first-hit confidence < 0.55; RRF fusion
  across paraphrases otherwise skipped — 94% fewer expansion calls).
- **Evidence-pool parity:** the lone specialist retrieves at top_k≈15 so
  the evidence union stays ~11–15 chunks — pruning shrinks CALLS, never
  evidence (the lesson the refusal receipts taught us).

**PATH C — Full fleet (cross-domain / qualitative questions)**
All three specialists in parallel (`asyncio.gather` — wall-clock is the
slowest, not the sum), each with per-company sub-retrieval on comparisons
so one company's chunks can't crowd out the other's. Each specialist
returns quarantine-aware reports: a degraded agent is quarantined and
named, never silently folded in.

*Our question:* today → Path B. Post-V2 → Path A (both triples exist).

### STAGE 6 — Clause-level cross-check (`cross_check_specialists`) · 0 tokens
Pure-Python contradiction detection over all specialist reports: metric
mentions grouped by (family, company, period, unit, basis — unit-space
separation: GAAP vs non-GAAP, per-share vs absolute, never crossed).
Rounding slack ($91.7B vs $91,650M) is accepted as rounding, not conflict.
A first-time conflict triggers ONE bounded re-retrieval ("sharpen"); if it
survives, the conflict is INJECTED into synthesis as a SOURCE CONSISTENCY
ALERT — **the system surfaces conflicts, never averages them.**

### STAGE 7 — Executive synthesis — THE MAKER (`synthesize_csuite_report`)
· gpt-oss-120b · 1 call · ~4–6K in / ~1.5K out
The CIO brief with `[n]` inline citations, under the 9-rule mandate
(one company per sentence · no shown arithmetic · explicit year tokens ·
quote the source's units · percentages verbatim from the table's
% Change column · **approximation is fabrication — quote verbatim or cite
nothing**). Evidence dedup: chunks already quoted verbatim in specialist
reports collapse to citation-preserving stubs — same grounding surface,
~40% less prompt.

### STAGE 8 — The 5 deterministic gates · 0 tokens · ms
Before any LLM audit runs, Python rejects:
1. **Citation bounds** — every `[n]` inside the evidence set; spans strictly
   slice the transcript.
2. **Unit & scale** — figures reconstructed from table units ($M vs $B,
   thousand/million/billion suffixes normalized at extraction); a "$500M
   claim over a $500B fact" dies here.
3. **Growth direction** — "grew/fell" validated against the comparative
   table columns actually cited.
4. **XBRL reconciliation** — consolidated claims reconciled against SEC
   ground truth with figure-level metric ownership (`_figure_metric_owner`:
   each figure judged ONLY against its nearest metric anchor — the
   anti-metric-noun-swap armor) and per-sentence company attribution.
5. **Echo/injection guard** — prompt-echo, leaked deliberation, and
   injection markers rejected pre-audit.

A rejection here loops back to a bounded rewrite (max_retries=2) — and
**abort-on-hint** skips doomed re-runs entirely when a 429 has already
measured the quota window shut.

### STAGE 9 — Grounding audit — THE CHECKER (`fact_checker_guard`)
· gpt-oss-120b · 1 call · ~4–6K in / small out
Procedural maker-checker: same model family on the primary lane, separate
call, separate `GroundingCheck` contract, adversarial cross-examination of
the draft against the raw cited chunks. On quota: peer failover
Groq-120B → NIM-120B → Gemini (sub-100B peers structurally barred from
certifying). **Fail-closed:** any unsupported claim → QUARANTINE → bounded
rewrite → refusal. An audit failure defaults UNGROUNDED, never "pass."
**V2 segmented audit:** span-verbatim, hedge-free, connective-free claims
are Python-certified (0 tokens); only interpretive clauses get the small-
context 120B call (~0.7K) — and the receipt records which claims were
certified by which.

### STAGE 10 — Receipt & cache write · 0 tokens
On certification: atomic claims + evidence spans + the full claim→chunk→
transcript→SHA-256→PDF-anchor chain persist to `verification_receipts`
(tenant-scoped, RLS); the answer enters the semantic cache carrying
`provenance_run_id`. **On refusal: a `verdict='refused'` receipt persists
too, naming the audit objection** — every run leaves a forensic trail.

### STAGE 11 — Verification (the moat, per answer) · 0 tokens
- Online: `GET /verify/{run_id}` recomputes the hash chain and re-slices
  transcripts at byte coordinates — 15/15 links, zero LLM tokens.
- Offline: `GET /export/{run_id}` → Ed25519-signed bundle; a regulator on
  an air-gapped laptop runs `verify_certificate.py` (stdlib only) and
  re-proves the answer in ~0.05s without trusting our server at all.

### STAGE 12 — The failure boundaries (fail-closed everywhere)
| Failure | Behavior |
|---|---|
| Quota wall (429) | cooldown layer owns it; abort-on-hint kills doomed re-runs; refusals receipt the objection |
| Infra failure (5xx/timeout) | circuit breaker (5 failures → open) |
| DB unreachable | bounded-wait pool → treated as retrieval failure → fail-closed |
| Router/synthesis/audit failure | UNGROUNDED default; peer rescue; else refusal |
| Every path's terminal state | certified+receipted, or refused+receipted — **never a guess** |

---

## 4. The cross-cutting provider fabric (ADR-008/012)

```
ROUTER + FLEET (extractive, failover-eligible):
  Groq (20B/27B) → NIM nemotron-120b → TokenRouter GLM-5.3
  (backups re-bound to the SAME structured schema — the crash fix)

EXECUTIVE SYNTHESIS + AUDIT (certifying — pinned):
  Groq gpt-oss-120b → (429) → NIM 120b → Gemini 3.5-flash
  (sub-100B structurally barred from certifying)

Quota 429s  → per-endpoint cooldowns (parsed retry hints, e.g. 645s)
Non-quota   → global circuit breaker (5 failures)
Timeouts    → brief type-based cooldown, then failover
```

---

## 5. The complete cost map (measured, 2026-09-13)

| Path | LLM calls | Tokens | Latency | Audit |
|---|---|---|---|---|
| Cache replay | 0 | **0** | ~3s | receipt resolves to original run |
| **V2 Path A** (fact) | 1 (router only) | **~1.5–3K** | **<5s** | deterministic (Amendments 1–4) |
| Path B (pruned fleet) | 4–5 | ~20K | ~45–60s | 120B (segmented post-V2: ~0.7K) |
| Path C (full fleet) | 5–7 | ~25K+ | ~60–120s | same |
| Refusal (any) | — | burned budget receipted | — | objection named |

**The invariant across all rows: zero fabricated certified answers —
measured nightly by the canary, not assumed.**

---

## 6. Reading order for an interviewer

1. The thesis (§0) → the invariant (§5, last line).
2. "Show me where a hallucination would die" → Stages 8→9, in order,
   with the 22-class ledger as receipts that each gate earned its place.
3. "What happens when you're wrong?" → Stage 12 + refused receipts.
4. "What happens under quota pressure?" → §4 + abort-on-hint.
5. "Prove an answer without your server" → Stage 11, air-gapped bundle.
6. "Why is the LLM even needed?" → Stages 7–9 own interpretation; numbers
   are looked up (Path A) or extracted (Paths B/C) — and ALWAYS verified
   against bytes by a checker that is structurally separate from the maker.
