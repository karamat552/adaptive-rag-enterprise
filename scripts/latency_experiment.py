"""THE LATENCY-VS-QUALITY EXPERIMENT HARNESS — the Pareto bake-off.

WHAT IT DOES
    Runs the SAME uncovered-question set through the live pipeline under
    DIFFERENT synthesis-evidence strategies (RAG_SYNTH_EVIDENCE_MAX_CHUNKS:
    0 = the full stack, 12, 8, 6...), measuring per question:
      - the end-to-end wall-clock latency
      - the tokens (input + output, per the pipeline's own telemetry)
      - the answer text (graded later by the judge — see
        scripts/generate_answer_eval.py --grade, the quality half of the
        Pareto pair)
    and writes one answers.jsonl per strategy + a summary.

TWO MODES
    run     — one strategy against the question set:
              python scripts/latency_experiment.py run --evidence-max 8 \
                --label cap8 --limit 6 --out drill/latexp
    pareto  — aggregate the runs' summaries into the comparison table:
              python scripts/latency_experiment.py pareto --out drill/latexp

SAFETY
    Write-capable (the live runs write refusal receipts): the production
    target is refused with NO override flag (the target_guard convention).
    The question set is the PRE-REGISTERED battery's fleet-class slice —
    no post-hoc questions.

HONEST LIMITS (stated up front)
    - n is small (6-12 questions per strategy): the percentiles are
      indicative, not statistics. The point is the DIRECTION and the
      magnitude of the trade-off, judged against the answer-quality
      scores from the same runs.
    - The quota weather dominates the tail: run in a calm window or the
      comparison measures the weather, not the strategies.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BATTERY_PATH = REPO / "tests" / "battery_phase1_preregistered.json"


def fleet_questions(limit: Optional[int]) -> List[Dict[str, Any]]:
    """The PRE-REGISTERED battery's fleet-class questions — the questions
    the FastPath correctly does NOT serve, where the synthesis latency
    actually lives. No post-hoc questions: the battery is fixed."""
    spec = json.loads(BATTERY_PATH.read_text(encoding="utf-8"))
    qs = [q for q in spec["questions"] if q.get("expect_path") == "fleet"]
    return qs[:limit] if limit else qs


def run_strategy(args) -> int:
    """One strategy: the env knob set before the pipeline import, the
    live runs per question, the measurements + answers written."""
    os.environ["RAG_SYNTH_EVIDENCE_MAX_CHUNKS"] = str(args.evidence_max)
    label = args.label or (f"cap{args.evidence_max}" if args.evidence_max
                           else "baseline")

    import target_guard  # scripts/ on sys.path when run directly
    from db import get_settings as db_settings
    target_guard.refuse_production_writes(
        {"runtime": db_settings().database_url or "",
         "admin": db_settings().admin_database_url or ""},
        "latency_experiment.py")

    import adaptive_rag as ar

    questions = fleet_questions(args.limit)
    if not questions:
        print("REFUSED: no fleet-class questions resolved.")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    print(f"[{label}] evidence-max={args.evidence_max} | "
          f"questions={len(questions)}")

    for i, q in enumerate(questions, 1):
        t0 = time.perf_counter()
        try:
            result = asyncio_run_query(q["q"])
        except Exception as exc:
            entry = {"id": q["id"], "q": q["q"], "label": label,
                     "outcome": "harness_error", "error": str(exc)[:200],
                     "latency_s": round(time.perf_counter() - t0, 2),
                     "tokens": 0, "answer": ""}
            rows.append(entry)
            print(f"  [{i}/{len(questions)}] harness_error: {exc}")
            continue
        dt = round(time.perf_counter() - t0, 2)
        tokens = sum((u.get("input", 0) + u.get("output", 0))
                     for u in (result.get("per_model") or {}).values())
        entry = {"id": q["id"], "q": q["q"], "label": label,
                 "outcome": result.get("outcome"),
                 "grounded": result.get("grounded"),
                 "fastpath_served": result.get("fastpath_served"),
                 "run_id": result.get("run_id"),
                 "latency_s": dt, "tokens": tokens,
                 "answer": (result.get("answer") or "")[:4000]}
        rows.append(entry)
        print(f"  [{i}/{len(questions)}] outcome={entry['outcome']} "
              f"| {dt:.1f}s | tokens={tokens}")

    answers_path = out_dir / f"answers_{label}.jsonl"
    with answers_path.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps({"id": e["id"], "q": e["q"],
                                "outcome": e["outcome"],
                                "answer": e["answer"]},
                               default=str) + "\n")
    summary = {"label": label, "evidence_max": args.evidence_max,
               "rows": rows, "n": len(rows),
               "latencies": [e["latency_s"] for e in rows],
               "tokens_total": sum(e["tokens"] for e in rows)}
    (out_dir / f"run_{label}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    print(f"RUN OK: {label} | {out_dir / f'run_{label}.json'}")
    return 0


def asyncio_run_query(q: str):
    """arun_query through a fresh event loop (the same pattern the bench
    uses — one loop per process, the async pipeline inside)."""
    import asyncio
    from adaptive_rag import arun_query
    return asyncio.run(arun_query(q))


def pareto(args) -> int:
    """Aggregate the runs' summaries into the comparison table. The
    quality half: the judge scores from generate_answer_eval --grade (the
    grades_{label}.json files, if present) join by label."""
    out_dir = Path(args.out)
    rows = []
    for f in sorted(out_dir.glob("run_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        lat = sorted(d.get("latencies") or [])
        p50 = lat[len(lat) // 2] if lat else None
        p95 = lat[int(round(0.95 * (len(lat) - 1)))] if lat else None
        grade_p = out_dir / f"grades_{d.get('label')}.json"
        judge = None
        if grade_p.exists():
            try:
                g = json.loads(grade_p.read_text(encoding="utf-8"))
                judge = (g.get("summary") or {}).get("mean_score")
            except Exception:
                pass
        rows.append({"label": d.get("label"),
                     "evidence_max": d.get("evidence_max"),
                     "n": d.get("n"), "p50": p50, "p95": p95,
                     "tokens_total": d.get("tokens_total"),
                     "judge_mean": judge})
    rows.sort(key=lambda x: (x.get("evidence_max") is None,
                             x.get("evidence_max") or 0))
    print(f"{'strategy':<12} {'n':>3} {'p50(s)':>8} {'p95(s)':>8} "
          f"{'tokens':>8} {'judge':>6}")
    for x in rows:
        print(f"{x['label']:<12} {x['n']:>3} {x['p50']:>8} {x['p95']:>8} "
              f"{x['tokens_total']:>8} {x['judge_mean'] if x['judge_mean'] else '—':>6}")
    print("\nPareto read: lower latency + higher judge = strictly better; "
          "a latency win with a quality drop is a TRADE, not a win.")
    (out_dir / "pareto.json").write_text(json.dumps(rows, indent=2),
                                         encoding="utf-8")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    run_p = sub.add_parser("run", help="one strategy against the question set")
    run_p.add_argument("--evidence-max", type=int, default=0,
                       help="RAG_SYNTH_EVIDENCE_MAX_CHUNKS value (0 = the "
                            "full evidence stack, the baseline)")
    run_p.add_argument("--label", default="",
                       help="strategy label for the output files")
    run_p.add_argument("--limit", type=int, default=6,
                       help="fleet-class questions to run (default 6)")
    run_p.add_argument("--out", default="drill/latexp",
                       help="output directory")

    par_p = sub.add_parser("pareto", help="aggregate the runs' summaries")
    par_p.add_argument("--out", default="drill/latexp")

    args = ap.parse_args(argv)
    if args.mode == "run":
        return run_strategy(args)
    return pareto(args)


if __name__ == "__main__":
    sys.exit(main())
