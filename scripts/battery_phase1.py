"""ADR-017 Phase 1 — the A.2 battery runner.

Consumes the PRE-REGISTERED battery (tests/battery_phase1_preregistered.json
— fixed before any measurement; A.2's no-post-hoc-questions rule) and:

  1. DETERMINISTIC PASS (zero tokens): every question's routing
     expectation is checked against path_a_decision + the live fact
     keys. This is the coverage matrix — it can never drift silently.
  2. SHADOW PASS (one pipeline run per question): runs the full V1
     pipeline (the shadow node rides along), reads the disagreement
     ledger, and prints the A.2 roll-up.

Usage:
  python scripts/battery_phase1.py            # deterministic pass only
  python scripts/battery_phase1.py --shadow   # + live V1-vs-V2 runs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("Battery")

BATTERY = Path(__file__).resolve().parent.parent / \
    "tests" / "battery_phase1_preregistered.json"


def deterministic_pass() -> int:
    """Zero-token routing matrix: battery expectations vs the guard."""
    from db import get_fact_rows
    from fact_extract import path_a_decision

    spec = json.loads(BATTERY.read_text(encoding="utf-8"))
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


async def shadow_pass() -> None:
    """Live V1-vs-V2 measurement over the battery (needs the gateway's
    pipeline; each question is one full run). Prints the A.2 roll-up
    from the disagreement ledger."""
    spec = json.loads(BATTERY.read_text(encoding="utf-8"))
    from adaptive_rag import arun_query
    for q in spec["questions"]:
        logger.info("RUN %-4s %s", q["id"], q["q"])
        try:
            result = await arun_query(q["q"])
            logger.info("     -> outcome=%s cached=%s",
                        result.get("outcome"), result.get("cached"))
        except Exception as exc:
            logger.warning("     -> run failed: %s", exc)
    from fact_shadow import shadow_summary
    logger.info("A.2 ROLL-UP: %s", shadow_summary())


def main() -> int:
    ap = argparse.ArgumentParser(description="ADR-017 Phase-1 battery")
    ap.add_argument("--shadow", action="store_true",
                    help="run the live V1 pipeline per question")
    ns = ap.parse_args()
    fails = deterministic_pass()
    if ns.shadow:
        asyncio.run(shadow_pass())
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
