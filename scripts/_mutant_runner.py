"""
Internal helper for pipeline_audit_mutations.py — NOT a user-facing tool.

Runs the stage audit from a throwaway repo COPY (argv[1]) with the sandbox's
local Postgres + deterministic fake embedder installed, so mutations to the
copy are exercised against the same seeded corpus the real audit uses.

Kept as a separate file (rather than inlined) because the audit imports
`db` / `adaptive_rag` by module name: the copy's directory must lead
sys.path, and those modules must not already be in sys.modules.
"""
from __future__ import annotations

import hashlib
import os
import runpy
import sys

root = os.path.abspath(sys.argv[1])
sys.argv = ["pipeline_audit.py"] + sys.argv[2:]
sys.path.insert(0, root)
os.chdir(root)

import numpy as np  # noqa: E402

import db  # noqa: E402

db.MIGRATIONS = tuple(
    db.Migration(m.version, m.description,
                 tuple("SELECT 1;" if "gin_trgm_ops" in s else s
                       for s in m.statements))
    for m in db.MIGRATIONS
)


def _fake_embed(text: str):
    digest = hashlib.sha256(text.encode()).digest()
    vec = np.random.default_rng(
        int.from_bytes(digest[:8], "big")).random(384).astype(np.float32)
    return vec / np.linalg.norm(vec)


db.embed_query = _fake_embed
db.embed_passages = lambda texts: [_fake_embed(t) for t in texts]
db._get_embedder = lambda: type(
    "E", (), {"embed": staticmethod(lambda self, t, **k: iter([_fake_embed(x) for x in t]))})()

runpy.run_path(os.path.join(root, "scripts", "pipeline_audit.py"),
               run_name="__main__")
