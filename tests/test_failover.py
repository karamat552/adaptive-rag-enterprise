"""
Stage-Aware Provider Failover + Generic OpenAI-Compatible Seam — Tests
======================================================================
Covers Phase D (ADR-008):
- Seam: RAG_BASE_URL/RAG_API_KEY settings; loud failure when model unset.
- Failover registry: RAG_FAILOVER_ENDPOINTS parsing + validation.
- Retry-hint parsing: Groq 'Please try again in 10m44.544s' -> 644.5s.
- Quota classification: 429/rate-limit/TPD classed vs timeout/5xx not.
- Failover behavior: primary 429 -> backup success; backup 429 -> cooldown
  honored on the NEXT call; non-quota errors never switch endpoints.
- THE SAFETY INVARIANT: executive-stage calls NEVER touch backup engines —
  a quota wall at executive must end in quarantine/refusal, never a weaker
  model certifying a financial answer.

Run:  pytest tests/test_failover.py -v
"""
import asyncio
import os
from pathlib import Path

if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import pytest  # noqa: E402


# ============================== retry-hint parsing =========================
def test_parse_groq_tpd_hint():
    from adaptive_rag import parse_retry_hint
    exc = Exception("Rate limit reached ... Please try again in 10m44.544s. "
                    "Need more tokens?")
    assert parse_retry_hint(exc) == pytest.approx(644.544)


def test_parse_hint_hours_minutes_seconds():
    from adaptive_rag import parse_retry_hint
    assert parse_retry_hint(Exception("try again in 1h 2m 3s")) == pytest.approx(3723.0)
    assert parse_retry_hint(Exception("try again in 120s")) == pytest.approx(120.0)
    assert parse_retry_hint(Exception("try again in 5m")) == pytest.approx(300.0)


def test_parse_hint_absent_returns_none():
    from adaptive_rag import parse_retry_hint
    assert parse_retry_hint(Exception("connection reset by peer")) is None
    assert parse_retry_hint(Exception("500 Internal Server Error")) is None


# ============================== quota classification ======================
def test_quota_error_classes():
    from adaptive_rag import _is_quota_error
    assert _is_quota_error(Exception("HTTP 429 Too Many Requests"))
    assert _is_quota_error(Exception("Rate limit reached for model ... TPD: Limit 200000"))
    assert _is_quota_error(Exception("quota exceeded"))
    assert not _is_quota_error(Exception("ReadTimeout: connection timed out"))
    assert not _is_quota_error(Exception("503 Service Unavailable"))
    assert not _is_quota_error(Exception("404 model not found"))


# ============================== endpoint registry =========================
def test_failover_registry_parses_and_validates(monkeypatch):
    import adaptive_rag as ar
    monkeypatch.setattr(ar, "_failover_endpoints", None)
    ar._S = None          # settings singleton must re-read the patched env
    monkeypatch.setenv(
        "RAG_FAILOVER_ENDPOINTS",
        '[{"base_url": "https://api.cerebras.ai/v1", "api_key_env": "CEREBRAS_API_KEY", '
        '"model": "llama-3.3-70b"}, '
        '{"base_url": "https://integrate.api.nvidia.com/v1", "api_key_env": "NIM_API_KEY", '
        '"model": "meta/llama-3.3-70b-instruct"}]')
    try:
        eps = ar.get_failover_endpoints()
        assert len(eps) == 2
        assert eps[0]["base_url"] == "https://api.cerebras.ai/v1"

        # Settings caches RAG_FAILOVER_ENDPOINTS at construction — both the
        # registry cache AND the settings singleton must be fresh for the
        # invalid-config branch to be exercised.
        monkeypatch.setattr(ar, "_failover_endpoints", None)
        ar._S = None
        monkeypatch.setenv("RAG_FAILOVER_ENDPOINTS", '[{"base_url": "x"}]')
        with pytest.raises(ValueError, match="base_url, api_key_env and model"):
            ar.get_failover_endpoints()
    finally:
        monkeypatch.setattr(ar, "_failover_endpoints", None)
        ar._S = None      # rebuild from the real .env for subsequent tests


