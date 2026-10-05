# Adaptive RAG — Enterprise Financial Intelligence Platform

![CI](https://github.com/karamat552/adaptive-rag-enterprise/actions/workflows/ci.yml/badge.svg)

**311 tests · battery-best 90% / live-run 50–67% recall (every miss documented) · 0 fabrications · every certification AND refusal crypto-receipted · 22 regression classes · 4 provider lanes**

> I built this to answer one question: *can an AI system prove — cryptographically,
> deterministically, without trust — that every number it outputs came from a
> verified source?*

**🚀 Live demo:** [adaptive-rag-enterprise.streamlit.app](https://adaptive-rag-enterprise.streamlit.app)
(free tier — click "wake it back up" if it shows the sleep screen)
· **API:** [adaptive-rag-enterprise.onrender.com](https://adaptive-rag-enterprise.onrender.com)
([/health](https://adaptive-rag-enterprise.onrender.com/health) ·
[/metrics](https://adaptive-rag-enterprise.onrender.com/metrics) ·
[/verify/2d96298969e7](https://adaptive-rag-enterprise.onrender.com/verify/2d96298969e7))

> **Proof in one click (no keys, no login, zero LLM tokens):**
> <https://adaptive-rag-enterprise.onrender.com/verify/2d96298969e7> —
> a live Path-A run whose receipt re-verifies deterministically
> (`verified: true`, `hash_ok: true`, `verbatim: true`). The 15/15
> white-whale receipt below is preserved as an immutable artifact in
> [`audit_bundle_white_whale/`](audit_bundle_white_whale/) — that one
> survives database resets; the live run does not.

---

## The Moat

Every certified answer carries a **cryptographic receipt**: claim → citation →
evidence span → page transcript → SHA-256 hash → source PDF anchor. An external
auditor can re-verify the entire chain offline, on an air-gapped machine, with
zero trust in this system.

```python
# Run this yourself — no API, no database, no trust required:
python verify_certificate.py certificate.json
# → PASS — chain verified offline (15/15 links, 0 citation issues)
# → signature attestation verified (Ed25519)
# → EDGAR re-verification: hash the source PDF and compare
```

**Five deterministic gates** run before the LLM auditor sees a single token —
each one zero-cost, each one born from a live-caught fabrication attempt:

| Gate | What it catches | Built because |
|---|---|---|
| Citation bounds | Fabricated footnotes pointing to non-existent evidence | [8]-vector tamper suite |
| Unit/scale assertion | "$500M" when the table says "$433M" | Live catch: GLM approximation |
| Growth direction | "Revenue grew" when it declined | XBRL crosscheck vs SEC facts |
| XBRL reconciliation | Claims contradicting official SEC ground truth, with figure-level metric ownership | Live catch: metric-noun swap $40,111M |
| Echo/injection guard | Prompt-injection echo, deliberation leakage | NEM reasoning model + adversarial probe |

*(The structured-output JSON-repair backstop — markdown-decorated output from
reasoning models — is a parsing-recovery mechanism in the engine path, not a
verification gate; listed here previously by mistake. Third-review finding,
2026-09-13.)*

**The invariant: zero fabricated certified answers across every battery, every
provider, every storm.** Not "we hope it doesn't lie" — *measured, with a
22-class regression ledger and a tamper-evidence proof artifact committed to
this repo.*

---

## Architecture

```
                        ┌─────────────────────────────────┐
                        │        User Question             │
                        └────────────┬────────────────────┘
                                     │
                         ┌───────────▼───────────┐
                         │   FastAPI Gateway      │
                         │  auth · rate limit     │
                         │  disconnect guard      │
                         │  metrics · SSE         │
                         └───────────┬───────────┘
                                     │
                     ┌───────────────▼───────────────┐
                     │     LangGraph Pipeline         │
                     │                               │
                     │  ┌─────────────────────────┐  │
                     │  │ Cache Check (semantic)  │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ Router + Premise Gate   │  │
                     │  │ (specialist pruning)    │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ Specialist Fleet (3∥)   │  │
                     │  │ financial · risk ·      │  │
                     │  │ product                 │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ Cross-Check Gate        │  │
                     │  │ (contradiction detect)  │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ Synthesis (120b)        │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ ╔═══════════════════╗   │  │
                     │  │ ║ 5 GATES           ║   │  │
                     │  │ ║ citation·scale·   ║   │  │
                     │  │ ║ growth·XBRL·echo  ║   │  │
                     │  │ ╚═══════════════════╝   │  │
                     │  │ Audit Guard (120b)      │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ Receipt Chain (SHA-256) │  │
                     │  └─────────────────────────┘  │
                     └───────────────────────────────┘
                                     │
                         ┌───────────▼───────────┐
                         │  Neon PostgreSQL       │
                         │  pgvector + RLS        │
                         │  receipts + XBRL      │
                         └───────────────────────┘
```

**Fail-closed by design:** if any gate rejects, any provider quota-walls, or
any stage degrades — the system *refuses* rather than certifies. It never
returns an unverified answer as if it were verified.

### V2 Phase 0 shipped — the span-anchored fact store (ADR-017)

The V2 refactor ([ARCHITECTURE_V2_PROPOSAL.md](ARCHITECTURE_V2_PROPOSAL.md))
inverts the pipeline: numbers are **looked up, not generated**. Phase 0 — the
foundation — is live:

- **`fact_rows`** (migration 006): every number anchored to exact PDF byte
  spans (`char_start/char_end` slicing `page_transcripts` byte-exactly) with
  company·metric·period bound **at write time** — a lookup cannot misbind.
- **Dual-key reconciliation** (Amendment 4): each consolidated fact row is
  checked against SEC-published XBRL at ingest (exact or ≤0.5% cross-scale).
  Agreement between the PDF span and the derived Q4 XBRL value also stamps
  `xbrl_facts.confirmed_by_pdf` — the B.1.5 three-way check. Gate 4 now
  *declines authority* over disconfirmed facts (fail-closed).
- **Fail-closed coverage** (B.1.1): Path A initially serves ONLY what two
  independent sources (PDF span + SEC XBRL) confirm — currently
  revenue + net income for Q4-2023 across all three companies. Everything
  else (EPS, segments, subtotals) is stored `unreconciled_fact` and served
  by the fleet only. Absence of contradiction is NEVER reconciliation.
- **Context exclusions** (B.1.3): pro-forma / as-previously-reported /
  restatement / GAAP-to-non-GAAP reconciliation pages and
  arithmetic-flagged chunks are excluded from extraction — the known
  mistagging categories are *excluded*, not eliminated (B.6.1 wording).
- **Routing guard** (A.4/B.1.2, pure functions + fuzz-tested): exact-match
  (entity, metric, period) resolution with atomic multi-entity demotion —
  any miss, ambiguity, or segment qualifier demotes the whole query to the
  fleet. Path A goes live only after the Phase-1 shadow gate.

Live state at epoch 9: 251 candidates extracted → 234 rows written,
8 reconciled (6 XBRL facts confirmed three-way), **0 span mismatches**,
verification pass 234/234 byte-exact. Re-run: `python db.py` (idempotent).

### V2 Phase 1 shipped — shadow mode (ADR-017)

Path A answers are now **built but never served**; every query is
measured against V1 in the disagreement ledger until the A.2 gate clears:

- **`fact_templates.py`** (A.5 ruling — pure f-strings, no engine): 3
  executive-grade shapes (single metric, prior-year comparative with
  computed YoY, multi-entity comparative). Every figure is re-matched
  inside the row's own span text — NUMERIC padding (`2.2700`) can never
  reach the answer; the page's own rendering (`2.27`) always does.
  Citations are bound to the row's own span, so the citation-bounds
  gate passes by construction. B.6.1 wording on every Path-A receipt:
  `deterministic_certification: known-context-exclusions-applied`.
- **`fact_shadow.py`** (the isolation contract): a post-audit graph node
  that builds the Path-A candidate, runs the deterministic gates over
  it, classifies V1-vs-V2 agreement (`agree_numeric / agree_partial /
  agree_refusal / disagree_value / disagree_shape`), and records the
  ledger — never raising, never mutating state, never serving. Both
  terminal paths (certified answer AND verified refusal) pass through it.
- **EPS dual-key coverage** (Phase-1 prep, live): EDGAR publishes no
  primary Q4 EPS fact (10-Qs cover Q1-Q3; the 10-K carries FY only), so
  Q4 EPS is derived FY−9mo exactly like revenue — reconciled within
  ±$0.01, a principled tolerance for cent-rounded arithmetic. **Tesla
  EPS (2.27 vs 2.26) reconciled; Meta EPS (5.33 vs 5.30) was
  DISCONFIRMED at 3 cents and stays off Path A** — the B.1.5 three-way
  check's first live catch. Apple EPS is honestly out of scope: its
  fiscal Q4 is calendar Q3, and the calendar-frame rule would misbind.
- **Receipt lineage** (migration 007, V3 item 0 pulled forward): every
  receipt records (model id, prompt hash) — attribution, never
  weight-freezing. Shadow forensics can name which model disagreed.
- **A.2 battery pre-registered** ([battery JSON](tests/battery_phase1_preregistered.json)):
  42 questions (14 exact, 8 alias, 4 multi-entity, 8 out-of-coverage,
  6 wrong-period/entity, 6 qualitative) + 15 corrupted-claim injections
  (incl. the mandated pro-forma variant). The deterministic routing
  matrix passes 42/42 zero-token. Nightly: `python scripts/battery_phase1.py --shadow`.
  The matrix already caught the interpretive-stem class live ("what
  drove Tesla's Q4 2023 net income growth?" now demotes — §2.4).

**Phase 1 exit gate (A.2, unchanged):** ≥98% agreement on covered
questions, 15/15 corrupted-claim catches, zero fabricated certified
answers, 7 consecutive green nights. `RAG_FACT_FASTPATH` stays unset.

---

## Measured Results (5 batteries · 3 providers · 30+ runs)

| Battery | Recall | Certified | Fabrications | Gold accuracy |
|---|---|---|---|---|
| Day-2 (baseline) | 22% | 2/10 | 0 | — |
| Day-5 (post-fixes) | **90%** | 9/10 | **0** | 100% |
| Day-6 (final, 13 questions) | **80%** | 8/10 | **0** | — |

**22 regression classes** — every bug found by live adversarial testing,
fixed, and locked with a named test. Full ledger: [KNOWN_ISSUES.md](KNOWN_ISSUES.md)

**The white-whale receipt** (Apple-vs-Meta comparison — the hardest question,
refused in 4 consecutive batteries before certifying): run `1efc9fe87875`,
**15/15 cryptographic links verified**. It is preserved in
[`audit_bundle_white_whale/`](audit_bundle_white_whale/) as a committed,
immutable artifact — verify it yourself, offline, in one command:

```bash
cd audit_bundle_white_whale && python verify_certificate.py
# → PASS — chain verified offline (15/15 links ok, 0 citation issues)  [exit 0]
```

> **Why the bundle and not a live link:** that receipt lives in corpus
> epoch 9. The production database was later rebuilt (the current corpus
> is epoch 2), so `/verify/1efc9fe87875` correctly 404s — the row is gone.
> The committed bundle is the durable form of the same proof, and it is
> the one an outside auditor would actually be handed. Live receipts are
> ephemeral by nature; anchor to the artifact.

---

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in keys/URLs
python ingest.py                # corpus → Neon (manifest-driven, atomic)
uvicorn main:app --port 8000 --workers 1   # gateway
streamlit run app.py            # executive client (second terminal)
```

Docker: `docker compose up --build` (backend :8000, frontend :8501).

> **Why `--workers 1`**: rate buckets, Prometheus counters, and the run
> semaphore are in-process state; extra workers would double the effective
> rate limit and split `/metrics`. The app is fully async — one worker
> saturates the LLM concurrency budget.

The production image **bakes the embedding + reranking ONNX models in at
build time** (`FASTEMBED_CACHE_PATH=/opt/models/fastembed`): a fresh
container's first request is a warm semantic-cache hit in ~7s.

---

## Executive Console (`frontend/`)

The production frontend is a **React 19 + Vite + Tailwind v4** single-page
app (dark theme, ~77 KB gzipped): live animated pipeline rail driven by the
SSE `transition` stream, per-claim `DETERMINISTIC` vs `LLM AUDIT` verifier
badges from the receipt, health telemetry, and fail-closed refusal framing.
The Streamlit client (`app.py`) remains as the lightweight fallback.
Dev/deploy: see [frontend/README.md](frontend/README.md); captured flow
evidence in [docs/screenshots/](docs/screenshots/).

## Gateway API (`main.py`)

| Route | Method | Purpose |
|---|---|---|
| `/query` | POST | JSON answer. Disconnect-aware: client abort cancels the run (499). Requires `X-API-Key` when `QUERY_API_KEYS` is set. |
| `/query/stream` | GET | SSE: `start` → `transition`×N → `result` \| `error`. Same auth. |
| `/search` | POST | Raw hybrid search (RRF fusion), tenant-scoped. No LLM. |
| `/verify/{run_id}` | GET | 🧾 **Verification receipt** — deterministic re-verify, zero LLM tokens. |
| `/export/{run_id}` | GET | 🔏 **Compliance Audit Bundle** — offline-verifiable zip (receipt + evidence + transcripts + Ed25519 attestation + stdlib verifier). |
| `/feedback` | POST | 👎 Poison-pill cache eviction. Requires `X-Admin-Key`. |
| `/health` | GET | Orchestration + DB health. |
| `/live` / `/ready` | GET | K8s liveness / readiness probes. |
| `/metrics` | GET | Prometheus text exposition. |

---

## Honest Limitations

Full ledger: [KNOWN_ISSUES.md](KNOWN_ISSUES.md) · Roadmap: [GAP_ANALYSIS.md](GAP_ANALYSIS.md)

- **The retrieval blind spot**: the audit verifies drafts against retrieved
  evidence — text never retrieved cannot contradict a draft. Every RAG system
  has this hole; this system *exposes* it via per-answer receipts.
- **Recall is 50-90%, not 100% — and the docs say so**: clean-window batteries measured 80-90% recall@answerable; live runs under provider weather (TPD walls, failover drafting variance) ranged 50-67% across four battery runs — every miss named, none hidden (KNOWN_ISSUES #1)
  (capacity walls, gate false-rejects, format variance). The fabrications
  count is the invariant, not the recall.
- **Hand-tuned lexicons**: the metric families, clause breakers, and % -Change
  patterns are SEC-English-specific. The architecture generalizes; the
  lexicons are re-derived per domain.
- **Single-worker deployment**: rate buckets and metrics are in-process.
  Redis is the prepared path (ADR-016) — trigger-gated on load-test results.

---

## Documentation

| Document | What it contains |
|---|---|
| [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) | ADRs 005-016: every design decision with evidence and rejected alternatives |
| [ARCHITECTURE_V2_PROPOSAL.md](ARCHITECTURE_V2_PROPOSAL.md) | ADR-017: the V2 deterministic-first refactor — spec of record (Appendices A+B), Phase 0 shipped |
| [V3_ROADMAP.md](V3_ROADMAP.md) | The capped horizon: CRO-review dispositions (Figure-DAG checker-only, tiered cross-filing diff, bi-temporal deterministic-layer-first) + sequenced V3 items |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | 22-class fixed ledger + 5 open problems + epistemic limits + operational posture |
| [GAP_ANALYSIS.md](GAP_ANALYSIS.md) | Version 2.0 roadmap: 12 prioritized gaps, score projection 63→90+ |
| [.env.example](.env.example) | Every configuration variable, annotated |

---

## Testing

```bash
pytest tests/ -q --ignore=tests/test_answer_accuracy.py --ignore=tests/test_app.py
# → 386 passed (offline + DB-integration when reachable; deterministic, CI-safe)
```

| Suite | Tests | What it proves |
|---|---|---|
| test_fact_extract.py | 47 | Phase-0 fact store: span-anchored extraction (real Apple/Meta/Tesla corpus fixtures), fail-closed column binding, B.1.1/B.1.3/B.1.5 dispositions, exact-match routing guard + interpretive-stem demotion + fuzz operators, live end-to-end sync+verify (integration-marked) |
| test_fact_templates.py | 13 | Phase-1 templates (span-verbatim figures, NUMERIC-padding guard), shadow executor isolation contract, agreement classifier, receipt lineage, live ledger round-trip (integration-marked) |
| test_ledger_fidelity.py | 11 | Citation-ledger fidelity: the detector fires on the live-observed 14-cited-vs-10-listed shape, the deterministic rebuild removes it, is idempotent, never fabricates a source, and leaves the prose byte-identical |
| test_tamper.py | 17 | Receipt chain survives span shifts, hash forgeries, relabeling, OOB |
| test_failover.py | 37 | Quota cooldowns, peer rescue, timeout handling, circuit ownership |
| test_contradictions.py | 36 | Scale normalization, GAAP/non-GAAP basis, period binding |
| test_receipt.py | 30 | Bullet fidelity, injection guard, channel completeness, signature |
| test_fuzz.py | 3 | 1,000 seeded mutations across 11 forgery operators, zero false accepts |
| test_chaos.py | 7 | 6 dependency-kill contracts + the fail-closed meta-contract |
| test_offline_bundle.py | 5 | Offline verifier equivalence (17 tamper vectors) |
| test_main.py | 36 | Auth middleware, SSE, rate limiting, gateway API |
| test_units.py | 19 | Scale engine, growth direction, unit assertions |
| test_consistency.py | 50+ | XBRL ownership, basis splits, prompt compression, year binding |
| test_guard_preaudit.py | ~10 | Echo guard, injection guard, citation pre-audit |
| test_multiquery.py | 11 | Conditional expansion, per-entity fan-out |
| test_tables.py | 14 | Table extraction, arithmetic verification |
| test_db.py | 10 | RLS isolation, pool, migration, epoch partitioning |

CI (`.github/workflows/ci.yml`): unit → pgvector-16 service integration → Docker build.

### Six diagrams — rendered from evidence, not from prose

**Start here:** `.archify/index.html` — a landing page linking all six, with the
combined map at the top. Each one is an interactive, searchable, traceable page
(open in a browser: search nodes, trace routes, switch views, export) and each is
a **single self-contained file** — no server, no CDN, no external references.

| Diagram | Type | Answers | Evidence pins | Artifact |
|---|---|---|---|---|
| **The whole system** (combined) | `architecture` | Everything on one canvas — ingestion, query path, gates, outcomes, proof | 23 | `architecture-complete-*/adaptive-rag-complete.html` |
| Runtime architecture | `architecture` | What talks to what, and where each box lives in the code | 12 | `architecture-runtime-*/adaptive-rag-runtime.html` |
| Gate gauntlet | `workflow` | Which deterministic checks run before the model is trusted, and where a run fails closed | 12 | `workflow-gate-gauntlet-*/gate-gauntlet.html` |
| Query lifecycle | `sequence` | One question start to finish, with the early exits that skip the model | 8 | `sequence-query-lifecycle-*/query-lifecycle.html` |
| Ingestion lineage | `dataflow` | Where every figure comes from — PDFs and SEC XBRL down to the vector store | 8 | `dataflow-ingestion-lineage-*/ingestion-lineage.html` |
| Run outcomes | `lifecycle` | Every state a run can end in, including the ones that never reach a user | 7 | `lifecycle-run-outcomes-*/run-outcomes.html` |

All six passed the same gate suite on the committed revision:

| Gate | combined | architecture | workflow | sequence | dataflow | lifecycle |
|---|---|---|---|---|---|---|
| `validate` (schema + composition + label clearance) | **pass** | **pass** | **pass** | **pass** | **pass** | **pass** |
| `deliver` | **pass** | **pass** | **pass** | **pass** | **pass** | **pass** |
| `check` (HTML/SVG structure, provenance) | **pass** | **pass** | **pass** | **pass** | **pass** | **pass** |
| `browser-check` (real Chrome, projected-text sizes) | skipped¹ | skipped¹ | skipped¹ | skipped¹ | skipped¹ | skipped¹ |

¹ No Chrome or Chromium binary exists in this sandbox, so the visual gate never
ran on any of the six. They are schema-, layout- and structure-valid, and every
label was checked *geometrically* for clearance by the validator (it flagged —
and we fixed — labels that were too wide for their boxes, messages closer than
28px, a 7px micro-segment, edges routing through unrelated nodes and label/route
clearance violations). Nobody has confirmed the projected-text sizes in a real
browser. Run `archify browser-check <output.html>` where Chrome exists to close
that gap.

The gate order is fixed and each stage must pass before the next runs:
`validate → deliver → check → browser-check`. A skipped check is recorded as
`skipped`, never as a pass — the receipts (`*.finalize-summary.json`) carry the
per-gate result and the exact diagnostics behind any failure.

They are not hand-drawn pictures. Nodes carry `sources` pinned to
repository-relative files and line numbers at a frozen commit
(`meta.repository.revision`), so every asserted component is checkable against
the code at that revision. For example the architecture map's `gates` cites
`adaptive_rag.py:2934` (`citation_pre_audit`), `store` cites `db.py:1143`
(`pgvector_hybrid_search`), and the workflow's paired gate nodes cite the six
checks in the order the source calls them — documents-present `2953`,
`citation_pre_audit` `2934`, `_ECHO_MARKERS` `2983`, `assert_claim_scales`
`2013`, `check_growth_claims` `2175`, `check_xbrl_figures` `2350` — followed by
`fact_checker_guard` `2948`.

The combined map is a single `architecture` spec that unions the five views —
19 components, 18 connections — rendered onto one 1778×926 canvas. It declares
no `viewBox`, so the renderer sizes the canvas itself and asks the reader to
scroll a complete architecture at comfortable text sizes rather than shrink a
semantically rich graph down to an emergency floor. It is the orientation view;
the five focused diagrams remain the detail.

All six were produced with [Archify](https://github.com/tt-a1i/archify) (MIT).
Regenerating any of them is one command per diagram, run from the repository
root:

```bash
node <archify>/bin/archify.mjs finalize <type> \
  .archify/<dir>/candidate.json .archify/<dir>/<name>.html \
  --quality showcase --repo-root . --json
```

Archify never lets the agent draw: the typed JSON IR is the source of truth, the
renderer is deterministic, and the gates run in a fixed order —
`validate → deliver → check → browser-check`. `ok: true` requires all four; a
non-zero exit is never a success. The diagnostics speak in exact numbers
(`clearGapPx`, `minimumGapPx`, `labelWidthPx`, `projectedFontPx`), which is why
the failures above were fixable rather than arguable.

### Pipeline audit — evidence per stage, not a green checkmark

`pytest` proves the units work. It does not prove the *pipeline* works, end to
end, in the order a request actually travels. Two commands close that gap:

```bash
python scripts/pipeline_audit.py            # 39 checks, 9 stages, zero LLM tokens
python scripts/pipeline_audit.py --json     # machine-readable (CI gate; exit 1 on any FAIL)
python scripts/pipeline_audit_mutations.py  # 10 injected defects — can the audit go red?
python scripts/local_stack_bootstrap.py     # local Postgres + corpus (audit prerequisites)
```

Every check prints the **observed value** as its evidence, so the claim and the
proof sit on the same line:

```
[2-pathA] covered triples serve at ZERO LLM tokens
      → 5/5 served — Apple:ok, Apple:ok, Tesla:ok, Tesla:ok, Meta:ok
[1-cache] replay restores documents (sources), not just the answer
      → cached_hit=True, documents restored=True
[8-serving] /query/stream forwards the RESOLVED tenant (not the raw param)
      → passes resolved tenant=True; still passes raw tenant_id=False
```

A check that cannot run reports **SKIP** (visible), never a silent pass. Last
full run: **38/38 PASS, 0 FAIL, 0 SKIP**.

The mutation controls exist because a green audit proves nothing until it can
go red. Each control injects a realistic regression — a dropped tenant
predicate, an inverted growth gate, a fixed `top_k` that reintroduces the
15→5 evidence-pool bug, a verifier that blesses broken links — into a
throwaway copy and asserts the matching check flips to FAIL. Last run:
**9/9 CAUGHT**.

Two controls are fixture-level *by design*: Path A and tenant-scoped caching
are defended in depth (forcing `path='fact'` for every query still demotes,
because `build_path_a_answer` independently declines; deleting the cache's
`WHERE tenant_id` predicate still cannot leak, because row-level security
sits beneath it). No single-line mutation can violate either guard, so the
controls mutate the audit's own fixtures to prove those assertions actually
fire. Both facts are documented at the control site.

---

## Configuration

Everything is env-driven — see [`.env.example`](.env.example) for the full
annotated surface (`DB_*`, `RAG_*`, provider keys, `PORT`,
`SERVICE_RATE_LIMIT_PER_MIN`, `ADMIN_API_KEY`, `LOG_FORMAT`, `API_BASE_URL`,
`QUERY_API_KEYS`, `RAG_EXEC_PEER_FAILOVER`, `RAG_SPECIALIST_PRUNING`,
`RAG_REASONING_HEADROOM`, `RAG_QUOTA_ABORT_S`, `RAG_EXPANSION_CONFIDENCE`).
