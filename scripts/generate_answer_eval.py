"""Answer-quality grading of the pipeline's answers — never scored before.

WHAT IT MEASURES
    The prose correctness of answers against a written rubric
    (answer-quality-v1). tests/test_answer_accuracy.py is marked live (needs
    real tokens), so answer quality has NEVER been scored by any script.
    Refusals are graded ON HONESTY, not punished as failure: the project
    spent real effort making refusals name their true cause, and a rubric
    that punished every refusal equally would reward exactly the dishonesty
    that work fixed.

DEFINITION (so the score cannot be read loosely)
    A judge model scores each answer 1-5 with the rubric text included
    VERBATIM in the prompt (so scoring is inspectable). The judge sees only
    the question and the answer — it scores what is IN the answer, not what
    the pipeline intended. A judge score is an OPINION OF ONE MODEL against
    a fixed rubric: quote it as such, with the judge model named, never as
    ground truth. Non-negotiables cap at 1: fabricated citation index,
    figure not verbatim in cited evidence, prose citing sources its own
    ledger does not list. Refusal scoring: refused AND named the true cause
    => 4; refused with no cause or a wrong cause => 2 (R4/R2 in the rubric).

    Known limitation, stated up front: the judge can verify that a refusal
    NAMES a cause, but verifying the cause is the TRUE one requires the
    pipeline's own state (outcome/audit markers). RUN captures the outcome,
    and GRADE includes it in the prompt so the judge can check the named
    cause against the pipeline's recorded outcome — that is the strongest
    cause-verification available without logs, and it is still a model
    judging a model. Before QUOTING a mean score I would additionally
    require: a second judge from a different model family, and a human
    spot-check of ~5 graded answers. Neither is implemented here.

PREREQUISITES
    BUILD: none (stdlib only; no keys, no DB, no network). The battery is
    READ-ONLY — its registered invariant is that questions are fixed.
    RUN: provider key + reachable DB + network (real pipeline, real tokens).
    GRADE: provider key (the judge is served through the repo's provider
    seam, default judge = the executive model).

Exit codes: 0 = stage completed and wrote its outputs; 1 = refused / failed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO = Path(__file__).resolve().parent.parent
BATTERY_PATH = REPO / "tests" / "battery_phase1_preregistered.json"
RUBRIC_VERSION = "answer-quality-v1"

RUBRIC_TEXT = """# Answer-quality rubric (v1)

Score each answer 1-5 on this scale. Score what is IN the answer, not what you
think the pipeline intended.

5  Correct figure(s), cited, units and period stated as the source declares them.
4  Correct and cited, but a presentation gap (missing year token, unit implied
   rather than stated, a cited figure whose basis is ambiguous).
3  Partially correct: the right company and period but a wrong or missing figure,
   OR the answer is right but the citations do not cover the claim.
2  Unsupported or unverifiable: a figure with no usable citation, or an answer
   that does not address the question asked.
1  Fabricated, contradictory, or internally inconsistent (e.g. the text states
   one value and the ledger/table states another).

## Refusals are graded on honesty, not punished as failure

R4  Refused AND named the true cause (out of domain; audit unavailable;
    provider down; metric not in corpus). Score 4.
R2  Refused without naming any cause, or with a cause that is not the real one
    ("outside scope" when the provider was simply unreachable). Score 2.

## Non-negotiables (any of these caps the score at 1)

