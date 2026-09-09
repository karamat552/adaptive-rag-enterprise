"""
Adaptive RAG — Production Serving Layer
========================================
  POST /query          -> JSON result (disconnect-aware: client abort cancels the run)
  GET  /query/stream   -> SSE node transitions + result (same cancellation semantics)
  POST /search         -> raw hybrid-search debugger (fusion_score passthrough)
  POST /feedback       -> poison-pill eviction (X-Admin-Key; fail-closed if unset)
  GET  /health         -> orchestration + DB health
  GET  /live           -> K8s liveness probe (process-alive only, dependency-free)
  GET  /ready          -> K8s readiness probe (DB reachable + LLM circuit closed)
  GET  /metrics        -> Prometheus text format (counters + latency histogram)

Ops (Production Hardening Pack):
  * per-IP token-bucket rate limit (SERVICE_RATE_LIMIT_PER_MIN; stale buckets swept)
  * ghost-request mitigation: a client disconnect cancels the LangGraph run and
    closes the astream, halting in-flight LLM token spend immediately — including
    during silent nodes (watchdog polls the connection between graph updates)
  * metrics: queries_total, cache_hits_total, llm_tokens_spent_total,
    evictions_total, rate_limited_total, client_disconnects_total,
    http_requests_total, rag_query_latency_seconds histogram
  * X-Request-ID == LangGraph run_id, threaded through every JSON log line via
    contextvars (an inbound X-Request-ID from an upstream gateway is honored)
  * graceful shutdown: SIGTERM -> lifespan -> close_pool()

.env: ADMIN_API_KEY=... (enables /feedback), SERVICE_RATE_LIMIT_PER_MIN=10,
LOG_FORMAT=text|json, PORT=8000
Run: uvicorn main:app --port 8000 --workers 1
     (single worker is intentional: rate buckets/metrics are in-process state)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from adaptive_rag import (
    CircuitOpenError,
    _get_semaphore,
    get_graph,
    get_health,
    get_settings,
)
from db import (
    close_pool,
    evict_from_semantic_cache,
    get_settings as db_get_settings,
    get_source_registry,
    get_verification_receipt,
    health_check,
    purge_expired_cache,
    verify_receipt_chain,
)

logger = logging.getLogger("RAGService")


# ============================== REQUEST-ID CONTEXT =========================
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


# ============================== JSON LOGS ==================================
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        p: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name,
            "message": record.getMessage(),
            "request_id": _request_id_var.get(),
        }
        if record.exc_info:
            p["exception"] = self.formatException(record.exc_info)
        return json.dumps(p, ensure_ascii=False)


if os.getenv("LOG_FORMAT", "text").lower() == "json":
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(_JsonFormatter())
    logging.getLogger().handlers = [_h]
    logging.getLogger().setLevel(logging.INFO)


# ============================== METRICS ====================================
_LAT_BUCKETS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0)


class _Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = {}
        self._lat_sum, self._lat_n = 0.0, 0
        self._lat_counts = [0] * len(_LAT_BUCKETS)

    def inc(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        if amount <= 0:
            return
        key = name
        if labels:
            lab = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{name}{{{lab}}}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe_latency(self, seconds: float) -> None:
        with self._lock:
            self._lat_sum += seconds
            self._lat_n += 1
            for i, bound in enumerate(_LAT_BUCKETS):
                if seconds <= bound:
                    self._lat_counts[i] += 1

    def render(self) -> str:
        with self._lock:
            lines = [f"{k} {v:g}" for k, v in sorted(self._counters.items())]
            if self._lat_n:
                for bound, cnt in zip(_LAT_BUCKETS, self._lat_counts):
                    lines.append(f'rag_query_latency_seconds_bucket{{le="{bound:g}"}} {cnt}')
                lines.append(f'rag_query_latency_seconds_bucket{{le="+Inf"}} {self._lat_n}')
                lines.append(f"rag_query_latency_seconds_sum {self._lat_sum:.3f}")
                lines.append(f"rag_query_latency_seconds_count {self._lat_n}")
        return "\n".join(lines) + "\n"


METRICS = _Metrics()


def _record_query_metrics(result: Dict[str, Any], elapsed: float) -> None:
    METRICS.inc("queries_total", outcome=result.get("outcome", "unknown"),
                cached="true" if result.get("cached") else "false")
    if result.get("cached"):
        METRICS.inc("cache_hits_total")
    usage = result.get("usage") or {}
    METRICS.inc("llm_tokens_spent_total", direction="input",
                amount=int(usage.get("input", 0)))
    METRICS.inc("llm_tokens_spent_total", direction="output",
                amount=int(usage.get("output", 0)))
    METRICS.observe_latency(elapsed)


# ============================== RATE LIMITER ===============================
_buckets: Dict[str, Dict[str, float]] = {}
_bucket_lock = threading.Lock()
_BUCKET_IDLE_S = 600.0     # drop per-IP buckets unused for 10 minutes
_SWEEP_INTERVAL_S = 300.0
_last_sweep = 0.0


def _rate_limit(ip: str) -> None:
    global _last_sweep
    limit = float(os.getenv("SERVICE_RATE_LIMIT_PER_MIN", "10"))
    now = time.monotonic()
    with _bucket_lock:
        if now - _last_sweep > _SWEEP_INTERVAL_S:
            _last_sweep = now
            stale = [k for k, b in _buckets.items()
                     if now - b["ts"] > _BUCKET_IDLE_S]
            for k in stale:
                del _buckets[k]
        b = _buckets.setdefault(ip, {"tokens": limit, "ts": now})
        b["tokens"] = min(limit, b["tokens"] + (now - b["ts"]) * (limit / 60.0))
        b["ts"] = now
        if b["tokens"] < 1.0:
            METRICS.inc("rate_limited_total")
            raise HTTPException(status_code=429,
                                detail="Rate limit exceeded — retry shortly")
        b["tokens"] -= 1.0


# ============================== MODELS =====================================
class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    tenant_id: Optional[str] = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    category_filter: Optional[str] = None
    company_filter: Optional[str] = None
    top_k: int = Field(5, ge=1, le=20)
    tenant_id: Optional[str] = None


class FeedbackRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    tenant_id: Optional[str] = None   # None -> purge ALL tenants (safety first)


# ============================== ADMIN AUTH =================================
def require_admin(x_admin_key: Optional[str] = Header(default=None)) -> None:
    expected = os.getenv("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=503,
                            detail="Feedback disabled: ADMIN_API_KEY not configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
        METRICS.inc("feedback_rejected_total")
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key")


# ============================ QUERY AUTHENTICATION =========================
# Tenant authentication (2026-09-08): RLS isolates tenants' DATA from each
# other, but identity was client-declared — any anonymous caller could pose
# as any tenant and burn its LLM budget. QUERY_API_KEYS closes that gap the
# same way ADMIN_API_KEY does for /feedback:
#   QUERY_API_KEYS='tenant-a:key1,tenant-b:key2'  (comma-separated tenant:key)
# - Header X-API-Key: <key> -> the tenant bound to that key (client may no
#   longer declare a DIFFERENT tenant_id — declared id must match the key's).
# - QUERY_API_KEYS unset -> OPEN MODE (local/demo/dev): every caller is the
#   default tenant, behavior unchanged. This mirrors the bootstrap posture
#   of the rest of the service and keeps the Streamlit demo working.
def _query_api_keys() -> Dict[str, str]:
    """Parse QUERY_API_KEYS -> {api_key: tenant_id}. Malformed entries fail
    LOUD at first use (config error, not runtime) as an explicit 500 —
    never silently open and never silently closed."""
    raw = os.getenv("QUERY_API_KEYS", "").strip()
    if not raw:
        return {}
    keys: Dict[str, str] = {}
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise HTTPException(
                status_code=500,
                detail=f"Server misconfiguration: QUERY_API_KEYS[{i}] must "
                       f"be 'tenant:key' — got {entry!r}")
        tenant, key = entry.split(":", 1)
        if not tenant or not key:
            raise HTTPException(
                status_code=500,
                detail=f"Server misconfiguration: QUERY_API_KEYS[{i}] must "
                       f"be 'tenant:key' — got {entry!r}")
        keys[key.strip()] = tenant.strip()
    return keys


def require_query_key(x_api_key: Optional[str] = Header(default=None),
                      tenant_id: Optional[str] = None) -> Optional[str]:
    """FastAPI dependency for /query + /query/stream. Returns the
    AUTHENTICATED tenant id (or None in open mode). The body's tenant_id
    is NOT visible to a dependency (FastAPI resolves this signature's
    tenant_id as a query param) — the ENDPOINT enforces the mismatch rule
    after this returns: the key IS the identity; a declared tenant that
    disagrees with the key's binding is impersonation and is rejected."""
    keys = _query_api_keys()
    if not keys:
        return None                    # open mode — no QUERY_API_KEYS configured
    METRICS.inc("auth_checked_total")
    if not x_api_key:
        METRICS.inc("auth_rejected_total")
        raise HTTPException(status_code=401,
                            detail="Missing X-API-Key (QUERY_API_KEYS is enforced)")
    bound = keys.get(x_api_key)
    if bound is None:
        METRICS.inc("auth_rejected_total")
        raise HTTPException(status_code=403, detail="Invalid API key")
    return bound


