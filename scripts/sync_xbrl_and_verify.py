"""Fetch real SEC XBRL data and walk the audit-failure causal chain, observed
number by observed number.

WHAT IT MEASURES / REPAIRS
    Without SEC access the pipeline audit reports 3 failures that all trace
    to ONE missing input:

        xbrl_facts = 0 rows          <- data.sec.gov unreachable
              |
              v
        reconciled = 0 of N         <- needs a PDF span AND an SEC XBRL value
              |                        to agree (two independent sources)
              v
        Path A serves 0/5           <- its lookup index is literally
                                     WHERE reconciled = true

    This script walks that chain IN ORDER and prints the observed numbers
    BEFORE and AFTER each step, so the causality is visible rather than
    asserted. Steps: (1) fetch + sync xbrl_facts via scripts/xbrl.py — the
    ONLY step that needs the network; (2) re-run reconciliation
    (db.sync_fact_rows); (3) re-run the affected audit stages (2-pathA,
    6-terminal); (4) re-run the two affected mutation controls.

DEFINITION (the honesty rule, implemented literally)
    If step 1 fails: STOP, exit 1. The script does NOT hand-write the
    Q4-2023 figures into the database to make step 3 go green — feeding in
    the answer proves the checker can pass, not that the SEC sync works.
    That is the fake-green this project exists to catch.
    Exit 2 = step 1 succeeded but reconciliation is still 0. That is a REAL
    FINDING about the matcher, not a script failure — the script says which
    of the two it is and stops before re-running audits that can only fail.

PREREQUISITES
    Network access to data.sec.gov (step 1) and a reachable database
    configured for db.py (steps 1-3). SEC fair access: the fetcher in
    scripts/xbrl.py already honors the declared User-Agent requirement.

Exit codes: 0 = chain walked; 1 = BLOCKED at step 1; 2 = fetched but 0
reconciled (matcher finding).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
AUDIT_STAGES = ("2-pathA", "6-terminal")


def _import_db():
    try:
        import db  # noqa: PLC0415
        return db
    except Exception as exc:
        print("REFUSED: cannot import the database layer (db.py).")
        print(f"  import error: {type(exc).__name__}: {exc}")
        print("  prerequisite missing: a working local Python stack for db.py")
        print("  (dev machine: the mmh3 Application-Control DLL block via")
        print("   db.py -> fastembed -> sparse.bm25 -> mmh3).")
        print("  Nothing can be synced or verified without it.")
        sys.exit(1)


def counts(db) -> Dict[str, int]:
    with db.get_db_connection(tenant_id="default") as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM xbrl_facts;")
        xbrl = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM fact_rows;")
        facts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM fact_rows WHERE reconciled = true;")
        recon = cur.fetchone()[0]
    return {"xbrl_facts": xbrl, "fact_rows": facts, "reconciled": recon}


def _show(label: str, c: Dict[str, int]) -> None:
    print(f"  [{label}] xbrl_facts={c['xbrl_facts']}  "
          f"fact_rows={c['fact_rows']}  reconciled={c['reconciled']}")


def step1_fetch(db) -> bool:
    """Fetch + sync xbrl_facts. The ONLY network step. Fail => STOP."""
    print("STEP 1: fetch + sync xbrl_facts (network; data.sec.gov)")
    try:
        import xbrl  # scripts/xbrl.py — the production fetcher/deriver
    except Exception as exc:
        print(f"  BLOCKED: cannot import scripts/xbrl.py: {type(exc).__name__}: {exc}")
        return False
    try:
        rows, _report = xbrl.derive_facts()
        if not rows:
            print("  BLOCKED: derive_facts() produced no rows (SEC unreachable?)")
            return False
        n = xbrl.sync_to_db(rows)
        print(f"  synced {n} xbrl fact(s) from {len(rows)} derived row(s)")
        return True
    except Exception as exc:
        print(f"  BLOCKED: fetch/sync failed: {type(exc).__name__}: {exc}")
        return False


def step2_reconcile(db) -> Dict[str, int]:
    print("STEP 2: re-run reconciliation (db.sync_fact_rows)")
    report = db.sync_fact_rows(tenant_id="default") or {}
    print(f"  sync_fact_rows -> {json.dumps(report, default=str)[:400]}")
    return report


def step3_audit() -> bool:
    print(f"STEP 3: re-run affected audit stages {AUDIT_STAGES}")
    all_ok = True
    for stage in AUDIT_STAGES:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "pipeline_audit.py"),
             "--only", stage, "--json"],
            capture_output=True, text=True, timeout=900, cwd=str(REPO))
        # parse the audit JSON: report PASS/FAIL/SKIP counts, not raw tails
        try:
            payload = json.loads(proc.stdout or "[]")
            items = payload if isinstance(payload, list) else payload.get("checks", [])
            counts: Dict[str, int] = {}
            for c in items:
                st = str(c.get("status", "?")).upper()
                counts[st] = counts.get(st, 0) + 1
            print(f"    stage {stage}: {counts}")
        except Exception:
            tail = (proc.stdout or "").strip().splitlines()[-2:]
            for ln in tail:
                print(f"    {ln}")
        if proc.returncode != 0:
            all_ok = False
            print(f"    audit stage {stage} exit={proc.returncode} (failures above)")
    return all_ok


def step4_mutations(skip: bool) -> bool:
    if skip:
        print("STEP 4: SKIPPED (--skip-mutations)")
        return True
    print("STEP 4: re-run the two affected mutation controls")
    # The two controls that need Path-A serving + a grounded receipt:
    listing = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "pipeline_audit_mutations.py"),
         "--list"],
        capture_output=True, text=True, timeout=300, cwd=str(REPO))
    # --list lines are "[stage] NAME"; the mutations --only filter matches
    # substrings of stage or NAME, so we pass the full NAME.
    names = [ln.strip().split("] ", 1)[1].strip() for ln in
             (listing.stdout or "").splitlines()
             if ln.strip().startswith("[") and "] " in ln]
    # The two controls the causal chain affects: Path-A demotion (needs
    # reconciled rows to have a served query to demote) and receipt span
    # tamper (needs a grounded receipt to exist).
    demotion = [n for n in names if "demotion set" in n.lower()]
    tamper = [n for n in names if "span is not hashed" in n.lower()]
    chosen = (demotion[:1] + tamper[:1])[:2]
    if not chosen:
        print("  could not identify the two affected mutation names via --list;")
        print(f"  --list output head: {(listing.stdout or '')[:200]!r}")
        return False
    ok = True
    for name in chosen:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "pipeline_audit_mutations.py"),
             "--only", name],
            capture_output=True, text=True, timeout=900, cwd=str(REPO))
        out = (proc.stdout or "")
        # Distinguish SETUP-FAIL (the mutation never applied) from MISSED
        # (it applied and ESCAPED) — an unrun control is a comment, an
        # escaped control is a finding; they must never read the same.
        if "SETUP-FAIL" in out:
            print(f"    mutation {name}: SETUP-FAIL (anchor missing — "
                  f"the mutation never applied; this is NOT an escape)")
            ok = False
        elif "MISSED" in out:
            print(f"    mutation {name}: MISSED (applied and ESCAPED — "
                  f"the check does not test what it claims)")
            ok = False
        elif proc.returncode != 0:
            print(f"    mutation {name}: RUNNER ERROR exit={proc.returncode}")
            ok = False
        else:
            print(f"    mutation {name}: CAUGHT (exit 0)")
    return ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-mutations", action="store_true",
                    help="skip step 4 (mutation re-runs)")
    args = ap.parse_args(argv)
    db = _import_db()
    import target_guard
    target_guard.refuse_production_writes(
        {"runtime": db.get_settings().database_url or "",
         "admin": db.get_settings().admin_database_url or ""},
        "sync_xbrl_and_verify.py")

    print("CAUSAL CHAIN: xbrl_facts -> reconciliation -> Path A -> audit")
    before = counts(db)
    _show("before", before)

    if not step1_fetch(db):
        print("\nBLOCKED at step 1. NOT hand-writing figures to force green —")
        print("feeding in the answer proves the checker can pass, not that the")
        print("SEC sync works. Exit 1.")
        return 1
    after1 = counts(db)
    _show("after step 1", after1)

    step2_reconcile(db)
    after2 = counts(db)
    _show("after step 2", after2)

    if after2["reconciled"] == 0:
        print("\nFINDING (not a script failure): fetch succeeded but reconciliation")
        print("is still 0. That is a REAL FINDING about the matcher/derivation,")
        print("not about this script. Stopping before re-running audits that can")
        print("only fail. Exit 2.")
        return 2

    ok3 = step3_audit()
    ok4 = step4_mutations(args.skip_mutations)
    _show("after step 3", counts(db))
    print("\nCHAIN WALKED." if (ok3 and ok4) else
          "\nChain walked, but some step reported failure — see the observed")
    print("numbers above for where." if not (ok3 and ok4) else "")
    return 0 if (ok3 and ok4) else 1


if __name__ == "__main__":
    sys.exit(main())
