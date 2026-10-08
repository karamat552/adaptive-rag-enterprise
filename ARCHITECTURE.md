# ARCHITECTURE — How Adaptive RAG Actually Works (plain language)

Written for the owner: what each stage is, why it exists, and — honestly —
where it is weak today. Everything below reflects the system as measured in
October 2026, including the B4 findings and the 2026-10-07 reading pass over
every file (ingest.py, segmented_audit.py, the audit tool, the offline
verifier, the client). Last updated: 2026-10-08.

## The one-sentence version

A user's financial question is answered by LLMs that may only speak from
retrieved SEC-filing text; five deterministic gates and one LLM auditor check
the answer against that text before it is served; every answer (and every
refusal) is stored as a cryptographic receipt that can be re-verified offline
by anyone.

## Lifecycle A — Ingestion (offline, deterministic, ZERO LLM tokens)

The system has TWO loops and keeping them separate is most of the design:
ingestion builds the ground truth ONCE; querying spends tokens per question.

**1. Download (`ingest.py`).** Resilient fetch of the three SEC-hosted PDFs:
atomic publish (a `.part` file is `os.replace()`d into place — a failed run
can never destroy a previously-good corpus), a magic-byte PDF check, size
caps, jittered exponential backoff on transient errors, fail-fast on
permanent ones (404/403/non-PDF). Idempotent re-runs reuse the verified local
copy and still fingerprint it (SHA-256).

**2. Layout-aware parsing.** Text blocks are sorted into (top-to-bottom,
left-to-right) reading order; headings are detected BEFORE chunk-splitting so
every chunk provably belongs to the section it appeared under (titles carry
forward across pages). Tables get their own extraction: two layouts by
structure — headed tables render every row as
`Label :: Column=value | ...` (the column semantics travel WITH the number;
a chunk boundary can never sever them), balance-sheet-style tables stay
grid-only (fabricating column names would be worse than none). Footnotes from
the same page are appended beneath their tables — a figure modified by a
footnote is never retrievable without that footnote nearby. An additive
arithmetic check runs per table chunk: a Total row that disagrees with the
sum of its members is FLAGGED (`arithmetic_ok=False`), surfaced to the fleet
and `/verify` — never silently dropped, and never proof of a parse error
either (legit non-additive totals exist).

**3. The transcript spine (the foundation of ALL proof).** The page
transcript is built FROM the emitted chunks themselves (joined `"\n\n"`),
while a running offset counter records each chunk's span. The invariant,
true BY CONSTRUCTION and verified on every chunk:

```
transcript[char_start:char_end] == chunk.text    (byte-exact)
chunk_hash = sha256(company ⊣ source ⊣ page ⊣ slice)   (unit separator prevents collisions)
```

There is no `find()`-search anywhere — a span cannot be ambiguous or drift.
This is why the span scans come back clean: the spans are exact because
nothing ever had to locate them after the fact.

**4. Embed + persist + epoch (`db.py`).** Each chunk is embedded with a LOCAL
ONNX model (bge-small-en-v1.5 — no API), stored with pgvector HNSW +
trigram indexes (hybrid RRF at query time), transcripts persisted per epoch.
Any corpus change bumps `corpus_state.epoch` — stale cache answers become
invisible instantly, no deletes.

**5. The fact store (ADR-017) + the XBRL ground truth.** A pure-Python sweep
of the transcripts extracts `company · metric · period · value · unit · basis`
rows, each span-anchored — misbinding is attacked AT WRITE TIME, so a later
lookup cannot invent a binding the way an LLM extraction can. SEC's
machine-readable facts are fetched (fair access: declared User-Agent, ≤10
req/s, jittered pacing); fiscal Q4 never appears as a primary XBRL fact, so
it is DERIVED (`Q4 = Full-Year − 9-Month`), lineage-hashed. A row reconciles
only when both sources agree within the 0.5% rounding-slack tolerance;
unreconciled rows stay in the store for retrieval but can never anchor an
answer. Today: 16 XBRL facts, 15 reconciled triples (the 2026-10-07
expansion added gross margin / operating income / R&D).

