"""Span-level retrieval recall over the pre-registered battery.

WHAT IT MEASURES
    Whether retrieval actually FINDS the evidence, at the span level — not
    whether the answer was right. This is the instrument for the 50-67%
    recall figure in KNOWN_ISSUES.md, which has never been re-measured by a
    script.

DEFINITION (so the number cannot be read loosely)
    Ground truth is DERIVED FROM THE DATABASE, not hand-written: for every
    battery question declaring `expect_triples`, each (company, metric,
    period) triple is resolved to RECONCILED fact_rows rows (dual-key
    verified: a PDF span AND an SEC XBRL value agreed). Each row is
    span-anchored to the exact transcript bytes the figure lives in.

    A retrieved chunk is a HIT only when it is from the same (source, page)
    AND its transcript span OVERLAPS the fact row's span. A chunk that is
    merely "about Apple revenue" but does not physically contain the number
    is a MISS. Chunk and fact spans share one coordinate system: the page
    transcript (verified 2026-10-05: chunk.content == transcript[char_start:char_end]).

    recall@k    = expected evidence units found in top-k / total expected units
                  (pooled over questions, unit-level)
    precision@k = retrieved chunks on a globally-expected page / k
                  where globally-expected pages = the (source, page) set of
                  ALL resolved evidence units across the battery.

    Reconciled-only ground truth is a deliberate power limitation: only
    span-verified, dual-key rows are trusted as "where the figure lives"
    (the 2026-10-05 span scan found value-not-in-span defects ONLY among
    unreconciled rows). Fewer expected units means a smaller denominator —
    reported, never hidden.

PREREQUISITES (each is a refusal, exit 1, not an error to work around)
    1. The REAL embedder BAAI/bge-small-en-v1.5. A substitute (e.g. the
       deterministic hash embedder) produces numbers with no semantic
       meaning — measured once: recall 0.038, pure noise. Substitutes are
       refused unless --allow-substitute-embedder, which stamps every
       result valid_for_claims: false and prints INVALID FOR CLAIMS.
    2. A populated, seeded database (scripts/local_stack_bootstrap.py) with
       fact_rows. Nothing reconciled -> the expected-evidence set is empty
       and there is NOTHING TO MEASURE (scripts/sync_xbrl_and_verify.py
       populates facts when SEC is reachable).
    3. Battery self-consistency. The battery's registered .mix_declaration
       (in_coverage_exact: 14, in_coverage_alias_phrasing: 8) DISAGREES with
       the actual question classes (9 and 9) — verified 2026-10-05 in this
       repo. Per-class numbers computed against labels that may have moved
       are a quiet lie: per-class recall is REFUSED unless
       --ignore-mix-declaration. Overall recall is still emitted (it does
       not depend on class labels). This script does not assert WHY they
       disagree — that requires history that no longer exists.

Exit codes: 0 = measured (overall); 1 = refused / nothing measurable.
Never quote a recall figure without its valid_for_claims flag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(REPO))   # repo root for `import db` / adaptive_rag
BATTERY_PATH = REPO / "tests" / "battery_phase1_preregistered.json"
REAL_EMBEDDER = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384  # bge-small-en-v1.5 output width; the hash substitute matches it


# ---------------------------------------------------------------------------
# imports are LAZY: --help must work without the DB stack, and a blocked
# import (e.g. the mmh3 Application-Control DLL block on the dev machine)
# must surface as a clean refusal, not a traceback.
# ---------------------------------------------------------------------------
def _import_db():
    try:
        import db  # noqa: PLC0415
        return db
    except Exception as exc:  # no bare except: name exactly what failed
        print("REFUSED: cannot import the database layer (db.py).")
        print(f"  import error: {type(exc).__name__}: {exc}")
        print("  prerequisite missing: a working local Python stack for db.py")
        print("  (on the dev machine this is the Windows Application Control")
        print("   block on the mmh3 DLL reached via db.py -> fastembed).")
        print("  A recall figure cannot be measured without it. Not a partial run.")
        sys.exit(1)


def install_deterministic_embedder(db) -> None:
    """SHA-256-seeded stand-in with REAL_EMBEDDER's dimensions.

    Deviation from spec, named: scripts/_mutant_runner.py has NO
    install_deterministic_embedder() (only a private _fake_embed at line 36);
    the repo wins, so the equivalent is implemented here. To a hash embedder
    'revenue' and 'net sales' are unrelated vectors — its recall is noise,
    which is exactly why results under it are stamped INVALID FOR CLAIMS.
    """
    def _embed(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        out: List[float] = []
        while len(out) < EMBED_DIM:
            digest = hashlib.sha256(digest).digest()
            out.extend(b / 255.0 for b in digest)
        return out[:EMBED_DIM]

    db.embed_query = _embed  # type: ignore[method-assign]
    db.embed_passages = lambda texts: [_embed(t) for t in texts]  # type: ignore[method-assign]


def check_embedder_is_real(db) -> Tuple[bool, str]:
    """True only when the real model is configured AND embed_query is the
    module's own function (not monkeypatched by this or any other script)."""
    try:
        model_name = db.get_settings().embed_model_name
    except Exception as exc:
        return False, f"cannot read embed model setting: {type(exc).__name__}: {exc}"
    if model_name != REAL_EMBEDDER:
        return False, f"configured embedder is {model_name!r}, not {REAL_EMBEDDER!r}"
    if getattr(db.embed_query, "__module__", None) != "db":
        return False, "db.embed_query has been substituted (module provenance mismatch)"
    return True, REAL_EMBEDDER




