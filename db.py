"""
Enterprise Vector Store, Hybrid Search & Semantic Cache — v2.4 (Migrations + CI)
===================================================================================
1. VERSIONED MIGRATIONS : Schema changes are append-only Migration entries applied
   atomically and recorded in schema_migrations. Re-running applies only new
   versions. Reviewable, repeatable, recorded (migration-runner pattern).
2. MANIFEST-DRIVEN SYNC : Ingestion's SHA256 manifest drives incremental
   re-indexing; changed sources get DELETE+reinsert; changes bump the corpus epoch.
3. CACHE CORRECTNESS    : Composite scope (query similarity + exact filters +
   embedding model + corpus epoch + tenant_id). Eviction radius == hit radius.
4. DEFENSE IN DEPTH     : App-level scoping OVER Postgres RLS. Two-tier identities:
   runtime = non-BYPASSRLS role (RLS enforces); admin = owner with explicit logged
   bypass. verify_rls_runtime_identity() rejects superuser/BYPASSRLS runtime roles.
5. FUZZY COMPANY MATCH  : pg_trgm, tuned threshold 0.15 ('Telsa' resolves).
6. POOL RESILIENCE      : Bounded-retry checkout, zero leak paths, keepalives,
   connect/statement timeouts.
7. EMBEDDING DISCIPLINE : Locked singleton; bge query prefix on QUERIES ONLY;
   raw ndarrays to pgvector's adapter; model stamped per row.
8. HONEST METRICS       : Inserted counts from RETURNING, counted only after COMMIT.

Interface (unchanged from v2.3):
  - pgvector_hybrid_search(..., tenant_id=None)      -> rows include id + chunk_hash
  - check_semantic_cache(query, filters=None, tenant_id=None)
  - save_to_semantic_cache(query, payload, filters=None, tenant_id=None)
  - evict_from_semantic_cache(query, tenant_id=None) # None = purge ALL tenants
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Sequence, Set, Tuple

import psycopg2
from psycopg2 import extras, pool
from pgvector.psycopg2 import register_vector
from pydantic import AliasChoices, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from fastembed import TextEmbedding

from ingest import DocumentChunk

logger = logging.getLogger("PgVectorEngine")

TRANSIENT_DB_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


# ===========================================================================
# 0. CONFIGURATION
# ===========================================================================
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_", env_file=".env", extra="ignore")

    # RUNTIME identity — MUST be a non-BYPASSRLS, non-superuser role (app_rag).
    database_url: str = Field(
        validation_alias=AliasChoices("DB_DATABASE_URL", "NEON_DATABASE_URL")
    )
    # OWNER identity — migrations, purges, stats. Falls back to runtime URL.
    admin_database_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DB_ADMIN_DATABASE_URL", "NEON_ADMIN_DATABASE_URL"),
    )
    embed_model_name: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    query_prefix: str = "Represent this sentence for searching relevant passages: "

    pool_min: int = 2
    pool_max: int = 15
    connect_timeout_s: int = 10
    statement_timeout_ms: int = 20_000
    hnsw_ef_search: int = 100

    cache_similarity: float = 0.92
    cache_ttl_days: int = 7
    migrate_batch_size: int = 64

    candidate_pool: int = 40
    rrf_k: int = 60

    # 0.3 default misses real typos; 0.25 missed 'telsa'->'tesla' (~0.20 sim).
    trgm_similarity_threshold: float = 0.15

    default_tenant: str = "default"

    jsonl_path: Path = Path("corpus_chunks.jsonl")
    manifest_path: Path = Path("corpus_chunks.manifest.json")


_settings: Optional[Settings] = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                try:
                    _settings = Settings()
                except Exception as exc:
                    raise RuntimeError(
                        "database_url missing: set DB_DATABASE_URL (runtime role) "
                        "and DB_ADMIN_DATABASE_URL (owner role) in .env"
                    ) from exc
    return _settings


# ===========================================================================
# 1. EMBEDDING (race-free singleton, explicit query/passage modes)
# ===========================================================================
_embedder: Optional[TextEmbedding] = None
_embedder_lock = threading.Lock()


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                cfg = get_settings()
                logger.info("Loading FastEmbed ONNX model: %s ...", cfg.embed_model_name)
                _embedder = TextEmbedding(model_name=cfg.embed_model_name)
    return _embedder


def embed_query(text: str) -> Any:
    """Raw float32 ndarray — pgvector's registered adapter converts it natively.
    (Python-list conversion yields numpy.float32 scalars psycopg2 cannot adapt.)"""
    return list(get_embedder().embed([get_settings().query_prefix + text]))[0]


def embed_passages(texts: Sequence[str]) -> List[Any]:
    """Raw ndarrays, same adapter convention as queries."""
    return list(get_embedder().embed(list(texts)))


# ===========================================================================
# 2. CONNECTION POOL + TENANT BINDING (RLS enforcement point)
# ===========================================================================
_db_pool: Optional[pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def init_pool() -> None:
    global _db_pool
    if _db_pool is None:
        with _pool_lock:
            if _db_pool is None:
                cfg = get_settings()
                logger.info("Initializing connection pool (min=%d max=%d)...",
                            cfg.pool_min, cfg.pool_max)
                _db_pool = pool.ThreadedConnectionPool(
                    minconn=cfg.pool_min,
                    maxconn=cfg.pool_max,
                    dsn=cfg.database_url,
                    connect_timeout=cfg.connect_timeout_s,
                    keepalives=1, keepalives_idle=30,
                    keepalives_interval=10, keepalives_count=3,
                    application_name="adaptive-rag-engine",
                )


def _configure_session(conn) -> None:
    """Per-session knobs at checkout. Timeout FIRST, then vector registration
    (doubles as liveness probe), then trgm/hnsw knobs."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, false);",
                    (str(get_settings().statement_timeout_ms),))
        register_vector(conn)
        cur.execute("SELECT set_config('pg_trgm.similarity_threshold', %s, false);",
                    (str(get_settings().trgm_similarity_threshold),))
        cur.execute("SELECT set_config('hnsw.ef_search', %s, false);",
                    (str(get_settings().hnsw_ef_search),))