def _assert_tenant_match(auth_tenant: Optional[str],
                         declared: Optional[str]) -> None:
    """Endpoint-side impersonation check (the dependency cannot see the
    body): a declared tenant that disagrees with the key's binding is a
    cross-tenant attack — 403."""
    if auth_tenant and declared and declared != auth_tenant:
        METRICS.inc("auth_rejected_total")
        raise HTTPException(
            status_code=403,
            detail=f"Key is not authorized for tenant '{declared}' "
                   f"(bound to '{auth_tenant}')")


# ============================== RUN PLUMBING ===============================
def _resolve_tenant(tenant_id: Optional[str]) -> str:
    # db.Settings owns the default; RagSettings (adaptive_rag) has no such field.
    return tenant_id or db_get_settings().default_tenant


def _shape_result(final: Dict[str, Any], run_id: str, elapsed: float) -> Dict[str, Any]:
    """Canonical result dict — identical shape for POST /query and SSE `result`."""
    return {
        "run_id": final.get("run_id") or run_id,
        "answer": final.get("final_executive_report", ""),
        "outcome": final.get("outcome", "unknown"),
        "grounded": bool(final.get("grounded", False)),
        "cached": bool(final.get("cached_hit", False)),
        "degraded_agents": final.get("degraded_agents", []),
        "sources": final.get("documents", []),
        # Cache-replay provenance: run_id whose receipt proves this answer.
        "provenance_run_id": final.get("provenance_run_id") or final.get("run_id") or run_id,
        "usage": {"input": final.get("usage_in", 0),
                  "output": final.get("usage_out", 0),
                  "total": final.get("usage_total", 0),
                  "llm_calls": final.get("llm_calls", 0)},
        "latency_s": round(elapsed, 2),
    }