def test_failover_registry_empty_when_unset(monkeypatch):
    import adaptive_rag as ar
    monkeypatch.setattr(ar, "_failover_endpoints", None)
    ar._S = None
    monkeypatch.delenv("RAG_FAILOVER_ENDPOINTS", raising=False)
    # pydantic-settings: the .env FILE also carries the var when the real
    # deployment configures failover — neutralize it for this test, restore after.
    import os
    saved = {}
    try:
        with open(".env", encoding="utf-8") as fh:
            lines = fh.readlines()
        for i, ln in enumerate(lines):
            if ln.startswith("RAG_FAILOVER_ENDPOINTS="):
                saved["line"], saved["idx"] = ln, i
                lines[i] = "# (neutralized by test)\n"
                with open(".env", "w", encoding="utf-8") as w:
                    w.writelines(lines)
                break
        ar._S = None          # rebuild settings from the neutralized env
        assert ar.get_failover_endpoints() == []
    finally:
        if saved:
            with open(".env", encoding="utf-8") as fh:
                lines = fh.readlines()
            lines[saved["idx"]] = saved["line"]
            with open(".env", "w", encoding="utf-8") as w:
                w.writelines(lines)
        monkeypatch.setattr(ar, "_failover_endpoints", None)
        ar._S = None


# ============================== seam construction =========================
def test_openai_compatible_engine(monkeypatch):
    import adaptive_rag as ar
    monkeypatch.setenv("RAG_PROVIDER", "openai_compatible")
    monkeypatch.setenv("RAG_BASE_URL", "https://api.cerebras.ai/v1")
    monkeypatch.setenv("RAG_API_KEY", "test-key")
    monkeypatch.setenv("RAG_ROUTER_MODEL", "llama-3.3-70b")
    monkeypatch.setenv("RAG_FLEET_MODEL", "llama-3.3-70b")
    monkeypatch.setenv("RAG_EXECUTIVE_MODEL", "llama-3.3-70b")
    ar._S = None
    try:
        eng = ar._build_engine("llama-3.3-70b")
        assert getattr(eng, "openai_api_base", None) == "https://api.cerebras.ai/v1" \
            or "cerebras" in str(getattr(eng, "root_client", getattr(eng, "client", "")))
    finally:
        ar._S = None


def test_openai_compatible_missing_model_fails_loud(monkeypatch):
    import adaptive_rag as ar
    monkeypatch.setenv("RAG_PROVIDER", "openai_compatible")
    monkeypatch.setenv("RAG_BASE_URL", "https://api.cerebras.ai/v1")
    monkeypatch.setenv("RAG_API_KEY", "test-key")
    monkeypatch.delenv("RAG_ROUTER_MODEL", raising=False)
    ar._S = None
    try:
        with pytest.raises(ValueError, match="openai_compatible needs a model"):
            ar.get_stage_model("router")
    finally:
        ar._S = None


# ============================== failover behavior =========================
class _FakeEngine:
    def __init__(self, behavior=None, label=""):
        self.behavior = behavior or []
        self.label = label
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if not self.behavior:
            raise AssertionError(f"engine {self.label} invoked with no scripted "
                                 f"behavior — the test must set it")
        action = self.behavior[min(self.calls - 1, len(self.behavior) - 1)]
        if isinstance(action, Exception):
            raise action
        return action


class _Resp:
    def __init__(self, text):
        self.content = text


@pytest.fixture()
def _failover_env(monkeypatch):
    import adaptive_rag as ar
    monkeypatch.setattr(ar, "_failover_endpoints", None)
    ar._S = None          # settings singleton re-reads the patched env
    monkeypatch.setenv(
        "RAG_FAILOVER_ENDPOINTS",
        '[{"base_url": "https://backup-a.test/v1", "api_key_env": "BK_A", "model": "m1"},'
        '{"base_url": "https://backup-b.test/v1", "api_key_env": "BK_B", "model": "m2"}]')
    monkeypatch.setenv("BK_A", "key-a")
    monkeypatch.setenv("BK_B", "key-b")
    engines = {}
    built = []

    def _build(ep):
        eid = f"{ep['base_url']}::{ep['model']}"
        if eid not in engines:
            built.append(eid)
        engines.setdefault(eid, _FakeEngine(label=eid))
        return engines[eid]

    monkeypatch.setattr(ar, "_get_backup_engine", _build)
    ar._endpoint_cooldown = ar.EndpointCooldown(default_s=60)
    # Eagerly instantiate both backups so tests can script behaviors BEFORE
    # the first failover call (the runner reuses these via the same dict).
    for ep in ar.get_failover_endpoints():
        _build(ep)
    yield {"engines": engines, "built": built}
    monkeypatch.setattr(ar, "_failover_endpoints", None)
    ar._S = None          # restore settings built from the real .env


def test_primary_429_fails_over_to_backup(_failover_env):
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[Exception("HTTP 429 rate limit exceeded")])
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [_Resp("ok")]
    result, collector = asyncio.run(
        ar._failover_stage_call(primary, [("human", "q")], "route"))
    assert result.content == "ok"


