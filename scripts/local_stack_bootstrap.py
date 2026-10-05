"""
Local stack bootstrap for the sandbox — replaces the lost /home/user/setup_stack.sh.

Why this file exists and what is REAL vs SUBSTITUTED:

  REAL      PostgreSQL 16.14 (npm @embedded-postgres/linux-x64) with the real
            pg_trgm 1.6 extension and pgvector 0.6.2 grafted in (PG16 ABI).
            Migrations, RLS, spans, receipts, epoch scoping — all genuine.
  REAL      The corpus: corpus_chunks.jsonl / .manifest.json / .transcripts.jsonl
            are the repo's own committed ingestion output, loaded through the
            production db.migrate_from_manifest() + db.sync_page_transcripts().
  SUBSTITUTED  The embedder. bge-small-en-v1.5 weights cannot be downloaded
            (HuggingFace is unreachable from this sandbox — this is also the
            documented cause of the one known test failure). We install the
            SAME deterministic sha256-seeded embedder the repo's own
            scripts/_mutant_runner.py installs, so the audit runs against a
            stable 384-d geometry instead of no geometry at all.

Nothing else is stubbed. The corpus, schema, gates and receipts are real.
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


def install_deterministic_embedder() -> None:
    import db

    def _fake_embed(text: str):
        digest = hashlib.sha256(text.encode()).digest()
        vec = np.random.default_rng(
            int.from_bytes(digest[:8], "big")).random(384).astype(np.float32)
        return vec / np.linalg.norm(vec)

    db.embed_query = _fake_embed
    db.embed_passages = lambda texts: [_fake_embed(t) for t in texts]
    db._get_embedder = lambda: type(
        "E", (), {"embed": staticmethod(
            lambda self, t, **k: iter([_fake_embed(x) for x in t]))})()


def main() -> int:
    install_deterministic_embedder()
    import db

    print("[1/4] schema + migrations")
    db.setup_database()
    print("      OK")

    print("[2/4] corpus chunks from the committed manifest")
    report = db.migrate_from_manifest()
    print(f"      OK — {report}")

    print("[3/4] page transcripts")
    n = db.sync_page_transcripts()
    print(f"      OK — {n} page rows")

    print("[4/4] fact store (span-anchored, reconciled)")
    fr = db.sync_fact_rows()
    print(f"      OK — {fr}")

    with db.admin_connection() as c, c.cursor() as cur:
        for t in ("multi_agent_chunks", "page_transcripts", "source_registry",
                  "fact_rows", "corpus_state"):
            cur.execute(f"SELECT count(*) FROM {t};")
            print(f"      {t:24s} {cur.fetchone()[0]:>6} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
