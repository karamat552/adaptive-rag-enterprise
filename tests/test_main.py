"""
Gateway Unit Tests (FastAPI, fully mocked — no LLM, no database)
================================================================
Runs the real ASGI app via httpx's ASGITransport with a fake LangGraph so the
whole serving layer (auth, rate limiting, disconnect guard plumbing, metrics,
SSE shaping, K8s probes) is verifiable in CI at zero token cost.

Live/integration concerns are covered by tests/test_db.py (marker: integration)
and by the eval.py harness.

Run:  pytest tests/test_main.py -v
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Tuple

# Unit-test-style dummy guard (same pattern as tests/test_db.py): only inject
# if no real config exists anywhere — keeps pydantic settings constructible
# in bare CI environments.
if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import httpx  # noqa: E402
import pytest  # noqa: E402

import main  # noqa: E402


_FINAL: Dict[str, Any] = {
    "run_id": "req-1",
    "final_executive_report": "Apple Services revenue [1]",
    "outcome": "vectorstore",
    "grounded": True,
    "cached_hit": False,
    "degraded_agents": [],
    "documents": ["Apple | aapl-10-q | Page 5\nServices: 22,314"],
    "usage_in": 10, "usage_out": 5, "usage_total": 15, "llm_calls": 2,
}


class FakeGraph:
    """Mirrors the CompiledGraph surface the gateway touches: ainvoke/astream.
    Like the real graph, the returned state carries the run_id it was given."""

    def __init__(self, final: Dict[str, Any] = None):
        self.final = final or dict(_FINAL)
        self.captured_state: Dict[str, Any] = {}
        self.captured_config: Dict[str, Any] = {}

    def _final_with(self, state: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(self.final)
        out["run_id"] = state.get("run_id", out.get("run_id"))
        return out

    async def ainvoke(self, state: Dict[str, Any], config: Any = None):
        self.captured_state, self.captured_config = dict(state), config
        return self._final_with(state)

    def astream(self, state: Dict[str, Any], config: Any = None,
                stream_mode: Any = None) -> AsyncGenerator[Tuple[str, Dict], None]:
        self.captured_state, self.captured_config = dict(state), config
        final = self._final_with(state)

        async def _gen() -> AsyncGenerator[Tuple[str, Dict], None]:
            yield "updates", {"gateway": {"route": "vectorstore", "usage_in": 1}}
            yield "updates", {"validate": {"grounded": True,
                                           "outcome": "vectorstore"}}
            yield "values", final
        return _gen()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    main._buckets.clear()
    monkeypatch.setattr(main, "METRICS", main._Metrics())   # isolated counters
    yield
    main._buckets.clear()


class _SyncASGI:
    """Sync facade over httpx.AsyncClient + ASGITransport (ASGITransport in
    httpx 0.28 is async-only and pytest-asyncio is not a project dep)."""

    def __init__(self, app) -> None:
        self._transport = httpx.ASGITransport(app=app)

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)

    def request(self, method: str, path: str, **kw):
        return asyncio.run(self._request(method, path, **kw))

    async def _request(self, method: str, path: str, **kw):
        async with httpx.AsyncClient(transport=self._transport,
                                     base_url="http://test") as c:
            return await c.request(method, path, **kw)


@pytest.fixture()
def client():
    yield _SyncASGI(main.app)


@pytest.fixture()
def fake_graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(main, "get_graph", lambda: g)
    return g


def _sse_events(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    events: List[Tuple[str, Dict[str, Any]]] = []
    event, data = None, None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = json.loads(line.split(":", 1)[1].strip())
        elif not line and event and data is not None:
            events.append((event, data))
            event, data = None, None
    return events


# ============================== PROBES =====================================
def test_liveness_is_dependency_free(client):
    r = client.get("/live")
    assert r.status_code == 200 and r.json() == {"status": "alive"}


def test_readiness_ok_when_db_up(client, monkeypatch):
    monkeypatch.setattr(main, "health_check", lambda: {"status": "ok"})
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert r.json()["checks"]["llm_circuit"] == "closed"


def test_readiness_fails_closed_when_db_down(client, monkeypatch):
    def _boom():
        raise ConnectionError("neon unreachable")
    monkeypatch.setattr(main, "health_check", _boom)
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["ready"] is False
    assert "unreachable" in r.json()["checks"]["db"]


def test_security_headers_present(client):
    r = client.get("/live")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["X-Request-ID"]


# ============================== POST /query ================================
def test_query_happy_path_shape_and_headers(client, fake_graph):
    r = client.post("/query", json={"question": "What was Apple services revenue?"})
    assert r.status_code == 200
    body = r.json()
    assert body["grounded"] is True
    assert body["outcome"] == "vectorstore"
    assert body["sources"] == _FINAL["documents"]          # unified contract
    assert body["usage"]["total"] == 15
    assert "latency_s" in body
    assert r.headers["X-Request-ID"] == body["run_id"]     # X-Request-ID == run_id


def test_query_passes_default_tenant(client, fake_graph):
    """Regression: SSE path used to crash on RagSettings.default_tenant."""
    client.post("/query", json={"question": "What was Apple services revenue?"})
    assert fake_graph.captured_state["tenant_id"] == "default"


def test_query_validation_rejects_short_question(client):
    r = client.post("/query", json={"question": "ab"})
    assert r.status_code == 422


def test_tokens_land_in_prometheus(client, fake_graph):
    client.post("/query", json={"question": "What was Apple services revenue?"})
    m = client.get("/metrics").text
    assert 'llm_tokens_spent_total{direction="input"} 10' in m
    assert 'llm_tokens_spent_total{direction="output"} 5' in m
    assert 'queries_total{cached="false",outcome="vectorstore"} 1' in m
    assert "cache_hits_total" not in m                     # cache miss -> no hit metric


def test_cache_hit_metrics(client, fake_graph):
    fake_graph.final = dict(_FINAL, cached_hit=True)
    client.post("/query", json={"question": "What was Apple services revenue?"})
    m = client.get("/metrics").text
    assert 'queries_total{cached="true",outcome="vectorstore"} 1' in m
    assert "cache_hits_total 1" in m


# ============================== RATE LIMITER ===============================
def test_rate_limit_429(client, fake_graph, monkeypatch):
    monkeypatch.setenv("SERVICE_RATE_LIMIT_PER_MIN", "2")
    for _ in range(2):
        assert client.post(
            "/query", json={"question": "What was Apple services revenue?"}
        ).status_code == 200
    r = client.post("/query", json={"question": "What was Apple services revenue?"})
    assert r.status_code == 429
    m = client.get("/metrics").text
    assert "rate_limited_total 1" in m


def test_probes_and_metrics_exempt_from_rate_limit(client, monkeypatch):
    monkeypatch.setenv("SERVICE_RATE_LIMIT_PER_MIN", "1")
    for _ in range(3):
        assert client.get("/live").status_code == 200
        assert client.get("/metrics").status_code == 200


# ============================== SSE ========================================
def test_sse_happy_path_event_sequence(client, fake_graph):
    r = client.get("/query/stream", params={"question": "Apple services revenue?"})
    assert r.status_code == 200
    events = _sse_events(r.text)
    names = [e for e, _ in events]
    assert names == ["start", "transition", "transition", "result"]
    start = dict(events)[ "start"]
    result = dict(events)["result"]
    assert start["run_id"] == result["run_id"]
    assert result["sources"] == _FINAL["documents"]        # unified contract
    assert result["latency_s"] >= 0                        # unified contract
    assert fake_graph.captured_state["tenant_id"] == "default"   # regression!


def test_sse_honors_explicit_tenant(client, fake_graph):
    client.get("/query/stream",
               params={"question": "Apple services revenue?", "tenant_id": "acme"})
    assert fake_graph.captured_state["tenant_id"] == "acme"


def test_sse_question_validation(client):
    r = client.get("/query/stream", params={"question": "ab"})
    assert r.status_code == 422


def test_sse_error_event_on_graph_crash(client, monkeypatch):
    class _BoomGraph:
        def astream(self, state, config=None, stream_mode=None):
            async def _gen():
                raise RuntimeError("synthesis exploded")
                yield  # pragma: no cover
            return _gen()
    monkeypatch.setattr(main, "get_graph", lambda: _BoomGraph())
    r = client.get("/query/stream", params={"question": "Apple services revenue?"})
    events = _sse_events(r.text)
    assert ("error" in [e for e, _ in events])


# ============================== /feedback AUTH =============================
def test_feedback_disabled_without_admin_key(client, monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    r = client.post("/feedback", json={"question": "What was Apple services revenue?"})
    assert r.status_code == 503


def test_feedback_rejects_wrong_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    r = client.post("/feedback",
                    json={"question": "What was Apple services revenue?"},
                    headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403
    m = client.get("/metrics").text
    assert "feedback_rejected_total 1" in m


def test_feedback_evicts_with_valid_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "s3cret")
    calls: List[Tuple[str, Any]] = []

    def _fake_evict(question, tenant_id=None):
        calls.append((question, tenant_id))
        return 2
    monkeypatch.setattr(main, "evict_from_semantic_cache", _fake_evict)
    r = client.post("/feedback",
                    json={"question": "What was Apple services revenue?",
                          "tenant_id": "acme"},
                    headers={"X-Admin-Key": "s3cret"})
    assert r.status_code == 200 and r.json()["evicted"] == 2
    assert calls and calls[0][1] == "acme"
    m = client.get("/metrics").text
    assert "evictions_total 1" in m


# ============================== /search tenant =============================
def test_search_forwards_tenant(client, monkeypatch):
    import db
    seen: Dict[str, Any] = {}

    def _fake_search(query, category_filter=None, company_filter=None,
                     top_k=10, tenant_id=None):
        seen.update(tenant_id=tenant_id, top_k=top_k)
        return [{"company": "apple", "content": "x", "fusion_score": 0.5,
                 "page": 1, "source": "aapl-10-q", "category": "financial",
                 "section_title": "s", "chunk_hash": "h", "contains_table": False}]
    monkeypatch.setattr(db, "pgvector_hybrid_search", _fake_search)
    r = client.post("/search", json={"query": "services revenue", "tenant_id": "acme"})
    assert r.status_code == 200
    assert seen["tenant_id"] == "acme" and seen["top_k"] == 5


# ============================== eval.py pure functions =====================
def test_shift_figure_plus_1m():
    from eval import _shift_figure
    text = "Services revenue reached $22,314M; that is $22.314B [1]."
    shifted, n = _shift_figure(text, "22", "314", "22", "315")
    assert n == 2                                   # both forms shifted
    assert "22,315" in shifted and "22.315" in shifted
    assert "22,314" not in shifted and "22.314" not in shifted


def test_shift_figure_respects_word_boundaries():
    from eval import _shift_figure
    text = "122,314 and 22,3145 are untouched; 22,314 is shifted."
    shifted, n = _shift_figure(text, "22", "314", "23", "314")
    assert n == 1 and "122,314" in shifted and "23,314" in shifted


def test_tamper_variants_are_detectable():
    from eval import _tamper_variants
    base = ("### Executive Summary\nApple's Services revenue was $22,314M in "
            "Q4 2023 [1], up from the prior quarter.")
    variants = {v["name"]: v for v in _tamper_variants(base)}
    assert {"plus_1m", "plus_1b", "fake_citation", "cross_company"} <= set(variants)
    assert "22,315" in variants["plus_1m"]["text"]
    assert "23,314" in variants["plus_1b"]["text"]
    assert "[99]" in variants["fake_citation"]["text"]
    assert "Tesla" in variants["cross_company"]["text"] \
        and "Apple" not in variants["cross_company"]["text"]


def test_contains_any():
    from eval import _contains_any
    assert _contains_any("revenue 22,314", ("22,314", "22.314")) == "22,314"
    assert _contains_any("nothing here", ("22,314",)) is None
