"""
Idempotent role bootstrap — works for BOTH Neon production and the CI container.
Order matters: migrations FIRST (tables must exist), then grants to runtime role.

Usage:
  set DB_ADMIN_DATABASE_URL=postgresql://owner:pw@host/db
  set APP_RAG_PASSWORD=alphanumeric_only
  python scripts/bootstrap_roles.py
"""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import setup_database, get_settings, logger  # noqa: E402


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-8s | %(message)s")

    password = os.getenv("APP_RAG_PASSWORD")
    if not password or not password.isalnum():
        raise SystemExit("APP_RAG_PASSWORD must be set and alphanumeric (URL-safe).")

    setup_database()  # migrations as OWNER — tables must exist before grants

    cfg = get_settings()
    conn = psycopg2.connect(cfg.admin_database_url or cfg.database_url,
                            connect_timeout=cfg.connect_timeout_s)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'app_rag';")
        if cur.fetchone():
            cur.execute(f"ALTER ROLE app_rag WITH LOGIN PASSWORD '{password}';")
            logger.info("Role app_rag exists — password refreshed.")
        else:
            cur.execute(f"CREATE ROLE app_rag LOGIN PASSWORD '{password}';")
            logger.info("Role app_rag created.")

        for stmt in (
            "GRANT USAGE ON SCHEMA public TO app_rag;",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rag;",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rag;",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rag;",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO app_rag;",
        ):
            cur.execute(stmt)
        cur.execute("SELECT rolbypassrls, rolsuper FROM pg_roles "
                    "WHERE rolname = 'app_rag';")
        bypass, superuser = cur.fetchone()
        if bypass or superuser:
            raise SystemExit("SECURITY: app_rag must NOT be superuser/BYPASSRLS.")
    conn.close()
    logger.info("Bootstrap complete. Runtime URL: "
                "postgresql://app_rag:<PASSWORD>@<host>/<db>?sslmode=require")


if __name__ == "__main__":
    main()