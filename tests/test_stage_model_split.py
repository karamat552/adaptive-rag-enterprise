"""THE SPLIT (ADR-026): the auditor's own model + the vetted endpoint route.

KNOWN_ISSUES #11's circularity, narrowed: RAG_AUDIT_MODEL gives the auditor
its own model (maker != checker — uncorrelated blind spots); the vetted
route table (_MODEL_ENDPOINT_ROUTES) points a benchmark-vetted model at its
home provider's endpoint, so a NIM model name is never silently sent to the
global provider (a 400 model-not-found there would quarantine every
synthesis). Failing-first tests for both halves.

CI-SAFE BY CONSTRUCTION: no test invokes a model; engine BUILDS run only
with a patched key env (ChatOpenAI validates keys at invoke, not build) —
the same lesson as the retrieval-branch CI failure (2026-10).
"""
import inspect
import os
import sys

import pytest

import adaptive_rag as ar


class _FakeChat:
    """Captures constructor kwargs. Installed as langchain_openai.ChatOpenAI
    via sys.modules so the route tests never import the real module (the
    local Application Control policy blocks its jiter DLL under pytest;
    CI has no such policy — but a stub is hermetic everywhere)."""
    def __init__(self, **kw):
        self.kwargs = kw


def _install_fake_chat(monkeypatch) -> list:
    calls: list = []

    def _capture(**kw):
        calls.append(kw)
        return _FakeChat(**kw)

    monkeypatch.setitem(sys.modules, "langchain_openai",
                        type("M", (), {"ChatOpenAI": _capture}))
    return calls


def _fresh_settings(**kw) -> ar.RagSettings:
    """Hermetic settings: explicit init kwargs override .env and process
    env (pydantic-settings: init kwargs are the highest priority)."""
    base = dict(provider="groq", router_model=None, fleet_model=None,
                executive_model=None, audit_model=None)
    base.update(kw)
    return ar.RagSettings(**base)


@pytest.fixture(autouse=True)
def _clean_caches_and_settings():
    """Engine/structured singletons are per-model caches and _S is module-
    global: a test that builds a real engine must not pollute (or read)
    another test's cache, and a settings override must never leak."""
    saved_engines = dict(ar._engines)
    saved_structured = dict(ar._structured)
    saved_settings = ar._S
    ar._engines.clear()
    ar._structured.clear()
    yield
    ar._engines.clear()
    ar._engines.update(saved_engines)
    ar._structured.clear()
    ar._structured.update(saved_structured)
    ar._S = saved_settings


# ---------------------------------------------------------------- the split
def test_audit_falls_back_to_executive_when_unset():
    """Zero behavior change: no RAG_AUDIT_MODEL -> audit == the executive
    model (the pre-split same-model default; groq's strongest)."""
    ar._S = _fresh_settings()
    assert ar.get_stage_model("audit") == ar.get_stage_model("executive")
    assert ar.get_stage_model("audit") == "openai/gpt-oss-120b"


def test_audit_falls_back_with_blank_env_value():
    """A blank RAG_AUDIT_MODEL= (the .env.example template shape) behaves
    exactly like unset — the or-chain treats '' as falsy."""
    ar._S = _fresh_settings(audit_model="")
    assert ar.get_stage_model("audit") == "openai/gpt-oss-120b"


def test_split_engages_when_audit_model_set():
    """RAG_AUDIT_MODEL set -> the auditor resolves its OWN model while
    synthesis stays on the executive model — a SPLIT, never a swap."""
    ar._S = _fresh_settings(audit_model="test-audit-model",
                            executive_model="test-exec-model")
    assert ar.get_stage_model("audit") == "test-audit-model"
    assert ar.get_stage_model("executive") == "test-exec-model"


def test_audit_provider_default_is_the_executive_default():
    """A provider whose defaults dict has no 'audit' key must not KeyError:
    the audit's provider default IS the executive default."""
    ar._S = _fresh_settings()
    assert ar.get_stage_model("audit") == \
        ar._PROVIDER_MODEL_DEFAULTS["groq"]["executive"]


def test_openai_compatible_audit_fails_loud_when_unset():
    """The generic seam's models are mandatory config: an unset audit model
    must raise (fail-closed), never resolve to an empty model name."""
    ar._S = ar.RagSettings(provider="openai_compatible", base_url="https://x/v1",
                           router_model=None, fleet_model=None,
                           executive_model=None, audit_model=None)
    with pytest.raises(ValueError):
        ar.get_stage_model("audit")


# ---------------------------------------------------------------- the wiring
def test_checker_builds_with_the_audit_model(monkeypatch):
    """Behavioral wiring: _get_checker must build the checker engine with
    the AUDIT stage's resolved model, not the executive's."""
    captured: dict = {}
    monkeypatch.setattr(ar, "_get_engine",
                        lambda m: captured.setdefault("model", m))
    monkeypatch.setattr(ar, "_repairing_structured",
                        lambda engine, schema: ("checker", engine))
    ar._S = _fresh_settings(audit_model="test-audit-model",
                            executive_model="test-exec-model")
    ar._get_checker()
    assert captured["model"] == "test-audit-model"


def test_checker_source_resolves_audit_stage():
    """Source inspection (the lineage-sig test convention): the audit getter
    must reference the audit stage — a silent revert to 'executive' fails
    this pin."""
    src = inspect.getsource(ar._get_checker)
    assert 'get_stage_model("audit")' in src


# ------------------------------------------------------- the endpoint route
def test_nim_route_builds_against_nim_endpoint(monkeypatch):
    """The vetted route: a NIM model name must be built against NIM's
    endpoint with NIM's key — sent to the global provider (Groq) it would
    400 model-not-found and quarantine every synthesis."""
    calls = _install_fake_chat(monkeypatch)
    monkeypatch.setenv("NIM_API_KEY", "test-key")
    ar._build_engine("nvidia/nemotron-3-super-120b-a12b")
    assert calls, "the engine builder must construct a chat engine"
    assert calls[0]["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert calls[0]["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert calls[0]["api_key"] == "test-key"


def test_nim_route_fail_closed_without_key(monkeypatch):
    """The route's key env unset -> ValueError at BUILD time (fail-closed),
    never a NIM model name silently sent to the global provider."""
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    with pytest.raises(ValueError):
        ar._build_engine("nvidia/nemotron-3-super-120b-a12b")


def test_route_never_applies_to_fleet_models(monkeypatch):
    """A fleet model resolves through the global provider as before — the
    route table is executive-class only (community lanes stay fleet-only
    per ADR-008)."""
    calls = _install_fake_chat(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    ar._S = _fresh_settings()
    ar._build_engine("openai/gpt-oss-20b")
    assert calls
    assert "api.groq.com" in calls[0]["base_url"]
    assert calls[0]["api_key"] == "test-key"


# ---------------------------------------------------------------- the health
def test_health_reports_audit_model_and_pin_state():
    """get_health must report the resolved audit model and reflect the
    split honestly: executive_pinned is True only while the audit model is
    unset (blank counts as unset — the or-chain semantics)."""
    ar._S = _fresh_settings()
    h = ar.get_health()
    assert h["models"]["audit"] == "openai/gpt-oss-120b"
    assert h["failover"]["executive_pinned"] is True
    ar._S = _fresh_settings(audit_model="test-audit-model")
    h = ar.get_health()
    assert h["models"]["audit"] == "test-audit-model"
    assert h["failover"]["executive_pinned"] is False
