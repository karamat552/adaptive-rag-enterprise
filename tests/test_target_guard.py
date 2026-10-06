"""target_guard tests — the holes that were FOUND, not imagined.

These pin the guard's classification so a rewrite cannot silently
reintroduce them. The mixed-URL hole is the one that matters most: a
guard that reads only the FIRST configured URL classified a local
runtime + production ADMIN combination as safe while writes still
reached production (found 2026-10-05 in the audit-tool review; the
audit's writes split across the two identities).

Pure functions; no DB, no network, no keys. 0.02s.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import target_guard


def _u(url):
    return target_guard._parse(url)


def test_mixed_urls_classify_production(monkeypatch):
    """THE hole: a local runtime URL + a production admin URL must classify
    as production and refuse — writes split across identities, so reading
    only the first URL let that combination pass as 'safe'."""
    monkeypatch.setattr(target_guard, "_production_targets",
                        lambda: {("prod-host.neon.tech", "neondb")})
    worst, offending = target_guard.classify_targets({
        "runtime": "postgresql://app_rag:pw@localhost:5432/ragdb",
        "admin": "postgresql://owner:pw@prod-host.neon.tech/neondb",
    })
    assert worst == "production"
    assert "admin" in offending


def test_both_identities_production(monkeypatch):
    monkeypatch.setattr(target_guard, "_production_targets",
                        lambda: {("prod-host.neon.tech", "neondb")})
    worst, offending = target_guard.classify_targets({
        "runtime": "postgresql://app_rag:pw@prod-host.neon.tech/neondb",
        "admin": "postgresql://owner:pw@prod-host.neon.tech/neondb",
    })
    assert worst == "production"
    assert set(offending) == {"runtime", "admin"}


def test_disposable_database_on_production_host_is_allowed(monkeypatch):
    """Classification is per (host, database), never host alone: the Neon
    disposable pattern (same host, different database) must be allowed —
    the production guard's job is protecting THE production database."""
    monkeypatch.setattr(target_guard, "_production_targets",
                        lambda: {("prod-host.neon.tech", "neondb")})
    worst, offending = target_guard.classify_targets({
        "runtime": "postgresql://app_rag:pw@prod-host.neon.tech/disposable_measure",
        "admin": "postgresql://owner:pw@prod-host.neon.tech/disposable_measure",
    })
    assert worst == "local-or-disposable"
    assert offending == []


def test_credential_strip_in_parse():
    """The netloc carries user:pass@host — the comparison must strip it
    (found live 2026-10-06: the guard silently didn't fire because the
    credential fragment broke the host equality)."""
    parsed = _u("postgresql://app_rag:secret@host.example.com/neondb")
    assert parsed == ("host.example.com", "neondb")


def test_local_targets_always_local():
    for url in ("postgresql://u:p@localhost:5432/x",
                "postgresql://u:p@127.0.0.1:5432/x",
                "postgresql://u:p@::1/x",
                "postgresql://u:p@/tmp/pgdata/x"):
        assert target_guard._is_local(url), url


def test_missing_env_refuses_remote(monkeypatch):
    """FAIL-CLOSED on unknown remotes: when .env is missing or unreadable,
    a remote target cannot be verified as non-production — it must be
    refused, not passed (the old version failed OPEN there)."""
    monkeypatch.setattr(target_guard, "REPO", Path("/nonexistent-repo-xyz"))
    worst, offending = target_guard.classify_targets({
        "runtime": "postgresql://u:p@some-remote-host.example.com/somedb",
    })
    assert worst == "unknown-remote"
    assert offending == ["runtime"]


def test_missing_env_allows_local(monkeypatch):
    """Local targets stay allowed even when .env cannot be read — the
    guard's job is protecting production, not blocking localhost dev."""
    monkeypatch.setattr(target_guard, "REPO", Path("/nonexistent-repo-xyz"))
    worst, offending = target_guard.classify_targets({
        "runtime": "postgresql://u:p@localhost:5432/x",
    })
    assert worst == "local-or-disposable"
    assert offending == []