def test_backup_429_marks_cooldown_skipped_next(_failover_env):
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[Exception("429"), Exception("429")])
    # Pre-build both backup engines by triggering one failover, then script:
    # A walled, B healthy. The FIRST call must fail over to B (skipping A
    # only on its SECOND encounter — first call has no cooldown yet).
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [Exception("429 rate limit")]
    _failover_env["engines"]["https://backup-b.test/v1::m2"].behavior = \
        [_Resp("from-b"), _Resp("from-b")]

    r1 = asyncio.run(ar._failover_stage_call(primary, [("h", "q")], "route"))
    assert r1[0].content == "from-b"
    assert _failover_env["engines"]["https://backup-a.test/v1::m1"].calls == 1

    # Second call: A is cooling down -> must go STRAIGHT to B, no A attempt.
    r2 = asyncio.run(ar._failover_stage_call(primary, [("h", "q")], "route"))
    assert r2[0].content == "from-b"
    assert _failover_env["engines"]["https://backup-a.test/v1::m1"].calls == 1, \
        "cooldown must prevent re-attempting backup A"


def test_non_quota_error_never_fails_over(_failover_env):
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[Exception("ReadTimeout: timed out")])
    with pytest.raises(Exception, match="timed out"):
        asyncio.run(ar._failover_stage_call(primary, [("h", "q")], "route"))
    invoked = {eid: e.calls for eid, e in _failover_env["engines"].items()}
    assert all(c == 0 for c in invoked.values()), \
        f"timeout must not invoke any backup: {invoked}"


def test_all_endpoints_walled_raises(_failover_env):
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[Exception("429 rate limit")])
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [Exception("429")]
    _failover_env["engines"]["https://backup-b.test/v1::m2"].behavior = \
        [Exception("429")]
    with pytest.raises(RuntimeError, match="all endpoints exhausted"):
        asyncio.run(ar._failover_stage_call(primary, [("h", "q")], "route"))


# ============================== THE SAFETY INVARIANT ======================
def test_executive_stage_never_touches_backups(_failover_env, monkeypatch):
    """The load-bearing guarantee: _llm_call(allow_failover=False) — used by
    synthesis and audit — must NEVER construct or invoke a backup engine,
    even when the primary is quota-walled."""
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[Exception("HTTP 429 rate limit exceeded")])

    def _no_backups(ep):
        raise AssertionError("executive stage must never build a backup engine")

    monkeypatch.setattr(ar, "_get_backup_engine", _no_backups)

    async def _call():
        return await ar._llm_call(primary, [("h", "q")], "synthesize")

    with pytest.raises(Exception, match="429"):
        asyncio.run(_call())
    invoked = {eid: e.calls for eid, e in _failover_env["engines"].items()}
    assert all(c == 0 for c in invoked.values()), \
        f"executive stage must never invoke a backup engine: {invoked}"


def test_llm_call_failover_flag_routes_to_backups(_failover_env, monkeypatch):
    """allow_failover=True at a quota-walled primary: succeeds via backup
    through the standard _llm_call path, and the circuit records success."""
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[Exception("429 rate limit")])
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [_Resp("via-backup")]

    async def _call():
        return await ar._llm_call(primary, [("h", "q")], "route",
                                  allow_failover=True)

    result, collector = asyncio.run(_call())
    assert result.content == "via-backup"


# ============================== consult fixes ==============================
def test_primary_cooldown_skips_doomed_attempt(_failover_env):
    """After the primary 429s, its NEXT call must go straight to backups —
    a 3-specialist fan-out must not burn a doomed primary attempt per pass."""
    import adaptive_rag as ar
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [_Resp("ok"), _Resp("ok")]

    class _CountingPrimary(_FakeEngine):
        async def ainvoke(self, messages, config=None):
            self.calls += 1
            raise Exception("429 rate limit exceeded")

    primary = _CountingPrimary()
    r1 = asyncio.run(ar._failover_stage_call(primary, [("h", "q")], "route"))
    r2 = asyncio.run(ar._failover_stage_call(primary, [("h", "q")], "route"))
    assert r1[0].content == "ok" and r2[0].content == "ok"
    assert primary.calls == 1, "second call must skip the cooling primary"


def test_cooldown_keys_are_per_model():
    """Today's TPD lesson: Groq budgets are PER-MODEL — a walled gpt-oss-20b
    must not block a still-open qwen3.8-27b. Verify the model-id extraction
    and that distinct models map to distinct cooldown keys."""
    from adaptive_rag import _runnable_model_id
    class _Eng:
        model_name = "openai/gpt-oss-20b"
    class _Bound:
        bound = _Eng()
    assert _runnable_model_id(_Eng()) == "openai/gpt-oss-20b"
    assert _runnable_model_id(_Bound()) == "openai/gpt-oss-20b"
    assert _runnable_model_id(object()) == "unknown"


