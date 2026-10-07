# TOKEN COST DIAGNOSIS — why one question cost ~178K tokens (and the fix)

Provenance: written from THIS machine's measured evidence (the per-node dig,
`scripts/token_dig.py`, and the B real bench). The other session's diagnosis
doc never arrived; this is the measured version, not the blended-average
reasoning. Last updated: 2026-10-07.

## 1. The measured reality: THREE profiles, not one

| Profile | Tokens/question | LLM calls | Evidence |
|---|---|---|---|
| **Covered question** (RAG_FACT_FASTPATH=1 — ON in production, owner-verified via the Render dashboard) | **0** | **0** | A01/A02 dug: `fact_fastpath` serves in ~6s; no LLM call |
| **Healthy uncovered question** | **~17-21K** | **10-13** | B01 dug: ~17K (gateway 680 + fleet 4,252 + synthesis 8,444 + audit 3,546); D01 dug: ~18K |
| **Pathological tail** | **~178K** | **49.7** | B real bench: A02 245K, A05 257K vs A04 33K — SAME run, same env |

The proof it is not the design: A04's 33K and A05's 257K happened in the
same bench run. The design cost of an uncovered question is ~18K; the
variance IS the bug.

## 2. The mechanism (read from the graph wiring)

The retry loop (`adaptive_rag.py`, evaluate_retry_thresholds ->
route_after_rewrite):

```
validate (gates + audit) -> evaluate_retry_thresholds:
    grounded -> done
    retry_count >= max_retries (2) -> refuse
    else -> rewrite (1 LLM call) -> exec_db
            ^^^^^^^^^^^^^^^^^^^^^^^^^  the FULL fleet re-runs:
                                        re-retrieval + 3 specialist calls
                                        + synthesis + audit — a complete
                                        second pipeline pass
```

One unverified outcome costs a full second pass (~10 calls, ~18K tokens); a
second failure a third pass. Stack on top:

- in-client retries: the provider engines are built with `max_retries=3`;
- executive peer rescue: a quota-class failure at the exec stage re-sends
  the big prompt to every vetted peer (each peer attempt consumes tokens
  on ITS provider);
- 429-rejected calls cost ZERO tokens — so the waste is NOT the retries
  themselves; it is the SUCCESSFUL duplicate pipeline passes the retry
  loop buys.

Measured multiplier: healthy 10-13 calls -> stormy 49.7; tokens follow.

## 3. The fix that LANDED (failing-first, gates green)

**`RAG_MAX_LLM_CALLS_PER_RUN` (default 24)** — commit `55fae6a`:
- the healthy profile (10-13 calls) gets ~2x headroom — untouched;
- the storm is bounded at ~2 pipeline passes instead of 4 — roughly half
  the pathological tail, and 49.7 calls can never recur;
- `LLMBudgetExceeded` fails closed into the existing refusal paths; it is
  its OWN truthful class (a budget trip is not a provider rate limit — the
  name-the-true-cause rule; the reviewer's pushback accepted);
  `_is_quota_error` excludes it explicitly; every except-path passes it
  through untouched: no circuit failure, no endpoint cooldown, no peer
  fallback (peers would add calls, not save them);
- failing test first (`tests/test_call_budget.py`, AttributeError -> 3
  passed); offline suite 404/0; battery 42/42 15/15 7/7 GREEN.

## 4. Ranked techniques (what to do next — each gated on its own measurement)

1. **Reuse the evidence pool on retry** (~9K/retry saved; ~6x the best
   audit tuning): today a retry re-runs `exec_db` even though the corpus
   did not change. Design: thread the existing `evidence_records` forward;
   only synthesis+audit re-run with the rewritten query. FIRST measure the
   retry's conversion rate (the one decision: does a retry ever flip
   refused->answered?) — never flips -> kill the retry entirely; flips ->
   build the cheap retry.
2. **Attack synthesis** (9.1K = 50% of a healthy question, the biggest
   single line): cap synthesis input tokens; apply the segmented audit's
   `build_small_context` idea to synthesis. Needs a measurement, not a
   hunch.
3. **Segmented audit (`RAG_SEGMENTED_AUDIT=1`)**: STAYS DARK — the code's
   own warning (`adaptive_rag.py:3239` "do not enable mid-A.2-gate") holds
   and the historic -65% figure appears NOWHERE in the repo (session
   memory, never committed as evidence). The prerequisite: its own
   measurement (segmented-vs-consolidated tokens + verdict identity +
   the disagreement-rate measurement the architecture doc demands).
4. **Evidence-pool caps per specialist**: the fleet's top_k~15 chunks are
   the biggest prompts; capping each specialist's evidence trims the fleet
   line. Modest, low risk — after 1 and 2.
5. **Already deployed**: the fastpath flag ON makes the covered half FREE
   (production reality — the 178K average was measured in a flag-OFF env);
   the call cap bounds the tail NOW.

## 5. What is still open

- The retry conversion-rate dig (the one decision) — after the Groq reset.
- The synthesis input measurement (prerequisite for technique 2).
- The segmented-audit prerequisite measurement (prerequisite for the flag).
- C's RUN token total: NOT MEASURED (capture bug, fixed in the script;
  re-running costs ~2M tokens against a wall it already hit).
