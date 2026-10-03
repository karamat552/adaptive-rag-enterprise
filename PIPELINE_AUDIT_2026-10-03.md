# Pipeline Audit — Production-Level Review
**Date:** 2026-10-03 · **Commit:** `be6918e` · **Scope:** ingestion → certificate → serving

This is the record of walking the pipeline from the **start** (ingest) to the
**end** (offline verifier), testing each stage **live**, and reporting what was
observed rather than what was assumed. Every claim below is a command you can
re-run.

---

## Headline

| Question | Answer | Evidence |
|---|---|---|
| Does the pipeline work end to end? | **Yes** | 38/38 audit checks PASS, live HTTP runs below |
| Can the audit detect breakage, or is it a green rubber stamp? | **It has teeth** | 9/9 injected defects CAUGHT |
| Did the repo's own tests survive? | **Yes, no regressions** | 375 passed; 1 known failure (explained) |
| Defects found and fixed | **2 real** (see below) | with tests proving each |
| Was the audit itself right the first time? | **No — 5 of its first 6 failures were its own bugs.** Documented in code | see "Honesty" |

---

## Stage-by-stage evidence

Each line is a check that printed the value it observed.

### 0 · Ingest
```
251 rows written, 0 span mismatches at write, re-verify 234/234 byte-exact,
10 XBRL-reconciled
runtime role 'app_rag' sees 0 rows of another tenant's corpus (must be 0)
```
The corpus is ingested, every fact row is re-verified byte-exact against its
span, and tenant isolation holds at the database level.

### 1 · Cache
```
near-exact replay hits; paraphrase misses
  → threshold 0.985: miss-before=True, hit-after=True, unrelated-miss=True
replay restores documents (sources), not just the answer
  → cached_hit=True, documents restored=True
cache is tenant-scoped  → tenant B sees tenant A's entry: False
legacy entry without provenance = MISS (self-heal) → served as replay: False
```
The cache replays only near-exact questions, never leaks across tenants, and
refuses to serve an entry it cannot prove.

### 2 · Path A — deterministic facts, zero LLM tokens
```
5/5 served — Apple:ok, Apple:ok, Tesla:ok, Tesla:ok, Meta:ok
all figures verbatim in span text; offenders=none
Path A writes a deterministic receipt that /verify resolves
  → receipt present, verified=True, links 1/1
```
Covered financial questions are answered **without calling a model at all**,
with figures copied verbatim from the filing's own span.

### 3 · Router
```
faithful round-trip: vectorstore→vectorstore, general_knowledge→general_knowledge,
out_of_domain→out_of_domain; router_unavailable never set; specialists preserved
router death FAIL-CLOSES and names the true cause → flagged=True, truthful text=yes
non-schema router reply fails closed (never AttributeErrors) → no crash
```
### 4 · Retrieval
```
5 rows; fields ok=True; top: Tesla p3 fusion=0.0164
RRF order=['a','b','c','d']; both-arm docs occupy ranks 1-2=True
evaluated union pool 1/2/3 specialists = 15/14/15 chunks (parity)
```
Hybrid search returns cited, spanned evidence; fusing the arms ranks
agreement first; **pruning specialists shrinks the call count, never the
evidence pool** (the 15→5 regression that once caused misattributed figures
is now guarded numerically).

### 5 · Deterministic gates
```
citation-bounds: out-of-range [9] → rejected; in-range [1][3] → accepted
unit/scale: 1000×-up lie → REJECTED; verbatim → passed; undeclared → declines
growth-direction: 'declined 25%' over a YoY=+1% pair → flagged=True
echo/injection: 8 markers present
guard rail: degraded run grounded=False; objection="specialists quarantined:
  ['financial']"; names the cause=True; reaches the refusal text=True
```
### 6 · Terminal states
```
verified refusal stores a refused receipt (autopsy trail) → verdict=refused
premise fast-path refuses uncovered metrics WITHOUT the fleet
shadow executor NEVER serves → returned {} ; serving-state mutations=none
graph declares every state channel the nodes use → missing=none
```
The last line matters more than it looks: LangGraph **silently drops**
undeclared state keys, which had previously killed features while unit tests
stayed green. That class of bug is now checked structurally.