def test_quota_failures_do_not_trip_circuit(_failover_env, monkeypatch):
    """Fan-out storm fix: quota-class failures on failover stages are owned by
    the cooldown layer, NOT the global circuit breaker — one TPD wall across
    3 specialists must not open the circuit meant for systemic failures."""
    import adaptive_rag as ar
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [Exception("429 rate limit")]
    _failover_env["engines"]["https://backup-b.test/v1::m2"].behavior = \
        [Exception("429 rate limit")]
    primary = _FakeEngine(behavior=[Exception("429 rate limit")])

    breaker = ar.CircuitBreaker(failure_threshold=5, cooldown_s=60)
    monkeypatch.setattr(ar, "_circuit", breaker)

    async def _call():
        return await ar._llm_call(primary, [("h", "q")], "route",
                                  allow_failover=True)
    with pytest.raises(RuntimeError, match="all endpoints exhausted"):
        asyncio.run(_call())
    # The 3 individual QUOTA walls recorded ZERO circuit failures (cooldown
    # owns them). Only the terminal 'all endpoints exhausted' — a genuine
    # systemic signal — counts, and only ONCE: failures == 1, not 3.
    assert breaker.failures == 1, \
        "3 quota walls must collapse to a single systemic failure record, not 3"
    assert breaker.state == "closed", "1 < 5 threshold — circuit stays closed"


# ================== timeout-class failover (live lesson 2026-09-06) =======
def test_is_timeout_error_type_based():
    """Only REAL timeout types (asyncio.TimeoutError, *TimeoutError class
    names) count — a string 'timed out' in a plain Exception stays with the
    circuit breaker (ADR-008 ownership: timeouts don't switch endpoints;
    the live white-whale starvation was a bare asyncio.TimeoutError from
    429-storm backoff burning the whole budget)."""
    from adaptive_rag import _is_timeout_error
    assert _is_timeout_error(asyncio.TimeoutError())
    assert _is_timeout_error(TimeoutError())
    assert not _is_timeout_error(Exception("ReadTimeout: timed out"))
    assert not _is_timeout_error(Exception("429 rate limit"))


def test_real_timeout_arms_cooldown_and_fails_over(_failover_env):
    """The white-whale contract: a bare asyncio.TimeoutError at the primary
    must (a) mark a SHORT cooldown so sibling specialists stop burning the
    stalled primary, and (b) proceed to backups — not raise. The openai
    client's 429 backoff retries can burn the entire timeout budget so the
    quota wall NEVER surfaces as a 429; it surfaces as this exact type."""
    import adaptive_rag as ar
    primary = _FakeEngine(behavior=[asyncio.TimeoutError()])
    _failover_env["engines"]["https://backup-a.test/v1::m1"].behavior = \
        [_Resp("rescued")]
    result, _ = asyncio.run(
        ar._failover_stage_call(primary, [("human", "q")], "route"))
    assert result.content == "rescued"
    # Cooldown must be armed for the PRIMARY (sibling calls skip straight
    # to backups while the window lasts).
    model_id = ar._runnable_model_id(primary)
    assert _endpoint_is_cooling(ar, f"primary::{ar.get_settings().provider}::{model_id}")


def _endpoint_is_cooling(ar, endpoint_id: str) -> bool:
    return ar._endpoint_cooldown.blocked(endpoint_id)


# ============ abort-on-hint (token plan, 2026-09-07) ======================
def test_transform_query_aborts_on_long_quota_hint():
    """The day-3 14:59 trace: synthesis 429'd with 'try again in 31m39.504s'
    -> optimizer ran iteration 2 anyway -> another 429 -> refusal. A hint
    at/above the abort bar must exhaust the loop immediately (no rewriter
    call, no fleet re-run — route_after_rewrite sends it to refusal)."""
    import asyncio
    import adaptive_rag as ar

    llm_calls = {"n": 0}

    async def _no_llm(*a, **k):
        llm_calls["n"] += 1
        raise AssertionError("abort must spend zero LLM calls")

    import unittest.mock as mock
    state = {"original_question": "q?", "search_query": "q",
             "retry_count": 0, "quota_hint_s": 1899.5}
    with mock.patch.dict("os.environ", {"RAG_QUOTA_ABORT_S": "900"}), \
         mock.patch.object(ar, "_llm_call", _no_llm):
        upd = asyncio.run(ar.transform_query(state))
    assert upd.get("retry_count") == ar.get_settings().max_retries
    assert upd.get("quota_aborted") is True
    assert llm_calls["n"] == 0
    assert ar.route_after_rewrite(upd) == "verified_refusal"


