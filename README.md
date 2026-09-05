# Adaptive RAG — Enterprise Financial Intelligence Platform

![CI](https://github.com/karamat552/adaptive-rag-enterprise/actions/workflows/ci.yml/badge.svg)

**🚀 Live demo:** [adaptive-rag-enterprise.streamlit.app](https://adaptive-rag-enterprise.streamlit.app)
· **API:** [adaptive-rag-enterprise.onrender.com](https://adaptive-rag-enterprise.onrender.com)
([/health](https://adaptive-rag-enterprise.onrender.com/health) ·
[/metrics](https://adaptive-rag-enterprise.onrender.com/metrics))

Self-correcting, async-first multi-agent RAG over SEC 10-Q filings (Tesla · Apple · Meta),
with a zero-trust fact-checking audit, **five deterministic zero-token gates** (citation bounds · unit/scale · growth-direction vs the filing's own comparative columns · **XBRL crosscheck against SEC-published facts** · tamper-evident receipt chain), **tamper-evident verification receipts**, epoch-guarded
semantic cache, and a hardened FastAPI serving layer built for Enterprise K8s/Docker.

> Design rationale and telemetry-gated roadmap: [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md)

```
app.py (Streamlit) ──SSE──▶ main.py (FastAPI gateway)
                              │  rate limit · auth · disconnect guard · metrics · probes
                              ▼
                         adaptive_rag.py (LangGraph v3.2 Maker-Checker)
                              │  cache_check → gateway → fleet(3∥) → synth → audit
                              ▼
                         db.py (v2.4) ──▶ Neon PostgreSQL + pgvector + RLS (app_rag)
```

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in keys/URLs
python ingest.py                # corpus → Neon (manifest-driven, atomic)
uvicorn main:app --port 8000 --workers 1   # gateway
streamlit run app.py            # executive client (second terminal)
```

Docker: `docker compose up --build` (backend :8000, frontend :8501).

> **Why `--workers 1`**: rate buckets, Prometheus counters, and the run semaphore are
> in-process state; extra workers would double the effective rate limit and split
> `/metrics`. The app is fully async — one worker saturates the LLM concurrency budget.

The production image **bakes the embedding + reranking ONNX models in at build time**
(`FASTEMBED_CACHE_PATH=/opt/models/fastembed`): a fresh container's first request is a
warm semantic-cache hit in ~7s — never an ONNX download racing the DB timeout.

## Gateway API (`main.py`)

| Route | Method | Purpose |
|---|---|---|
| `/query` | POST | JSON answer. Disconnect-aware: client abort cancels the run (499). |
| `/query/stream` | GET | SSE: `start` → `transition`×N (node labels) → `result` \| `error`. Keep-alive comments every 15 s defeat proxy idle timeouts. |
| `/search` | POST | Raw hybrid search (RRF fusion), tenant-scoped. No LLM. |
| `/verify/{run_id}` | GET | 🧾 **Verification receipt** for a grounded run: claim list + evidence chain + deterministic on-demand re-verify (slice stored page transcripts, re-hash `company⊣source⊣page⊣slice`, compare to `chunk_hash`). Zero LLM tokens; tamper-evident by construction. 404 for refusals/unverified runs. Response includes page transcripts, per-chunk DB attribution truth, and the source-PDF SHA registry — the data behind the Streamlit **🔏 Prove it** explorer. |
| `/feedback` | POST | 👎 poison-pill cache eviction. Requires `X-Admin-Key` (fail-closed 503 if unset, 403 on wrong key). |
| `/health` | GET | Orchestration + DB health (status, epoch, chunks, cache entries). |
| `/live` | GET | K8s **liveness** — process-alive only, deliberately dependency-free. |
| `/ready` | GET | K8s **readiness** — 200 iff DB reachable **and** LLM circuit not open; else 503. |
| `/metrics` | GET | Prometheus text exposition. |

```bash
curl -X POST localhost:8000/query -H "Content-Type: application/json" \
     -d '{"question": "What were Apple Products vs Services revenue in Q4 2023?"}'

curl -N "localhost:8000/query/stream?question=Compare+Apple+and+Meta+revenue"   # watch transitions

curl localhost:8000/verify/$RUN_ID   # 🧾 receipt: claims + evidence + deterministic re-verify

curl -X POST localhost:8000/feedback -H "X-Admin-Key: $ADMIN_API_KEY" \
     -d '{"question": "What were Apple Products vs Services revenue in Q4 2023?"}'
```

`X-Request-ID` (minted per request, or honored from an upstream gateway) equals the
LangGraph `run_id` and is stamped on every response **and every JSON log line**.

## Executive Client (`app.py` v2)

Chat-style production UI with the same token discipline as the gateway — the LLM fires
only on an explicit submit; every other interaction re-renders from session state.

- **Live pipeline visualization** — SSE node transitions stream into a status panel
  (cache check → routing → specialist fleet → synthesis → compliance audit).
- **Audited answers** — outcome / grounded / latency / token metrics, inline `[n]`
  footnotes, `Evidence [x]` fold-downs index-aligned with the citations, and a
  cached-answer badge so you always know what you're looking at.
- **👎 Flag Inaccurate** — one click evicts the semantic-cache radius for that question
  (X-Admin-Key) and marks the transcript so it can't be re-flagged.
- **One-click suggested questions** (demo-ready), `.md` report export, recent-runs table.
- **Connection-aware** — degraded-gateway banner with remediation hints, per-error
  messaging; failed runs are never cached so a retry is always clean.
- **Responsive + themed** — dark emerald theme in `.streamlit/config.toml`, telemetry
  off, mobile CSS; verified headlessly by `tests/test_app.py` (Streamlit AppTest).

## Production Hardening Pack

- **Per-IP token bucket** (`SERVICE_RATE_LIMIT_PER_MIN`) — refills per minute; stale
  buckets swept after 10 min so memory can't grow unbounded. Probes/metrics exempt.
- **Ghost-request mitigation** — both `/query` and `/query/stream` poll the socket
  every 0.5 s *even during silent LLM stages*; a disconnect cancels the task and
  `aclose()`s the astream, so in-flight Groq/Gemini calls stop burning tokens
  immediately. Counted in `client_disconnects_total`.
- **Metrics** — `queries_total{outcome,cached}`, `cache_hits_total`,
  `llm_tokens_spent_total{direction=input|output}`, `evictions_total`,
  `rate_limited_total`, `client_disconnects_total`, `feedback_rejected_total`,
  `http_requests_total{status}`, `rag_query_latency_seconds` (histogram buckets).
- **Graceful shutdown** — SIGTERM → lifespan → `close_pool()` (plus the `atexit`
  backstop in `db.py`).
- **Structured logs** — `LOG_FORMAT=json` → one JSON object per line, every record
  carrying `request_id` via `contextvars` (works for logs raised inside the
  orchestrator/db during a request).
- **Security headers** — `X-Content-Type-Options: nosniff`, `Referrer-Policy`.
- **Citation pre-audit** (orchestrator) — out-of-range `[n]` footnotes are fabricated
  by construction; `fact_checker_guard` rejects them deterministically *before* the
  120B auditor is invoked (zero tokens, zero false-positive risk — number
  substantiation deliberately stays with the LLM auditor).

### K8s probes

```yaml
livenessProbe:  { httpGet: { path: /live,  port: 8000 }, periodSeconds: 10 }
readinessProbe: { httpGet: { path: /ready, port: 8000 }, periodSeconds: 15,
                  failureThreshold: 2 }   # 503 while DB down or LLM circuit open
```

## Evaluation harness (`eval.py`)

| Suite | What it proves | Cost |
|---|---|---|
| `canary` | **Auditor liveness**: verified Apple baseline ($22.314B Services) is tampered programmatically (+$1M, +$1B, fake `[99]` citation, cross-company transplant) and re-audited by the *live* `fact_checker_guard`. Every tampered draft must come back `grounded=False` **and** must not poison the semantic cache; the clean control must pass and cache-write. | ~4 audit LLM calls |
| `gold` | Factual accuracy vs. figures verified against the filings; honest refusals skip, hallucination fails. | `--limit` runs |
| `ragas` | Faithfulness / Context Precision / Answer Relevancy (ragas library when importable, else the built-in native judge — same metrics, formulas, thresholds). Cache-clean runs. | `--limit` runs + judge calls |

```bash
python eval.py --suite canary                    # deterministic, cheapest gate
python eval.py --suite all --limit 3 --report eval_report.json
```

Exit codes: `0` pass · `1` failures/skips · `2` harness error. The report records
corpus sha256, corpus epoch, provider/models, and total tokens spent.

## Testing

```bash
pytest tests/test_main.py -v          # gateway + eval units (fully mocked, CI-safe)
pytest tests/test_db.py -m "not integration"
pytest tests/test_db.py               # integration (live Neon, RLS fail-closed, cache)
```

CI (`.github/workflows/ci.yml`): unit → pgvector-16 service integration (RLS-bound
role via `scripts/bootstrap_roles.py`) → Docker image build.

## Verification (2026-08-29, live run)

- **pytest: 43/43 green** — DB units + live-Neon integration (RLS fail-closed, cache roundtrip,
  incremental migration) + 23-case gateway ASGI suite (auth, rate limiting, SSE shaping,
  probes, metrics) + 8 deterministic citation pre-audit units + 2 Streamlit AppTest UI smokes.
- **Live gateway smoke: all pass** — `/live` `/ready` probes; unified `/query` contract with
  `X-Request-ID == run_id`; SSE without `tenant_id` (former crash path); mid-stream client
  abort → run cancelled, `client_disconnects_total +1`, remaining fleet/synth/audit LLM calls
  never issued; `/feedback` without key → 403 fail-closed.
- **`eval.py --suite canary`: 8/8** — the live auditor rejected all four tampered variants
  (+$1M, +$1B, fabricated `[99]` citation with 91.7% margin, Apple→Tesla transplant),
  zero cache poison, clean control certified and cached. ~33k audit tokens total.
- **`eval.py --suite gold`: 1/1**, exit 0.

## Configuration

Everything is env-driven — see [`.env.example`](.env.example) for the full annotated
surface (`DB_*`, `RAG_*`, provider keys, `PORT`, `SERVICE_RATE_LIMIT_PER_MIN`,
`ADMIN_API_KEY`, `LOG_FORMAT`, `API_BASE_URL`).
