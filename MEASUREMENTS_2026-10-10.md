# MEASUREMENTS 2026-10-10 — the fresh latency bench (the FastPath profile)

**Setup:** the bench ran on a SEEDED DISPOSABLE stack (Docker
pgvector:pg16, localhost:5433) — NOT production: the target guard
(ADR-025) correctly refused the .env's database for a write-capable
script (it writes refusal receipts). The stack was seeded through the
production loaders (223 corpus chunks, 45 page transcripts, 16 XBRL
facts, 18-19 reconciled triples). `RAG_FACT_FASTPATH=1` — the production
posture. The bench's 5 questions are the battery's first 5 — ALL covered
metrics, the SAME questions as the 2026-10-06 measurement.

## The apples-to-apples result (same 5 questions, same bench)

| Profile | p50 | p95 | tokens/answer | LLM calls | answered |
|---|---|---|---|---|---|
| 2026-10-06 (FastPath OFF — the fleet path) | 168,047 ms | — | 178,553 | ~49.7 | 3/5 |
| **2026-10-10 (FastPath ON — production posture)** | **94 ms** | **375 ms** | **0** | **0** | **5/5** |

Per-node breakdown (sum over the 5 answered runs): cache_check 639 ms,
fact_fastpath 90 ms — the entire cost is the deterministic
infrastructure.

## How to read this honestly

1. The 2026-10-06 numbers described the FLEET path on covered questions —
   the worst of both worlds (covered questions answered the slow way,
   pre-budget-cap). The fresh numbers describe the SAME questions on the
   FastPath. The swing (168s → 94ms; 178K → 0 tokens) is the FastPath +
   the budget cap + the retry kill, measured — not assumed.
2. DB-location caveat: the stale bench ran against the Neon DB (network
   RTTs); the fresh one against localhost. The RTT difference is
   sub-second noise against a 168s→94ms swing; the dominant factor is the
   path change. Production's END-TO-END FastPath (HTTP + Neon RTTs) was
   measured earlier at ~6-10s — a different measurement (includes the
   network), not contradicted by this one.
3. The UNCOVERED profile's clean re-measure (the fleet path's post-budget
   numbers) still waits for the calm quota window (KNOWN_ISSUES #2): the
   executive model (gpt-oss-120b) was quota-walled all day from the
   vetting + eval runs; a fleet-path bench today would measure the
   failover/peer-rescue profile, not the healthy one. Scheduled: after
   Groq's daily reset (05:30 IST).

## Disposable-stack findings (recorded)

- The two mutation controls (sync_xbrl_and_verify step 4) MISSED on the
  fresh stack ("applied and ESCAPED") where they were CAUGHT on
  production (2026-10-07). The mutation controls are STATE-DEPENDENT:
  they assert against production's corpus/epoch state. A fresh seed's
  state does not reproduce the production condition the controls encode.
  Not a regression in the pipeline; a scope limit of the controls —
  worth a KNOWN_ISSUES entry before anyone trusts a green mutation pass
  on a fresh stack.
- The XBRL reconciliation on a fresh stack works end to end: 0 → 16
  xbrl_facts, 0 → 18-19 reconciled (the causal chain walk, zero LLM
  tokens).

## Environment incidents during this session (the OS policy)

Windows app-control flagged TWO package DLLs in one day (jiter 0.17.0,
then psycopg2-binary 2.9.13) — each killed every client/DB init until
re-installed at a DIFFERENT version (jiter 0.16.0, psycopg2-binary
2.9.12 — fresh file hashes pass the cloud verdict). requirements.lock
pins the ORIGINAL versions (CI/prod unaffected). The sustainable fix is
an OS-level exclusion, not version whack-a-mole.