def _checkout_healthy_connection() -> psycopg2.extensions.connection:
    """Bounded-retry checkout. Every connection is handed to the caller or closed."""
    init_pool()
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        conn = _db_pool.getconn()
        try:
            conn.autocommit = True
            _configure_session(conn)
            return conn
        except TRANSIENT_DB_ERRORS as exc:
            last_exc = exc
            _db_pool.putconn(conn, close=True)
            logger.warning("Stale connection recycled (attempt %d/3).", attempt)
    raise last_exc  # type: ignore[misc]


def _bind_tenant(conn, tenant_id: str) -> None:
    """RLS identity binding + read-back verification (fail loud, never leak)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.rls_bypass', 'off', false), "
            "       set_config('app.tenant_id', %s, false);",
            (tenant_id,),
        )
        cur.execute("SELECT current_setting('app.rls_bypass', true), "
                    "current_setting('app.tenant_id', true);")
        bypass, bound = cur.fetchone()
        if bypass != "off" or bound != tenant_id:
            raise RuntimeError(
                f"Tenant binding failed (bypass={bypass!r}, tenant={bound!r}). "
                "Session GUCs require the DIRECT database hostname, not -pooler."
            )


def _unbind_tenant(conn) -> None:
    """Never return a tenant-bound connection to the pool."""
    try:
        with conn.cursor() as cur:
            cur.execute("RESET app.tenant_id; "
                        "SELECT set_config('app.rls_bypass', 'off', false);")
    except Exception:
        logger.warning("Unbind failed on released connection — closing socket.",
                       exc_info=True)
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_db_connection(autocommit: bool = True, *, tenant_id: Optional[str] = None):
    """Tenant-scoped connection. RLS turns a forgotten WHERE clause into an
    empty set — a developer mistake becomes visible, not a breach."""
    conn = _checkout_healthy_connection()
    conn.autocommit = autocommit
    _bind_tenant(conn, tenant_id or get_settings().default_tenant)
    try:
        yield conn
    finally:
        _unbind_tenant(conn)
        _db_pool.putconn(conn, close=bool(conn.closed))


def _connect_admin() -> psycopg2.extensions.connection:
    """Direct, UNPOOLED connection as the OWNER role (DDL/maintenance).

    Two non-negotiables, both learned the hard way:
    1. autocommit=True BEFORE any statement — psycopg2 opens an implicit txn on
       first execute(); flipping autocommit afterwards raises
       'set_session cannot be used inside a transaction'.
    2. app.rls_bypass='on' — FORCE ROW LEVEL SECURITY subjects the OWNER to the
       policies too. Without this GUC: admin INSERTs violate WITH CHECK and
       admin DELETEs/counts see ZERO rows."""
    cfg = get_settings()
    if not cfg.admin_database_url:
        logger.warning("DB_ADMIN_DATABASE_URL not set — admin ops fall back to the "
                       "runtime identity (DDL will fail under least privilege).")
    conn = psycopg2.connect(
        cfg.admin_database_url or cfg.database_url,
        connect_timeout=cfg.connect_timeout_s,
        keepalives=1, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3,
        application_name="adaptive-rag-admin",
    )
    conn.autocommit = True  # MUST precede any execute()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('statement_timeout', %s, false), "
            "       set_config('app.rls_bypass', 'on', false);",
            (str(cfg.statement_timeout_ms),),
        )
        register_vector(conn)
        cur.execute("SELECT current_user;")
        logger.info("Admin identity: %s (RLS bypass engaged)", cur.fetchone()[0])
        cur.execute("SELECT set_config('pg_trgm.similarity_threshold', %s, false);",
                    (str(cfg.trgm_similarity_threshold),))
    return conn


@contextmanager
def admin_connection(autocommit: bool = True):
    """Owner-identity maintenance connection (unpooled)."""
    conn = _connect_admin()
    conn.autocommit = autocommit
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def verify_rls_runtime_identity() -> None:
    """Startup guard: the RUNTIME role must be subject to RLS. Superusers bypass
    RLS implicitly, so rolsuper OR rolbypassrls both disqualify."""
    with get_db_connection(tenant_id="__probe__") as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT current_user, (SELECT rolsuper OR rolbypassrls FROM pg_roles "
            "WHERE rolname = current_user);")
        user, bypasses = cur.fetchone()
    if bypasses:
        raise RuntimeError(
            f"Runtime role '{user}' is superuser or carries BYPASSRLS — RLS cannot "
            "enforce isolation. Point DB_DATABASE_URL at the app_rag role.")
    logger.info("Runtime identity '%s' is RLS-subject ✅", user)


def close_pool() -> None:
    global _db_pool
    if _db_pool is not None:
        _db_pool.closeall()
        _db_pool = None
        logger.info("Connection pool closed.")


atexit.register(close_pool)


def _db_retry() -> Retrying:
    return Retrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=8),
        retry=retry_if_exception_type(TRANSIENT_DB_ERRORS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


# ===========================================================================
# 3. VERSIONED MIGRATIONS (append-only; one atomic transaction per version)
# ===========================================================================
@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    statements: Tuple[str, ...]


MIGRATIONS: Tuple[Migration, ...] = (
    Migration("001", "initial schema: tables, indices, RLS policies", (
        "CREATE EXTENSION IF NOT EXISTS vector;",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm;",

        """CREATE TABLE IF NOT EXISTS multi_agent_chunks (
            id SERIAL PRIMARY KEY,
            chunk_hash VARCHAR(64) UNIQUE NOT NULL,
            content TEXT NOT NULL,
            company VARCHAR(100),
            source VARCHAR(255),
            page INT,
            year INT,
            quarter VARCHAR(8),
            category VARCHAR(50),
            section_title VARCHAR(255),
            contains_table BOOLEAN DEFAULT FALSE,
            embedding_model TEXT,
            tenant_id VARCHAR(50) NOT NULL DEFAULT 'default',
            content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
            embedding VECTOR(384),
            created_at TIMESTAMPTZ DEFAULT now()
        );""",
        "ALTER TABLE multi_agent_chunks ADD COLUMN IF NOT EXISTS quarter VARCHAR(8);",
        "ALTER TABLE multi_agent_chunks ADD COLUMN IF NOT EXISTS contains_table BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE multi_agent_chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;",
        "ALTER TABLE multi_agent_chunks ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(50) NOT NULL DEFAULT 'default';",
        "ALTER TABLE multi_agent_chunks ALTER COLUMN year DROP DEFAULT;",

        """CREATE TABLE IF NOT EXISTS semantic_cache (
            id SERIAL PRIMARY KEY,
            query_text TEXT NOT NULL,
            cache_key_hash VARCHAR(64),
            filters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            embed_model TEXT NOT NULL,
            corpus_epoch BIGINT NOT NULL,
            tenant_id VARCHAR(50) NOT NULL DEFAULT 'default',
            embedding VECTOR(384) NOT NULL,
            cached_response JSONB NOT NULL,
            hit_count INT DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT now(),
            last_accessed TIMESTAMPTZ DEFAULT now()
        );""",
        "ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS cache_key_hash VARCHAR(64);",
        "ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS filters_json JSONB NOT NULL DEFAULT '{}'::jsonb;",
        "ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS embed_model TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5';",
        "ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS corpus_epoch BIGINT NOT NULL DEFAULT 0;",
        "ALTER TABLE semantic_cache ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(50) NOT NULL DEFAULT 'default';",

        """CREATE TABLE IF NOT EXISTS corpus_state (
            id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            epoch BIGINT NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );""",
        "INSERT INTO corpus_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;",

        """CREATE TABLE IF NOT EXISTS source_registry (
            source TEXT PRIMARY KEY,
            pdf_sha256 TEXT NOT NULL,
            chunks INT NOT NULL,
            corpus_epoch BIGINT NOT NULL,
            ingested_at TIMESTAMPTZ DEFAULT now()
        );""",

        "CREATE INDEX IF NOT EXISTS idx_chunks_hnsw ON multi_agent_chunks USING hnsw (embedding vector_cosine_ops);",
        "CREATE INDEX IF NOT EXISTS idx_cache_hnsw ON semantic_cache USING hnsw (embedding vector_cosine_ops);",
        "CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON multi_agent_chunks USING gin (content_tsv);",
        "CREATE INDEX IF NOT EXISTS idx_chunks_company ON multi_agent_chunks (company);",
        "CREATE INDEX IF NOT EXISTS idx_chunks_category ON multi_agent_chunks (category);",
        "CREATE INDEX IF NOT EXISTS idx_chunks_company_trgm ON multi_agent_chunks USING gin ((LOWER(company)) gin_trgm_ops);",
        "CREATE INDEX IF NOT EXISTS idx_chunks_tenant ON multi_agent_chunks (tenant_id);",
        "CREATE INDEX IF NOT EXISTS idx_cache_tenant_epoch ON semantic_cache (tenant_id, corpus_epoch);",
        "CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON semantic_cache (last_accessed);",

        "ALTER TABLE multi_agent_chunks ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE multi_agent_chunks FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS tenant_isolation ON multi_agent_chunks;",
        """CREATE POLICY tenant_isolation ON multi_agent_chunks
               USING      (current_setting('app.rls_bypass', true) = 'on'
                           OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__'))
               WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
                           OR tenant_id = current_setting('app.tenant_id', true));""",

        "ALTER TABLE semantic_cache ENABLE ROW LEVEL SECURITY;",
        "ALTER TABLE semantic_cache FORCE ROW LEVEL SECURITY;",
        "DROP POLICY IF EXISTS tenant_isolation ON semantic_cache;",
        """CREATE POLICY tenant_isolation ON semantic_cache
               USING      (current_setting('app.rls_bypass', true) = 'on'
                           OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__'))
               WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
                           OR tenant_id = current_setting('app.tenant_id', true));""",
    )),
    # 002+ go here. Example:
    # Migration("002", "per-tenant chunk uniqueness", (
    #     "ALTER TABLE multi_agent_chunks DROP CONSTRAINT multi_agent_chunks_chunk_hash_key;",
    #     "ALTER TABLE multi_agent_chunks ADD CONSTRAINT uq_tenant_chunk UNIQUE (tenant_id, chunk_hash);",
    # )),
)


def setup_database() -> None:
    """Applies pending migrations in order, one atomic transaction each, recorded
    in schema_migrations. Re-running applies only new versions. Fully idempotent
    on databases created by older versions of this module (IF NOT EXISTS everywhere)."""
    logger.info("Running schema migrations...")
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(8) PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );""")

    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations;")
        applied = {row[0] for row in cur.fetchall()}

    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        logger.info("Schema up to date (%d migrations applied).", len(applied))
        return

    with admin_connection(autocommit=False) as conn, conn.cursor() as cur:
        for migration in pending:
            logger.info("Applying %s: %s ...", migration.version, migration.description)
            try:
                for stmt in migration.statements:
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (%s, %s);",
                    (migration.version, migration.description))
                conn.commit()
                logger.info("✅ %s applied", migration.version)
            except Exception as exc:
                conn.rollback()
                logger.error("Migration %s FAILED — rolled back: %s",
                             migration.version, exc)
                raise
    logger.info("Schema up to date: %d applied this run, %d total.",
                len(pending), len(MIGRATIONS))


