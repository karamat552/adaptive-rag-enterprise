# THE FLEET LATENCY PROFILE — measured 2026-11-12

**What this is:** the first complete latency/token profile of the FLEET
path — the questions the FastPath correctly does NOT serve — across all
three of its regimes, on the live pipeline (disposable stack, cache
read+write disabled, post-budget-cap `RAG_MAX_LLM_CALLS_PER_RUN=24`,
production-matching config). Companion artifacts: `drill/latexp/`
(`run_baseline_v2.json`, `run_fleetC.json`, `run_fleetD.json`,
`pareto.json`) and `eval_out/token_dig.json`.

**Method:** `scripts/latency_experiment.py run` (the pre-registered
battery's fleet slice only — no post-hoc questions; the `--offset`
window added this day reaches the C/D regimes). The synthesis-input
attribution is `scripts/token_dig.py B02` (per-node usage deltas).
B-class is the 2026-11-11 baseline_v2 run (same harness, same
cache-disable discipline).

---

## 1. The profile — three regimes, not one number

| Regime | Questions | Outcome | p50 latency | tokens/question | n |
|---|---|---|---|---|---|
| **B — uncovered but served** | segment/niche figures (automotive revenue, ad revenue, iPhone, FoA) | `vectorstore`, certified | **78.1 s** | **16-20 K** | 4 |
| **C — out-of-period honest refusals** | Q4-2024, Q1-2023, FY-2022, Nvidia, bare "Q4" | `verified_refusal` | **3.2 s** | **~1.7 K** | 6 |
| **D — qualitative/interpretive** | risks, strategy, drivers, compare | 1 served certified · 5 refused | **30.3 s** (served) | **18.2 K** (served) | 6 |

**The reads that matter:**

1. **The refusal regime is nearly free.** C-class p50 3.2 s / ~1.7 K
   tokens: the honest-refusal posture (the system's identity) costs
   ~2 % of a served answer. Fail-closed is not just safe — it is cheap.
   The served regime (B) runs ~24× longer and ~11× the tokens.
2. **The C-class tail is the deep-search-then-refuse pattern.** C01
   (Q4 2024) and C02 (Q1 2023) refused only after re-retrieval
   exhausted: 50.7-51.0 s and 6.5-21.8 K tokens. Worth knowing: an
   out-of-period question costs up to ~22 K tokens to say "we don't
   have that period" — the period pre-gate (deterministic, zero-token)
   already catches the obvious ones (C03-C06 refused in 1.8-3.2 s);
   the tail is the plausible-period cases that must be searched to be
   honestly refused.
3. **The D-class run was quota-weather-contaminated — stated plainly.**
   Live 429 storms hit mid-run (31 s backoffs, the fleet model's
   cooldown, specialists quarantined). D01 still served a certified
   37-claim narrative in 30.3 s; D02's 80.9 s refusal and D03-D06's
   instant refusals are weather artifacts (cooldown-quarantine →
   fail-closed), NOT clean pipeline measurements. A calm-window re-run
   of D02-D06 would likely serve some of them — the battery's D-class
   expect_path marks them fleet-servable.

**The budget cap does not bind in normal fleet operation:** a clean
served run spends 6-8 LLM calls (route + specialists + synth + audit)
against the cap of 24; it only binds during retry storms, which is its
job.

---

## 2. The synthesis-input measurement — the writer is the whale

Per-node token attribution for B02 ("Tesla's automotive revenue in
Q4 2023", total ≈ 19.5 K tokens across the run):

| node | tokens in | tokens out | calls | role |
|---|---|---|---|---|
| gateway (router) | 681 | 96 | 1 | classification |
| exec_db (specialist) | 6,731 | 49 | 1 | evidence extraction |
| **csuite_synth (the writer)** | **12,730** | 973 | 3 | the C-suite brief |
| validate (the auditor) | 5,476 | 430 | 1 | grounding audit |

**The writer's input is ~12.7 K tokens — roughly 2× the specialist's
and 2.3× the auditor's input; it is the single largest token consumer
on the fleet path.** Cross-check: the D-class served run's synth node
read 4,314 tokens in (of an 18.2 K run) — D-class synthesis reads less
because qualitative retrieval returns shorter, non-table evidence.

**What this means for the "shrink what the writer reads" idea:** the
prerequisite is now measured, and the blunt instrument is already
refuted — the cap experiments (`run_cap6/cap8_v2`: prefix-slicing the
evidence to 6-8 chunks) bought tokens but produced refusals (the
writer, given less evidence, honestly declines). Any real reduction
must come from the evidence ASSEMBLY — tighter dedup, per-metric
retrieval (the specialist already does per-entity fusion), or
span-level evidence selection — not from a prefix cap. This is a
design input for a future experiment, not a committed change.

---

## 3. The incident the run caught (and closed)

The D-class slice ran into a live Groq quota wall — and the failover
exercised BOTH backup lanes, which both failed with non-quota errors:

- **NIM**: `nvidia/nemotron-3-super-120b-a12b` → HTTP 404 "Function
  …: Not found for account" (catalog still lists it; the account's
  function binding is gone).
- **OpenRouter**: `z-ai/glm-5.2:free` → 404 "use z-ai/glm-5.2" — the
  provider retired the free variant (the paid slug was NOT adopted; a
  paid lane must never be one 429 away).

Both lanes were replaced the same hour with live-probed (200 + sane
completion) models — `nvidia/nemotron-3.5-super-vl-120b-a12b` (NIM,
120B-class, peer-pool compliant) and `nvidia/nemotron-3-ultra-550b-a55b:free`
(OpenRouter) — commit `0c6be93`. **Corollary evidence of the stakes:**
a permuted-battery run against PRODUCTION at 07:51 UTC the same
morning (mid-quota-wall, dead lanes) refused 10/10 questions —
including covered metrics, because even the router is an LLM call.
Fail-closed held (zero fabricated answers, nothing cached — verified:
0 refusal rows in the semantic cache), but the outage class is real:
dead lanes turn a quota wall into a total refusal sweep. A latent
sibling bug is recorded, not fixed: backup engines cached globally
replay "Event loop is closed" under per-question `asyncio.run`
harnesses (production's single uvicorn loop is unaffected).

---

## 4. Honesty ledger for this profile

- n is small (4/6/6): indicative direction and magnitude, not
  statistics — the harness's own stated limit.
- B-class baseline is 2026-11-11 (one day older, same config).
- D-class: 5 of 6 rows are weather-contaminated; only D01 is a clean
  served-row. The table says so; the raw rows are in `run_fleetD.json`.
- The disposable stack carries the production-ingested corpus (223
  chunks, 234 fact rows) — retrieval-identical; the LLM spend hits the
  same Groq quota either way.