async def _execute_run(question: str, tenant_id: Optional[str],
                       run_id: str) -> Dict[str, Any]:
    """One graph execution (admission-controlled) — the only run path in the app."""
    t0 = time.perf_counter()
    async with _get_semaphore():
        final = await get_graph().ainvoke(
            {"original_question": question, "retry_count": 0,
             "run_id": run_id, "tenant_id": _resolve_tenant(tenant_id)},
            config={"recursion_limit": 25})
    return _shape_result(final, run_id, time.perf_counter() - t0)


# ============================== APP / LIFESPAN =============================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Service starting — provider=%s", get_settings().provider)
    # MODEL-BOOT SMOKE TEST (2026-09-07): free-tier catalogs rotate without
    # notice (live lesson: two mid-project model 404s broke every run until
    # defaults were updated). A 1-token probe per configured stage model at
    # startup turns 'silently broken provider' into a loud, immediate,
    # named-model error. RAG_SKIP_BOOT_SMOKE=1 disables (offline/CI runs).
    if os.getenv("RAG_SKIP_BOOT_SMOKE") != "1" and get_settings().provider != "openai_compatible":
        try:
            import adaptive_rag as _ar
            await asyncio.wait_for(
                asyncio.to_thread(_ar.boot_smoke_test), timeout=90)
        except Exception as exc:
            logger.warning("BOOT SMOKE FAILED — a configured model is not "
                           "serving; fix RAG_*_MODEL before trusting answers: %s", exc)
    try:
        n = await asyncio.to_thread(purge_expired_cache)
        logger.info("Startup TTL purge: %d entries removed.", n)
    except Exception as exc:
        logger.warning("Startup purge skipped: %s", exc)
    yield
    logger.info("Shutting down (SIGTERM/STOP) — closing DB pool.")
    try:
        await asyncio.to_thread(close_pool)
        logger.info("DB pool closed cleanly. Goodbye.")
    except Exception as exc:
        logger.warning("Pool close issue (atexit backstop remains): %s", exc)