- A certified answer that contains a fabricated citation index.
- A figure that does not appear verbatim in the cited evidence.
- An answer whose prose cites evidence its own sources ledger does not list.
"""


def load_questions() -> Dict[str, Any]:
    spec = json.loads(BATTERY_PATH.read_text(encoding="utf-8"))
    questions = spec.get("questions") or []
    if not questions:
        print("REFUSED: battery has no questions.")
        sys.exit(1)
    return spec


def has_provider_key() -> bool:
    return bool(os.getenv("GROQ_API_KEY") or os.getenv("NIM_API_KEY")
                or os.getenv("APINEX_API_KEY") or os.getenv("OPENROUTER_API_KEY")
                or os.getenv("GEMINI_API_KEY"))


def _fixed_invariant_text(spec: Dict[str, Any]) -> str:
    """Find the 'questions are FIXED at registration' sentence in the PARSED
    battery — a raw-text regex picks up JSON escape quotes; parsed values
    are clean."""
    def _walk(node: Any):
        if isinstance(node, str) and "FIXED at registration" in node:
            yield node
        elif isinstance(node, dict):
            for v in node.values():
                yield from _walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from _walk(v)
    for hit in _walk(spec):
        return hit.strip().strip('"')
    return "questions are FIXED at registration (sentence not found in battery)"


def build_packet(out_dir: Path) -> None:
    """BUILD stage — no keys, no DB, no network. The battery is read-only."""
    spec = load_questions()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "_meta": {
            "battery_registered_at": spec.get("registered_at"),
            "rubric_version": RUBRIC_VERSION,
            "n_questions": len(spec["questions"]),
            "registered_invariant": _fixed_invariant_text(spec),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    }
    packet_path = out_dir / "packet.jsonl"
    with packet_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for q in sorted(spec["questions"], key=lambda x: x["id"]):
            fh.write(json.dumps({
                "id": q["id"], "q": q["q"], "class": q["class"],
                "expect_path": q.get("expect_path"),
                "expect_triples": q.get("expect_triples") or [],
            }, ensure_ascii=False) + "\n")
    (out_dir / "rubric.md").write_text(RUBRIC_TEXT, encoding="utf-8")
    print(f"BUILD OK: {packet_path} "
          f"({meta['_meta']['n_questions']} questions) + "
          f"{out_dir / 'rubric.md'} ({RUBRIC_VERSION})")
    print(f"  registered_invariant: {meta['_meta']['registered_invariant']}")


def run_answers(packet_path: Path, out_dir: Path, n: int) -> Optional[Path]:
    """RUN stage — real pipeline, real tokens. Refuses without a key."""
    try:
        from dotenv import load_dotenv
        load_dotenv()   # adaptive_rag.py:67 does this at import; the key check
                        # runs BEFORE that import, so load .env explicitly here
    except ImportError:
        pass
    if not has_provider_key():
        print("REFUSED: RUN needs a provider key (real pipeline, real tokens).")
        print("  prerequisite missing: GROQ_API_KEY / NIM_API_KEY / APINEX_API_KEY /")
        print("  OPENROUTER_API_KEY / GEMINI_API_KEY — none set.")
        return None
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        import adaptive_rag  # noqa: PLC0415
    except Exception as exc:
        print("REFUSED: cannot import the pipeline layer for RUN.")
        print(f"  import error: {type(exc).__name__}: {exc}")
        return None
    import asyncio

    lines = [json.loads(ln) for ln in
             packet_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    questions = [ln for ln in lines if "_meta" not in ln][:n]
    answers_path = out_dir / "answers.jsonl"
    with answers_path.open("w", encoding="utf-8") as fh:
        for q in questions:
            result = asyncio.run(adaptive_rag.arun_query(q["q"]))
            # arun_query returns per_model telemetry (no nested "usage" key
            # — the first capture read the wrong key and recorded 0 for every
            # run; live-caught 2026-10-06). Sum per-model totals instead.
            per_model = result.get("per_model") or {}
            tokens_total = sum(
                (m.get("input") or 0) + (m.get("output") or 0)
                for m in per_model.values())
            rec = {"id": q["id"], "q": q["q"], "answer": result.get("answer"),
                   "outcome": result.get("outcome"),
                   "grounded": result.get("grounded"),
                   "run_id": result.get("run_id"),
                   "tokens_total": tokens_total,
                   "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime())}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  [{q['id']}] outcome={rec['outcome']} run={rec['run_id']}")
    print(f"RUN OK: {answers_path} ({len(questions)} answers)")
    return answers_path


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def grade(answers_path: Path, out_dir: Path, judge_model: str) -> None:
    """GRADE stage — a judge model scores 1-5 with the rubric verbatim in
    the prompt. Judge gotcha (hit in the reference build): judges wrap JSON
    in markdown fences; strip before json.loads, and a parse failure is
    score None / verdict judge_error — never a zero and never a pass."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    if not has_provider_key():
        print("REFUSED: GRADE needs a provider key for the judge model.")
        return None
    try:
        from openai import OpenAI  # the repo's provider seam dependency
    except Exception as exc:
        print(f"REFUSED: cannot import the OpenAI-compatible client: {exc}")
        return None
    # JUDGE CLIENT (fix 2026-10-08): the api_key must FOLLOW the base_url —
    # the old first-set provider-key chain sent Groq's key to whatever
    # RAG_JUDGE_BASE_URL pointed at (a 401 on any non-Groq judge endpoint,
    # e.g. the NIM judge). The key is resolved from the judge URL's host.
    base_url = os.getenv("RAG_JUDGE_BASE_URL",
                         "https://api.groq.com/openai/v1")
    _KEY_BY_HOST = (("api.groq.com", "GROQ_API_KEY"),
                    ("openrouter.ai", "OPENROUTER_API_KEY"),
                    ("integrate.api.nvidia.com", "NIM_API_KEY"),
                    ("apiinex", "APINEX_API_KEY"))
    api_key = next((os.getenv(env) for host, env in _KEY_BY_HOST
                    if host in base_url and os.getenv(env)), None)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)

    judge_tokens_total = 0
    answers = [json.loads(ln) for ln in
               answers_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows: List[Dict[str, Any]] = []
    for rec in answers:
        prompt = (
            f"{RUBRIC_TEXT}\n\n"
            f"## Answer to grade\n\n"
            f"Question: {rec['q']}\n\n"
            f"Pipeline-recorded outcome: {rec.get('outcome')}\n\n"
            f"Answer:\n{rec.get('answer')}\n\n"
            "Apply the rubric exactly. Reply with ONLY a JSON object: "
            '{"score": <1-5>, "verdict": "<one short sentence>", '
            '"judge_reason": "<why, citing the rubric row you used>"}. '
            "If the answer is a refusal, apply R4/R2 using the "
            "pipeline-recorded outcome to judge whether the named cause is true.")
        try:
            # JUDGE OUTPUT ROOM (fix 2026-10-08): max_tokens=400 truncated
            # the JSON mid-reason on 3 of 12 graded answers (judge_error);
            # the judge model is a REASONING model — it burns reasoning
            # tokens BEFORE content, so the cap must cover both. 2000 does.
            resp = client.chat.completions.create(
                model=judge_model, max_tokens=2000, temperature=0.0,
                messages=[{"role": "user", "content": prompt}])
            judge_tokens_total += int(getattr(getattr(resp, "usage", None),
                                              "total_tokens", 0) or 0)
            raw = (resp.choices[0].message.content or "").strip()
            try:
                parsed = json.loads(_strip_fences(raw))
            except json.JSONDecodeError:
                # LENIENT FALLBACK (fix 2026-10-08): the reasoning can eat
                # the cap mid-JSON; the score field comes FIRST in the
                # mandated shape, so a truncated response still carries it
                # verbatim — extract rather than lose the score.
                m = re.search(r'"score"\s*:\s*([1-5])', raw)
                if not m:
                    raise
                parsed = {"score": int(m.group(1)),
                          "verdict": "graded from truncated judge output",
                          "judge_reason": raw[:1000]}
            score = parsed.get("score")
            if not isinstance(score, int) or not (1 <= score <= 5):
                raise ValueError(f"score out of range: {score!r}")
            verdict = str(parsed.get("verdict", ""))[:300]
            reason = str(parsed.get("judge_reason", ""))[:1000]
        except Exception as exc:
            score, verdict, reason = None, "judge_error", f"{type(exc).__name__}: {exc}"
        rows.append({"id": rec["id"], "score": score, "verdict": verdict,
                     "judge_reason": reason, "outcome": rec.get("outcome")})
        print(f"  [{rec['id']}] score={score} ({verdict[:60]})")

    csv_path = out_dir / "grades.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "score", "verdict", "judge_reason"])
        for r in rows:
            w.writerow([r["id"], r["score"] if r["score"] is not None else "n/a",
                         r["verdict"], r["judge_reason"]])
    scored = [r["score"] for r in rows if r["score"] is not None]
    summary = {
        "rubric_version": RUBRIC_VERSION,
        "judge_model": judge_model,
        "n_graded": len(rows),
        "n_judge_error": sum(1 for r in rows if r["score"] is None),
        "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
        "refusal_scores": [r["score"] for r in rows
                           if r.get("outcome") in
                           ("verified_refusal", "unverified_system",
                            "out_of_domain")],
        "judge_tokens_total": judge_tokens_total,
        "run_tokens_total": sum(int(a.get("tokens_total") or 0) for a in answers),
        "caution": "an opinion of one judge model against a fixed rubric; "
                   "quote with the model named, never as ground truth",
    }
    (out_dir / "grades.json").write_text(
        json.dumps({"summary": summary, "grades": rows}, indent=2,
                   ensure_ascii=False), encoding="utf-8")
    print(f"GRADE OK: {csv_path} + grades.json — mean={summary['mean_score']} "
          f"over {len(scored)} scored ({summary['n_judge_error']} judge_error)")
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build-only", action="store_true",
                   help="emit packet.jsonl + rubric.md; no keys, no DB, no network")
    ap.add_argument("--run", action="store_true", help="run the pipeline over the packet")
    ap.add_argument("--grade", action="store_true", help="judge-grade the answers")
    # --run and --grade COMPOSE (the owner's invocation is --run --grade --n 12):
    # stages are separable AND chainable; requiring at least one stage keeps
    # a bare invocation from silently doing nothing.
    ap.add_argument("--answers", default="", help="answers.jsonl to grade (default OUT/answers.jsonl)")
    ap.add_argument("--out", default="eval_out", help="output directory (default eval_out/)")
    ap.add_argument("--n", type=int, default=42, help="limit questions for RUN")
    ap.add_argument("--judge-model", default=os.getenv("RAG_JUDGE_MODEL",
                                                       "openai/gpt-oss-120b"),
                    help="judge model through the provider seam")
    args = ap.parse_args(argv)
    out_dir = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)

    try:
        if not (args.build_only or args.run or args.grade):
            ap.error("choose at least one stage: --build-only / --run / --grade")
            return 1
        if args.build_only:
            build_packet(out_dir)
            return 0
        if args.run:
            packet = out_dir / "packet.jsonl"
            if not packet.exists():
                print(f"REFUSED: {packet} missing — run --build-only first.")
                return 1
            got = run_answers(packet, out_dir, args.n)
            return 0 if got else 1
        if args.grade:
            answers = (Path(args.answers) if args.answers
                       else out_dir / "answers.jsonl")
            if not answers.exists():
                print(f"REFUSED: {answers} missing — run --run first.")
                return 1
            grade(answers, out_dir, args.judge_model)
            return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(f"REFUSED: stage failed: {type(exc).__name__}: {exc}")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
