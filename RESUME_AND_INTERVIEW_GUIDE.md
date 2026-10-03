# Resume & Interview Guide — Adaptive RAG Enterprise

Written 2026-10-03 after a full code read + live run of the stack.

---

## 1. Verdict

**This project is genuinely strong — roughly top 1–5% of portfolio
projects. Do NOT start over.** Five weeks of work and 122 commits are
worth keeping. But "strong project" and "gets you interview calls" are two
different things, and the gap between them is where you are losing right
now. The fixes below are days of work, not weeks.

---

## 2. Why it's strong (evidence, not vibes)

| Signal | Evidence I verified |
|---|---|
| Sustained real work | **122 commits**, 2026-08-30 → 2026-10-02, conventional-commit discipline, clean progression |
| Senior-level design practice | **19 ADRs** with Context / Decision / **Rejected alternatives** — not just "what I built" |
| Actual test rigour | **377 tests pass** against a real PostgreSQL I stood up; a **22-class regression ledger** |
| It really works | ADR-017 fact store: 251 candidates → 234 rows, **0 span mismatches, 234/234 byte-exact** |
| Real security | RLS verified as a genuine **non-superuser, non-BYPASSRLS** role; unscoped query sees 0 rows |
| The moat is real | SHA-256 claim→chunk→span→transcript chain **+ Ed25519**, verifiable offline |
| Rare honesty | `KNOWN_ISSUES.md` names its own weaknesses — most candidates hide theirs |

The differentiator is **not** "a RAG chatbot" — thousands of candidates
have one. It is: **an AI system that can prove every number it outputs
came from a verified source, and refuses instead of guessing.** That is a
thesis, not a feature list. Lead with it.

---

## 3. The honest weaknesses

1. **Three documents.** Apple/Meta/Tesla Q4-2023. At interview, the
   retrieval problem is small. Own it: "the corpus is deliberately tiny —
   the contribution is the verification architecture, and the lexicons are
   re-derived per domain by design."
2. **Recall is 50–90%, so it refuses a lot.** A recruiter asking a random
   question may get a refusal. This is correct behaviour and *terrible
   demo UX* — it needs to be framed before they try it.
3. **Free-tier provider fragility.** Your own docs record three provider
   rotations and a model that vanished mid-project. If the demo dies
   because a free lane retired, you look broken, not thrifty.
4. **232 KB of docs / 19 ADRs.** This cuts both ways. An interviewer *will*
   probe ADR-017. If you can't explain shadow mode live, a 232 KB doc set
   reads as AI-generated padding — and that hurts more than a short README
   would have helped.
5. **No 10-second "wow".** Your moat takes 2 minutes to explain. Most
   reviewers give you 30 seconds.

---

## 4. Do these first — in this order

### Priority 0 — the demo must work (do this today)
I could not reach your live URLs from my sandbox (network-blocked), so
**you must verify these yourself, in an incognito window, on a phone**:

- https://adaptive-rag-enterprise.streamlit.app
- https://adaptive-rag-enterprise.onrender.com/health

**A dead demo link on a resume is fatal** — worse than having no link.
Render free tier spins down; the first request can take 30–60s. If it's
cold, either pin it awake or remove the link and offer a 60-second screen
recording instead. Also check `/verify/{a-real-run_id}` still returns PASS.

### Priority 1 — a 30-second proof flow
Whatever the demo is, the first thing a visitor should be able to do:

> ask one question → see the answer → click **Verify** → see
> `PASS — 15/15 links verified` → click **Download audit bundle**

One button, one visible win. Everything else is detail.

### Priority 2 — your 60-second pitch (memorise, don't read)
> "Enterprise RAG over SEC filings. The problem isn't answering
> questions — it's *proving* the answer. Every certified number carries a
> cryptographic receipt: claim → citation → exact PDF byte span → SHA-256,
> signed with Ed25519, re-verifiable offline by an auditor with zero trust
> in my system. Five deterministic gates run before the LLM auditor, and
> when anything fails the system refuses rather than guessing — zero
> fabricated certified answers across every battery."

Then hand them a number: **"234/234 spans byte-exact verified, 0
fabrications."**

### Priority 3 — be able to defend three ADRs, cold
Pick these and know them without notes:
1. **ADR-005 / receipt chain** — how tamper-evidence works and what it does
   *not* prove (the answer is in the bundle README: it proves the span
   exists in the ingested corpus, not that the corpus equals the original PDF).
2. **ADR-009 / unit-scale gate** — "$500M when the table says $433M".
3. **ADR-017 / shadow mode** — Path A is built but never served; every
   query is measured against V1 until the A.2 gate clears. Know why.

If you can't explain one of these, delete it from the README rather than
risk it in an interview.

### Priority 4 — fix what makes it look broken
- `classify_canary.py:89,92` — undefined `recall` on the **live** exit
  path (real `NameError`, not dead code).
- `db.py:1768-1777` — dead code referencing undefined names.
- `/search` returns a bare 500 when the embedder is missing — no hint why.
- Boot smoke test is skipped for `openai_compatible` (your DeepSeek/NIM
  lane) — a dead model there is not caught at boot.
- Merged already: SSE cross-tenant leak, truthful router-outage refusal
  (PR #1).

---

## 5. CV bullet you can use as-is

> **Adaptive RAG — Enterprise Financial Intelligence Platform** · Python,
> FastAPI, LangGraph, PostgreSQL/pgvector, React
> `github.com/karamat552/adaptive-rag-enterprise`
> - Built a tamper-evident RAG pipeline over SEC filings where every
>   certified figure carries a cryptographic receipt (claim → citation →
>   PDF byte span → SHA-256, Ed25519-signed) re-verifiable offline by an
>   external auditor with zero trust in the system.
> - Enforced 5 deterministic pre-audit gates (citation bounds, unit/scale,
>   growth direction, XBRL reconciliation, injection echo); the system
>   **fails closed and refuses rather than certifying an unverified
>   answer** — 0 fabricated certified answers across all batteries.
> - 377-test adversarial suite incl. tamper (17 vectors), fuzz (1,000
>   seeded mutations), chaos and failover contracts; 22-class regression
>   ledger; RLS-verified multi-tenant isolation under a least-privilege role.

**Do not** say "built a RAG chatbot". Say "built a system that proves its
own answers".

---

## 6. Answering "should I switch projects?"

**No.** Switching now means discarding your single best asset — a project
with a real thesis, real tests, and real history — to start a generic one
you'd have less time to make good. Your problem is not project quality. It
is:

1. the demo may be broken (unknown — verify today),
2. the story isn't compressed for a 30-second read,
3. some rough edges make it look unfinished.

All three are fixable in days. A new project resets you to zero and gives
you the same three problems in three months.

**One caveat, honestly:** the resume gets you the *call*. The interview
gets you the *job*. Depth you cannot explain is a liability, not a
strength — so spend the next few days re-reading your own ADRs until you
can whiteboard them, not writing new ones.

---

## 7. What I'd do this week

| Day | Action |
|---|---|
| 1 | Verify both demo URLs cold, in incognito. Fix or remove. |
| 2 | Build the 30-second proof flow (ask → verify → PASS). |
| 3 | Memorise the 60-second pitch; trim the README top to it. |
| 4 | Be able to whiteboard ADR-005, ADR-009, ADR-017 from memory. |
| 5 | Fix the four "looks broken" items; merge PR #1. |
| 6 | Update CV bullet + LinkedIn. Ask 2 people to try the demo and report what confused them. |
| 7 | Rest. Then start applying. |