### 7 · Receipts and the offline verifier
```
committed white-whale bundle verifies offline (15/15) → exit=0, verdict=PASS
tamper: clean verified=True → tampered verified=False
Ed25519: algorithm=Ed25519; valid signature verifies=True; tampered payload rejected=True
operator runbook: documented key-gen script exists → script_exists=True
```
### 8 · Serving
```
14 routes; missing=none
open mode is an AFFIRMATIVE opt-in → 503 when keys unset & open-mode off;
  401 missing key; 403 present-but-invalid key
/query/stream forwards the RESOLVED tenant (not the raw param)
boot smoke covers every provider lane
```

---

## Live proof over real HTTP

Server: `python /tmp/serve_api.py` → uvicorn on `0.0.0.0:8000`, real DB (epoch 2, 223 chunks).

**1 · Deterministic answer, zero LLM tokens**
```
POST /query  {"question": "What was Tesla's revenue in Q4 2023?"}
→ run 5740ad0b4a2a
  answer   : "Tesla reported total revenues of $25,167 million for Q4-2023 [1]."
  usage    : {input: 0, output: 0, total: 0, llm_calls: 0}
  latency  : 0.04 s
```
The figure is correct and span-verbatim; no model was called.

**2 · Cache replay — the earlier bug, fixed and re-proven live**
```
POST /query  (same question, seeded certified entry)
→ run 4e25286a10e2
  cached         : True
  provenance_run : 5740ad0b4a2a      ← the run that EARNED the certification
  sources count  : 1                  ← the ac41bc7 fix, live
  usage          : 0 tokens
```
**3 · `/verify` resolves that provenance**
```
GET /verify/5740ad0b4a2a
→ verification.verified : true
  links_checked : 1   links_ok : 1
  links[0]      : {hash_ok: true, verbatim: true}
  reason        : "all links verified"
  receipt.corpus_epoch : 2
```
**4 · Fail-closed refusal names the true cause** (no provider key configured)
```
POST /query  {"question": "What was Tesla's dividend per share in Q4 2023?"}
→ outcome: out_of_domain, grounded: False, 0 tokens
  "…the routing stage is unavailable (the language model behind it is
   unreachable, unconfigured, or rate-limited). This is a service-side
   problem, not a verdict on your question…"
```
It refuses instead of guessing, and it blames the outage rather than the
user's question.

---

## Defects found and fixed

### 1 · The refusal forgot the one cause it knew (production-visible)
`fact_checker_guard` computed the exact reason — `"specialists quarantined:
['financial']"` — and then returned only `{grounded: False, outcome:
unverified_system}`. The reason went to a log line and died there, so a user
whose run was killed by a quarantined specialist was told the generic
*"draft failed the grounding audit after N attempts."*

**Fixed:** the reason now travels on a declared state channel
(`audit_objection` — undeclared keys are dropped at node merge) and is
surfaced in the user-facing refusal. Covered by 2 new tests, both **proven to
fail on the pre-fix code**.

### 2 · The signing runbook pointed at a file that didn't exist
`db.sign_certificate_payload()` tells operators to run
`scripts/generate_attestation_key.py`. That file was absent, so the only
documented route to enabling Ed25519 attestation dead-ended — every
deployment, including production, silently shipped `{"status": "unsigned"}`.

**Fixed:** the script now exists (with `--check` to report the live posture
without printing secrets), and the signing path is proven end-to-end:
sign → verify → tampered payload rejected. The audit asserts the crypto
rather than the presence of a key, so a garbage signature can no longer pass.

Note the honest framing: unsigned is a *documented* posture, and the hash
chain stays verifiable offline either way. The defect was that the documented
way to turn signing **on** didn't exist.

---

## Honesty: the audit was wrong five times before it was right

