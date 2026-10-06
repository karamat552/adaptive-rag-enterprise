"""The 49.7-calls-per-question finding (2026-10-06, B real bench): the
healthy pipeline is ~10-13 LLM calls per question (measured per-node:
gateway 1 + fleet 3 + synthesis 1-5 + audit 1); the pathological tail
(retry loop x3 pipeline passes + in-client retries + peer rescues under
quota weather) measured 49.7 calls / 178K tokens per question. The
per-run call budget must fire and fail closed — bounded, never unbounded.

Offline: engines are fakes; zero tokens, zero DB.
"""
import asyncio
from types import SimpleNamespace

import pytest

import adaptive_rag as ar


async def _fake_ainvoke(messages, config=None):
    return SimpleNamespace(content="ok [1]")


def _engine():
    return SimpleNamespace(ainvoke=_fake_ainvoke)


def test_call_budget_fires_at_the_cap(monkeypatch):
    """At the cap, the next call must raise LLMBudgetExceeded (fail-closed),
    not silently proceed into a 49.7-call storm."""
    ar._MODEL_USAGE.clear()
    ar._MODEL_USAGE["test/model"] = {"input": 0, "output": 0, "calls": 24}
    s = ar.get_settings()
    monkeypatch.setattr(s, "max_llm_calls_per_run", 24, raising=False)
    with pytest.raises(ar.LLMBudgetExceeded):
        asyncio.run(ar._llm_call(_engine(), [("human", "q")], "test",
                                 allow_failover=False))


def test_call_budget_is_quota_class_not_circuit_class():
    """A budget exhaustion is a cooldown event, not a systemic failure: it
    must NOT trip the 5-failure circuit (the ADR-008 ownership rule)."""
    assert ar._is_quota_error(
        ar.LLMBudgetExceeded("rate limit: per-run call budget exhausted"))


def test_healthy_run_below_cap_proceeds(monkeypatch):
    """The healthy profile (~10-13 calls) must be unaffected: 3 calls under
    a cap of 24 proceed normally."""
    ar._MODEL_USAGE.clear()
    ar._MODEL_USAGE["test/model"] = {"input": 0, "output": 0, "calls": 3}
    s = ar.get_settings()
    monkeypatch.setattr(s, "max_llm_calls_per_run", 24, raising=False)
    result, _collector = asyncio.run(
        ar._llm_call(_engine(), [("human", "q")], "test", allow_failover=False))
    assert result.content == "ok [1]"
