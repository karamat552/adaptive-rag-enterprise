"""Per-stage latency and token cost of the real pipeline, question by question.

WHAT IT MEASURES
    Where the wall-clock actually goes. "p95 = 12s" is not actionable; knowing
    that most of it is the specialist fleet + synthesis and that the
    deterministic stages (cache_check, premise probe, the gate chain inside
    the guard) cost milliseconds and ZERO tokens is. The graph is streamed
    with stream_mode="updates", which yields once per completed node, so the
    delta between consecutive yields is that node's own wall-clock. Token
    usage is summed from the state updates the pipeline already tracks.

DEFINITION (so the numbers cannot be read loosely)
    latency: wall-clock milliseconds per NODE, measured as the interval
    between consecutive update yields (single-threaded graph: valid).
    answer vs refusal: an outcome in REFUSAL_OUTCOMES
    {verified_refusal, unverified_system, out_of_domain} is a REFUSAL and is
    EXCLUDED from latency statistics. Deviation from spec, named: the repo
    has no ANSWERED_OUTCOMES constant (verified 2026-10-05); the refusal
    literals above are the repo's real outcome values, so "answered" is
    defined as outcome NOT in that set. WHY refusals are excluded: a refusal
    is fast BECAUSE IT SKIPPED THE WORK. The first version of this script
    counted refusals as answers and reported "4/4 answered" while every log
    line said FAIL-CLOSED to refusal — averaging refusals in flatters a
    system that never answered. If 0 of N answered: "answered 0/N",
    "latency NOT REPORTED", exit 1. No p50 over nothing.
    cost: an ESTIMATE from the pipeline's own token counters at a
    user-supplied price (--price-in/--price-out, USD per 1M). It inherits
    the accuracy of both. Default price 0 => columns print n/a, never a
    made-up figure. The cost denominator is the SAME set as the latency
    denominator (answered runs only).

TARGET GUARD (this script WRITES — refusal receipts)
    bench_latency runs the real pipeline, which stores receipts. It must
    never run against the production database. scripts/target_guard.py
    classifies EVERY effective URL (runtime AND admin identities) against
    the production (host, database) pairs from .env and REFUSES production.
    There is deliberately NO flag to override: a production write must
    never be one argument away (the audit-tool lesson, 2026-10-05). A
    disposable database on the production HOST is allowed — classification
    is per (host, database), never host alone.

PREREQUISITES
    A reachable seeded database (scripts/local_stack_bootstrap.py) + (for
    model stages) a provider key. Under --deterministic-only no key is
    needed: provider keys are stripped from THIS process so model stages
    cannot run; stages that would call a model are reported SKIPPED, never
    0ms — a zero would read as "instant".

Exit codes: 0 = at least one answered run measured; 1 = refused / nothing
measurable (including answered 0/N).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(REPO))   # repo root for `import db` / adaptive_rag
BATTERY_PATH = REPO / "tests" / "battery_phase1_preregistered.json"
REFUSAL_OUTCOMES = {"verified_refusal", "unverified_system", "out_of_domain"}
USAGE_KEYS = ("usage_in", "usage_out", "usage_total", "llm_calls")
PROVIDER_KEY_VARS = ("GROQ_API_KEY", "NIM_API_KEY", "APINEX_API_KEY",
                    "OPENROUTER_API_KEY", "GEMINI_API_KEY")


def _import_pipeline():
    try:
        import adaptive_rag  # noqa: PLC0415
        import db  # noqa: PLC0415
        return adaptive_rag, db
    except Exception as exc:
        print("REFUSED: cannot import the pipeline layer (adaptive_rag/db).")
        print(f"  import error: {type(exc).__name__}: {exc}")
        print("  prerequisite missing: a working local Python stack for db.py.")
        print("  No numbers without it.")
        sys.exit(1)


def has_provider_key() -> bool:
    return any(os.getenv(v) for v in PROVIDER_KEY_VARS)


def load_questions() -> List[Dict[str, Any]]:
    spec = json.loads(BATTERY_PATH.read_text(encoding="utf-8"))
    qs = spec.get("questions") or []
    if not qs:
        print("REFUSED: battery has no questions.")
        sys.exit(1)
    return qs


def time_one(question: str, adaptive_rag) -> Dict[str, Any]:
    """Stream one question; attribute inter-yield deltas to the node that
    just completed. graph.astream is an ASYNC generator — iterate with
    `async for` under asyncio.run (an earlier version used sync iteration
    and died on 'async_generator is not iterable')."""
    async def _stream() -> Dict[str, Any]:
        graph = adaptive_rag.get_graph()
        state = {"original_question": question, "retry_count": 0,
                 "run_id": f"bench-{time.strftime('%H%M%S')}-{hash(question) & 0xffff:x}",
                 "tenant_id": "default"}
        stage_ms: Dict[str, int] = {}
        tokens = {k: 0 for k in USAGE_KEYS}
        outcome: Optional[str] = None
        t0 = time.monotonic()
        last = t0
        ran_nodes: List[str] = []
        async for update in graph.astream(
                state, config={"recursion_limit": 25},
                stream_mode="updates"):
            now = time.monotonic()
            if not isinstance(update, dict):
                continue
            for node, payload in update.items():
                stage_ms[node] = stage_ms.get(node, 0) + int((now - last) * 1000)
                ran_nodes.append(node)
                if isinstance(payload, dict):
                    if payload.get("outcome"):
                        outcome = payload["outcome"]
                    for k in USAGE_KEYS:
                        if isinstance(payload.get(k), (int, float)):
                            tokens[k] += int(payload[k])
            last = now
        return {"outcome": outcome,
                "answered": outcome not in REFUSAL_OUTCOMES and outcome is not None,
                "ms_total": int((time.monotonic() - t0) * 1000),
                "stage_ms": stage_ms, "ran_nodes": ran_nodes, "tokens": tokens}

    import asyncio
    return asyncio.run(_stream())


def _pct(values: List[int], p: float) -> Optional[int]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100.0 * (len(s) - 1))))
    return s[idx]


def summarise(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    answered = [r for r in runs if r["answered"]]
    summary: Dict[str, Any] = {"n_runs": len(runs), "n_answered": len(answered)}
    if answered:
        totals = [r["ms_total"] for r in answered]
        summary["p50_ms"] = _pct(totals, 50)
        summary["p95_ms"] = _pct(totals, 95)
        stage_totals: Dict[str, int] = {}
        for r in answered:
            for st, ms in r["stage_ms"].items():
                stage_totals[st] = stage_totals.get(st, 0) + ms
        summary["stage_ms_sum_over_answered"] = dict(sorted(stage_totals.items()))
        summary["tokens_per_answered_query_mean"] = {
            k: (round(statistics.fmean([r["tokens"][k] for r in answered]), 1)
                if any(r["tokens"][k] for r in answered) else 0)
            for k in USAGE_KEYS}
    else:
        summary["p50_ms"] = None
        summary["p95_ms"] = None
    return summary


def cost_columns(summary: Dict[str, Any], price_in: float, price_out: float) -> str:
    if not summary.get("n_answered") or not (price_in or price_out):
        return "n/a (no price supplied / nothing answered)"
    t = summary["tokens_per_answered_query_mean"]
    per_q = (t.get("usage_in", 0) * price_in
             + t.get("usage_out", 0) * price_out) / 1_000_000.0
    return f"${per_q:.6f}/query (estimate: pipeline counters x user-supplied price)"


def run_all(args: argparse.Namespace) -> int:
    adaptive_rag, db = _import_pipeline()

    # TARGET GUARD: this script writes (refusal receipts). Production is
    # refused with NO override flag; classification covers both identities.
    import target_guard  # scripts/ is on sys.path when run directly
    target_guard.refuse_production_writes(
        {"runtime": db.get_settings().database_url or "",
         "admin": db.get_settings().admin_database_url or ""},
        "bench_latency.py")

    questions = load_questions()[:args.n]
    if args.deterministic_only:
        # Make the mode literal: strip provider keys from THIS process so no
        # model stage can run. The graph's fail-closed paths then produce
        # refusals (router unavailable) and only deterministic stages run —
        # matching "--deterministic-only needs no provider key".
        stripped = [k for k in PROVIDER_KEY_VARS if os.environ.pop(k, None)]
        if stripped:
            print(f"  deterministic-only: stripped {len(stripped)} provider "
                  f"key(s) from this process — model stages cannot run.")
    if not has_provider_key() and not args.deterministic_only:
        print("REFUSED: no provider key configured and --deterministic-only not")
        print("  passed. Model stages would fail-closed into refusals, and a")
        print("  refusal-only run is not a latency measurement of the pipeline.")
        return 1

    target = db.get_settings().database_url or ""
    masked = target.split("@")[-1] if "@" in target else "n/a"
    print("\nBENCHING: per-stage latency + token cost")
    print(f"  questions    : {len(questions)} (pre-registered battery, first n)")
    print(f"  db target    : {masked} (disposable/local — production refused)")
    print(f"  provider key : {'present' if has_provider_key() else 'ABSENT'}"
          f"{' (deterministic-only mode)' if args.deterministic_only else ''}")
    print(f"  refusal rule : outcomes in {sorted(REFUSAL_OUTCOMES)} are EXCLUDED")

    runs: List[Dict[str, Any]] = []
    known_nodes: set = set()
    try:
        known_nodes = set(adaptive_rag.get_graph().nodes.keys())
    except Exception:
        known_nodes = set()

    for q in questions:
        r = time_one(q["q"], adaptive_rag)
        r["question"] = q["q"]
        runs.append(r)
        print(f"  [{q['id']}] outcome={r['outcome']} answered={r['answered']} "
              f"total={r['ms_total']}ms tokens={r['tokens']['usage_total']}")

    summary = summarise(runs)
    if not summary["n_answered"]:
        print(f"\nanswered {summary['n_answered']}/{summary['n_runs']}")
        print("latency NOT REPORTED — every run refused; a refusal is fast")
        print("because it skipped the work (see docstring). This is a refusal,")
        print("not a fast system.")
        if args.json:
            Path(args.json).write_text(
                json.dumps({"summary": summary, "runs": runs}, indent=2, default=str),
                encoding="utf-8")
        return 1

    print(f"\nanswered {summary['n_answered']}/{summary['n_runs']} "
          f"(refusals excluded from latency statistics)")
    print(f"  p50 = {summary['p50_ms']}ms   p95 = {summary['p95_ms']}ms "
          f"(over answered runs only)")
    print("  stage ms (sum over answered, deterministic order):")
    for st, ms in sorted(summary["stage_ms_sum_over_answered"].items()):
        print(f"    {st:24s} {ms}ms")
    if args.deterministic_only:
        skipped = sorted(known_nodes - {s for r in runs for s in r["ran_nodes"]})
        if skipped:
            print("  model-dependent stages SKIPPED (no key / deterministic-only):")
            for st in skipped:
                print(f"    {st:24s} SKIPPED")
    print(f"  tokens/query (mean over answered): "
          f"{summary['tokens_per_answered_query_mean']}")
    print(f"  cost: {cost_columns(summary, args.price_in, args.price_out)}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"summary": summary, "runs": runs}, indent=2, default=str),
            encoding="utf-8")
        print(f"  JSON written: {args.json}")
    if args.markdown:
        lines = ["| stage | ms (sum over answered) |", "|---|---|"]
        for st, ms in sorted(summary["stage_ms_sum_over_answered"].items()):
            lines.append(f"| {st} | {ms} |")
        Path(args.markdown).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  markdown written: {args.markdown}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=5,
                    help="number of battery questions to run (default 5)")
    ap.add_argument("--deterministic-only", action="store_true",
                    help="no provider key needed; model stages report SKIPPED, never 0ms")
    ap.add_argument("--price-in", type=float, default=0.0,
                    help="USD per 1M input tokens (default 0 => cost prints n/a)")
    ap.add_argument("--price-out", type=float, default=0.0,
                    help="USD per 1M output tokens (default 0 => cost prints n/a)")
    ap.add_argument("--json", default="", help="write full JSON here")
    ap.add_argument("--markdown", default="", help="write a stage-ms markdown table here")
    args = ap.parse_args(argv)
    try:
        return run_all(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"REFUSED: benchmark failed before producing numbers: "
              f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