app = FastAPI(title="Adaptive RAG — Enterprise Financial Intelligence",
              version="2.1.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
                   allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
                   allow_methods=["*"], allow_headers=["*"])


# ============================== MIDDLEWARE =================================
@app.middleware("http")
async def tracing_and_limits(request: Request, call_next):
    # Honor an upstream gateway's ID; otherwise mint one. Threaded into logs
    # via contextvar for the whole request, including adaptive_rag/db records.
    request_id = (request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12])[:64]
    request.state.request_id = request_id
    ctx = _request_id_var.set(request_id)
    t0 = time.perf_counter()
    try:
        try:
            if request.url.path not in ("/health", "/metrics", "/",
                                         "/live", "/ready"):
                _rate_limit(request.client.host if request.client else "unknown")
            response = await call_next(request)
        except HTTPException as exc:
            METRICS.inc("http_requests_total", status=str(exc.status_code))
            return JSONResponse(status_code=exc.status_code,
                                content={"detail": exc.detail},
                                headers={"X-Request-ID": request_id})
        except Exception:
            METRICS.inc("http_requests_total", status="500")
            raise
        elapsed = time.perf_counter() - t0
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = f"{elapsed:.3f}s"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        METRICS.inc("http_requests_total", status=str(response.status_code))
        logger.info("[API] %s | %s %s | %d | %.2fs", request_id, request.method,
                    request.url.path, response.status_code, elapsed)
        return response
    finally:
        # Note: the StreamingResponse body task inherits a *copy* of the
        # context made while the ID was set, so SSE-generator logs keep it.
        _request_id_var.reset(ctx)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error | %s", getattr(request.state, "request_id", "-"))
    return JSONResponse(status_code=500, headers={
        "X-Request-ID": getattr(request.state, "request_id", "")},
        content={"error": "internal_error",
                 "request_id": getattr(request.state, "request_id", None)})


# ============================== DISCONNECT GUARD ===========================
_DISCONNECT_POLL_S = 0.5
_SSE_KEEPALIVE_S = 15.0


async def _run_guarded(request: Request, question: str,
                       tenant_id: Optional[str], run_id: str) -> Dict[str, Any]:
    """Runs the graph as a task; polls client connection. Disconnect -> cancel
    the task -> CancelledError propagates into in-flight LLM calls -> token
    spend for the ghost request stops immediately."""
    task = asyncio.create_task(_execute_run(question, tenant_id, run_id))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_DISCONNECT_POLL_S)
            if task in done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                METRICS.inc("client_disconnects_total")
                logger.warning("[%s] Client disconnected — run cancelled "
                               "(token spend halted).", run_id)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise HTTPException(status_code=499,
                                    detail="Client disconnected; run cancelled")
    except asyncio.CancelledError:
        task.cancel()
        raise


# ============================== POST /query ================================
@app.post("/query")
async def query(req: QueryRequest, request: Request,
                auth_tenant: Optional[str] = Depends(require_query_key)
                ) -> JSONResponse:
    run_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    # Authenticated tenant wins over the declared one (the key IS identity);
    # a declared tenant that disagrees with the key's binding is rejected.
    _assert_tenant_match(auth_tenant, req.tenant_id)
    tenant = auth_tenant or req.tenant_id
    try:
        result = await _run_guarded(request, req.question, tenant, run_id)
    except CircuitOpenError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise HTTPException(status_code=499, detail="Client disconnected")
    except Exception as exc:
        logger.exception("Query failed [%s]", run_id)
        raise HTTPException(status_code=500,
                            detail="Internal processing error — see server logs") from exc
    _record_query_metrics(result, result["latency_s"])
    return JSONResponse(content=result, headers={"X-Request-ID": run_id})


