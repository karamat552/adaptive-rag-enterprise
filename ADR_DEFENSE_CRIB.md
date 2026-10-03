# ADR Defense Crib — the three you must be able to whiteboard

An interviewer will probe one of these. If you can't explain it without
notes, a 232 KB doc set reads as AI-generated padding — which hurts more
than a short README would have helped. Learn these three cold.

Rule for all three: **say the mechanism first, then the failure it
prevents, then the honest limitation.** The limitation is what makes you
sound like an engineer instead of a brochure.

---

## 1. ADR-005 — the receipt chain (your headline feature)

**One-line answer:** every certified answer carries a cryptographic proof
of where each number came from, and an outside auditor can re-check it
offline without trusting my system.

**The mechanism, in five steps:**
1. At ingest, every chunk gets `[char_start, char_end)` offsets into a
   per-page transcript. Spans are located by **verbatim `find()` — never
   offset arithmetic**. A chunk that can't be located gets `NULL`
   ("not locatable", never a fabricated span).
2. The page transcript is pinned to the source PDF by the manifest's
   SHA-256. So the chain is anchored to a document I don't control.
3. When the audit certifies an answer, the draft is split into claim
   sentences with their `[n]` citations, and the canonical evidence
   records are stored on the receipt.
4. `GET /verify/{run_id}` recomputes: slice the stored transcript,
   re-hash `company ⊣ source ⊣ page ⊣ slice`, compare to `chunk_hash`,
   and require the slice equals the chunk content verbatim. **Zero LLM
   tokens.**
5. The export bundle ships the receipt + transcripts + the verifier, so
   it re-verifies on an air-gapped machine.

**What it prevents:** tampering with a span, the content, the transcript,
or a citation index breaks the chain *by construction*. Unspanned
(pre-2.1) evidence fails closed as `no_span` — it never silently
certifies.

**Why not the alternatives:** parallel fleet speed is bounded by synthesis
anyway; multi-provider routing is config anyone can copy with LiteLLM;
"self-rewiring agents" are non-deterministic. The receipt chain is the one
thing a wrapper can't fake — it needs ingest-lineage discipline and a
tamper-test habit.

**The limitation — say this before they find it:**
> "It proves each cited span exists verbatim in the corpus I ingested, and
> that the chain is intact. It does **not** prove my ingested corpus
> matches the original PDF — that's why the bundle carries the EDGAR
> SHA-256 so an auditor re-downloads and re-hashes the filing themselves.
> And it can't cover text retrieval never found."

**Likely follow-up:** *"What if someone edits the DB?"* → The chain is
recomputed from stored transcripts against stored chunk hashes; an edit to
either breaks the `sha256(...) == chunk_hash` equality. `chunk_hash` is
UNIQUE, and `company/source/page` truth comes from the chunk table, not
from the receipt — so relabeling a company self-consistently still fails.
That's the `attribution_mismatch` status, and it was a real tamper-suite
finding.

---

## 2. ADR-009 — the unit & scale gate

**One-line answer:** a regex reading the table's own declared units
catches "$21,563 billion" for an "(in millions)" table — before the LLM
auditor ever sees it.

**The mechanism, in three steps:**
1. **`parse_declared_units`** reads the table's own declaration —
   `"($ in millions, except percentages and per share data)"` →
   `{money_scale: 1e6, per_share_exception: True}`.
2. **`assert_claim_scales`** requires every `$`-figure in a cited claim to
   be **reconstructable** from the cited evidence: raw table-unit values
   quoted verbatim, declared-scale renderings (`$21.6 billion` ==
   `21,563 × 1e6`), or pairwise sums of cited member rows. 2% tolerance
   for rounding.
3. Fail-closed: a scale lie rejects the draft (`unverified_system`) before
   a single auditor token is spent.

**The subtle design point — use this one, it's the best answer in the
whole repo:**
> "There's deliberately **no freestanding ×1000 branch**. A value that is
> 1000× every reconstructable figure *is* the lie, not a re-rendering. If
> you special-case ×1000, you're encoding the bug you're trying to catch."

**The limitation — say it:**
> "Reconstructability is per-citation-set. A figure legitimately derived
> from values spread across many chunks — like a 4-segment total, beyond
> pairwise sums — could false-positive. I accepted that because the retry
> path re-runs with different evidence, and XBRL ingestion replaces
> reconstructability with exact structured facts. It's gated: revisit if
> live runs show false positives."

**Live proof:** the real certified Apple receipt (2,890-char draft, 15
evidence chunks, "$22.3 billion" over in-millions tables) produces
**zero false positives**; the engine declines to judge undeclared prose.