**6. The failure-mode battery (`tests/test_ingest_failure_modes.py`,
added 2026-10-08).** The ingestion engine's safety nets are exercised
deliberately, not assumed: a non-PDF URL and a 404 fail fast with the
`.part` cleaned, a 429 retries, a mid-download drop cleans the `.part`
(a partial file can never reach the corpus), an oversize row stays
whole, a non-additive table total is flagged, and a dead run never
clobbers the committed corpus. The transcript spine is verified
byte-exact on all 223 chunks of the live corpus in the same battery.

## Lifecycle B — Query (the stages below)

## Stage by stage

**1. Gateway (`main.py`, FastAPI, one worker).** Auth (API keys bound to
tenants; anonymous callers are scoped to the `default` tenant only — the
spoofing hole that let an anonymous caller declare any tenant was closed
2026-10-01), rate limits, request IDs, and the five read endpoints. Every DB
call goes through a worker thread so the event loop stays responsive
(measured: `/health` p50 during a 137 s pipeline run was 3,250 ms vs
3,234 ms idle — the loop does not block; `/health`'s own cost is a database
round trip).

**2. Semantic cache.** A new question is embedded and compared with past
questions in the same tenant; a near-exact match replays the previous
certified answer (zero LLM calls, ~2 s) with a pointer to the original
receipt. The cache is epoch-guarded: any corpus change invalidates it
instantly. A replay whose stored answer lacks provenance is deleted
(self-heal) rather than trusted.

**3. FastPath (`fact_fastpath`).** For questions whose (company, metric,
period) exists in the fact store — rows extracted from PDF spans AND
reconciled against SEC's structured XBRL data (dual-key) — a deterministic
answer is served with zero LLM calls (~6-10 s, mostly embedding). **The
flag is ON in production** (owner-set `RAG_FACT_FASTPATH=1` in the Render
env, verified via the Render dashboard 2026-10-02); the code default is
OFF and the check is exact-match (`!= "1"` — a stray space or trailing
newline silently disables it), and the nightly runners never set it (the
shadow battery must measure V1). Today the fact store covers **15
reconciled triples** across three companies — the 2026-10-07 coverage
expansion added gross_margin / operating_income / rd_expense via SEC
companyfacts (16 facts, was 8) — all span-verified with zero mismatches;
segment revenues stay fleet-class by design (dimensional facts the
companyfacts API does not expose).

**4. Router.** A small LLM classifies the question (financial-comparison /
risk / product / out-of-domain). Out-of-domain questions never reach the
expensive machinery.

**5. Premise probe.** Before burning the fleet, one cheap retrieval over the
question's metric terms: zero hits means the corpus has no support and we
refuse immediately (~seconds instead of minutes).

**6. Specialist fleet.** Up to three specialists (financial / risk / product)
— each retrieves its own evidence (hybrid vector + keyword search, RRF-fused),
then drafts with mandatory [n]-citations. Pruning runs only the specialist
the router selected by default; all three for comparison questions. Each
specialist binds to the caller's tenant (fixed 2026-10-02: retrieval
previously bound the default tenant regardless of caller).

