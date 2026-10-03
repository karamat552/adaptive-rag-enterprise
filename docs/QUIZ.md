# Adaptive RAG — Self-Quiz (20 questions, answers below)

For the owner: test your own understanding of the system you can now explain
in interviews. Answers at the bottom — no peeking.

## Pipeline

1. In one sentence: what three things must be true for the FastPath to serve
   an answer with zero LLM calls?
2. What is the semantic cache's similarity threshold for a replay, and what
   happens to a cached row when the corpus changes?
3. Why does the premise probe run BEFORE the specialist fleet, and what
   does it do when it finds zero corpus support?
4. What are the three specialist roles, and when does pruning run only ONE
   of them instead of all three?
5. What does the cross-check do when two specialists report DIFFERENT
   figures for the same (metric, company, period)?

## Gates & verification

6. Name the five deterministic gates and what each checks.
7. What happens to a draft when ANY deterministic gate flags it — and what
   happens when the LLM auditor is unavailable (quota/timeout)?
8. Which gates are figure-based, and why can NONE of them catch the claim
   "Apple reported a net LOSS of $22,956 million" when net income was
   +22,956?
9. What does "dual-key" mean for a fact row, and why does FastPath need it?
10. What does an abstaining gate do, and where (if anywhere) is that
    abstention recorded today?

## Receipts

11. What six kinds of things does a receipt store for a grounded run?
12. What is the difference between a run_id and a provenance_run_id on a
    cached replay, and why does the replay never re-mint its own proof?
13. What can an external auditor verify OFFLINE from an exported bundle,
    and what does the bundle explicitly NOT prove?
14. What happens in the receipt when a run is refused — does a refusal get
    a receipt too?
15. Why does the self-heal DELETE a cached row that lacks provenance instead
    of just serving it?

## Tenancy

16. How does row-level security scope a query to one tenant, and what two
    roles exist (and which can bypass RLS)?
17. After the 2026-10-01 auth fix: what can an ANONYMOUS caller access, and
    what happens if they declare tenant_id="acme"?
18. What is a tenant epoch (migration 008 design), and why does the cache
    need BOTH the public epoch and a tenant epoch?
19. Why can the runtime role (app_rag) read but never CREATE or bump a
    tenant's epoch row?

## Honesty

20. Give three measured reasons the pipeline currently cannot claim
    "verified" for every figure it serves.

---
---

## Answers

1. The question's (company, metric, period) must exist in the fact store
   (extracted from a PDF span) AND be reconciled against SEC's structured
   XBRL data (dual-key), AND the FastPath flag must be on.
2. 0.985 near-exact embedding similarity; any corpus change bumps the epoch
   and every cached answer from the old epoch becomes unreachable.
3. To avoid burning the full fleet (~minutes, many tokens) on a question the
   corpus cannot answer; zero support → an immediate, specific honest
   refusal.
4. Financial / risk / product; pruning (default ON) runs only the
   router-selected specialist for single-aspect questions — all three run
   for comparison questions.
5. It flags the conflict as a contradiction (grouped by metric/company/
   period/unit/basis), triggers one bounded re-retrieval, and if the
   conflict survives it is surfaced in the answer — never averaged.
6. Citation bounds ([n] in range); echo/injection guard; unit-scale gate
   (figures reconstructable from declared units); growth-direction gate
   (direction agrees with the evidence pair); XBRL gate (matches SEC facts
   for covered triples).
7. The run refuses (fail-closed) — the draft is never served; an
   unavailable auditor also means refusal (audit_unavailable), never a pass.
8. All five parse $-figures; "loss" is a prose polarity word — the figure
   22,956 is present and matches evidence, so every figure-based gate passes;
   only the LLM auditor reads the word (B4: 0/4 sign flips caught).
9. The fact must appear BOTH in a PDF span of the ingested filing AND in
   SEC's XBRL data for the same (company, metric, period) — two independent
   keys; the FastPath only anchors on rows both keys confirm.
10. Declines to judge (no finding, not a pass); today it is recorded only in
    server logs — the receipt has no structured gate-outcome field (the
    gate_outcomes design is drafted, awaiting approval).
11. Question, answer, per-claim verifier stamps, evidence spans + hashes,
    page-transcript anchors, corpus epoch (+ model lineage: model_id and
    prompt hash).
12. The replay gets a fresh run_id; provenance_run_id points to the ORIGINAL
    run that was certified — extraction never ran on a replay, so it cannot
    re-mint proof; /verify resolves the original receipt.
13. That every cited span exists verbatim in the ingested corpus with
    cryptographic hash custody, and (optionally) the Ed25519 signature; it
    does NOT prove the ingested corpus matches the original EDGAR PDFs
    beyond the recorded SHA-256 anchors — the auditor re-downloads and
    re-hashes those.
14. Yes — refusals get receipts too (audit_verdict="refused", zero claims),
    with the objection recorded in the refusal text.
15. An unverifiable cached answer cannot be re-proven (its receipt chain is
    gone); serving it would be minting trust without proof — the system
    deletes and re-certifies instead.
16. A session GUC (app.tenant_id) binds each connection; policies compare
    tenant_id against it under FORCE ROW LEVEL SECURITY. Runtime role
    app_rag (no bypass) and a separate admin identity (bypass only for
    migrations/ingest).
17. The default tenant only — anonymous callers may confirm 'default' but
    declaring any other tenant is a 403; the key IS the identity.
18. A per-tenant counter that bumps when that tenant's private corpus
    changes; a cache row is replayable only if BOTH the public epoch and
    the tenant epoch match — private uploads invalidate only their own
    tenant's cache.
19. Migration 008 grants SELECT only — epoch creation/bumps are admin-plane
    (ingest) operations; the runtime cannot forge or race its own
    invalidation (proven by the no-claim-default isolation test).
20. Any three of: the scale gate abstains on real chunks (units header is
    chunked separately); the growth gate abstains (pairs parser matches
    the real chunk formats 0/37); the XBRL gate covers only ~7 triples and
    has a rounding tolerance; sign/polarity is invisible to figure gates;
    the segment-vs-consolidated misbind is live; the LLM auditor's catch
    rate is unmeasured.
