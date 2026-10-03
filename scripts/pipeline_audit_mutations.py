"""
Pipeline Audit — MUTATION CONTROLS (proof the audit has teeth).
===============================================================
An audit that reports 38/38 PASS is worthless until you can show it would
have reported FAIL had the code been broken. This runner injects a known
defect into a throwaway COPY of the repo, runs the affected stage, and
asserts the corresponding check flips to FAIL.

Every mutation is a realistic regression drawn from the project's own bug
history or a plausible future one — not a syntax error, which any import
would catch.

Run:
    python scripts/pipeline_audit_mutations.py            # all controls
    python scripts/pipeline_audit_mutations.py --list     # what is covered

Exit 0 = every mutation was CAUGHT (the audit is sensitive to it).
Exit 1 = at least one mutation went UNDETECTED (that check is theatre).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Tuple

REPO = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.gettempdir()) / "pipeline_audit_mutants"

# Applied inside the throwaway copy, by (relative path, old, new).
Mutation = Callable[[Path], None]


class Control(NamedTuple):
    name: str
    stage: str          # --only filter
    check: str          # check name that MUST fail
    mutate: Mutation
    why: str


def _sub(rel: str, old: str, new: str) -> Mutation:
    def _apply(root: Path) -> None:
        path = root / rel
        src = path.read_text(encoding="utf-8")
        assert old in src, f"mutation anchor missing in {rel}: {old[:60]!r}"
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
    return _apply


def _tenant_fixture(root: Path) -> None:
    """Point tenant B at tenant A, simulating an unscoped cache read.

    Fixture-level for the same reason as _demote_fixture: the real leak is
    blocked by RLS *and* the WHERE clause, so removing either one alone is
    invisible. This proves the assertion fires when a leak actually occurs.
    """
    path = root / "scripts" / "pipeline_audit.py"
    src = path.read_text(encoding="utf-8")
    anchor = "leaked = check_semantic_cache(q, tenant_id=t_b)"
    assert anchor in src, "tenant fixture anchor missing"
    path.write_text(src.replace(anchor, anchor.replace("t_b", "t_a"), 1),
                    encoding="utf-8")


def _demote_fixture(root: Path) -> None:
    """Swap a genuine demotion case for a query that Path A really SERVES.

    Fixture-level control: see the Control entry's comment for why a
    pipeline-level mutation cannot flip this check.
    """
    path = root / "scripts" / "pipeline_audit.py"
    src = path.read_text(encoding="utf-8")
    anchor = "# no fact row: Tesla pays none"
    assert anchor in src, "demote fixture anchor missing"
    line = [ln for ln in src.split("\n") if anchor in ln][0]
    replacement = ('    "What was Apple\'s total net sales in Q4 2023?",'
                   '      # MUTATED: this one SERVES')
    path.write_text(src.replace(line, replacement, 1), encoding="utf-8")


CONTROLS: List[Control] = [
    Control(
        "cache ignores tenant scope", "1-cache",
        "cache is tenant-scoped (no cross-tenant replay)",
        # The classic multi-tenant leak: drop tenant_id from the cache key.
        _tenant_fixture,
        "a cross-tenant cache replay would leak another tenant's corpus",
    ),
    Control(
        "citation bounds gate accepts anything", "5-gates",
        "GATE citation-bounds: fabricated [n] rejected",
        _sub("adaptive_rag.py", "return match.group(0)\n    return None",
             "return None\n    return None"),
        "a fabricated [99] citation must not survive pre-audit",
    ),
    Control(
        "growth gate direction inverted", "5-gates",
        "GATE growth-direction: 'grew' when it declined is caught",
        _sub("adaptive_rag.py",
             'if any((p["current"] > p["prior"]) == claimed_up for p in matching):',
             'if any((p["current"] < p["prior"]) == claimed_up for p in matching):'),
        "a claim that moves opposite to the company's own numbers must flag",
    ),
    Control(
        "RRF collapses distinct documents", "4-retrieval",
        "RRF fusion ranks a document present in both arms first",
        # Drop chunk_hash from the dedupe key -> every row falls into one key.
        _sub("adaptive_rag.py",
             'key = r.get("chunk_hash") or f"{r.get(\'source\')}|{r.get(\'page\')}|{r.get(\'content\', \'\')[:100]}"',
             'key = "constant"'),
        "fusion that merges all candidates cannot rank by agreement",
    ),
    Control(
        "specialist pruning halves the evidence pool", "4-retrieval",
        "specialist pruning keeps evidence-pool PARITY",
        # The exact historical bug: fixed k=5 regardless of fleet size.
        _sub("adaptive_rag.py",
             "per_specialist_k = max(5, 15 // max(1, len(specialists)))",
             "per_specialist_k = 5  # historical regression: fixed k"),
        "the 15->5 pool regression that caused misattributed figures",
    ),
    Control(
        "router ignores the model's decision", "3-router",
        "vectorstore / general_knowledge / out_of_domain routing",
        _sub("adaptive_rag.py",
             'extras: Dict[str, Any] = {"route": decision.destination}',
             'extras: Dict[str, Any] = {"route": "out_of_domain"}'),
        "a router that never routes would silently refuse every query",
    ),
    Control(
        "degraded run is allowed to certify", "5-gates",
        "guard rail: a degraded run can NEVER certify",
        _sub("adaptive_rag.py",
             'if degraded or not draft or draft == _QUARANTINE:',
             'if not draft or draft == _QUARANTINE:'),
        "a quarantined specialist must not produce a certified brief",
    ),
    Control(
        "Path A would serve a covered query inside the demotion set", "2-pathA",
        "out-of-coverage / ambiguous queries DEMOTE (fail-closed)",
        # NOTE ON WHY THIS MUTATES THE FIXTURE, NOT THE PIPELINE:
        # removing the coverage gate in fact_extract (forcing path='fact' for
        # every query) still DEMOTES all five cases — `build_path_a_answer`
        # independently declines when there is no exact match, and the node's
        # `candidate is None` gate catches it. Path A is defended in depth, so
        # no single-line code mutation can violate it. The way to show THIS
        # CHECK is not vacuous is to hand it a query that truly serves and
        # confirm the assertion fires.
        _demote_fixture,
        "the demote assertion must actually fire when a served query appears",
    ),
    Control(
        "receipt evidence span is not hashed", "7-receipt",
        "tamper: a mutated evidence span BREAKS the chain",
        _sub("db.py",
             "verified = (links_checked > 0 and links_ok == links_checked",
             "verified = (links_checked > 0 and True"),
        "if spans are not hashed, tampering is undetectable",
    ),
]


def _stage_failed(root: Path, stage: str, check_name: str) -> Tuple[bool, str]:
    """Run one stage in the mutant tree; True if that check reported FAIL."""
    env = dict(os.environ)
    env.setdefault("DB_ADMIN_DATABASE_URL",
                   "postgresql://postgres@/postgres?host=/tmp/pgdata")
    env.setdefault("DB_DATABASE_URL",
                   "postgresql://app_rag:ciruntimepw123@/postgres?host=/tmp/pgdata")
    env.setdefault("ALLOW_OPEN_MODE", "true")
    env.setdefault("RAG_SKIP_BOOT_SMOKE", "1")
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "_mutant_runner.py"),
         str(root), "--only", stage, "--json"],
        capture_output=True, text=True, env=env, timeout=600,
    )
    out = proc.stdout.strip()
    start = out.find("[")
    if start == -1:
        return False, f"no JSON from runner (rc={proc.returncode}): {out[-200:]}"
    try:
        rows = json.loads(out[start:])
    except json.JSONDecodeError as exc:
        return False, f"unparseable JSON: {exc}"
    for r in rows:
        if r.get("check") == check_name:
            return r.get("status") == "FAIL", f"{r.get('status')}: {r.get('evidence')[:110]}"
    return False, f"check not found in stage {stage} (crash swallowed it?)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    if args.list:
        for c in CONTROLS:
            print(f"[{c.stage}] {c.name}\n    must fail: {c.check}\n    why: {c.why}")
        return 0

    controls = [c for c in CONTROLS if not args.only or args.only in c.stage
                or args.only in c.name]
    WORK.mkdir(parents=True, exist_ok=True)
    base = WORK / "base"
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(REPO, base, ignore=shutil.ignore_patterns(
        ".git", "node_modules", "__pycache__", "*.pyc", ".venv"))

    print("=" * 78)
    print("PIPELINE AUDIT — MUTATION CONTROLS (can the audit detect breakage?)")
    print("=" * 78)
    undetected = []
    for c in controls:
        root = WORK / c.name.replace(" ", "_").replace("/", "_")
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(base, root)
        try:
            c.mutate(root)
        except AssertionError as exc:
            print(f"\n  SETUP-FAIL  {c.name}\n        → {exc}")
            undetected.append((c, "anchor missing — mutation never applied"))
            continue
        caught, detail = _stage_failed(root, c.stage, c.check)
        print(f"\n  {'CAUGHT ' if caught else 'MISSED '} {c.name}")
        print(f"        mutation: {c.why}")
        print(f"        {c.check} → {detail}")
        if not caught:
            undetected.append((c, detail))

    print("\n" + "=" * 78)
    print(f"TOTAL {len(controls)} mutations | "
          f"{len(controls) - len(undetected)} CAUGHT | {len(undetected)} MISSED")
    if undetected:
        print("\nUNDETECTED (these checks do not actually test what they claim):")
        for c, detail in undetected:
            print(f"  - {c.name} :: {detail}")
    print("=" * 78)
    return 1 if undetected else 0


if __name__ == "__main__":
    sys.exit(main())
