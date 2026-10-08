# ADR-022 (DRAFT — NOT APPLIED; battery and workflows unchanged until the owner approves)

## Amend the A.2 promotion gate: the nightly battery cannot reach a verdict

**Status:** draft for owner decision · **Date:** 2026-10-02 · **Supersedes:** the
7-consecutive-green-nights clause of the A.2 gate (ADR-017 Phase 1)

## Problem (all measured, raw evidence in the session record)

The A.2 gate requires ≥98% V1–V2 agreement over the 42-question battery, 15/15
corrupted-claim catches, and 7 consecutive green nights. Current state:

1. **The nightly run is killed at its own 90-minute timeout, every night.**
   Runs #9–#16: conclusion `cancelled`, step "A.2 battery — matrix + corrupted
   + shadow" killed at 90 min 0 s ± 21 s each time. Zero runs have reached a
   verdict since the shadow pass became part of the nightly job. The gate can
   never turn green under the current definition — this is mechanical, not a
   verdict against the system.
2. **The scheduler fires 4.5–5.5 h late** (cron `0 1 * * *`; actual starts
   05:28–06:48 UTC across #7–#16). This is GitHub Actions' busy-slot delay; it
   does not break correctness, but it collides the run with the day's Groq
   window and the keep-alive morning traffic.
3. **Cancelled runs leave NO artifact** (measured: runs #15 and #16 have empty
   artifact lists — the timeout kills the job before the `if: always()`
   upload step). Consequence: per-question token/outcome data for the nightly
   runs DOES NOT EXIST. The battery code records per-question rows
   (`shadow_rows`: outcome, tokens, per-model usage, latency, capacity state)
   — but only into a report written at completion.
4. **The Groq daily token wall (200,000 TPD) is suspected to bind mid-run**
   (Oct 1: 199,842/200,000 used at 07:45 UTC, during the nightly window) —
   **needs verification**: per-question tokens were never captured (point 3),
   so nightly-consumes-the-wall remains a well-supported hypothesis, not a
   measurement. One directly measured data point: a single full-pipeline run
   under partial-wall conditions burned **41,542 tokens** and still ended in
   a refusal (2026-10-02, B1 measurement accident — retries and peer-failover
   dominate a wall-weather run).
5. **Each nightly run writes 33–41 real receipts** into production
   (measured: 39 on Oct 1, 33 on Oct 2 — the shadow pass is live V1 runs by
   design). At this rate the receipts table grows ~12% per night of attempts.

## Options (numbers first; per-question cost is currently UNMEASURED — sizing
 below uses the bounded estimate 2.5–5K tokens per healthy run, derived from
 ~39 real runs consuming most of one day's ~200K; a wall-weather run measured
 at 41.5K. Both bounds must be replaced by measurements from the capture fix.)

| Option | Nightly cost | What the gate then measures | Notes |
|---|---|---|---|
| **A. Nightly subset (10–12 q)** | ≈25–60K + 0-token matrix/corrupted | Agreement on a fixed subset (mix of covered+uncovered) + the FULL 15-corrupted gate, nightly | Corrupted gate is zero-token and stays complete; agreement coverage narrows from 42 to the subset |
| **B. Split battery across 4 nights (11/night)** | ≈28–55K/night | Full 42-question agreement per 4-night cycle; "green night" must become "green cycle" | Redefinition required; a mid-cycle provider outage voids the cycle |
| **C. Small paid exec lane** | Removes the exec-stage wall (the dominant consumer); cost = lane price (needs verification — no pricing measured) | Unchanged full battery | Keeps the gate's original meaning; adds spend + a lane the vetting gauntlet must approve |
| **D. Weekly full + nightly subset (A)** | Nightly 25–60K; weekly full ≈105–210K — **a full 42-question run may exceed the 200K wall by itself** | Nightly subset + weekly full coverage | The weekly full only fits with C, B, or a multi-day split — not alone |
| **E. Timeout 90→180 min** | None directly; completes the run but risks consuming the ENTIRE wall in one night | The original gate unchanged | Does not fix the wall; combined with A it gives headroom for slow lanes |
| **F. Schedule change** | None | None — delivery delay is platform-side | The 01:00 cron is already correct; actual start is scheduler-jittered. A workflow_dispatch manual trigger remains available for owner-initiated runs |

**Receipt retention (owner decision, carried here per directive):**
(i) tag battery receipts (dedicated `source` marker column or question-prefix
convention) and exclude them from served-answer analytics; (ii) cap battery
receipts by count/age (prune N>200 with the newest kept); (iii) move the
battery's shadow ledger to its own table (cleanest, needs a migration).
No pruning happens today; nightly rows continue to accumulate at ~33–41/night.

## Proposed decision path (prerequisite-first)

1. **Capture fix first (small battery change, needs approval):** flush the
   report incrementally after EACH question (write the JSON + log per-question
   tokens/outcome to stdout). Then even a timeout-cancelled run leaves an
   artifact and the data to size everything else. Without this, every option
   is sized on estimates.
2. **One manual full run post-reset** (workflow_dispatch, capture on) to
   measure per-question tokens — replaces both bounds above with measurements.
3. **Then choose:** recommended = A (subset sized from step 2, e.g. 8 covered
   + 4 uncovered) + E (timeout 180) + retention option (i). C stays available
   if the measured per-question cost makes even the subset tight.

## Gate redefinition clause (required)

Under any amended option, **the green-night counter restarts from zero under
the new definition on the first night the amended battery runs.** The
pre-amendment streak (currently 0 green) is void, not carried.

**Correction 2026-10-02:** the fastpath serving flag is ALREADY ON in
production (owner-set `RAG_FACT_FASTPATH=1`, verified via the Render
dashboard) — the A.2 gate is therefore not a promotion gate but a
**confirmation gate**: its verdict (agreement + corrupted catches under the
amended definition) either confirms the owner's decision or triggers a
retreat (flag off). The nightly runners never set the flag (the shadow
battery measures V1 by design), so battery receipts are full-pipeline runs
regardless of the production flag.

## Consequences

- The nightly stops dying at its own timeout; verdicts become reachable.
- Agreement coverage narrows under A/B/D — an accepted, documented loss.
- The token-per-question measurement unblocks honest sizing (and B3 follow-on
  work) instead of estimates labeled as estimates.
- No battery, workflow, or workflow schedule changes until the owner approves
  this ADR. STOP.