def test_transform_query_retries_on_short_quota_hint():
    """An RPM-scale hint (30s) is below the abort bar — the loop retries
    normally (the window opens in time)."""
    import asyncio
    import adaptive_rag as ar

    class _Mutation:
        new_query = "rewritten q"

    async def _fake_llm(*a, **k):
        return _Mutation(), ar.UsageCollector()

    import unittest.mock as mock

    async def _fast_sleep(_s):
        return None

    state = {"original_question": "q?", "search_query": "q",
             "retry_count": 0, "quota_hint_s": 30.0}
    with mock.patch.dict("os.environ", {"RAG_QUOTA_ABORT_S": "900"}), \
         mock.patch.object(ar, "_llm_call", _fake_llm), \
         mock.patch.object(ar.asyncio, "sleep", _fast_sleep):
        upd = asyncio.run(ar.transform_query(state))
    assert upd.get("quota_aborted") is None
    assert upd.get("search_query") == "rewritten q"


def test_synthesis_429_threads_quota_hint():
    """csuite_synth's except path must thread parse_retry_hint(exc) into
    state as quota_hint_s — the abort depends on it being there."""
    import asyncio
    import adaptive_rag as ar

    async def _boom(*a, **k):
        raise Exception("429 Rate limit ... Please try again in 25m00s")

    import unittest.mock as mock
    state = {"original_question": "q?", "search_query": "q",
             "documents": ["Meta | Meta_Q4_2023.pdf | Page 1 revenue text"],
             "evidence_records": [{"company": "Meta", "content": "x",
                                    "chunk_hash": "h", "page": 1,
                                    "source": "s.pdf"}],
             "financial_report": "fr", "risk_report": "rr",
             "product_report": "pr", "retry_count": 0,
             "contradictions": [], "run_id": "t", "tenant_id": "default"}
    with mock.patch.object(ar, "_llm_call", _boom):
        upd = asyncio.run(ar.synthesize_csuite_report(state))
    assert upd.get("quota_hint_s") == 1500.0, \
        "the 429's retry hint must reach the optimizer via state"
    assert "synthesis" in (upd.get("degraded_agents") or [])


def test_audit_429_threads_quota_hint():
    """A/B run-2 finding (2026-09-07): the abort machinery only heard
    SYNTHESIS 429s — audit-stage 429s ('try again in 16m43.104s') died in
    the auditor except-path and the optimizer burned a full doomed re-run.
    The audit failure must thread parse_retry_hint into state like
    csuite_synth does."""
    import asyncio
    import adaptive_rag as ar

    async def _boom_audit(*a, **k):
        raise Exception("429 Rate limit ... Please try again in 16m43.104s")

    import unittest.mock as mock
    state = {"original_question": "q?", "run_id": "t", "tenant_id": "default",
             "retry_count": 0, "documents": ["Meta | Meta_Q4_2023.pdf | Page 1 revenue text"],
             "final_executive_report": "draft text",
             "degraded_agents": [], "evidence_records": []}
    with mock.patch.object(ar, "_llm_call", _boom_audit):
        upd = asyncio.run(ar.fact_checker_guard(state))
    assert upd.get("grounded") is False
    assert upd.get("outcome") == "unverified_system"
    assert upd.get("quota_hint_s") == pytest.approx(1003.104), \
        "audit-stage 429 hint must reach the optimizer's abort check"


def test_backup_engine_per_endpoint_max_tokens(monkeypatch):
    """Live lesson 2026-09-09 (Token Router lane): GLM-5.3-free is a
    reasoning model — reasoning_content burns tokens BEFORE content, so a
    stage cap of 800 returns content=None (finish=length) and the call
    looks like a failure. A lane may declare its own max_tokens headroom
    (the consult.py ADR-008 war-story-4 lesson, now in the engine path)."""
    import adaptive_rag as ar
    captured = {}

    class _FakeChat:
        def __init__(self, **kw):
            captured.update(kw)

        def with_structured_output(self, *a, **k):
            return self

    import langchain_openai as lo
    monkeypatch.setattr(lo, "ChatOpenAI", _FakeChat)
    ar._backup_engines.clear()
    ep = {"base_url": "https://lane.test/v1", "api_key_env": "NIM_API_KEY",
          "model": "z-ai/glm-5.3-free", "timeout_s": 150, "max_tokens": 6000}
    ar._get_backup_engine(ep)
    assert captured.get("max_tokens") == 6000, \
        "reasoning-lane headroom must reach the engine"
    assert captured.get("timeout") == 150
    # Lane WITHOUT max_tokens: stage caps apply — engine gets no override.
    captured.clear()
    ar._backup_engines.clear()
    ep2 = {"base_url": "https://lane2.test/v1", "api_key_env": "NIM_API_KEY",
           "model": "m2", "timeout_s": 0, "max_tokens": 0}
    ar._get_backup_engine(ep2)
    assert "max_tokens" not in captured or captured.get("max_tokens") is None


