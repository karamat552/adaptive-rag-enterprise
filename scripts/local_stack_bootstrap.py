"""Seed a disposable local stack for the measurement scripts. Idempotent.

WHAT IT DOES
    Gives measure_recall.py and bench_latency.py something to measure: a
    database with the repo's own schema, the committed corpus chunks, page
    transcripts, and fact rows — loaded through the PRODUCTION loaders
    (db.migrate_from_manifest, db.sync_page_transcripts, db.sync_fact_rows),
    never through private inserts that could drift from real ingestion.

DEFINITION (so its output cannot be misread)
    Idempotent: safe to re-run; every loader used is itself idempotent
    (manifest-sync semantics: unchanged SHA256 -> skipped, changed ->
    delete+reinsert, any change -> epoch bump). Counts printed are the
    loaders' OWN return values, not re-queried optimistic numbers.

PREREQUISITES / SAFETY
    1. A reachable EMPTY OR DISPOSABLE Postgres with pgvector + pg_trgm
       extensions available (CREATE EXTENSION IF NOT EXISTS is in the
       migrations). Pass its URL via --database-url (or env DB_DATABASE_URL).
       The unix-socket convention from the spec (?host=/tmp/pgdata) does NOT
       exist in this repo's db.py — the repo wins: the URL is whatever you
       pass, and db.py consumes it verbatim.
    2. REFUSES the production host: if --database-url matches the Neon
       production host found in .env, the script refuses unless
       --i-know-this-is-production is passed. Seeding REPLACES corpus rows
       (delete+reinsert + epoch bump) — pointing this at production would
       churn the live corpus. This guard is deliberate: the audit-tool
       lesson (2026-10-05) is that write-capable scripts must never silently
       trust the ambient .env target.
    3. If facts cannot be seeded without SEC access (fact reconciliation
       needs the XBRL side), it SAYS SO instead of seeding placeholders
       that look like facts. Placeholder facts would make recall appear
       measurable when it is not (its expected-evidence set is
       reconciled-rows-only by definition).

Exit codes: 0 = seeded (or already consistent); 1 = refused / failed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _production_host() -> str:
    env_path = REPO / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DB_DATABASE_URL="):
            try:
                return urlparse(line.split("=", 1)[1].strip().strip('"')).netloc
            except Exception:
                return ""
    return ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", default=os.getenv("DB_DATABASE_URL", ""),
                    help="target Postgres URL (schema + corpus land HERE; "
                         "must be disposable, NOT production)")
    ap.add_argument("--admin-url", default=os.getenv("DB_ADMIN_DATABASE_URL", ""),
                    help="owner-identity URL for the same target (migrations, "
                         "transcripts, reconciliation)")
    ap.add_argument("--skip-facts", action="store_true",
                    help="skip fact_rows reconciliation (recall over reconciled "
                         "rows will then correctly REFUSE: nothing reconciled)")
    ap.add_argument("--i-know-this-is-production", action="store_true",
                    help="override the production-host refusal (not recommended)")
    args = ap.parse_args(argv)

    if not args.database_url or not args.admin_url:
        print("REFUSED: --database-url / --admin-url (or their env vars) missing.")
        print("  A measurement stack must be an EXPLICIT target, never the")
        print("  ambient .env default — that is how production gets churned.")
        return 1

    prod_host = _production_host()
    target_host = urlparse(args.database_url).netloc.rpartition("@")[-1]
    if (prod_host and target_host == prod_host
            and not args.i_know_this_is_production):
        print("REFUSED: the target URL's host matches the PRODUCTION database")
        print(f"  host in .env ({prod_host}). Seeding REPLACES corpus rows and")
        print("  bumps the epoch — that must never run against production.")
        print("  Pass --i-know-this-is-production to override deliberately.")
        return 1

    # Point db.py at the target BEFORE importing it (settings read env once).
    os.environ["DB_DATABASE_URL"] = args.database_url
    os.environ["DB_ADMIN_DATABASE_URL"] = args.admin_url

    print("SEEDING local measurement stack")
    print(f"  target : {target_host}")
    print(f"  guard  : production host = {prod_host or 'n/a'} "
          f"({'refused on match' if prod_host else 'not found in .env'})")

    try:
        if not os.getenv("APP_RAG_PASSWORD"):
            print("REFUSED: APP_RAG_PASSWORD must be set for role bootstrap")
            print("  (scripts/bootstrap_roles.py creates the non-superuser,")
            print("  non-BYPASSRLS runtime role and refuses without it).")
            return 1
        import bootstrap_roles  # noqa: F401  (scripts/ on sys.path)
        bootstrap_roles.main()
        import db
        db.setup_database()  # migrations (idempotent; owner identity)

        print("  migrating corpus chunks (db.migrate_from_manifest) ...")
        report = db.migrate_from_manifest()
        print(f"    committed rows: {report.rows_inserted} "
              f"(changed sources replaced, epoch bumped if any change)")

        print("  syncing page transcripts (db.sync_page_transcripts) ...")
        n_pages = db.sync_page_transcripts()
        print(f"    page rows synced: {n_pages}")

        if args.skip_facts:
            print("  facts: SKIPPED (--skip-facts) — recall's expected-evidence")
            print("    set is reconciled-rows-only and will correctly REFUSE.")
        else:
            print("  reconciling facts (db.sync_fact_rows) ...")
            facts = db.sync_fact_rows(tenant_id="default") or {}
            print(f"    sync report: {facts}")
            recon = facts.get("reconciled", 0)
            if recon == 0:
                print("    NO FACTS RECONCILED. This is a REAL prerequisite gap,")
                print("    not a seeded success: reconciliation needs BOTH a PDF")
                print("    span AND an SEC XBRL value to agree. Without SEC access")
                print("    (scripts/sync_xbrl_and_verify.py step 1) there is")
                print("    nothing to reconcile, and measure_recall.py will")
                print("    correctly REFUSE. No placeholder facts were written.")
        print("SEEDING DONE.")
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(f"REFUSED: seeding failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