def _production_host() -> str:
    env_path = REPO / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DB_DATABASE_URL="):
            return (line.split("=", 1)[1].strip().strip('"')
                    .split("@")[-1].split("/")[0])
    return ""


def _guard_production(db_url: str, allow_flag: bool, what: str) -> None:
    """Refuse the production host unless explicitly opted in. The 2026-10-05
    audit-tool lesson: a measurement script that silently trusts the ambient
    .env target is a production footgun, even read-only."""
    from urllib.parse import urlparse
    target = urlparse(db_url).netloc.rpartition("@")[-1]  # strip user:pass
    prod = _production_host()
    if prod and target == prod and not allow_flag:
        print("REFUSED: the database target is the PRODUCTION host")
        print(f"  ({prod}). {what} must run against a seeded disposable stack")
        print("  (scripts/local_stack_bootstrap.py), not production, unless")
        print("  you pass --allow-production deliberately.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# battery
# ---------------------------------------------------------------------------
def load_battery() -> Dict[str, Any]:
    spec = json.loads(BATTERY_PATH.read_text(encoding="utf-8"))
    questions = spec.get("questions") or []
    if not questions:
        print("REFUSED: battery has no questions — nothing to measure.")
        sys.exit(1)
    return spec


def mix_check(spec: Dict[str, Any]) -> Tuple[bool, Dict[str, int], Dict[str, int]]:
    """Compare .mix_declaration with the actual question['class'] counts."""
    declared = {k: v for k, v in (spec.get("mix_declaration") or {}).items()
                if k != "total"}
    actual: Dict[str, int] = {}
    for q in spec["questions"]:
        actual[q["class"]] = actual.get(q["class"], 0) + 1
    return declared == actual, declared, actual


# ---------------------------------------------------------------------------
# ground truth: reconciled fact rows -> span-anchored expected units
# ---------------------------------------------------------------------------
def expected_evidence(db, triples_by_question: Dict[str, List[Tuple[str, str, str]]]
                      ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Resolve expect_triples to RECONCILED fact_rows spans.

    Returns (units, stats) where each unit is
      {question_id, class, company, metric, period, source, page,
       char_start, char_end}
    Facts and chunks share the page-transcript coordinate system, so the
    (source, page) join key and both spans are directly comparable.
    """
    with db.get_db_connection(tenant_id="default") as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.company, f.metric_key, f.period, f.reconciled,
                   f.chunk_hash, f.char_start, f.char_end,
                   c.source, c.page
            FROM fact_rows f
            JOIN multi_agent_chunks c ON c.chunk_hash = f.chunk_hash;
        """)
        rows = [dict(zip(
            ("company", "metric_key", "period", "reconciled", "chunk_hash",
             "char_start", "char_end", "source", "page"),
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])))
            for r in cur.fetchall()]

    stats = {"fact_rows_total": len(rows),
             "reconciled_total": sum(1 for r in rows if r["reconciled"])}
    by_triple: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        if r["reconciled"]:
            by_triple.setdefault((r["company"], r["metric_key"], r["period"]),
                                 []).append(r)

    units: List[Dict[str, Any]] = []
    for qid, triples in sorted(triples_by_question.items()):
        for company, metric, period in triples:
            for r in by_triple.get((company, metric, period), []):
                units.append({"question_id": qid, "company": company,
                              "metric": metric, "period": period,
                              "source": r["source"], "page": r["page"],
                              "char_start": r["char_start"],
                              "char_end": r["char_end"]})
    return units, stats


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Half-open span overlap: [start, end). Both spans are transcript-relative."""
    return a[0] < b[1] and b[0] < a[1]


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def measure(args: argparse.Namespace) -> int:
    db = _import_db()
    _guard_production(db.get_settings().database_url,
                      args.allow_production, "read-only span-overlap measurement")
    spec = load_battery()
    k_values = sorted({int(k) for k in args.k.split(",") if k.strip()})
    top_k = max(k_values)

    # --- refusal 1: embedder -------------------------------------------
    real, why = check_embedder_is_real(db)
    valid_for_claims = True
    if not real:
        if not args.allow_substitute_embedder:
            print("REFUSED: the configured embedder cannot support a claims-grade")
            print(f"  recall figure ({why}).")
            print("  Pass --allow-substitute-embedder for a plumbing-only run")
            print("  (every result is stamped valid_for_claims: false).")
            return 1
        install_deterministic_embedder(db)
        valid_for_claims = False
        print("INVALID FOR CLAIMS, plumbing check only — substitute embedder active.")

    # --- refusal 2: no expected evidence --------------------------------
    triples_by_question: Dict[str, List[Tuple[str, str, str]]] = {}
    for q in spec["questions"]:
        t = q.get("expect_triples") or []
        if t:
            triples_by_question[q["id"]] = [tuple(x) for x in t]
    units, stats = expected_evidence(db, triples_by_question)
    resolved_qids = {u["question_id"] for u in units}
    if not units:
        print("REFUSED: the expected-evidence set is empty — there is nothing to")
        print(f"  measure. fact_rows={stats['fact_rows_total']}, "
              f"reconciled={stats['reconciled_total']}.")
        print("  prerequisite missing: reconciled facts (seed a stack with")
        print("  scripts/local_stack_bootstrap.py, then populate with")
        print("  scripts/sync_xbrl_and_verify.py).")
        return 1

    # --- refusal 3: mix-declaration mismatch (per-class only) -------------
    mix_ok, declared, actual = mix_check(spec)
    per_class: Optional[Dict[str, Any]] = None
    per_class_reason = None
    if mix_ok:
        per_class_allowed = True
    elif args.ignore_mix_declaration:
        per_class_allowed = True
        print("WARNING: --ignore-mix-declaration passed; per-class numbers are")
        print("  computed against question labels that may have moved after")
        print("  registration. Do not quote them as registered-mix results.")
    else:
        per_class_allowed = False
        print("MIX-DECLARATION MISMATCH — per-class recall REFUSED:")
        for cls in sorted(set(declared) | set(actual)):
            print(f"  {cls:36s} declared={declared.get(cls, 0):2d} actual={actual.get(cls, 0):2d}")
        print("  The battery's registered invariant says questions are FIXED at")
        print("  registration. Either five were re-labelled (invariant violated)")
        print("  or the declaration was never updated (benign) — this script")
        print("  does not assert which. Overall recall is unaffected (it does")
        print("  not depend on class labels). Pass --ignore-mix-declaration to")
        print("  emit per-class numbers anyway.")
        per_class_reason = "mix_declaration != actual class counts; " \
                           "pass --ignore-mix-declaration to override"

    # --- header block -----------------------------------------------------
    target = db.get_settings().database_url
    masked = (target.split("@")[-1] if target and "@" in target else "n/a")
    print(f"\nMEASURING: span-level retrieval recall")
    print(f"  battery      : {BATTERY_PATH.name} "
          f"({len(spec['questions'])} questions, {len(triples_by_question)} with triples)")
    print(f"  db target    : {masked}")
    print(f"  embedder     : {'REAL' if valid_for_claims else 'SUBSTITUTE (hash)'} "
          f"({REAL_EMBEDDER} expected)")
    print(f"  expected units: {len(units)} across {len(resolved_qids)} questions "
          f"(fact_rows={stats['fact_rows_total']}, reconciled={stats['reconciled_total']})")
    print(f"  k values     : {k_values}")

    # --- retrieve + score -------------------------------------------------
    by_k_hits = {k: 0 for k in k_values}
    by_k_prec_num = {k: 0 for k in k_values}
    by_k_prec_den = {k: 0 for k in k_values}
    per_class_hits: Dict[str, Dict[int, int]] = {}
    per_class_units: Dict[str, Dict[int, int]] = {}
    misses: List[Dict[str, Any]] = []
    expected_pages = {(u["source"], u["page"]) for u in units}

    for q in spec["questions"]:
        qid, qclass = q["id"], q["class"]
        q_units = [u for u in units if u["question_id"] == qid]
        chunks = db.pgvector_hybrid_search(q["q"], top_k=top_k, tenant_id="default")
        for k in k_values:
            top = chunks[:k]
            by_k_prec_den[k] += len(top)
            by_k_prec_num[k] += sum(1 for c in top
                                    if (c.get("source"), c.get("page")) in expected_pages)
            if not q_units:
                continue
            hit_units = 0
            for u in q_units:
                if any(c.get("source") == u["source"] and c.get("page") == u["page"]
                       and _overlaps((u["char_start"], u["char_end"]),
                                     (int(c["char_start"]), int(c["char_end"])))
                       for c in top):
                    hit_units += 1
            by_k_hits[k] += hit_units
            per_class_hits.setdefault(qclass, {})[k] = \
                per_class_hits.get(qclass, {}).get(k, 0) + hit_units
            per_class_units.setdefault(qclass, {})[k] = \
                per_class_units.get(qclass, {}).get(k, 0) + len(q_units)
            if hit_units < len(q_units) and k == max(k_values):
                misses.append({
                    "question_id": qid, "class": qclass,
                    "units_found": hit_units, "units_expected": len(q_units),
                    "reason": f"{len(q_units) - hit_units} expected unit(s) not in "
                              f"top-{k} chunks (same-source+page+span-overlap required)"})

    overall = {str(k): {
        "recall@%d" % k: (round(by_k_hits[k] / len(units), 4) if units else None),
        "precision@%d" % k: (round(by_k_prec_num[k] / by_k_prec_den[k], 4)
                             if by_k_prec_den[k] else None),
        "units_found": by_k_hits[k], "units_total": len(units),
    } for k in k_values}
    if per_class_allowed:
        per_class = {}
        for cls in sorted(actual):
            for k in k_values:
                tot = per_class_units.get(cls, {}).get(k, 0)
                hit = per_class_hits.get(cls, {}).get(k, 0)
                per_class.setdefault(cls, {})[f"recall@{k}"] = \
                    round(hit / tot, 4) if tot else None
                per_class[cls][f"units@{k}"] = {"found": hit, "total": tot}
    result = {
        "definition": "span-overlap recall: retrieved chunk must share "
                      "(source, page) AND overlap the reconciled fact row's "
                      "transcript span; ground truth = reconciled fact_rows "
                      "resolved from battery expect_triples",
        "k_values": k_values,
        "embedder": {"expected": REAL_EMBEDDER,
                     "valid_for_claims": valid_for_claims},
        "valid_for_claims": valid_for_claims,
        "n_questions": len(spec["questions"]),
        "n_with_triples": len(triples_by_question),
        "expected_units": len(units),
        "expected_units_by_resolved_questions": len(resolved_qids),
        "fact_stats": stats,
        "mix_declaration_matches": mix_ok,
        "per_class_refused_reason": per_class_reason,
        "overall": overall,
        "per_class": per_class if per_class_allowed else None,
        "misses": misses,
    }

    print("\nOVERALL (valid_for_claims: {})".format(
        "true" if valid_for_claims else "FALSE — DO NOT QUOTE"))
    for k in k_values:
        print(f"  recall@{k}    = {overall[str(k)]['recall@%d' % k]}")
        print(f"  precision@{k} = {overall[str(k)]['precision@%d' % k]}")
    if per_class_allowed:
        print("\nPER-CLASS recall@max (labels "
              + ("trusted" if mix_ok else "UNVERIFIED — mix mismatch ignored") + ":")
        for cls in sorted(per_class):
            kk = max(k_values)
            print(f"  {cls:36s} recall@{kk} = {per_class[cls][f'recall@{kk}']}")
    else:
        print("\nPER-CLASS: REFUSED (mix-declaration mismatch). "
              "See mismatch above; --ignore-mix-declaration to override.")
    if misses:
        print(f"\nMISSES ({len(misses)} questions with unfound units at max k):")
        for m in misses:
            print(f"  [{m['question_id']}] {m['reason']}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nJSON written: {args.json}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--k", default="5,10", help="comma-separated k values (default 5,10)")
    ap.add_argument("--json", default="", help="write the full result JSON here")
    ap.add_argument("--allow-substitute-embedder", action="store_true",
                    help="run with the deterministic hash embedder; results are "
                         "stamped INVALID FOR CLAIMS (plumbing check only)")
    ap.add_argument("--allow-production", action="store_true",
                    help="explicitly allow the production host as target")
    ap.add_argument("--ignore-mix-declaration", action="store_true",
                    help="emit per-class recall despite the mix-declaration mismatch")
    args = ap.parse_args(argv)
    try:
        return measure(args)
    except SystemExit:
        raise
    except Exception as exc:  # a crash is a crash — never a silent number
        print(f"REFUSED: measurement failed before producing a result: "
              f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