**7. Cross-check.** Deterministic, zero tokens: extracts metric mentions from
all specialist drafts and flags groups where the same (metric, company,
period, unit, basis) carries disagreeing figures. Conflicts are surfaced, never
averaged. (Known weakness: no segment/consolidated scope axis — a segment
figure and the consolidated figure for the same metric read as a
contradiction; B2's design adds the axis.)

**8. Synthesis.** The executive model writes the final report from the
specialist drafts, quoting figures verbatim with [n] citations. If the
executive lane fails, a bounded peer-rescue lane may retry (the 245 s
quota-abort lesson).

**9. The five deterministic gates (zero tokens, pre-audit).**
- *Citation bounds*: every [n] must index a real evidence slot.
- *Echo/injection guard*: drafts restating their own instructions or
  leaking deliberation are quarantined before the audit.
- *Unit-scale gate*: money figures must be reconstructable from the units
  the cited chunk declares.
- *Growth-direction gate*: "grew/declined N%" claims must agree in direction
  with the cited evidence's own comparative pair.
- *XBRL gate*: figures for covered (company, metric, period) must match the
  SEC-published structured facts.
Any gate FLAG rejects the run (fail-closed) — it refuses rather than serves.

**10. LLM auditor.** The final semantic check: the draft plus its evidence
goes to the executive model, which must ground every claim. Unavailable
auditor (quota, timeout) = refusal, never a pass. There is also a BUILT
segmented-audit mode (`RAG_SEGMENTED_AUDIT=1`, **deliberately dark**): the
triage splits the draft's claims — span-verbatim, hedge-free,
connective-free claims are Python-certified at zero tokens; only the
interpretive clauses get a small-context executive call — and receipts
record which guarantee backs each sentence. It stays dark until its own
disagreement-rate measurement exists (the code's own warning: "do not
enable mid-A.2-gate"). (Measured weaknesses — see below.)

**11. Receipts.** Every grounded answer and every refusal stores a receipt:
question, answer, per-claim verifier stamps, evidence spans with hashes,
page-transcript anchors, corpus epoch, model lineage — plus a SHA-256 chain
and an offline bundle (`/export`), re-verifiable by an
auditor with zero access to this system.

**12. Tenancy.** Postgres row-level security isolates each tenant's corpus,
cache, receipts and facts; the runtime role (`app_rag`) cannot bypass RLS,
and the admin identity is separate. Private uploads (per-tenant epochs,
migration 008) are designed but PAUSED by owner decision.

## Known weaknesses (measured, October 2026 — do not paper over these)

0. **FIXED 2026-10-07 — the token story, corrected:** covered questions
   cost ZERO (the FastPath); a healthy uncovered question costs ~17-21K
   tokens / 10-13 LLM calls; the 178K/49.7-call monster was an UNBOUNDED
   retry loop — three full pipeline passes per unverified outcome, ending
   in the same refusal (the retry dig, scripts/token_dig.py). **The retry
   is now DEAD** (unverified -> refuse; the dig proved it never converted)
   and a per-run call budget (`RAG_MAX_LLM_CALLS_PER_RUN=24`, peer calls
   tracked — the blind spot fixed) bounds everything else. See
   TOKEN_COST_DIAGNOSIS.md.

1. **The scale gate almost never engages on the real corpus** — the units
   declaration ("(In millions…)") is chunked separately from the figures it
   describes, so the gate abstained on 37/37 real-evidence drafts (B4).
2. **The growth gate parsed zero comparative pairs from the real chunk
   formats** (B4: 37/37 abstain) — it is effectively inert today.
3. **The XBRL gate is the only engaged content gate** (40.5% catch over the
   seeded-error set), and it has a rounding tolerance that small
   fabricated-figure errors slip through.
4. **Sign/polarity lives in prose** ("net loss of $X") — invisible to all
   figure-based gates; only the LLM auditor can catch it (0/4 deterministic
   catches in B4).
5. **CORRECTED 2026-10-02 (was my misdiagnosis)**: Tesla's Q4-2023 GAAP
   diluted EPS really IS $2.27 (10-K p.25 — net income $7,928M including
   the one-time tax benefit, over ~3.49B shares); $0.71 is the NON-GAAP
   figure (p.4). My benchmark draft quoted the non-GAAP value as plain
   "diluted EPS" — the XBRL gate was right to flag it. The 234-row span
   scan found ZERO mismatches among dual-key reconciled rows. Real nuance
   worth keeping: GAAP/non-GAAP basis distinctions live with the audit,
   and the fact store carries no basis label on claims.
6. **The segment-vs-consolidated misbind** (a gross-margin question answered
   with the Services segment revenue) is real and reproduced; the v1
   runtime scope gate EXISTS on branch `trackb/b2-segment-scope` (a1a808d:
   line-level classification, 0/9 golds and 0/15 real receipts
   wrong-rejected, 3/5 synthetic swaps caught) — not merged; the 6 live
   regression tests + a deploy decision are the gate.
7. **The LLM auditor's catch rate is unmeasured** — it is the only check for
   46% of the seeded errors; its benchmark (B4's LLM phase) is scheduled
   post-quota-reset.
8. **The nightly battery never reaches a verdict** — killed at its 90-minute
   timeout every night; the schedule is PAUSED (owner decision) and the
   ADR-022 draft defines the amendment; the per-question SHADOW-ROW capture
   (a4e1886) means the next run leaves data even if killed.
9. **Receipt accumulation**: each nightly attempt wrote 33-41 receipts to
   the production database by design; retention is an open owner decision.

## What is deliberately NOT claimed

No "zero hallucination", no "tamper-proof", no per-figure accuracy claims
without intervals. FastPath is ~6-10 s with zero LLM calls. The honest posture:
every number served is either verified against evidence or the answer is a
refusal — and the receipts let anyone check that claim themselves.
