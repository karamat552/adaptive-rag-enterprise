-- ============================================================================
-- MIGRATION 008 (DRAFT — FOR OWNER REVIEW; NOT WIRED INTO db.py MIGRATIONS
-- UNTIL WRITTEN APPROVAL. The db.py runner auto-applies pending migrations
-- on service boot, so wiring this before approval would apply it to the
-- production Neon on the next deploy.)
--
-- ADR-021: per-tenant epochs + public/private corpus scoping (Phase 1).
-- Design: the dual-epoch model — corpus_state (id=1) stays the PUBLIC
-- epoch untouched (nightly battery bit-identical); tenant_corpus_state
-- governs private uploads only.
--
-- Lock estimates (Neon, small tables): every statement below takes a brief
-- ACCESS EXCLUSIVE lock on the table it creates/alters. Worst case is the
-- semantic_cache ADD COLUMN (concurrent writes block for the catalog-only
-- change, sub-second at our row counts — Neon does not rewrite the table
-- for a nullable column add). ALTER POLICY on multi_agent_chunks /
-- page_transcripts takes a ShareRowExclusive lock: concurrent SELECTs
-- (RLS reads) are NOT blocked; concurrent INSERTs would queue for the
-- milliseconds the DDL takes. No long-running transactions exist in the
-- app (all DB calls are short), so lock-wait risk is negligible.
-- ============================================================================

-- ============================== UP ==========================================

-- 1. Per-tenant private epoch. tenant_id matches the existing RLS tables
--    (VARCHAR(50) — verified against fact_rows migration 006 and
--    shadow_disagreements migration 007).
CREATE TABLE IF NOT EXISTS tenant_corpus_state (
    tenant_id  VARCHAR(50) PRIMARY KEY,
    epoch      BIGINT       NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

ALTER TABLE tenant_corpus_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_corpus_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON tenant_corpus_state;
CREATE POLICY tenant_isolation ON tenant_corpus_state
    USING      (current_setting('app.rls_bypass', true) = 'on'
                OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__'))
    WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
                OR tenant_id = current_setting('app.tenant_id', true));

-- 2. Cache + receipts gain the tenant-epoch column (nullable; backfill
--    sets it; new writes always set it). Nullable so pre-approval rows and
--    the backfill window coexist without NOT NULL rewrite locks.
ALTER TABLE semantic_cache        ADD COLUMN IF NOT EXISTS tenant_epoch BIGINT;
ALTER TABLE verification_receipts ADD COLUMN IF NOT EXISTS tenant_epoch BIGINT;

-- 3. Public-corpus visibility: corpus tables admit the caller's tenant OR
--    the reserved public tenant 'default'. Cache, receipts, fact_rows stay
--    STRICT (a tenant's cached answers are private by contract — cache
--    poisoning test 11 depends on this).
DROP POLICY IF EXISTS tenant_isolation ON multi_agent_chunks;
CREATE POLICY tenant_isolation ON multi_agent_chunks
    USING      (current_setting('app.rls_bypass', true) = 'on'
                OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__')
                OR tenant_id = 'default')
    WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
                OR tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS tenant_isolation ON page_transcripts;
CREATE POLICY tenant_isolation ON page_transcripts
    USING      (current_setting('app.rls_bypass', true) = 'on'
                OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__')
                OR tenant_id = 'default')
    WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
                OR tenant_id = current_setting('app.tenant_id', true));

-- 4. Grants: runtime reads its own row for cache validation; writes and
--    epoch bumps are admin-plane (ingest runs as the admin identity).
GRANT SELECT ON tenant_corpus_state TO app_rag;
GRANT USAGE ON SCHEMA public TO app_rag;              -- no-op if present
REVOKE INSERT, UPDATE, DELETE ON tenant_corpus_state FROM app_rag;

-- ============================== BACKFILL ====================================
-- Every existing tenant starts at the current PUBLIC epoch: their
-- backfilled cache rows (tenant_epoch = public epoch) are valid at write
-- time, and no private upload has ever bumped them. Idempotent.
INSERT INTO tenant_corpus_state (tenant_id, epoch)
SELECT DISTINCT tenant_id, (SELECT epoch FROM corpus_state WHERE id = 1)
FROM semantic_cache
ON CONFLICT (tenant_id) DO NOTHING;

INSERT INTO tenant_corpus_state (tenant_id, epoch)
VALUES ('default', (SELECT epoch FROM corpus_state WHERE id = 1))
ON CONFLICT (tenant_id) DO NOTHING;

UPDATE semantic_cache
SET    tenant_epoch = (SELECT epoch FROM corpus_state WHERE id = 1)
WHERE  tenant_epoch IS NULL;

UPDATE verification_receipts
SET    tenant_epoch = (SELECT epoch FROM corpus_state WHERE id = 1)
WHERE  tenant_epoch IS NULL;

-- ============================== ROLLBACK ====================================
-- Every piece is additive; rollback drops the columns/table and restores
-- the strict corpus policies. Cache soft-invalidates by design (rows keep
-- working — the old single-epoch predicate ignores tenant_epoch).
-- Receipt chain verification falls back to corpus_epoch (additive column,
-- the old code never reads it). No data loss possible.
--
-- ALTER TABLE semantic_cache        DROP COLUMN IF EXISTS tenant_epoch;
-- ALTER TABLE verification_receipts DROP COLUMN IF EXISTS tenant_epoch;
-- DROP TABLE IF EXISTS tenant_corpus_state;
-- (restore strict corpus policies:)
-- DROP POLICY IF EXISTS tenant_isolation ON multi_agent_chunks;
-- CREATE POLICY tenant_isolation ON multi_agent_chunks
--     USING      (current_setting('app.rls_bypass', true) = 'on'
--                 OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__'))
--     WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
--                 OR tenant_id = current_setting('app.tenant_id', true));
-- DROP POLICY IF EXISTS tenant_isolation ON page_transcripts;
-- CREATE POLICY tenant_isolation ON page_transcripts
--     USING      (current_setting('app.rls_bypass', true) = 'on'
--                 OR tenant_id = COALESCE(current_setting('app.tenant_id', true), '__unbound__'))
--     WITH CHECK (current_setting('app.rls_bypass', true) = 'on'
--                 OR tenant_id = current_setting('app.tenant_id', true));

-- ============================== C7 INCREMENT (for reference) ==================
-- The atomic epoch bump, executed INSIDE the ingestion transaction
-- (row lock serializes concurrent ingests; the ensure-row handles
-- first-creation). Python-side code:
--   cur.execute("INSERT INTO tenant_corpus_state (tenant_id) VALUES (%s) "
--               "ON CONFLICT (tenant_id) DO NOTHING", (tenant,))
--   cur.execute("UPDATE tenant_corpus_state SET epoch = epoch + 1, "
--               "updated_at = now() WHERE tenant_id = %s "
--               "RETURNING epoch", (tenant,))
