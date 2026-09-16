"""ADR-017 Phase 1 — the A.2 battery runner.

Consumes the PRE-REGISTERED battery (tests/battery_phase1_preregistered.json
— fixed before any measurement; A.2's no-post-hoc-questions rule) and:

  1. DETERMINISTIC PASS (zero tokens): every question's routing
     expectation is checked against path_a_decision + the live fact
     keys. This is the coverage matrix — it can never drift silently.
  2. CORRUPTED PASS (zero tokens): the 15 pre-registered corrupted-
     claim injections (X01-X15) run through the deterministic claim
     verifier (fact_claims.verify_claim_against_facts) against LIVE
     fact rows. A verified corrupted claim is a FALSE PASS — under A.2
     it resets the green-nights clock. Template-rendered control
     claims (one per covered triple) must verify, proving the
     instrument is not a blanket rejector.
  3. SHADOW PASS (one full V1 pipeline run per question): the shadow
     node rides along, writes the disagreement ledger, and this runner
     reads each question's ledger row back by run_id — capacity-
     blocked questions (quota walls, degraded agents) are classified
     INCOMPLETE, never counted as disagreements (a quota-starved V1
     refusal would otherwise poison the agreement rate — the same
     capacity-vs-logic discipline the canary classifier established).

Verdict per run (the A.2 green-night criteria, evaluated per night):
  GREEN       all covered questions measured, agreement >= 98%,
              corrupted 15/15, controls pass
  RED         corrupted false-pass, control failure, or agreement < 98%
              on a fully measured night
  INCOMPLETE  capacity blocks or missing ledger rows — the clock does
              not advance, and is not falsely reset either
  SMOKE       a --limit run (harness validation only, never a gate
              measurement)

Exit codes: 0=GREEN · 1=RED · 2=INCOMPLETE · 3=harness error.

Usage:
  python scripts/battery_phase1.py                        # matrix only
  python scripts/battery_phase1.py --corrupted           # + X01-X15
  python scripts/battery_phase1.py --shadow              # + live runs
  python scripts/battery_phase1.py --shadow --corrupted --report shadow_battery_report.json
  python scripts/battery_phase1.py --shadow --limit 3    # smoke, never GREEN
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("Battery")

BATTERY = Path(__file__).resolve().parent.parent / \
    "tests" / "battery_phase1_preregistered.json"

AGREE_CLASSES = ("agree_numeric", "agree_partial", "agree_refusal")
GREEN = "GREEN"
RED = "RED"
INCOMPLETE = "INCOMPLETE"
SMOKE = "SMOKE"


def _spec() -> Dict[str, Any]:
    return json.loads(BATTERY.read_text(encoding="utf-8"))


def deterministic_pass() -> int:
    """Zero-token routing matrix: battery expectations vs the guard."""
    from db import get_fact_rows
    from fact_extract import path_a_decision

    spec = _spec()
    rows = get_fact_rows()
    keys = {(r["company"], r["metric_key"], r["period"]) for r in rows}
    fails = 0
    for q in spec["questions"]:
        d = path_a_decision(q["q"], keys)
        got = d["path"]
        want = q["expect_path"]
        ok = got == want
        if not ok:
            fails += 1
        flag = "OK " if ok else "FAIL"
        logger.info("[%s] %-4s %-58s path=%s want=%s %s",
                    flag, q["id"], q["q"][:58], got, want,
                    d.get("reason", ""))
    total = len(spec["questions"])
    logger.info("Deterministic routing matrix: %d/%d correct.", total - fails,
                total)
    return fails


def corrupted_pass() -> Tuple[List[str], List[str]]:
    """Zero-token corrupted-claim gate: X01-X15 must each be caught by a
    POSITIVE defense reason (not an unjudged decline), and every
    template-rendered control claim must VERIFY. Returns
    (false_passes, control_failures)."""
    from db import get_fact_rows
    from fact_claims import control_claims, verify_claim_against_facts

    spec = _spec()
    rows = get_fact_rows()

    control_failures: List[str] = []
    for name, claim in control_claims(rows):
        v = verify_claim_against_facts(claim, rows)
        if not v["verified"]:
            control_failures.append(name)
            logger.error("[CTRL-FAIL] %-34s %s -> %s %s", name, claim[:40],
                         v["reason"], v.get("detail", ""))
        else:
            logger.info("[CTRL-OK ] %-34s verified", name)

    false_passes: List[str] = []
    for x in spec["corrupted_claim_injections"]:
        v = verify_claim_against_facts(x["corrupted_claim"], rows)
        caught = (not v["verified"]) and \
            not v["reason"].startswith("unjudged")
        if caught:
            logger.info("[CAUGHT ] %-4s %-28s reason=%s", x["id"],
                        x["operator"], v["reason"])
        else:
            false_passes.append(x["id"])
            logger.error("[FALSEPASS] %-4s %r verified=%s reason=%s %s",
                         x["id"], x["corrupted_claim"][:60], v["verified"],
                         v["reason"], v.get("detail", ""))
    total = len(spec["corrupted_claim_injections"])
    logger.info("Corrupted-claim gate: %d/%d caught, %d/%d controls verified.",
                total - len(false_passes), total,
                len(control_claims(rows)) - len(control_failures),
                len(control_claims(rows)))
    return false_passes, control_failures


def _fetch_ledger_row(run_id: str, question: str) -> Optional[Dict[str, Any]]:
    """This run's ledger row — per-night measurement, not epoch pooling."""
    from db import admin_connection
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agreement_class, agreement_detail FROM "
            "shadow_disagreements WHERE run_id=%s AND question=%s "
            "ORDER BY id DESC LIMIT 1", (run_id, question))
        r = cur.fetchone()
        return {"agreement_class": r[0],
                "agreement_detail": r[1]} if r else None


