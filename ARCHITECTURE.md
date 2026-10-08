# ARCHITECTURE — How Adaptive RAG Actually Works (plain language)

Written for the owner: what each stage is, why it exists, and — honestly —
where it is weak today. Everything below reflects the system as measured in
October 2026, including the B4 findings. Last updated: 2026-10-02.

## The one-sentence version

A user's financial question is answered by LLMs that may only speak from
retrieved SEC-filing text; five deterministic gates and one LLM auditor check
the answer against that text before it is served; every answer (and every
refusal) is stored as a cryptographic receipt that can be re-verified offline
by anyone.

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
auditor (quota, timeout) = refusal, never a pass. (Measured weaknesses —
see below.)

**11. Receipts.** Every grounded answer and every refusal stores a receipt:
question, answer, per-claim verifier stamps, evidence spans with hashes,
page-transcript anchors, corpus epoch, model lineage — plus a SHA-256 chain
and an Ed25519-signable offline bundle (`/export`), re-verifiable by an
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
