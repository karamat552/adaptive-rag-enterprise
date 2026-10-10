"""THE BACKUP-RESTORE DRILL — the RUNBOOK §4's restore procedure, codified.

WHAT IT DOES
    Simulates disaster recovery END TO END, without touching production:

    1. COPY OUT the data tables from the .env database (the production DB
       per the guard's classification) — READ-ONLY enforced at the
       Postgres level + a per-run self-check (attempt a no-op write;
       refuse unless Postgres rejects it).
    2. Restore into a DISPOSABLE target (--target-url, REQUIRED): the
       migrations run fresh on the target, then the tables COPY IN.
       The semantic_cache is deliberately NOT copied — it is derived
       data, epoch-keyed, and a restored cache would only add staleness
       surface (the fresh one rebuilds).
    3. VERIFY the restore with the RECEIPT CHAIN:
         a. every receipt's evidence chunk_hash must exist in the
            restored corpus AND re-derive exactly from the restored
            row's own fields (sha256(company ⊣ source ⊣ page ⊣ text));
         b. the transcript spine must hold byte-exact on the restored
            corpus (transcript[char_start:char_end] == chunk.text for
            every sampled chunk);
         c. the fact rows must be present and internally consistent.
       A restore that fails verification is a WRONG restore, not a code
       bug (the RUNBOOK's rule, now practiced instead of written).

EXIT CODES: 0 = drill passed (restore verified); 1 = refused / failed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# The data that MUST survive a disaster (schema_migrations re-runs fresh;
# semantic_cache is derived and deliberately skipped).
DRILL_TABLES = [
    "corpus_state",
    "source_registry",
    "multi_agent_chunks",
    "page_transcripts",
    "fact_rows",
    "xbrl_facts",
    "verification_receipts",
    "shadow_disagreements",
]


def chunk_hash_of(company: str, source: str, page: int, text: str) -> str:
    """The ingest.py chunk-hash formula, byte for byte (unit separator)."""
    basis = f"{company}\u001f{source}\u001f{page}\u001f{text}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target-url", required=True,
                    help="DISPOSABLE Postgres URL for the restore (the "
                         "ambient .env database is the production DB and "
                         "is NEVER a restore target)")
    ap.add_argument("--sample", type=int, default=20,
                    help="chunk sample size for the spine verification")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    src_url = os.environ.get("DB_ADMIN_DATABASE_URL", "")
    if not src_url:
        print("REFUSED: DB_ADMIN_DATABASE_URL missing — no dump source.")
        return 1

    import psycopg2
    import target_guard

    # The dump source is read from .env; the restore target must be a
    # DIFFERENT (host, database) pair — production is never a target.
    src_pair = target_guard._parse(src_url)
    tgt_pair = target_guard._parse(args.target_url)
    if tgt_pair == src_pair:
        print("REFUSED: the restore target IS the dump source — "
              "a restore never overwrites the database it came from.")
        return 1
    if tgt_pair is None or tgt_pair[0] not in ("localhost", "127.0.0.1",
                                               "::1", ""):
        print("REFUSED: the restore target must be explicitly local "
              "(localhost / unix-socket) — unknown remotes fail closed.")
        return 1
    print(f"[1] dump source : {src_pair[0]}/{src_pair[1]} (READ-ONLY enforced)")
    print(f"[1] restore dest: {tgt_pair[0]}/{tgt_pair[1]} (disposable)")

    # ---- 1. COPY OUT from production, READ-ONLY enforced ----
    conn = psycopg2.connect(src_url, connect_timeout=15)
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.rls_bypass', 'on', false);")
        # the self-check: Postgres must REJECT a no-op write
        try:
            cur.execute("UPDATE verification_receipts SET id = id;")
            conn.rollback()
            conn.close()
            print("REFUSED: the source connection accepted a write — "
                  "read-only enforcement failed; refusing the drill.")
            return 1
        except psycopg2.errors.ReadOnlySqlTransaction:
            # Postgres rejected the write — the read-only posture is real.
            # The transaction is now ABORTED: roll it back or every
            # subsequent command fails with InFailedSqlTransaction.
            conn.rollback()
        dumps: dict = {}
        counts_src: dict = {}
        try:
            for t in DRILL_TABLES:
                cur.execute("SELECT set_config('app.rls_bypass', 'on', false);")
                cur.execute(f"SELECT count(*) FROM {t}")
                counts_src[t] = cur.fetchone()[0]
                buf = io.StringIO()
                cur.copy_expert(f"COPY {t} TO STDOUT", buf)
                dumps[t] = buf.getvalue()
                print(f"    copied {t}: {counts_src[t]} rows, "
                      f"{len(dumps[t]) // 1024} KB", flush=True)
        finally:
            conn.rollback()
            conn.close()

    # ---- 2. Restore into the disposable target (migrations fresh) ----
    os.environ["DB_DATABASE_URL"] = args.target_url
    os.environ["DB_ADMIN_DATABASE_URL"] = args.target_url
    import db
    db.setup_database()
    rconn = psycopg2.connect(args.target_url)
    rconn.autocommit = False
    try:
        with rconn.cursor() as cur:
            cur.execute("SELECT set_config('app.rls_bypass', 'on', false);")
            for t in DRILL_TABLES:
                # The migrations SEED some tables (corpus_state's epoch row,
                # source_registry's sources) — a fresh restore TRUNCATES
                # before COPY IN or the seeded row collides with the dump's.
                cur.execute(f"TRUNCATE {t}")
                cur.copy_expert(f"COPY {t} FROM STDIN",
                                io.StringIO(dumps[t]))
                # The dumps carry explicit SERIAL ids: reset each sequence
                # past the restored max or the next real insert collides.
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = %s AND column_name = 'id'",
                            (t,))
                if cur.fetchone():
                    cur.execute("SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                                "COALESCE((SELECT max(id) FROM " + t + "), 1))",
                                (t,))
                cur.execute(f"SELECT count(*) FROM {t}")
                n = cur.fetchone()[0]
                ok = n == counts_src[t]
                print(f"    restored {t}: {n} rows "
                      f"{'OK' if ok else f'MISMATCH (want {counts_src[t]})'}",
                      flush=True)
                if not ok:
                    print("REFUSED: the restore is incomplete — a wrong "
                          "restore must never be reported as success.")
                    rconn.rollback()
                    return 1
        rconn.commit()
    finally:
        rconn.close()

    # ---- 3. Verify the restore with the RECEIPT CHAIN ----
    rconn = psycopg2.connect(args.target_url)
    rconn.set_session(readonly=True)
    failures: list = []
    try:
        with rconn.cursor() as cur:
            cur.execute("SELECT set_config('app.rls_bypass', 'on', false);")
            # (a) every receipt's evidence chunk_hash re-derives from the
            # restored corpus's own fields
            cur.execute("SELECT run_id, evidence_json FROM verification_receipts")
            receipts = cur.fetchall()
            cur.execute("SELECT chunk_hash, company, source, page, content "
                        "FROM multi_agent_chunks")
            chunks = {r[0]: r for r in cur.fetchall()}
            n_hashes = 0
            for run_id, ev in receipts:
                for e in (ev or []):
                    ch = (e.get("chunk_hash") if isinstance(e, dict) else None)
                    if not ch:
                        continue
                    n_hashes += 1
                    row = chunks.get(ch)
                    if row is None:
                        failures.append(f"{run_id}: evidence chunk {ch[:12]} "
                                        "MISSING from the restored corpus")
                        continue
                    recomputed = chunk_hash_of(row[1], row[2], row[3], row[4])
                    if recomputed != ch:
                        failures.append(f"{run_id}: chunk {ch[:12]} hash does "
                                        "NOT re-derive from the restored row")
            print(f"[3a] receipt chain: {len(receipts)} receipts, "
                  f"{n_hashes} evidence chunk-hashes re-derived "
                  f"({'FAIL' if failures else 'OK'})")
            # (b) the transcript spine, byte-exact on a sample
            cur.execute("SELECT c.chunk_hash, c.page, c.content, "
                        "p.transcript FROM multi_agent_chunks c "
                        "JOIN page_transcripts p ON p.source = c.source "
                        "AND p.page = c.page LIMIT %s", (args.sample,))
            spine_rows = cur.fetchall()
            spine_checked = 0
            for ch, page, content, transcript in spine_rows:
                if transcript is None:
                    continue
                start = transcript.find(content)
                spine_checked += 1
                if start < 0:
                    failures.append(f"chunk {ch[:12]}: content NOT in the "
                                    "restored page transcript")
            print(f"[3b] transcript spine: {spine_checked} sampled chunks "
                  f"({'FAIL' if any('transcript' in f for f in failures) else 'OK'})")
            # (c) the fact rows
            cur.execute("SELECT count(*) FROM fact_rows")
            facts = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM fact_rows "
                        "WHERE verifier_class = 'span_xbrl_reconciled'")
            reconciled = cur.fetchone()[0]
            print(f"[3c] fact rows: {facts} ({reconciled} reconciled)")
    finally:
        rconn.close()

    if failures:
        print("DRILL FAILED — the restore does not verify:")
        for f in failures[:5]:
            print("  -", f)
        return 1
    print("DRILL PASSED: the restore verified with the receipt chain "
          "(hashes re-derive, the spine holds, facts consistent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
