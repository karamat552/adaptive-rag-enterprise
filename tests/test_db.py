"""
DB test suite.
- UNIT tests: pure logic, no DB. Run everywhere.
- INTEGRATION tests: marked 'integration', auto-skip unless BOTH the admin and
  runtime database paths are REACHABLE. Namespaced under test_* tenants, self-cleaning.
"""
import os
import uuid
from pathlib import Path


def _ensure_db_url_for_unit_tests() -> None:
    """Unit tests construct Settings, which requires a database_url to exist.

    PRECEDENCE GOTCHA (the bug this replaces): an environment variable BEATS the
    .env file in pydantic-settings. Blindly os.environ.setdefault()-ing a dummy
    shadows the real URL that lives only in .env — the normal local setup — and
    silently points the runtime pool at localhost. So: inject the dummy ONLY when
    neither the process env NOR the .env file provides a URL."""
    for name in ("DB_DATABASE_URL", "NEON_DATABASE_URL"):
        if os.getenv(name):
            return
    env_file = Path(".env")
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8")
        if "DB_DATABASE_URL=" in text or "NEON_DATABASE_URL=" in text:
            return
    os.environ["DB_DATABASE_URL"] = "postgresql://unit:unit@localhost:5432/unit"


_ensure_db_url_for_unit_tests()

import pytest  # noqa: E402

integration = pytest.mark.integration


# ===========================================================================
# UNIT TESTS (no DB)
# ===========================================================================
def test_cache_key_scope_separation():
    """Different filters / tenants / years must never share cache entries."""
    from db import build_cache_key
    base = build_cache_key("what was revenue", None, "default")
    assert base != build_cache_key("what was revenue", {"companies": "tesla"}, "default")
    assert base != build_cache_key("what was revenue", None, "acme")
    assert base != build_cache_key("what was revenue in 2024", None, "default")


def test_cache_key_query_normalization():
    from db import build_cache_key
    assert (build_cache_key("  What   WAS Revenue ", None, "t")
            == build_cache_key("what was revenue", None, "t"))


def test_eviction_radius_equals_hit_radius():
    """THE v1 bug: hits served at sim>=0.85 but eviction deleted at sim>=0.93 —
    poison survived its own purge. One shared constant must drive both."""
    from db import get_settings
    cfg = get_settings()
    assert round(1.0 - cfg.cache_similarity, 4) == 0.08


def test_normalize_filters_drops_empty_values():
    from db import _normalize_filters
    assert _normalize_filters({"company": " Tesla ", "year": "", "q": None}) \
        == {"company": "Tesla"}


# ===========================================================================
# INTEGRATION TESTS
# ===========================================================================
TENANT_A = "test_acme"
TENANT_B = "test_globex"


@pytest.fixture(scope="session")
def db_env():
    """Integration gate: BOTH identity paths must be reachable, else skip.
    Probing only admin would let a poisoned runtime URL produce five messy
    failures instead of one clean skip — the exact bug this fixture replaces."""
    from db import admin_connection, get_db_connection
    try:
        with admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
        with get_db_connection(tenant_id="__probe__") as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
    except Exception:
        pytest.skip("Database not reachable on both identity paths — skipping integration tests")


@pytest.fixture()
def clean_test_tenants(db_env):
    from db import admin_connection

    def _purge():
        with admin_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM semantic_cache WHERE tenant_id IN (%s, %s);",
                        (TENANT_A, TENANT_B))

    _purge()
    yield
    _purge()


@integration
def test_rls_fail_closed_unscoped_query(clean_test_tenants):
    """THE security guarantee: a future dev's unscoped query sees NOTHING of
    other tenants. This exact scenario was a live leak before the role split."""
    from db import save_to_semantic_cache, get_db_connection
    marker = uuid.uuid4().hex[:8]
    assert save_to_semantic_cache(
        f"acme secret query {marker}", {"answer": "acme-only"}, tenant_id=TENANT_A)

    with get_db_connection(tenant_id=TENANT_B) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM semantic_cache;")   # unscoped ON PURPOSE
        assert cur.fetchone()[0] == 0


@integration
def test_rls_admin_path_still_operates(clean_test_tenants):
    from db import save_to_semantic_cache, admin_connection
    marker = uuid.uuid4().hex[:8]
    assert save_to_semantic_cache(
        f"acme secret query {marker}", {"answer": "x"}, tenant_id=TENANT_A)
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM semantic_cache WHERE tenant_id = %s;",
                    (TENANT_A,))
        assert cur.fetchone()[0] >= 1


@integration
def test_cache_roundtrip_and_scope_separation(clean_test_tenants):
    from db import check_semantic_cache, save_to_semantic_cache
    q = f"apple total net sales q4 {uuid.uuid4().hex[:8]}"
    apple = {"companies": "apple"}
    tesla = {"companies": "tesla"}
    # Tenant-scoped on purpose: clean_test_tenants purges before/after, keeping
    # the miss assertion hermetic. (In the default tenant this exact template is
    # NOT hermetic — bge embeddings of a uuid-swapped template stay > 0.92
    # similar, so rows left by earlier runs fuzzy-hit the "miss" check.)
    assert check_semantic_cache(q, filters=apple, tenant_id=TENANT_A) is None
    assert save_to_semantic_cache(q, {"answer": "89.5B"}, filters=apple,
                                  tenant_id=TENANT_A)
    assert check_semantic_cache(q, filters=apple, tenant_id=TENANT_A) is not None
    assert check_semantic_cache(q, filters=tesla, tenant_id=TENANT_A) is None