# ============================== GET /query/stream (SSE) ====================
_NODE_LABELS = {
    "cache_check": "Checking semantic cache...",
    "gateway": "Routing question...",
    "exec_db": "Spawning specialist fleet...",
    "cross_check": "Cross-checking specialists for contradictions...",
    "sharpen": "Contradiction found — sharpening retrieval...",
    "csuite_synth": "Synthesizing executive brief...",
    "validate": "Running compliance audit...",
    "rewrite": "Optimizing search query...",
    "abandon": "Answering from general knowledge...",
    "verified_refusal": "Verified refusal — could not ground answer.",
    "bad_req": "Out-of-domain query blocked.",
}


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(request: Request, question: str,
                        tenant_id: Optional[str], run_id: str
                        ) -> AsyncGenerator[str, None]:
    t0 = time.perf_counter()
    yield _sse("start", {"run_id": run_id})
    final: Dict[str, Any] = {}
    accounted = False   # flips True once a result/error event is delivered
    async with _get_semaphore():
        stream = get_graph().astream(
            {"original_question": question, "retry_count": 0,
             "run_id": run_id, "tenant_id": _resolve_tenant(tenant_id)},
            config={"recursion_limit": 25},
            stream_mode=["updates", "values"])
        try:
            aiter = stream.__aiter__()
            last_beat = time.monotonic()
            while True:
                # The next item is awaited as a Task so the poll loop below can
                # watch the socket during silent nodes (a 45s synthesis emits
                # nothing); on timeout the SAME task is re-awaited, never
                # cancelled, so the stream itself is left untouched.
                nxt = asyncio.ensure_future(aiter.__anext__())
                disconnected = False
                while True:
                    done, _ = await asyncio.wait({nxt}, timeout=_DISCONNECT_POLL_S)
                    if nxt in done:
                        break
                    if await request.is_disconnected():
                        disconnected = True
                        break
                    if time.monotonic() - last_beat >= _SSE_KEEPALIVE_S:
                        # SSE comment lines defeat idle-timeout drops on ingress
                        # proxies without firing client event handlers.
                        yield ": keep-alive\n\n"
                        last_beat = time.monotonic()
                if disconnected:
                    logger.warning("[%s] SSE client disconnected — stream "
                                   "cancelled (token spend halted).", run_id)
                    nxt.cancel()
                    with contextlib.suppress(asyncio.CancelledError,
                                             StopAsyncIteration, Exception):
                        await nxt
                    return
                try:
                    mode, payload = nxt.result()
                except StopAsyncIteration:
                    break
                if mode == "updates":
                    for node in (payload or {}):
                        yield _sse("transition", {
                            "node": node,
                            "message": _NODE_LABELS.get(node, node)})
                    last_beat = time.monotonic()
                elif mode == "values":
                    final = payload or final
        except asyncio.CancelledError:
            logger.warning("[%s] SSE cancelled — token spend halted.", run_id)
            raise
        except Exception as exc:
            logger.exception("SSE run failed [%s]", run_id)
            yield _sse("error", {"run_id": run_id, "detail": str(exc)})
            accounted = True
            return
        finally:
            # Disconnect accounting must survive every teardown path: watchdog
            # return, CancelledError re-raise, AND GeneratorExit (aclose() by
            # the middleware) — none of which the except-clauses cover alone.
            if not accounted:
                METRICS.inc("client_disconnects_total")
            # Cancels in-flight LLM calls if we aborted before exhaustion.
            with contextlib.suppress(Exception):
                await stream.aclose()
    result = _shape_result(final, run_id, time.perf_counter() - t0)
    _record_query_metrics(result, result["latency_s"])
    accounted = True
    yield _sse("result", result)


@app.get("/query/stream")
async def query_stream(request: Request, question: str,
                       tenant_id: Optional[str] = None,
                       auth_tenant: Optional[str] = Depends(require_query_key)):
    if len(question) < 3 or len(question) > 500:
        raise HTTPException(status_code=422, detail="question must be 3-500 chars")
    run_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    _assert_tenant_match(auth_tenant, tenant_id)
    tenant = auth_tenant or tenant_id   # key-bound tenant wins (auth, not claim)
    return StreamingResponse(
        _event_stream(request, question, tenant_id, run_id),
        media_type="text/event-stream",
        headers={"X-Request-ID": run_id, "Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"})