def test_repairing_structured_passes_native(monkeypatch):
    """_repairing_structured: healthy lanes never pay the repair cost —
    a native parse result is returned untouched."""
    import asyncio
    import adaptive_rag as ar

    class _Raw:
        content = "irrelevant"

    class _Parsed:
        destination = "vectorstore"

    class _Bound:
        async def ainvoke(self, messages, config=None, **kw):
            return {"raw": _Raw(), "parsed": _Parsed(),
                    "parsing_error": None}

        def invoke(self, messages, config=None, **kw):
            return {"raw": _Raw(), "parsed": _Parsed(),
                    "parsing_error": None}

    class _Engine:
        def with_structured_output(self, schema, include_raw=False, **kw):
            assert include_raw is True
            return _Bound()

    wrapped = ar._repairing_structured(_Engine(), ar.RouteDecision)
    r = asyncio.run(wrapped.ainvoke([("human", "q")]))
    assert r.destination == "vectorstore"
    assert wrapped.invoke([("human", "q")]).destination == "vectorstore"


def test_repairing_structured_repairs_markdown_enum(monkeypatch):
    """Live lesson 2026-09-09 (Token Router GLM-5.3-free, run 4): the
    router returned '**vectorstore**

This question is about Tesla
    revenue...' — markdown enum PLUS trailing prose. The wrapper repairs
    via the result-dict path AND the exception path (langchain raises
    through include_raw=True instead of returning the dict)."""
    import asyncio
    import adaptive_rag as ar

    live_text = ("**vectorstore**\n\nThis question is about Tesla "
                 "revenue which can be answered from the filings corpus.")

    class _Raw:
        content = live_text

    class _Bound:
        async def ainvoke(self, messages, config=None, **kw):
            return {"raw": _Raw(), "parsed": None,
                    "parsing_error": ValueError("json_invalid: " + live_text)}

        def invoke(self, messages, config=None, **kw):
            raise ValueError("json_invalid: " + live_text)

    calls = {"plain": 0}

    class _Plain:
        content = live_text

    class _Engine:
        def with_structured_output(self, schema, include_raw=False, **kw):
            return _Bound()

        async def ainvoke(self, messages, config=None, **kw):
            calls["plain"] += 1
            return _Plain()

        def invoke(self, messages, config=None, **kw):
            calls["plain"] += 1
            return _Plain()

    wrapped = ar._repairing_structured(_Engine(), ar.RouteDecision)
    r = asyncio.run(wrapped.ainvoke([("human", "q")]))
    assert r.destination == "vectorstore", "dict-path repair"
    r2 = wrapped.invoke([("human", "q")])
    assert r2.destination == "vectorstore", "exception path repairs via plain fallback"
    assert calls["plain"] >= 1, "the raise path must fall back to a plain call"


def test_repairing_structured_reraises_when_unrepairable(monkeypatch):
    """Fail-closed intact: garbage that cannot be repaired re-raises —
    the stage's existing failure semantics own the outcome."""
    import asyncio
    import pytest
    import adaptive_rag as ar

    class _Raw:
        content = "I cannot classify this request."

    class _Bound:
        async def ainvoke(self, messages, config=None, **kw):
            return {"raw": _Raw(), "parsed": None,
                    "parsing_error": ValueError("json_invalid")}

        def invoke(self, messages, config=None, **kw):
            return {"raw": _Raw(), "parsed": None,
                    "parsing_error": ValueError("json_invalid")}

    class _Engine:
        def with_structured_output(self, schema, include_raw=False, **kw):
            return _Bound()

        async def ainvoke(self, messages, config=None, **kw):
            return _Raw()          # plain fallback also returns garbage

        def invoke(self, messages, config=None, **kw):
            return _Raw()

    wrapped = ar._repairing_structured(_Engine(), ar.RouteDecision)
    with pytest.raises(ValueError):
        asyncio.run(wrapped.ainvoke([("human", "q")]))
    with pytest.raises(ValueError):
        wrapped.invoke([("human", "q")])