def get_corpus_epoch(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT epoch FROM corpus_state WHERE id = 1;")
        return cur.fetchone()[0]


def bump_corpus_epoch() -> int:
    """Any corpus change bumps the epoch -> cached answers derived from older
    data are instantly unreachable."""
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE corpus_state SET epoch = epoch + 1, updated_at = now() "
            "WHERE id = 1 RETURNING epoch;")
        new_epoch = cur.fetchone()[0]
    logger.warning("Corpus epoch bumped to %d — semantic cache invalidated.", new_epoch)
    return new_epoch


# ===========================================================================
# 4. SEMANTIC CACHE (scoped composite lookup; eviction == hit radius; tenant-aware)
# ===========================================================================
def build_cache_key(query_text: str, filters: Optional[Dict[str, str]],
                    tenant_id: str = "default") -> str:
    """Deterministic scope fingerprint (pure function — unit-tested)."""
    payload = {
        "q": " ".join(query_text.lower().split()),
        "filters": filters or {},
        "model": get_settings().embed_model_name,
        "tenant": tenant_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _normalize_filters(filters: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {k: str(v).strip() for k, v in (filters or {}).items() if v}


def check_semantic_cache(
    query_text: str,
    filters: Optional[Dict[str, str]] = None,
    tenant_id: Optional[str] = None,
    similarity_threshold: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Fuzzy on query text, EXACT on scope: tenant (explicit WHERE + RLS beneath),
    filters, embedding model, and corpus epoch must all match."""
    cfg = get_settings()
    tid = tenant_id or cfg.default_tenant
    threshold = similarity_threshold if similarity_threshold is not None else cfg.cache_similarity
    max_dist = round(1.0 - threshold, 4)
    scope = _normalize_filters(filters)
    qvec = embed_query(query_text)

    sql = """
        SELECT id, query_text, cached_response,
               (embedding <=> %(qvec)s::vector) AS distance
        FROM semantic_cache
        WHERE tenant_id     = %(tenant)s
          AND corpus_epoch  = (SELECT epoch FROM corpus_state WHERE id = 1)
          AND embed_model   = %(model)s
          AND filters_json  = %(scope)s::jsonb
          AND last_accessed > now() - (%(ttl)s * INTERVAL '1 day')
          AND (embedding   <=> %(qvec)s::vector) <= %(max_dist)s
        ORDER BY distance
        LIMIT 1;
    """
    params = {
        "qvec": qvec, "tenant": tid, "model": cfg.embed_model_name,
        "scope": json.dumps(scope), "max_dist": max_dist,
        "ttl": cfg.cache_ttl_days,
    }

    def _run():
        with get_db_connection(tenant_id=tid) as conn, \
                conn.cursor(cursor_factory=extras.DictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    row = _db_retry()(_run)
    if not row:
        return None

    with get_db_connection(tenant_id=tid) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE semantic_cache SET hit_count = hit_count + 1, last_accessed = now() "
            "WHERE id = %s;",
            (row["id"],),
        )
    logger.info("⚡ [CACHE HIT] tenant=%s sim=%.3f | scope=%s | orig: '%s'",
                tid, 1.0 - row["distance"], scope, row["query_text"][:60])
    return dict(row["cached_response"])


def save_to_semantic_cache(
    query_text: str,
    response_payload: Dict[str, Any],
    filters: Optional[Dict[str, str]] = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """Conditional insert: skips if an equivalent answer exists in this scope."""
    cfg = get_settings()
    tid = tenant_id or cfg.default_tenant
    scope = _normalize_filters(filters)
    qvec = embed_query(query_text)
    max_dist = round(1.0 - cfg.cache_similarity, 4)

    sql = """
        INSERT INTO semantic_cache
            (query_text, cache_key_hash, filters_json, embed_model, corpus_epoch,
             tenant_id, embedding, cached_response)
        SELECT %(query_text)s, %(key_hash)s, %(scope)s::jsonb, %(model)s,
               (SELECT epoch FROM corpus_state WHERE id = 1),
               %(tenant)s, %(qvec)s::vector, %(payload)s::jsonb
        WHERE NOT EXISTS (
            SELECT 1 FROM semantic_cache
            WHERE tenant_id    = %(tenant)s
              AND corpus_epoch = (SELECT epoch FROM corpus_state WHERE id = 1)
              AND embed_model  = %(model)s
              AND filters_json = %(scope)s::jsonb
              AND last_accessed > now() - (%(ttl)s * INTERVAL '1 day')
              AND (embedding <=> %(qvec)s::vector) <= %(max_dist)s
        );
    """
    params = {
        "query_text": query_text,
        "key_hash": build_cache_key(query_text, scope, tid),
        "scope": json.dumps(scope),
        "model": cfg.embed_model_name,
        "tenant": tid,
        "qvec": qvec,
        "payload": json.dumps(response_payload),
        "max_dist": max_dist,
        "ttl": cfg.cache_ttl_days,
    }
    try:
        with get_db_connection(tenant_id=tid) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            written = cur.rowcount
        if written:
            logger.info("💾 Cached answer (tenant=%s scope=%s): '%s...'",
                        tid, scope, query_text[:50])
        return bool(written)
    except Exception as exc:
        logger.warning("Cache write skipped (non-fatal): %s", exc)
        return False


def evict_from_semantic_cache(query_text: str, tenant_id: Optional[str] = None) -> int:
    """Poison-pill purge; radius EQUALS the hit radius. tenant_id=None purges
    across ALL tenants via admin connection (a wrong answer is wrong for everyone)."""
    cfg = get_settings()
    max_dist = round(1.0 - cfg.cache_similarity, 4)
    qvec = embed_query(query_text)

    sql = "DELETE FROM semantic_cache WHERE (embedding <=> %s::vector) <= %s"
    params: Tuple[Any, ...] = (qvec, max_dist)
    if tenant_id:
        sql += " AND tenant_id = %s"
        params += (tenant_id,)
    sql += ";"

    try:
        with admin_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            purged = cur.rowcount
        logger.info("🗑️ [CACHE PURGED] %d entr%s matched '%s'",
                    purged, "y" if purged == 1 else "ies", query_text[:60])
        return purged
    except Exception as exc:
        logger.error("Cache eviction failed: %s", exc)
        return 0


def purge_expired_cache() -> int:
    """TTL hygiene — call opportunistically (after each migration)."""
    cfg = get_settings()
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM semantic_cache WHERE last_accessed < now() - (%s * INTERVAL '1 day');",
            (cfg.cache_ttl_days,),
        )
        purged = cur.rowcount
    if purged:
        logger.info("TTL purge: removed %d stale cache entries.", purged)
    return purged


# ===========================================================================
# 5. MANIFEST-DRIVEN MIGRATION (incremental, transactional, honest counts)
# ===========================================================================
INSERT_CHUNKS_SQL = """
    INSERT INTO multi_agent_chunks (
        chunk_hash, content, company, source, page, year, quarter,
        category, section_title, contains_table, embedding_model, tenant_id, embedding
    ) VALUES %s
    ON CONFLICT (chunk_hash) DO NOTHING
    RETURNING id;
"""

UPSERT_REGISTRY_SQL = """
    INSERT INTO source_registry (source, pdf_sha256, chunks, corpus_epoch)
    VALUES (%s, %s, %s, (SELECT epoch FROM corpus_state WHERE id = 1))
    ON CONFLICT (source) DO UPDATE SET
        pdf_sha256 = EXCLUDED.pdf_sha256,
        chunks = EXCLUDED.chunks,
        corpus_epoch = EXCLUDED.corpus_epoch,
        ingested_at = now();
"""


@dataclass
class MigrationReport:
    reindexed: List[str] = field(default_factory=list)
    skipped_unchanged: List[str] = field(default_factory=list)
    deleted_orphaned: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    rows_inserted: int = 0     # COMMITTED rows only
    rows_invalid: int = 0


def _iter_source_batches(
    path: Path, wanted: Set[str], batch_size: int
) -> Generator[Tuple[Optional[str], List[dict]], None, None]:
    """Streams JSONL grouped into per-source batches (memory-constant)."""
    buffer: List[dict] = []
    current: Optional[str] = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line.")
                continue
            src = (row.get("metadata") or {}).get("source")
            if src not in wanted:
                continue
            if src != current and buffer:
                yield current, buffer
                buffer = []
            current = src
            buffer.append(row)
            if len(buffer) >= batch_size:
                yield src, buffer
                buffer = []
    if buffer:
        yield current, buffer


def migrate_from_manifest(
    jsonl_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
) -> MigrationReport:
    """Syncs DB state to the ingestion manifest (admin identity):
      - unchanged SHA256 -> skipped; changed -> DELETE+reinsert per source;
        orphaned registry entries -> deleted; any change -> epoch bump.
      - rows_inserted counts COMMITTED rows only."""
    cfg = get_settings()
    jsonl_path = jsonl_path or cfg.jsonl_path
    manifest_path = manifest_path or cfg.manifest_path
    report = MigrationReport()

    if not jsonl_path.exists():
        raise FileNotFoundError(f"{jsonl_path} not found — run ingest.py first.")

    manifest_shas: Dict[str, str] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_shas = {
            s["filename"]: s.get("pdf_sha256") or ""
            for s in manifest.get("sources", [])
            if s.get("status") != "failed"
        }
    else:
        logger.warning("No manifest found — treating all sources as changed.")

    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT source, pdf_sha256 FROM source_registry;")
        registry = dict(cur.fetchall())

    changed = {n for n, sha in manifest_shas.items() if registry.get(n) != sha}
    report.skipped_unchanged = sorted(set(manifest_shas) - changed)
    report.deleted_orphaned = sorted(set(registry) - set(manifest_shas))

    if not changed and not report.deleted_orphaned:
        logger.info("DB already in sync with manifest — nothing to do.")
        purge_expired_cache()
        return report

    with admin_connection(autocommit=False) as conn, conn.cursor() as cur:
        for orphan in report.deleted_orphaned:
            logger.warning("Source removed upstream — deleting rows for %s", orphan)
            cur.execute("DELETE FROM multi_agent_chunks WHERE source = %s;", (orphan,))
            cur.execute("DELETE FROM source_registry WHERE source = %s;", (orphan,))
        conn.commit()

        tx_source: Optional[str] = None
        tx_count = 0
        try:
            for src, rows in _iter_source_batches(jsonl_path, changed, cfg.migrate_batch_size):
                if src != tx_source:
                    if tx_source is not None:
                        cur.execute(UPSERT_REGISTRY_SQL,
                                    (tx_source, manifest_shas[tx_source], tx_count))
                        conn.commit()
                        report.rows_inserted += tx_count
                        report.reindexed.append(tx_source)
                        logger.info("✅ %s re-indexed (%d chunks)", tx_source, tx_count)
                    tx_source, tx_count = src, 0
                    cur.execute("DELETE FROM multi_agent_chunks WHERE source = %s;", (src,))

                valid: List[DocumentChunk] = []
                for row in rows:
                    try:
                        valid.append(DocumentChunk(**row))
                    except ValidationError as exc:
                        report.rows_invalid += 1
                        logger.warning("Invalid chunk rejected: %s", exc.errors()[:1])

                if valid:
                    embeddings = embed_passages([c.text for c in valid])
                    payload = [
                        (
                            c.chunk_hash, c.text, c.metadata.company, c.metadata.source,
                            c.metadata.page, c.metadata.year, c.metadata.quarter,
                            c.metadata.category, c.metadata.section_title,
                            c.metadata.contains_table, cfg.embed_model_name,
                            cfg.default_tenant, emb,
                        )
                        for c, emb in zip(valid, embeddings)
                    ]
                    inserted_rows = extras.execute_values(
                        cur, INSERT_CHUNKS_SQL, payload, fetch=True)
                    tx_count += len(inserted_rows or [])

            if tx_source is not None:
                cur.execute(UPSERT_REGISTRY_SQL,
                            (tx_source, manifest_shas[tx_source], tx_count))
                conn.commit()
                report.rows_inserted += tx_count
                report.reindexed.append(tx_source)
                logger.info("✅ %s re-indexed (%d chunks)", tx_source, tx_count)
        except Exception as exc:
            conn.rollback()
            logger.error("Migration failed at %s — rolled back cleanly: %s",
                         tx_source, exc)
            report.failed[tx_source or "<unknown>"] = str(exc)

    if report.reindexed or report.deleted_orphaned:
        bump_corpus_epoch()
    purge_expired_cache()

    logger.info("Migration done: reindexed=%s skipped=%s orphans=%s inserted=%d invalid=%d failed=%s",
                report.reindexed, report.skipped_unchanged, report.deleted_orphaned,
                report.rows_inserted, report.rows_invalid, list(report.failed))
    return report


# ===========================================================================
# 6. HYBRID RRF SEARCH (named params, tenant-scoped, fuzzy company, citation fields)
# ===========================================================================
def pgvector_hybrid_search(
    query_text: str,
    category_filter: Optional[str] = None,
    company_filter: Optional[str] = None,
    top_k: int = 10,
    tenant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cfg = get_settings()
    tid = tenant_id or cfg.default_tenant
    qvec = embed_query(query_text)

    params: Dict[str, Any] = {
        "qvec": qvec, "query": query_text, "model": cfg.embed_model_name,
        "pool": cfg.candidate_pool, "rrf_k": cfg.rrf_k, "top_k": top_k,
        "tenant": tid,
    }
    sem_filters = " AND tenant_id = %(tenant)s"
    kw_filters = " AND tenant_id = %(tenant)s"
    if category_filter:
        params["category"] = category_filter
        sem_filters += " AND category = %(category)s"
        kw_filters += " AND category = %(category)s"
    if company_filter:
        params["company"] = company_filter
        fuzzy = "(LOWER(company) = LOWER(%(company)s) OR LOWER(company) %% LOWER(%(company)s))"
        sem_filters += f" AND {fuzzy}"
        kw_filters += f" AND {fuzzy}"

    sql = f"""
    WITH semantic_matches AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS vec_rank
        FROM multi_agent_chunks
        WHERE embedding_model = %(model)s{sem_filters}
        ORDER BY embedding <=> %(qvec)s::vector
        LIMIT %(pool)s
    ),
    keyword_matches AS (
        SELECT id, ROW_NUMBER() OVER (
                   ORDER BY ts_rank(content_tsv, plainto_tsquery('english', %(query)s)) DESC
               ) AS kw_rank
        FROM multi_agent_chunks
        WHERE content_tsv @@ plainto_tsquery('english', %(query)s){kw_filters}
        LIMIT %(pool)s
    )
    SELECT c.id, c.chunk_hash, c.content, c.company, c.source, c.page,
           c.category, c.section_title, c.contains_table,
           COALESCE(1.0 / (%(rrf_k)s + s.vec_rank), 0.0)
         + COALESCE(1.0 / (%(rrf_k)s + k.kw_rank), 0.0) AS fusion_score
    FROM multi_agent_chunks c
    LEFT JOIN semantic_matches s ON c.id = s.id
    LEFT JOIN keyword_matches  k ON c.id = k.id
    WHERE s.id IS NOT NULL OR k.id IS NOT NULL
    ORDER BY fusion_score DESC
    LIMIT %(top_k)s;
    """

    def _run():
        with get_db_connection(tenant_id=tid) as conn, \
                conn.cursor(cursor_factory=extras.DictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    return _db_retry()(_run)


# ===========================================================================
# 7. OPS HELPERS
# ===========================================================================
def health_check(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = get_settings()
    tid = tenant_id or cfg.default_tenant
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT epoch FROM corpus_state WHERE id = 1;")
        epoch = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM multi_agent_chunks;")
        chunks = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM semantic_cache WHERE tenant_id = %s "
            "AND corpus_epoch = (SELECT epoch FROM corpus_state WHERE id = 1);",
            (tid,),
        )
        live_cache = cur.fetchone()[0]
    return {"status": "healthy", "epoch": epoch, "chunks": chunks,
            "live_cache_entries": live_cache, "model": cfg.embed_model_name,
            "tenant": tid}


def corpus_stats() -> Dict[str, Any]:
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT company, COUNT(*) FROM multi_agent_chunks GROUP BY company ORDER BY 2 DESC;")
        by_company = dict(cur.fetchall())
        cur.execute("SELECT category, COUNT(*) FROM multi_agent_chunks GROUP BY category ORDER BY 2 DESC;")
        by_category = dict(cur.fetchall())
    return {"by_company": by_company, "by_category": by_category}


# ===========================================================================
# 8. ENTRY POINT
# ===========================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    setup_database()
    verify_rls_runtime_identity()
    report = migrate_from_manifest()
    logger.info("Health: %s", health_check())

    logger.info("Sanity search: 'Tesla vehicle delivery numbers'...")
    for i, res in enumerate(pgvector_hybrid_search("Tesla vehicle delivery numbers", top_k=2), 1):
        logger.info("  Hit #%d [%s | p%s] %.4f | %s...",
                    i, res["company"], res["page"], res["fusion_score"], res["content"][:60])

    logger.info("Fuzzy company demo: company_filter='Telsa'...")
    fuzzy_hits = pgvector_hybrid_search("vehicle delivery numbers", company_filter="Telsa", top_k=3)
    logger.info("  'Telsa' resolved to: %s (%d hits)",
                {h["company"] for h in fuzzy_hits} or "NOTHING", len(fuzzy_hits))

    test_q = "What is Apple's total net sales in Q4 2023?"
    logger.info("Cache miss expected: %s", check_semantic_cache(test_q) is None)
    save_to_semantic_cache(test_q, {"answer": "$89.5 billion total net sales.", "grounded": True})
    hit = check_semantic_cache("What was Apple's total net sales for Q4 2023?")
    logger.info("Cache hit on similar phrasing: %s", bool(hit))
    logger.info("Eviction purges %d entries.", evict_from_semantic_cache(test_q))

    logger.info("RLS drill: saving entry as tenant 'acme'...")
    save_to_semantic_cache("acme confidential benchmark", {"answer": "acme-only"}, tenant_id="acme")
    with get_db_connection(tenant_id="globex") as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM semantic_cache;")
        seen = cur.fetchone()[0]
    logger.info("  Unscoped SELECT as 'globex' sees %d rows (must be 0)", seen)
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM semantic_cache;")
        logger.info("  Admin path sees %d rows (maintenance still works)", cur.fetchone()[0])
    evict_from_semantic_cache("acme confidential benchmark")
    logger.info("RLS drill complete.")