def _sum_tokens(per_model: Optional[Dict[str, Any]]) -> int:
    # _MODEL_USAGE entries are {input, output, calls} — the field-name
    # mismatch (total_tokens) silently reported 0 tokens per question on
    # the first live run (2026-09-16). Input + output is the true cost.
    total = 0
    for usage in (per_model or {}).values():
        total += int(usage.get("input") or 0) + int(usage.get("output") or 0)
    return total


async def shadow_pass(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Live V1-vs-V2 measurement over the battery (the shadow node rides
    every full pipeline run). Records per-question outcome, tokens,
    capacity state, and the ledger row written by THIS run."""
    spec = _spec()
    questions = spec["questions"]
    if limit:
        questions = questions[:limit]
    from adaptive_rag import arun_query
    rows: List[Dict[str, Any]] = []
    for q in questions:
        logger.info("RUN %-4s %s", q["id"], q["q"])
        entry: Dict[str, Any] = {"id": q["id"], "q": q["q"],
                                 "expect_path": q["expect_path"]}
        try:
            result = await arun_query(q["q"])
        except Exception as exc:                 # harness failure — a
            logger.warning("     -> run failed: %s", exc)   # question's own
            entry.update({"outcome": "harness_error", "error": str(exc),
                          "agreement_class": None, "capacity_blocked": False,
                          "tokens": 0, "per_model": {}, "latency_s": None})
            rows.append(entry)
            continue
        degraded = result.get("degraded_agents") or []
        answer_text = result.get("answer") or ""
        # Capacity class = the run could not produce a fair V1 answer:
        # an announced quota window, degraded specialists, or an audit
        # that never judged (technical refusal — the marker the
        # 2026-09-16 fix threads through the refusal text).
        capacity = bool(result.get("quota_hint_s")) or bool(degraded) \
            or ("audit stage unavailable" in answer_text)
        ledger = None
        try:
            ledger = _fetch_ledger_row(result.get("run_id") or "", q["q"])
        except Exception as exc:
            logger.warning("     -> ledger read failed (non-fatal): %s", exc)
        entry.update({
            "outcome": result.get("outcome"),
            "grounded": result.get("grounded"),
            "cached": result.get("cached"),
            "quota_hint_s": result.get("quota_hint_s"),
            "degraded_agents": degraded,
            "capacity_blocked": capacity,
            "agreement_class": (ledger or {}).get("agreement_class"),
            "agreement_detail": (ledger or {}).get("agreement_detail"),
            "tokens": _sum_tokens(result.get("per_model")),
            "per_model": result.get("per_model") or {},
            "latency_s": result.get("latency_s"),
        })
        logger.info("     -> outcome=%s cached=%s agree=%s tokens=%s "
                    "latency=%ss", entry["outcome"], entry["cached"],
                    entry["agreement_class"], entry["tokens"],
                    entry["latency_s"])
        rows.append(entry)
    return rows


def evaluate(rows: List[Dict[str, Any]], false_passes: List[str],
             control_failures: List[str], matrix_fails: int,
             limited: bool) -> Tuple[str, Dict[str, Any]]:
    """The A.2 green-night verdict over this run's measurements."""
    covered = [r for r in rows if r["expect_path"] == "fact"]
    covered_measured = [r for r in covered
                        if r.get("agreement_class") and
                        not r.get("capacity_blocked")]
    agree = [r for r in covered_measured
             if r["agreement_class"] in AGREE_CLASSES[:2]]
    capacity_blocked = sorted(r["id"] for r in covered
                             if r.get("capacity_blocked"))
    missing_ledger = sorted(r["id"] for r in covered
                            if not r.get("agreement_class")
                            and not r.get("capacity_blocked"))
    agreement_pct = round(100 * len(agree) / len(covered_measured), 2) \
        if covered_measured else 0.0
    rollup = {
        "covered_total": len(covered),
        "covered_measured": len(covered_measured),
        "agreement_pct": agreement_pct,
        "by_class": {}, "capacity_blocked": capacity_blocked,
        "missing_ledger": missing_ledger,
        "corrupted_false_passes": false_passes,
        "control_failures": control_failures,
        "matrix_fails": matrix_fails,
    }
    for r in covered_measured:
        rollup["by_class"][r["agreement_class"]] = \
            rollup["by_class"].get(r["agreement_class"], 0) + 1

    if limited:
        verdict = SMOKE
    elif false_passes or control_failures:
        verdict = RED
    elif matrix_fails:
        verdict = RED
    elif capacity_blocked or missing_ledger or \
            len(covered_measured) < len(covered):
        verdict = INCOMPLETE
    elif agreement_pct >= 98.0:
        verdict = GREEN
    else:
        verdict = RED
    return verdict, rollup


def main() -> int:
    ap = argparse.ArgumentParser(description="ADR-017 Phase-1 battery")
    ap.add_argument("--shadow", action="store_true",
                    help="run the live V1 pipeline per question")
    ap.add_argument("--corrupted", action="store_true",
                    help="run the X01-X15 corrupted-claim gate (zero tokens)")
    ap.add_argument("--report", default="shadow_battery_report.json",
                    help="report path (written when --shadow/--corrupted)")
    ap.add_argument("--limit", type=int, default=None,
                    help="smoke subset of questions — verdict is never GREEN")
    ns = ap.parse_args()

    report: Dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "battery": BATTERY.name,
        "limited": bool(ns.limit),
    }
    try:
        matrix_fails = deterministic_pass()
        report["deterministic_matrix"] = {
            "total": len(_spec()["questions"]), "fails": matrix_fails}

        false_passes: List[str] = []
        control_failures: List[str] = []
        if ns.corrupted:
            false_passes, control_failures = corrupted_pass()
            report["corrupted"] = {
                "false_passes": false_passes,
                "control_failures": control_failures}

        rows: List[Dict[str, Any]] = []
        if ns.shadow:
            rows = asyncio.run(shadow_pass(ns.limit))
            verdict, rollup = evaluate(rows, false_passes,
                                       control_failures, matrix_fails,
                                       bool(ns.limit))
            report["shadow_rows"] = rows
            report["rollup"] = rollup
            report["tokens"] = {
                "covered_total": sum(r["tokens"] for r in rows
                                     if r["expect_path"] == "fact"),
                "fleet_total": sum(r["tokens"] for r in rows
                                   if r["expect_path"] == "fleet"),
            }
            try:
                from fact_shadow import shadow_summary
                report["ledger_epoch_summary"] = shadow_summary()
            except Exception as exc:
                logger.warning("ledger epoch summary unavailable: %s", exc)
        else:
            verdict = (RED if (matrix_fails or false_passes
                               or control_failures) else GREEN)
    except Exception as exc:                        # harness error class
        logger.exception("BATTERY HARNESS ERROR: %s", exc)
        report["verdict"] = "HARNESS_ERROR"
        report["error"] = str(exc)
        Path(ns.report).write_text(json.dumps(report, indent=2),
                                   encoding="utf-8")
        return 3

    report["verdict"] = verdict
    if ns.corrupted or ns.shadow:
        Path(ns.report).write_text(json.dumps(report, indent=2),
                                   encoding="utf-8")
        logger.info("Report written: %s | VERDICT: %s", ns.report, verdict)
    else:
        logger.info("VERDICT: %s (matrix only)", verdict)
    return {GREEN: 0, RED: 1, INCOMPLETE: 2, SMOKE: 0}.get(verdict, 3)


if __name__ == "__main__":
    sys.exit(main())