def test_bind_output_cap_headroom_floor():
    """Live lesson 2026-09-09 (Token Router run 5): synthesis 'succeeded'
    with out=1800 and an EMPTY draft — reasoning_content consumed the
    stage cap before any content was emitted. A headroom-declaring engine
    (failover lane max_tokens) or a reasoning-primary (RAG_REASONING_
    HEADROOM) must never be capped BELOW its headroom: the stage cap
    becomes a floor there. Engines without headroom keep the exact cap."""
    import adaptive_rag as ar
    import unittest.mock as mock

    bound = {}

    class _Engine:
        max_tokens = 6000            # lane-declared headroom

        def bind(self, **kw):
            bound.update(kw)
            return self

    ar._bind_output_cap(_Engine(), 1800)
    assert bound["max_tokens"] == 6000, \
        "lane headroom must floor the stage cap"

    bound.clear()

    class _PlainEngine:
        def bind(self, **kw):
            bound.update(kw)
            return self

    with mock.patch.dict("os.environ", {"RAG_REASONING_HEADROOM": "5000"}):
        ar._bind_output_cap(_PlainEngine(), 1800)
    assert bound["max_tokens"] == 5000, \
        "reasoning-primary headroom must floor the stage cap"

    bound.clear()
    with mock.patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("RAG_REASONING_HEADROOM", None)
        ar._bind_output_cap(_PlainEngine(), 1800)
    assert bound["max_tokens"] == 1800, \
        "no headroom declared -> exact stage cap, unchanged behavior"


# ============ peer-to-peer executive failover (ADR-008 amendment) ========
def test_exec_peer_failover_disabled_by_default(monkeypatch):
    """The executive stays PINNED unless RAG_EXEC_PEER_FAILOVER=1 — the
    amendment is opt-in, the ADR-008 default behavior is unchanged."""
    import asyncio
    import adaptive_rag as ar

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise Exception("429 Rate limit reached TPD")

    rescued = {"n": 0}

    async def _fake_fallback(messages, stage, schema=None):
        rescued["n"] += 1
        return object()

    import unittest.mock as mock
    monkeypatch.delenv("RAG_EXEC_PEER_FAILOVER", raising=False)
    with mock.patch.object(ar, "_exec_peer_fallback", _fake_fallback):
        with pytest.raises(Exception, match="429"):
            asyncio.run(ar._llm_call(_Boom(), [("h", "q")], "synthesize"))

    assert rescued["n"] == 0, "without the flag, quota walls stay fail-closed"


def test_exec_peer_failover_rescues_quota_walls(monkeypatch):
    """With the flag set, a day-capped TPD wall at the executive escalates
    to the vetted peer pool — the exact class that killed both batteries'
    audit stages."""
    import asyncio
    import adaptive_rag as ar

    class _Resp:
        content = "rescued by peer"

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise Exception("429 Rate limit reached ... TPD: Limit 200000")

    async def _fake_fallback(messages, stage, schema=None):
        return _Resp(), ar.UsageCollector()

    import unittest.mock as mock
    monkeypatch.setenv("RAG_EXEC_PEER_FAILOVER", "1")
    with mock.patch.object(ar, "_exec_peer_fallback", _fake_fallback):
        result, collector = asyncio.run(
            ar._llm_call(_Boom(), [("h", "q")], "synthesize"))
    assert result.content == "rescued by peer"


def test_exec_peer_non_quota_never_escalates(monkeypatch):
    """THE GUARDRAIL: a non-quota executive failure (garbage output,
    protocol error, 5xx) NEVER reaches a peer — fail-closed exactly as
    ADR-008 always required."""
    import asyncio
    import adaptive_rag as ar

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise Exception("500 Internal Server Error")

    rescued = {"n": 0}

    async def _fake_fallback(messages, stage, schema=None):
        rescued["n"] += 1
        return None

    import unittest.mock as mock
    monkeypatch.setenv("RAG_EXEC_PEER_FAILOVER", "1")
    with mock.patch.object(ar, "_exec_peer_fallback", _fake_fallback):
        with pytest.raises(Exception, match="500"):
            asyncio.run(ar._llm_call(_Boom(), [("h", "q")], "audit"))
    assert rescued["n"] == 0, "non-quota failures must never touch peers"


def test_exec_peer_pool_is_allowlist_only():
    """The peer pool is an explicit vetted allowlist — never the general
    failover registry, never 8B/20B fleet models, never community lanes."""
    import adaptive_rag as ar
    models = [p["model"] for p in ar._EXEC_PEER_POOL]
    assert "nvidia/nemotron-3-super-120b-a12b" in models
    assert "gemini-3.5-flash" in models

    import re as _re

    def _param_count(m: str):
        # 'nemotron-3-super-120b-a12b' -> 120; 'gemini-3.5-flash' -> None
        mm = _re.search(r"-(\d+)b", m.lower())
        return int(mm.group(1)) if mm else None

    for m in models:
        pc = _param_count(m)
        assert pc is None or pc >= 100, \
            f"{m}: sub-100B models may never hold the executive seat"
        assert "free" not in m.lower(), \
            f"{m}: community/free lanes may never hold the executive seat"


