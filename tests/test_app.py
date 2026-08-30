"""
UI Smoke Tests (Streamlit AppTest — offline, no gateway, CI-safe)
=================================================================
Runs app.py as a real script through Streamlit's testing framework and asserts
the production UX contract on the degraded path (gateway down):
  * the script executes with NO uncaught exception,
  * the gateway-down state renders guidance instead of crashing,
  * onboarding empty-state + suggested questions render.
The full interactive flow (chat submit -> SSE -> evidence) is exercised live by
scripts/smoke_gateway.py against a running gateway.

Run:  pytest tests/test_app.py -v
"""
import os
from pathlib import Path

# Same dummy-guard pattern as tests/test_db.py for bare CI environments.
if not (os.getenv("DB_DATABASE_URL") or os.getenv("NEON_DATABASE_URL")
        or (Path(".env").exists()
            and ("DB_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")
                 or "NEON_DATABASE_URL=" in Path(".env").read_text(encoding="utf-8")))):
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

DEAD_GATEWAY = "http://localhost:1"   # port 1: connection refused, instantly


def _isolated_env(monkeypatch):
    """Make gateway-state tests deterministic: point the app at a guaranteed-
    dead endpoint and clear Streamlit's global caches so a client from a
    previous run (or a LIVE local gateway) can't leak in."""
    monkeypatch.setenv("API_BASE_URL", DEAD_GATEWAY)
    st.cache_data.clear()
    st.cache_resource.clear()


def test_app_boot_and_degraded_path(monkeypatch):
    _isolated_env(monkeypatch)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"app.py raised: {at.exception}"
    # Gateway-down UX: sidebar error + onboarding info render gracefully.
    assert at.sidebar.error, "expected the gateway-unreachable banner in the sidebar"
    assert at.info, "expected the onboarding empty-state message"
    # Suggested questions render as clickable one-click starts.
    assert len(at.button) >= 3, "expected the suggested-question buttons to render"


def test_app_chat_input_present(monkeypatch):
    _isolated_env(monkeypatch)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception
    assert at.chat_input, "expected the chat input on the main surface"