The first run reported **31 PASS / 6 FAIL**. Five of those six failures were
bugs in the audit, not the pipeline — and each is now documented at the site
so nobody re-derives it:

| Reported failure | What was actually wrong |
|---|---|
| Router sent everything to `out_of_domain` | The test mocked `_llm_call`, but `route_question` resolves `_get_router()` as a *call argument* — evaluated first. With no API key the node correctly fail-closed. **The router was right.** |
| RRF fusion didn't rank agreement first | The fixture used bare `{"id": n}` dicts; real rows carry `chunk_hash` (UNIQUE NOT NULL). All five docs collapsed into one fallback key. **Fusion was right.** |
| Specialist pruning broke pool parity | The check grepped for `target_pool=` — a variable that exists nowhere in the codebase. The real expression was `max(5, 15 // max(1, len(specialists)))`. |
| Growth gate missed an inverted claim | The check fed a prose string; the parser keys on Phase-B pair lines (`label :: Q4-2022=n \| Q4-2023=n`). **The gate was right.** |
| Path A served an out-of-coverage query | "Meta's Q4 2023 EPS" *is* covered — EPS is derived (14,017 ÷ 2,630 = $5.33), and Path A served the correct figure. The expectation was an unfounded assertion. |

The sixth was real, but framed wrongly: it asserted a signature block exists,
so it scored a correctly-configured deployment as broken **and** never
verified any signature.

Two lessons, both enforced in the committed code:
1. **A strengthened check must pass on the unmutated code first.** One
   strengthened assertion failed on the real code *and* on the mutant — which
   the mutation harness would have scored as "CAUGHT". It was a false positive;
   the check was fixed before its result was trusted.
2. **Evaluate the source expression, don't re-derive it.** The pool-parity
   check originally computed its own `max(5, 15 // n)` and therefore ignored
   the mutant entirely — it scored a hardcoded `k=5` regression as PASS. It now
   `eval`s the expression the code actually runs.

---

## Production-level assessment

**What is genuinely production-grade here**
- Deterministic factual answers with **zero** model tokens for covered
  questions — the cheapest possible path is also the most trustworthy one.
- Every certified answer carries a receipt whose hash chain verifies
  **offline**, independent of the database, with an immutable committed bundle
  (`audit_bundle_white_whale/`) as standing proof.
- Fail-closed everywhere: router outage, degraded specialist, unverifiable
  cache entry, missing evidence — all refuse rather than guess, and the
  refusal names the cause.
- Tenant isolation enforced at two independent layers (query predicate + RLS).
- A test suite that proves *negative* properties (1,000 fuzz forgeries, 0 false
  accepts) rather than only happy paths.

**Remaining honest gaps**
- The `signature` block is `{"status": "unsigned"}` in production because no
  key is configured. Enabling it is now one command plus an env var.
- The **citation-ledger fidelity gap** is still open and is the owner's call:
  a certified answer cited 【13】/【14】 while its own ledger enumerated 10
  sources. The citations were *in range* (the bounds gate is correct) — the
  ledger is model-written prose. Recommended fix: derive the ledger
  deterministically from the cited evidence.
- The audit's LLM stages are exercised through routing/state contracts with
  mocked engines, so stage *logic* is verified without spending quota. A full
  live-LLM run still needs provider keys.
- `tests/test_db.py::test_embedding_geometry_supports_near_exact_replay` is
  the one failing test in the CI-equivalent run: it needs the real
  `bge-small-en-v1.5` weights, and model downloads are blocked in CI/sandbox.

---

## Reproduce

```bash
bash /home/user/setup_stack.sh                  # deps + local Postgres + seeded corpus
python scripts/pipeline_audit.py                # 38 checks  → 38 PASS
python scripts/pipeline_audit.py --json          # CI gate (exit 1 on any FAIL)
python scripts/pipeline_audit_mutations.py      # 9 defects  → 9 CAUGHT
pytest tests --ignore=tests/test_answer_accuracy.py -m "not integration and not live"
                                                # 375 passed
```
