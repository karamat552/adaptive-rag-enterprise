# MEASUREMENTS — 2026-10-06 (the frozen numbers)

Provenance: written from THIS machine's session-captured outputs (the other
session's `deliver_to_main.patch` never arrived — the viewer route failed 3x;
the paste-attachment route worked 3x this session). When that patch arrives,
reconcile against these numbers, not the other way around: every figure below
was observed in a command's raw output on this machine. Instruments:
`scripts/measure_recall.py`, `scripts/bench_latency.py`,
`scripts/generate_answer_eval.py`, `scripts/sync_xbrl_and_verify.py`,
`scripts/local_stack_bootstrap.py`, `scripts/token_dig.py` — all on main
(`f5501d6` at time of writing).

Targets: a seeded DISPOSABLE Neon database (`adaptive_rag_measure`, dropped
after use) for write-capable runs; production (read-only, Postgres-enforced)
for the recall measurements. Provider: Groq primary + APInex/NIM/OpenRouter
failover lanes.

---

## A. Span-level retrieval recall (production, READ-ONLY enforced + self-checked)

> 2026-10-07 UPDATE: the coverage expansion (16 XBRL facts, was 8;
> 15 reconciled triples, was 7; B01/B04/B05 re-classed in_coverage_exact)
> changes the battery's expected-units set — the recall figures below were
> measured PRE-expansion; re-run for the post-expansion numbers.

```
key [reconciled_only]  (33 units, 22 questions contributing):
  recall@5    = 0.3939  (13/33 units)
  precision@5 = 0.1857
  recall@10   = 0.3939  (13/33 units)
  precision@10 = 0.1571
key [all_span_rows]    (33 units, 22 questions contributing): IDENTICAL
questions_retrieved=42   returned_zero_chunks=0
MIX-DECLARATION MISMATCH — per-class REFUSED (declared 14/8 vs actual 9/9;
  and the declaration's own counts sum to 46, not 42 — three pinned defects,
  tests/test_battery_mix_declaration.py)
```
- **This is ONE stage** (did the chunk physically contain the span?) — NOT
  end-to-end recall@answerable; it does not supersede KNOWN_ISSUES' 50-67%.
- The widened key adds nothing for THIS battery: every `expect_triples`
  triple resolves to reconciled rows.
- Misses (20 units, placed): **17 = overlapping chunk exists but never
  retrieved at depth 50 (ranking)**; 3 = retrieved deeper than max-k;
  0 = page-not-in-corpus. Per question: A02/A04/A06-A10/A12/A14-A16 (1 each),
  A11/E02/E03/E04 (2 each). A-class holds 13 of 20 — the "E-class misses
  hardest" claim was withdrawn.
- Substitute-embedder demo: `--allow-substitute-embedder` → recall@5 =
  **0.0303 (1/33)**, `valid_for_claims: false`, INVALID FOR CLAIMS stamped —
  a 13x collapse; the stamp is load-bearing.
- Span scan (corrected per-digit matching, all 234 fact_rows):
  reconciled **0/8** mismatches; unreconciled **15/226** (Apple
  segment-revenue labels carrying cost-of-sales values — KNOWN_ISSUES #12).

## B. Per-stage latency + token cost (disposable, real pipeline)

```
answered 3/5 (refusals excluded from latency statistics)
p50 = 168,047ms   p95 = 190,234ms (over answered runs only)
stage ms (sum over the 3 answered runs):
  validate    224,450   (gates + audit chain — ~75s per answered question)
  csuite_synth 86,528   (~29s each)
  exec_db      60,183   (~20s each)
  shadow_fact  24,687   (~8s each)
  rewrite      14,953      cache_check 14,234    premise 7,592
  gateway       3,140      cross_check      14
tokens/query (mean over answered): usage_in 161,336  usage_out 17,217
  usage_total 178,553  llm_calls 49.7
cost: n/a (no verified price supplied — never invented)
per-run: A01 refused 150.5s/186,373 tok · A02 answered 168s/245,094 ·
  A03 refused 140.4s/187,938 · A04 answered 77.5s/33,059 ·
  A05 answered 190.2s/257,506
```
- **The three profiles** (the diagnosis; see TOKEN_COST_DIAGNOSIS.md):
  covered = 0 tokens; healthy uncovered ≈ 17-21K / 10-13 calls; the
  pathological tail ≈ 178K / 49.7 calls — variance WITHIN one run.
- Caveats owned: my pre-run estimate was ≤25K (off by an order of
  magnitude); B's JSON artifact was destroyed by `rm -rf eval_out` before
  the C re-run — the numbers survive in this session's captured log.

## C. Answer quality (disposable, judge = openai/gpt-oss-120b, rubric verbatim)

```
12 answers: 5 vectorstore / 7 verified_refusal (several refusals mid-run:
  the 429 wall — measured "Used 197,895 / Limit 200,000")
GRADES: mean 3.67 over 9 scored · 3 judge_error (JSON truncation at the
  400-token judge cap — recorded as score: None, NEVER zeroed)
refusal scores: 4,4,4,4,(None),4,4 — the R4 honesty rule worked: refusals
  that named the true cause scored 4, not 0
judge_tokens_total = 11,747   run_tokens_total = 0 (capture bug: wrong key;
  fixed in the script — the real RUN token cost is NOT MEASURED)
artifacts: eval_out/grades.csv + grades.json (gitignored)
```
- Caution (pinned in the JSON): an opinion of ONE judge model against a
  fixed rubric; before public quoting require a second judge from a
  different family + a human spot-check of ~5 answers.

## D. The XBRL causal chain (disposable; real data.sec.gov fetch)

```
[before] xbrl_facts=0  fact_rows=234  reconciled=0     <- the audit's 3 failures
STEP 1: synced 8 xbrl fact(s) from 8 derived row(s)    (real network fetch)
STEP 2: sync_fact_rows -> {rows: 251, reconciled: 9, span_mismatches: 0,
          excluded: 51, epoch: 2}   -> table reconciled=8 (9 candidates,
          8 committed — the 1-row report-vs-table delta noted, unreconciled)
STEP 3: stage 2-pathA {PASS: 4}   stage 6-terminal {PASS: 4}
STEP 4: mutation "Path A would serve a covered query inside the demotion
          set" CAUGHT · mutation "receipt evidence span is not hashed" CAUGHT
[exit: 0] CHAIN WALKED
BLOCKED-PATH DEMO: dead proxy -> step 1 fails -> STOP, exit 1, nothing
  hand-written (the anti-fake-green rule, exercised)
```

## E. The stack bootstrap (disposable, idempotent)

```
migrations 7/7 · chunks 223 (Tesla 139, Apple 28, Meta 56) · 45 transcripts
fact sync: 251 candidates -> 234 written, 0 span mismatches, reconciled 0
  (pre-SEC — honest gap reported, no placeholder facts)
IDEMPOTENT: re-run committed rows: 0
```

## The environment events (this machine)

- The mmh3 Application-Control DLL block appeared between Oct 2-5 and has
  LIFTED (policy rescinded) — local verification works again (offline suite
  404/0 as of this writing).
- The Windows path-length limit (WinError 206) killed the mutation runner's
  copytree via vendored site-packages — fixed (venv ignore patterns).
- The Groq TPD wall: exhausted by the measurement session itself
  (197,895/200,000 measured mid-run) — the retry dig waits for the reset.