def test_exec_peer_rescue_returns_schema_object(monkeypatch):
    """DEEP-DIVE DEFECT 1 (2026-09-10): the audit stage reads audit.grounded
    — a peer rescue returning raw text would AttributeError exactly when
    the feature fires. With peer_schema set, the peer's raw text is
    repaired + validated into the same GroundingCheck object."""
    import asyncio
    import adaptive_rag as ar

    class _Raw:
        content = '**{"grounded": true, "explanation": "verified"}**'

    captured = {}

    class _FakeEngine:
        def __init__(self, **kw):
            pass

        async def ainvoke(self, messages, config=None, **kw):
            return _Raw()

    import langchain_openai as lo
    monkeypatch.setattr(lo, "ChatOpenAI", _FakeEngine)
    monkeypatch.setenv("NIM_API_KEY", "test-key")

    async def _go():
        return await ar._exec_peer_fallback([("h", "q")], "audit",
                                            schema=ar.GroundingCheck)

    result, collector = asyncio.run(_go())
    assert result.grounded is True
    assert result.explanation == "verified"


def test_exec_peer_unparseable_structured_output_next_peer(monkeypatch):
    """A structured-stage peer whose output cannot be repaired is a
    NON-quota failure — next peer, never certify on garbage."""
    import asyncio
    import adaptive_rag as ar

    class _Raw:
        content = "I cannot audit this."

    class _FakeEngine:
        def __init__(self, **kw):
            pass

        async def ainvoke(self, messages, config=None, **kw):
            return _Raw()

    import langchain_openai as lo
    monkeypatch.setattr(lo, "ChatOpenAI", _FakeEngine)
    monkeypatch.setenv("NIM_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    # Both peers unparseable -> no rescue -> fail-closed path upstream.
    result = asyncio.run(ar._exec_peer_fallback([("h", "q")], "audit",
                                                schema=ar.GroundingCheck))
    assert result is None


def test_quota_walls_do_not_trip_global_circuit(monkeypatch):
    """DEEP-DIVE DEFECT 2 (2026-09-10): quota 429s were counting toward the
    5-failure GLOBAL circuit — a day-capped executive wall could stall
    every stage for 60s. Quota walls are COOLDOWN events (ADR-008's
    ownership rule, now enforced in the pinned branch too); only non-quota
    failures count."""
    import asyncio
    import adaptive_rag as ar

    class _AlwaysQuotaWalled:
        async def ainvoke(self, *a, **k):
            raise Exception("429 Rate limit reached ... TPD: Limit 200000")

    import unittest.mock as mock
    monkeypatch.setenv("RAG_EXEC_PEER_FAILOVER", "1")
    before = ar._circuit.failures
    with mock.patch.object(ar, "_exec_peer_fallback",
                           lambda m, s, schema=None: _none_async()):
        for _ in range(3):
            with pytest.raises(Exception, match="429"):
                asyncio.run(ar._llm_call(_AlwaysQuotaWalled(),
                                         [("h", "q")], "audit"))
    assert ar._circuit.failures == before, \
        "quota walls must never count toward the global circuit"


async def _none_async():
    return None


def test_non_quota_failures_still_trip_circuit():
    """Guardrail: the ownership fix must not mute REAL failures — 5xx and
    timeouts keep counting exactly as before."""
    import asyncio
    import adaptive_rag as ar

    class _Always500:
        async def ainvoke(self, *a, **k):
            raise Exception("500 Internal Server Error")

    before = ar._circuit.failures
    for _ in range(2):
        with pytest.raises(Exception, match="500"):
            asyncio.run(ar._llm_call(_Always500(), [("h", "q")], "audit"))
    assert ar._circuit.failures == before + 2


def test_per_model_usage_tracking():
    """Token-efficiency telemetry (2026-09-10): _track_model_usage accumulates
    per-model token usage so every A/B and battery can answer 'where did the
    budget go?' with measured precision. Tests the tracker directly."""
    import adaptive_rag as ar

    class _FakeCollector:
        input_tokens = 5000
        output_tokens = 700
        calls = 2

    ar._MODEL_USAGE.clear()
    ar._track_model_usage("openai/gpt-oss-120b", _FakeCollector())
    ar._track_model_usage("openai/gpt-oss-120b", _FakeCollector())
    ar._track_model_usage("qwen/qwen3.8-27b", _FakeCollector())

    mu = ar._MODEL_USAGE
    assert "openai/gpt-oss-120b" in mu and "qwen/qwen3.8-27b" in mu
    assert mu["openai/gpt-oss-120b"]["input"] >= 10000
    assert mu["openai/gpt-oss-120b"]["calls"] >= 2
    assert mu["qwen/qwen3.8-27b"]["input"] >= 5000

    # Reset behavior
    ar._MODEL_USAGE.clear()
    assert ar._MODEL_USAGE == {}
