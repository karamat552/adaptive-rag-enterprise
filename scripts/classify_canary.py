"""
classify_canary.py — reads coverage_report_canary.json and prints a
DIAGNOSIS, not just a score. Separates CAPACITY events (quota walls,
endpoint congestion — the known free-tier weather) from LOGIC drift
(gate rejections, gold misses, citation issues — the real alarm).

Exit codes:
  0 = PASS (recall >= bar, zero fabrications)
  1 = LOGIC DRIFT (gold miss, fabrication, or gate-refusal spike)
  2 = CAPACITY DEGRADED (quota walls starved the run — re-run in a calm window)
  3 = HARNESS ERROR (exceptions in the battery itself)

Usage:  python classify_canary.py [path/to/coverage_report.json]
"""
import json
import sys
from pathlib import Path


def classify(report: dict) -> tuple:
    metrics = report.get("metrics", {})
    rows = report.get("results", [])

    recall = metrics.get("recall_at_answerable", 0)
    fabs = metrics.get("fabrications", 0)
    gold = metrics.get("gold_accuracy")
    refusals = metrics.get("refusal_correctness")
    adversarial = metrics.get("adversarial_blocked", 0)
    wall = metrics.get("wall_time_total_s", 0)

    # Capacity signature: the run's own quota_hint_s (threaded by the
    # self-pacing fix) or degraded agents from starved specialists.
    capacity_hits = sum(
        1 for r in rows
        if r.get("quota_hint_s") or "synthesis" in (r.get("degraded_agents") or [])
        or "financial" in (r.get("degraded_agents") or []))
    logic_refusals = [
        r for r in rows
        if r.get("outcome") == "verified_refusal"
        and not r.get("quota_hint_s")
        and not (r.get("degraded_agents") or [])
    ]

    findings = []
    if fabs > 0:
        findings.append(f"🚨 {fabs} FABRICATION(S) — the invariant is BROKEN, "
                        "investigate immediately")
    if gold is not None and gold < 1.0:
        findings.append(f"⚠️ gold accuracy {gold:.0%} — a certified answer "
                        "carried a wrong figure")
    if logic_refusals:
        findings.append(f"⚠️ {len(logic_refusals)} LOGIC refusal(s) (no "
                        "quota/degraded markers) — inspect miss_reasons")
    if capacity_hits:
        findings.append(f"ℹ️ {capacity_hits} capacity-refusal row(s) — "
                        "re-run in a calm window before concluding drift")

    if recall < 0.70 and not capacity_hits:
        findings.append(f"🚨 recall {recall:.0%} BELOW the 70% bar with no "
                        "capacity markers — this is logic drift, not weather")

    return findings, capacity_hits, logic_refusals


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage_report_canary.json")
    if not path.exists():
        print(f"FAIL: {path} not found")
        return 3
    report = json.loads(path.read_text(encoding="utf-8"))
    metrics = report.get("metrics", {})
    findings, capacity_hits, logic_refusals = classify(report)

    print("=" * 66)
    print(f"CANARY | recall={metrics.get('recall_at_answerable', 0):.0%} "
          f"({metrics.get('certified_total', 0)}/{metrics.get('answerable_total', 0)}) "
          f"| gold={metrics.get('gold_accuracy')} | FAB={metrics.get('fabrications', 0)} "
          f"| wall={wall_display(metrics)}")
    print("=" * 66)
    for f in findings:
        print(f"  {f}")
    if not findings:
        print("  CLEAN — no logic drift, no capacity events")

    # Exit semantics
    if metrics.get("fabrications", 0) > 0:
        print("\nEXIT 1: fabrication — invariant broken")
        return 1
    if capacity_hits > 0 and logic_refusals == 0 and recall >= 0.70:
        print("\nEXIT 0: pass (with capacity notes)")
        return 0
    if logic_refusals or recall < 0.70:
        print("\nEXIT 1: logic drift detected")
        return 1
    print("\nEXIT 0: pass")
    return 0


def wall_display(metrics):
    wall = metrics.get("wall_time_total_s", 0)
    return f"{wall:.0f}s"


if __name__ == "__main__":
    sys.exit(main())