**Likely follow-up:** *"Why not just let the LLM auditor check units?"* →
"Because an LLM can be talked past it; a regex reading the declared units
can't. And this gate is zero-token — it costs nothing to run first."

---

## 3. ADR-017 — shadow mode (your newest, and the deepest)

**One-line answer:** I built the deterministic answering path, but I don't
serve it. Every query runs it alongside the LLM path and records whether
they agree — measured, in shadow, until a pre-registered gate clears.

**The problem it solves:** in V1, numbers are *generated* by an LLM and
then audited. In V2 (Path A), numbers are **looked up** from fact rows
whose company/metric/period were bound at write time. A lookup can't
misbind the way a generation can. But a new path is a new risk — so it
must *earn* production.

**The mechanism:**
1. **`fact_rows`** (migration 006): every number anchored to exact PDF byte
   spans, with company·metric·period bound at write time.
2. **Dual-key reconciliation:** each consolidated row is checked against
   SEC-published XBRL at ingest (exact, or ≤0.5% cross-scale). Agreement
   between the PDF span and the derived Q4 XBRL value stamps
   `xbrl_facts.confirmed_by_pdf` — a three-way check.
3. **Fail-closed coverage:** Path A serves **only** what two independent
   sources confirm — revenue and net income for Q4-2023 across three
   companies. **"Absence of contradiction is never reconciliation."**
4. **Routing guard:** exact-match `(entity, metric, period)`. Any miss,
   ambiguity, or segment qualifier demotes the whole query to the fleet.
5. **`fact_shadow.py`:** a post-audit node that builds the Path-A
   candidate, runs the gates over it, classifies V1-vs-V2 agreement
   (`agree_numeric / agree_partial / agree_refusal / disagree_value /
   disagree_shape`), and records the ledger — **never raising, never
   mutating state, never serving.** Both terminal paths (certified AND
   refusal) pass through it, plus cache replays — the third terminal, which
   was a live-caught miss.

**The exit gate — quote it exactly:** ≥98% agreement on covered questions,
15/15 corrupted-claim catches, zero fabricated certified answers, **7
consecutive green nights**. `RAG_FACT_FASTPATH` stays unset.

**The limitation — and you have a great answer here:**
> "EPS is honestly partial. Meta's Q4 EPS was **disconfirmed** at 3 cents
> (5.33 vs 5.30) and stays off Path A — that was the three-way check's
> first live catch. Apple's fiscal Q4 is calendar Q3, so the calendar-frame
> rule would misbind and I left it out of scope rather than guess."

**Likely follow-up:** *"How do you know shadow mode doesn't affect
serving?"* → "It's a pure isolation contract: it never raises, never
mutates state, never serves — and there's a test that pins the isolation.
The one time it broke was subtle: cache replays were the *third* terminal
path and weren't being measured, which silently dropped every replayed
answer from the study."

---

## The 60-second pitch (memorise this exact sequence)

> "Enterprise RAG over SEC filings. The problem isn't answering questions
> — it's **proving** the answer. Every certified number carries a
> cryptographic receipt: claim → citation → exact PDF byte span → SHA-256,
> Ed25519-signed, re-verifiable offline by an auditor with zero trust in my
> system. Five deterministic gates run before the LLM auditor sees a token,
> and when anything fails it refuses rather than guessing.
>
> The number: **234 of 234 spans byte-exact verified, zero fabricated
> certified answers across every battery.**"

Then stop talking. Let them ask.

---

## Numbers to have on the tip of your tongue

| Claim | Number |
|---|---|
| Spans byte-exact verified | **234 / 234**, 0 mismatches |
| Test suite | 369–377 passing (offline + integration) |
| Regression classes locked | 22, each with a named test |
| Fabricated certified answers | **0**, every battery, every provider |
| Receipt links (white-whale run) | 15 / 15 crypto-verified |
| Recall @ answerable | 80–90% clean batteries; 50–67% under provider weather |
| Corpus | 3 filings, 223 chunks, 45 pages |
| ADRs | 19 |
| Commits | 122 over ~5 weeks |

## Two questions you should ASK them

1. "How do you currently prove an LLM-produced number in a regulated
   report?" *(tests whether the team has thought about this at all)*
2. "When the audit and the model disagree, which one wins in your
   system?" *(no good answer means the system can't be trusted)*

---

## If you get stuck

Say: **"Let me be precise about what it does and doesn't prove."** Then
give the limitation. Honest limitation-handling is what separates a senior
engineer from a demo. Every ADR in this repo already does it — that's the
actual signal you're selling.