# ============================== POST /search ===============================
@app.post("/search")
async def search(req: SearchRequest) -> Dict[str, Any]:
    from db import pgvector_hybrid_search
    rows = await asyncio.to_thread(
        pgvector_hybrid_search, req.query,
        category_filter=req.category_filter,
        company_filter=req.company_filter, top_k=req.top_k,
        tenant_id=req.tenant_id)
    return {"status": "success", "total_results": len(rows), "results": rows}


# ============================== GET /verify/{run_id} =======================
@app.get("/verify/{run_id}")
async def verify(run_id: str, tenant_id: Optional[str] = None,
                 provenance_run_id: Optional[str] = None) -> JSONResponse:
    """Re-verification receipt for a grounded run: claim list + evidence chain
    + DETERMINISTIC on-demand recompute (slice stored page transcripts,
    re-hash company⊣source⊣page⊣slice, compare to chunk_hash). Zero LLM tokens;
    a tampered receipt, span, or corpus breaks the chain by construction.
    404 when no receipt exists (refusals/unverified runs store none).
    Cache-replay provenance: a semantic-cache replay's OWN run_id has no
    receipt (extraction never ran); the /query response carries the
    provenance_run_id of the ORIGINAL certification — pass it here and this
    endpoint serves THAT receipt, labeled with both ids so the replay can
    never masquerade as fresh certification."""
    receipt = await asyncio.to_thread(get_verification_receipt, run_id, tenant_id)
    resolved_from = None
    if not receipt and provenance_run_id and provenance_run_id != run_id:
        receipt = await asyncio.to_thread(
            get_verification_receipt, provenance_run_id, tenant_id)
        if receipt:
            resolved_from = provenance_run_id
    if not receipt:
        raise HTTPException(status_code=404,
                            detail="No verification receipt for this run_id "
                                   "(only grounded runs store receipts; for a "
                                   "cached answer pass its provenance_run_id)")
    try:
        chain = await asyncio.to_thread(verify_receipt_chain, receipt)
        sources = await asyncio.to_thread(get_source_registry)
    except Exception as exc:
        logger.exception("Chain recompute failed [%s]", run_id)
        raise HTTPException(status_code=500,
                            detail="chain recompute failed — see server logs") from exc
    return JSONResponse(content={"receipt": receipt, "verification": chain,
                                 "sources": sources,
                                 "requested_run_id": run_id,
                                 "resolved_from_provenance": resolved_from})


# ============================== POST /feedback =============================
@app.post("/feedback", dependencies=[Depends(require_admin)])
async def feedback(req: FeedbackRequest) -> Dict[str, Any]:
    purged = await asyncio.to_thread(
        evict_from_semantic_cache, req.question, req.tenant_id)
    METRICS.inc("evictions_total")
    logger.warning("Feedback eviction: %d entries purged for '%s...'",
                   purged, req.question[:50])
    return {"evicted": purged, "question": req.question[:60]}


# ============================== GET /health + probes + /metrics ============
@app.get("/health")
async def health() -> Dict[str, Any]:
    out: Dict[str, Any] = {"service": "ok"}
    out.update(get_health())
    try:
        out["db"] = await asyncio.to_thread(health_check)
    except Exception as exc:
        out["db"] = {"status": "unreachable", "error": str(exc)}
    return out


@app.get("/live")
async def live() -> Dict[str, str]:
    """K8s liveness: the process is up. Deliberately dependency-free — a
    liveness failure restarts the pod, so it must never probe downstreams."""
    return {"status": "alive"}


@app.get("/ready")
async def ready() -> JSONResponse:
    """K8s readiness: this instance should receive traffic now."""
    checks: Dict[str, str] = {}
    db_ok = True
    try:
        await asyncio.to_thread(health_check)
    except Exception as exc:
        db_ok, checks["db"] = False, f"unreachable: {type(exc).__name__}"
    circuit = str(get_health().get("llm_circuit", "unknown"))
    checks["llm_circuit"] = circuit
    ok = db_ok and circuit != "open"
    return JSONResponse(status_code=200 if ok else 503,
                        content={"ready": ok, "checks": checks})


@app.get("/metrics")
async def metrics() -> Response:
    # Plain-text Prometheus exposition (a bare `return str` would be
    # JSON-quoted by FastAPI and break every scrape).
    return Response(content=METRICS.render(),
                    media_type="text/plain; version=0.0.4; charset=utf-8")


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