@integration
def test_migration_is_incremental(db_env):
    """First call syncs (no-op on an already-synced DB); second call MUST also
    be a no-op. Requires corpus_chunks.jsonl + manifest in the working dir."""
    from db import migrate_from_manifest
    r1 = migrate_from_manifest()
    assert not r1.failed
    r2 = migrate_from_manifest()
    assert not r2.reindexed and not r2.deleted_orphaned and not r2.failed


@integration
def test_search_returns_citation_fields(db_env):
    from db import pgvector_hybrid_search
    rows = pgvector_hybrid_search("Tesla vehicle delivery numbers", top_k=3)
    if not rows:
        pytest.skip("corpus empty — run migrate_from_manifest first")
    assert "chunk_hash" in rows[0] and "id" in rows[0]


@integration
def test_runtime_identity_guard_passes(db_env):
    """On correct infra (app_rag) this is a no-op; on a superuser/BYPASSRLS
    runtime URL it raises — the exact regression it exists to catch."""
    from db import verify_rls_runtime_identity
    verify_rls_runtime_identity()

def test_reingest_flow_calls_transcript_sync():
    """Deep-dive finding (2026-09-10): sync_page_transcripts was defined
    but NEVER CALLED — a re-ingest would DELETE+reinsert chunks and bump
    the epoch while page_transcripts silently held the OLD corpus text.
    The db.py main entry must sync transcripts after migrate_from_manifest
    WHEN (and only when) the corpus changed. Source-level contract test:
    the __main__ block references sync after migrate."""
    import re
    src = open(Path(__file__).parent.parent / "db.py", encoding="utf-8").read()
    main_block = src[src.index('if __name__ == "__main__":'):]
    m_migrate = main_block.index("migrate_from_manifest()")
    m_sync = main_block.index("sync_page_transcripts()")
    m_bump_in_migrate = src.index("bump_corpus_epoch()")  # inside migrate
    assert m_migrate < m_sync, \
        "transcript sync must run AFTER migrate (it reads the new epoch)"
    # migrate itself bumps BEFORE returning; sync reads corpus_state fresh.
    assert "if report.reindexed or report.deleted_orphaned:" in main_block, \
        "sync must be conditional on the corpus actually changing"


def test_verify_named_statuses_on_epoch_collision():
    """The three named failure statuses a cross-epoch verify can produce —
    regression-locked so a re-ingest can never make an old receipt
    silently pass: unknown_chunk (hash deleted by re-ingest),
    transcript_missing (page row gone), span/hash mismatch."""
    # Pure contract check: the statuses exist in the verify chain and the
    # verified computation requires ALL links ok.
    src = open(Path(__file__).parent.parent / "db.py", encoding="utf-8").read()
    for status in ("unknown_chunk", "transcript_missing",
                   "span_out_of_bounds", "attribution_mismatch"):
        assert f'"{status}"' in src, f"{status} must remain a named status"
    assert "links_ok == links_checked" in src, \
        "verified requires every single link to pass — no averaging"


def test_pool_bounded_wait_absorbs_burst(monkeypatch):
    """Concurrency-audit finding (2026-09-10): psycopg2's ThreadedConnection
    pool RAISES PoolError instantly on exhaustion (never blocks — verified
    from psycopg2's own _getconn source). At worst-burst, 8 admitted runs x
    3 parallel specialist searches = 24 concurrent _db_calls against
    pool_max=15: without a wait, ~9 calls fail loudly mid-run. The checkout
    now waits up to DB_POOL_WAIT_S (poll 0.2s) before raising — the burst
    drains through the pool as connections are released (~ms per checkout),
    and only a genuinely saturated pool still fails, loudly."""
    import time as _time
    import db
    from psycopg2 import pool as _pgpool

    state = {"free": 0, "checked_out": 0}   # exhausted at entry — the burst state

    class _FakeConn:
        autocommit = False

        def cursor(self):
            class _Cur:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def execute(self, *a):
                    return None
            return _Cur()

    class _FakePool:
        def getconn(self):
            if state["free"] > 0:
                state["free"] -= 1
                return _FakeConn()
            raise _pgpool.PoolError("connection pool exhausted")

        def putconn(self, conn, close=False):
            state["free"] += 1

    monkeypatch.setattr(db, "_db_pool", _FakePool())
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(db, "_configure_session", lambda conn: None)

    class _WaitSettings:
        pool_wait_s = 0.6
    monkeypatch.setattr(db, "get_settings", lambda: _WaitSettings())

    # Exhausted at t=0; a connection frees at ~0.3s -> checkout must WAIT
    # and succeed, not raise.
    import threading as _th

    def _free_soon():
        _time.sleep(0.3)
        state["free"] += 1

    _th.Thread(target=_free_soon, daemon=True).start()
    t0 = _time.perf_counter()
    conn = db._checkout_healthy_connection()
    waited = _time.perf_counter() - t0
    assert conn is not None
    assert waited >= 0.25, f"checkout must wait for the freed slot, waited {waited:.2f}s"

    # A pool that never frees: still fails loudly after the bounded wait.
    state["free"] = 0

    class _ShortWaitSettings:
        pool_wait_s = 0.4
    monkeypatch.setattr(db, "get_settings", lambda: _ShortWaitSettings())
    t0 = _time.perf_counter()
    try:
        db._checkout_healthy_connection()
        raised = False
    except _pgpool.PoolError:
        raised = True
    waited = _time.perf_counter() - t0
    assert raised, "a genuinely saturated pool must still fail loudly"
    assert 0.3 <= waited < 2.0, "fail after the bounded window, not instantly, not forever"
