# DESIGN — Evaluation Harness (B5; DESIGN FIRST — no runs until B2 lands, within quota)

Purpose: a publishable, reproducible evaluation of the WHOLE system (not just
the gates): accuracy, refusal correctness, fabrication count, citation
accuracy, latency, tokens — with honest statistics.

## 1. The 150-question set (owner reviews the answer key before any run)

| Bucket | N | Content | Ground truth |
|---|---|---|---|
| Direct extraction | 50 | single-figure questions over the three Q4-2023 filings (revenue, net income, gross margin, EPS, R&D, segment lines) — spread evenly across Apple/Meta/Tesla and across statement pages | figures extracted deterministically from the corpus (the B4 extraction method) + cross-checked against the XBRL store where covered |
| Segment/footnote | 30 | true segment questions ("Services segment revenue", "automotive revenue", "FoA advertising") and footnote items (dividends, buybacks, energy deployments) | same extraction; segment labels tagged |
| Unanswerable / out-of-domain | 40 | wrong period (Q4 2024), absent metrics (Apple dividend per share), out-of-domain (weather), corpus-absent companies (Microsoft) | CORRECT behavior = honest refusal (verified_refusal), NOT a fabricated answer |
| Adversarial / false-premise | 30 | premise-injected questions ("Why did Tesla's dividend grow?"), metric-misbinds ("Apple Services gross margin"), unit traps ("in billions"), growth-direction inversions | refusal or correctly-scoped answer per the filings |

Construction rules: every ground-truth figure is machine-extracted from the
indexed corpus and recorded in `eval/answer_key.json` with (question, company,
period, metric, gold figures, expected behavior class) — the owner reviews and
signs the key before the first run. No paraphrase-of-memory golds.

## 2. Baselines (the system is only meaningful against alternatives)

1. **Plain RAG:** top-k retrieval (k=8) + ONE single prompt, no router, no
   fleet, no gates, no auditor — the reference everyone recognizes.
2. **Single-LLM no-retrieval control:** the question alone to the executive
   model — measures what the model 'knows' without the corpus (the
   hallucination-floor baseline).
Both run over the same question set, same provider budget discipline.

## 3. Metrics (per system + per bucket)

- **Answer accuracy:** gold figure(s) present in a grounded answer
  (string-level on the comma-formatted figure, the same test as B2/B4).
- **Refusal correctness:** refusal where refusal is right; answer where an
  answer exists (refusal-on-answerable and answer-on-unanswerable are both
  failures, reported separately).
- **Fabrication count:** grounded answers whose figures appear in NO cited
  evidence span (machine-checked via the receipt's evidence chain — this is
  the strongest definition available to us). Zero-event counts are NEVER
  reported as "0% fabrication"; the rule-of-three upper bound is stated.
- **Citation accuracy:** cited [n] indices in range AND the cited span
  contains the claim's figures (receipt-verifiable).
- **Latency p50/p95** and **tokens per answer** (from the usage payloads).

## 4. Statistics

- Wilson 95% intervals on every proportion (per bucket and overall).
- Rule-of-three upper bounds for zero-event counts, stated explicitly.
- n=150 gives ±8% at worst (p=0.5); per-bucket n≈30-50 gives ±11-18% —
  intervals, not point estimates, are the deliverable.

## 5. Execution plan (after B2 lands; spread over days within quota)

- `eval/harness.py` — same runner pattern as the battery (RAG_DISABLE_CACHE_*
  set; per-question flush so a quota kill loses nothing).
- Per-day token budget: ~40 questions/day ≈ 100-200K/day worst case →
  3-4 days for the system under test; baselines are cheaper (1 prompt each).
- One report: system vs baselines per bucket with intervals; every miss
  listed with its receipt run_id.
- STOP: design approval by the owner before the answer key is finalized.
