# Adaptive RAG — Enterprise Financial Intelligence Platform

![CI](https://github.com/karamat552/adaptive-rag-enterprise/actions/workflows/ci.yml/badge.svg)

**311 tests · 90% recall · 0 fabrications · every certification AND refusal crypto-receipted · 22 regression classes · 4 provider lanes**

> I built this to answer one question: *can an AI system prove — cryptographically,
> deterministically, without trust — that every number it outputs came from a
> verified source?*

**🚀 Live demo:** [adaptive-rag-enterprise.streamlit.app](https://adaptive-rag-enterprise.streamlit.app)
· **API:** [adaptive-rag-enterprise.onrender.com](https://adaptive-rag-enterprise.onrender.com)
([/health](https://adaptive-rag-enterprise.onrender.com/health) ·
[/metrics](https://adaptive-rag-enterprise.onrender.com/metrics) ·
[/verify/{run_id}](https://adaptive-rag-enterprise.onrender.com/verify/1efc9fe87875))

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
| Structured-output | Markdown-decorated output that breaks JSON parsing | GLM-5.3-free reasoning model |
| Echo/injection guard | Prompt-injection echo, deliberation leakage | NEM reasoning model + adversarial probe |

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
**15/15 cryptographic links verified**, exports as an offline-verifiable
compliance bundle (see `audit_bundle_white_whale/`).

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
- **Recall is 80-90%, not 100%**: every miss is documented with its root cause
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
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | 22-class fixed ledger + 5 open problems + epistemic limits + operational posture |
| [GAP_ANALYSIS.md](GAP_ANALYSIS.md) | Version 2.0 roadmap: 12 prioritized gaps, score projection 63→90+ |
| [.env.example](.env.example) | Every configuration variable, annotated |

---

## Testing

```bash
pytest tests/ -q --ignore=tests/test_answer_accuracy.py --ignore=tests/test_app.py
# → 311 passed (offline, deterministic, CI-safe)
```

| Suite | Tests | What it proves |
|---|---|---|
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

---

## Configuration

Everything is env-driven — see [`.env.example`](.env.example) for the full
annotated surface (`DB_*`, `RAG_*`, provider keys, `PORT`,
`SERVICE_RATE_LIMIT_PER_MIN`, `ADMIN_API_KEY`, `LOG_FORMAT`, `API_BASE_URL`,
`QUERY_API_KEYS`, `RAG_EXEC_PEER_FAILOVER`, `RAG_SPECIALIST_PRUNING`,
`RAG_REASONING_HEADROOM`, `RAG_QUOTA_ABORT_S`, `RAG_EXPANSION_CONFIDENCE`).
