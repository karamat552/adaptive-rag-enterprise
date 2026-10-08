# PLAN — Corpus & XBRL Expansion (B6; PLAN ONLY — no ingestion without owner approval)

Goal: more dual-key-reconciled facts means more zero-LLM FastPath coverage —
but dual-key REQUIRES both the XBRL fact AND a matching PDF span, so every
expanded company needs its filing ingested too.

## 1. Sources and mechanics

- **XBRL:** SEC `companyfacts` API per company
  (`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`), curated
  to the same concept map the gate already uses (Revenues, NetIncomeLoss,
  EarningsPerShareDiluted, GrossProfit).
- **Filings:** SEC `submissions` API → the latest 10-K/10-Q for each
  company → the EDGAR-hosted PDF → the EXISTING ingest pipeline (chunking,
  transcripts, fact-row span-anchoring) → the EXISTING dual-key sync
  reconciles fact rows against the fetched XBRL.
- **No new pipeline code is required for v1 expansion** — the ingest +
  sync + gate already handle this shape; the work is orchestration,
  rate-limiting, and validation (re-run the corrupted gate + a spot-answer
  pass per new company).

## 2. SEC fair access compliance (mandatory)

- Hard cap **≤10 requests/second**, measured and logged.
- Declared **User-Agent** with contact details
  (e.g. "Adaptive-RAG/1.0 (research; contact: <owner email>)") on every
  request — the SEC's stated requirement.
- Off-peak fetching (the current 3-company corpus is ~a few MB of PDFs;
  even a 10-company expansion is a light, short crawl — no bulk mirroring).

## 3. Storage sizing (estimates; current plan limits NEEDS VERIFICATION)

- Measured today: 223 chunks + 45 page transcripts + 234 fact rows — total
  database size is in the low MBs (exact figure to be measured with
  `pg_database_size` before expansion; labeled estimate until then).
- Per added company (one 10-Q + facts): ~1-3 MB of chunks/transcripts/fact
  rows (estimate from the current three companies' footprint).
- **Proposed v1 expansion: +3 companies (e.g., Microsoft, NVIDIA, Amazon),
  one filing each** ≈ +5-10 MB — trivially fits any tier. **Cap the corpus
  at ~50 companies** (~100-200 MB estimate) — sized to stay comfortably
  inside Neon's free-tier storage even WITH the nightly receipt growth
  (~1-2 KB/receipt × 33-41/night) factored in; the B3 retention decision
  should land before any larger expansion.
- The real constraint is NOT storage — it is the **Groq token wall** (a
  bigger corpus does not itself consume more tokens per question, but the
  nightly battery's shadow pass and B5's eval set grow with coverage).

## 4. What expansion changes about the gates (honest)

- XBRL-gate coverage grows (more covered metrics → fewer abstains — B4
  measured 22/37 abstains on the 7 currently covered triples).
- Dual-key FastPath coverage grows with each reconciled triple.
- The known failure classes REMAIN: sign/prose polarity invisible to
  figure gates; small fabrications under the XBRL rounding tolerance;
  units-declaration chunking (scale gate abstains until fixed); the
  question-scope classifier (B2's design).

## 5. Decision points for the owner

1. Which companies (I propose MSFT, NVDA, AMZN + their latest 10-Qs)?
2. Confirm the User-Agent contact string.
3. Approve the crawl window (a single off-peak session).
4. The B3 retention decision before scaling beyond v1.
