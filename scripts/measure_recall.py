"""Span-level retrieval recall over the pre-registered battery.

WHAT IT MEASURES
    Whether retrieval actually FINDS the evidence, at the span level — one
    stage of the pipeline, and nothing else. It is NOT end-to-end recall:
    `recall@answerable` (scripts/coverage_eval.py) asks "of questions the
    corpus supports, how many produced a certified answer" and includes
    routing, models, gates, the auditor and provider weather. This script
    asks only "did the retrieved chunk physically contain the figure's
    span". The two numbers have different units, different denominators and
    different miss causes; neither supersedes the other.

DEFINITION (so the number cannot be read loosely)
    Ground truth is DERIVED FROM THE DATABASE, not hand-written: for every
    battery question declaring `expect_triples`, each (company, metric,
    period) triple is resolved to fact_rows rows span-anchored to the exact
    transcript bytes the figure lives in. Two keys are reported when
    --include-unreconciled is passed: RECONCILED-ONLY (dual-key verified)
    and ALL-SPAN-ROWS (the widened key).

    A retrieved chunk is a HIT only when it is from the same (source, page)
    AND its transcript span OVERLAPS the fact row's span. A chunk that is
    merely "about Apple revenue" but does not physically contain the number
    is a MISS. This is a STRICT measure: an end-to-end certified answer can
    rest on evidence a span-overlap check rejects (adjacent page, figure
    restated elsewhere) — so this figure is a lower bound on one stage, not
    a verdict on the system.

    recall@k    = expected evidence units found in top-k / total expected units
    precision@k = retrieved chunks on an expected page / retrieved chunks
    Missed units are PLACED (not-retrieved-at-depth / overlapping-chunk-
    exists-but-never-retrieved / page-not-in-corpus / chunking gap) — the
    placement is the diagnosis, the count is not.

READ-ONLY ENFORCEMENT (this measurement may target production)
    pgvector_hybrid_search and the ground-truth SELECTs are read-only
    (verified: db.py:1143 ff contains zero write statements). A comment is
    not a guarantee, so the guard is EXERCISED on every run: every pooled
    connection is set to `SET SESSION CHARACTERISTICS AS TRANSACTION READ
    ONLY`, then the script attempts a no-op UPDATE and REFUSES to measure
    unless Postgres rejects it. A safety check that has never fired is a
    comment, not a guard.

PREREQUISITES (each is a refusal, exit 1 — not an error to work around)
    1. The REAL embedder BAAI/bge-small-en-v1.5. Substitutes (e.g. the
       deterministic hash embedder) are refused unless
       --allow-substitute-embedder, which stamps every result
       valid_for_claims: false — a hash has no semantic geometry (measured
       once: recall 0.038, pure noise).
    2. Populated fact_rows; nothing resolvable -> nothing to measure
       (seed with scripts/local_stack_bootstrap.py, populate with
       scripts/sync_xbrl_and_verify.py).
    3. Battery self-consistency: the registered .mix_declaration
       (in_coverage_exact 14, in_coverage_alias_phrasing 8) DISAGREES with
       the actual classes (9 and 9) — independently verified twice
       (2026-10-05/06). Per-class recall is REFUSED without
       --ignore-mix-declaration; overall recall does not depend on labels.

Exit codes: 0 = measured; 1 = refused / nothing measurable.
Never quote a recall figure without its valid_for_claims flag AND its
denominator (units and contributing questions).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(REPO))   # repo root for `import db`
BATTERY_PATH = REPO / "tests" / "battery_phase1_preregistered.json"
REAL_EMBEDDER = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384  # bge-small-en-v1.5 output width; the hash substitute matches it
DEEP_K = 50       # diagnostic depth for placing missed units


def _import_db():
    try:
        import db  # noqa: PLC0415
        return db
    except Exception as exc:
        print("REFUSED: cannot import the database layer (db.py).")
        print(f"  import error: {type(exc).__name__}: {exc}")
        print("  prerequisite missing: a working local Python stack for db.py.")
        sys.exit(1)


def install_deterministic_embedder(db) -> None:
    """SHA-256-seeded stand-in with REAL_EMBEDDER's dimensions.

    Deviation from the original spec, named: scripts/_mutant_runner.py has
    no install_deterministic_embedder() (only a private _fake_embed); the
    repo wins, so the equivalent is implemented here. To a hash embedder
    'revenue' and 'net sales' are unrelated vectors — its recall is noise,
    which is exactly why results under it are stamped INVALID FOR CLAIMS."""
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
    try:
        model_name = db.get_settings().embed_model_name
    except Exception as exc:
        return False, f"cannot read embed model setting: {type(exc).__name__}: {exc}"
    if model_name != REAL_EMBEDDER:
        return False, f"configured embedder is {model_name!r}, not {REAL_EMBEDDER!r}"
    if getattr(db.embed_query, "__module__", None) != "db":
        return False, "db.embed_query has been substituted (module provenance mismatch)"
    return True, REAL_EMBEDDER


def install_read_only(db) -> None:
    """Every pooled connection becomes READ ONLY at the Postgres level;
    Postgres — not a comment — enforces it."""
    _orig = db.get_db_connection

    @contextmanager
    def _ro_conn(**kwargs):
        with _orig(**kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;")
            yield conn

    db.get_db_connection = _ro_conn  # type: ignore[method-assign]


def self_check_read_only(db) -> bool:
    """Attempt a no-op write; the measurement proceeds ONLY if Postgres
    refuses it. Exercises the guard on every single run."""
    try:
        with db.get_db_connection(tenant_id="default") as conn, conn.cursor() as cur:
            cur.execute("UPDATE corpus_state SET updated_at = updated_at WHERE id = 1;")
        return False   # the write SUCCEEDED — the guard is broken
    except Exception:
        return True    # read-only violation raised — guard working


# ---------------------------------------------------------------------------
# battery
# ---------------------------------------------------------------------------
def load_battery() -> Dict[str, Any]:
    spec = json.loads(BATTERY_PATH.read_text(encoding="utf-8"))
    if not spec.get("questions"):
        print("REFUSED: battery has no questions — nothing to measure.")
        sys.exit(1)
    return spec


def mix_check(spec: Dict[str, Any]) -> Tuple[bool, Dict[str, int], Dict[str, int]]:
    declared = {k: v for k, v in (spec.get("mix_declaration") or {}).items()
                if k != "total"}
    actual: Dict[str, int] = {}
    for q in spec["questions"]:
        actual[q["class"]] = actual.get(q["class"], 0) + 1
    return declared == actual, declared, actual


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------
def expected_evidence(db, triples_by_question: Dict[str, List[Tuple[str, str, str]]],
                      include_unreconciled: bool
                      ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """Resolve expect_triples to fact-row spans, per ground-truth key."""
    with db.get_db_connection(tenant_id="default") as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT f.company, f.metric_key, f.period, f.reconciled,
                   f.chunk_hash, f.char_start, f.char_end,
                   c.source, c.page
            FROM fact_rows f
            JOIN multi_agent_chunks c ON c.chunk_hash = f.chunk_hash;
        """)
        raw = cur.fetchall()
        cur.execute("SELECT source, page, char_start, char_end FROM multi_agent_chunks;")
        chunk_rows = cur.fetchall()

    rows = [dict(zip(("company", "metric_key", "period", "reconciled",
                      "chunk_hash", "char_start", "char_end", "source", "page"),
                     r)) for r in raw]
    stats = {"fact_rows_total": len(rows),
             "reconciled_total": sum(1 for r in rows if r["reconciled"]),
             "chunks_total": len(chunk_rows)}

    reconciled: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    all_rows: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r["company"], r["metric_key"], r["period"])
        all_rows.setdefault(key, []).append(r)
        if r["reconciled"]:
            reconciled.setdefault(key, []).append(r)

    def _units(index: Dict[Tuple[str, str, str], List[Dict[str, Any]]]
               ) -> List[Dict[str, Any]]:
        units: List[Dict[str, Any]] = []
        for qid, triples in sorted(triples_by_question.items()):
            for company, metric, period in triples:
                for r in index.get((company, metric, period), []):
                    units.append({"question_id": qid, "company": company,
                                  "metric": metric, "period": period,
                                  "source": r["source"], "page": r["page"],
                                  "char_start": r["char_start"],
                                  "char_end": r["char_end"]})
        return units

    keys = {"reconciled_only": _units(reconciled)}
    if include_unreconciled:
        keys["all_span_rows"] = _units(all_rows)
    return keys, stats


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    """Half-open span overlap: [start, end). Both spans are transcript-relative."""
    return a[0] < b[1] and b[0] < a[1]


def _place_missed_unit(unit: Dict[str, Any],
                        deep_chunks: List[Dict[str, Any]],
                        chunk_rows: List[Tuple]) -> str:
    """Diagnose WHY a unit missed. The placement is the diagnosis."""
    src, page = unit["source"], unit["page"]
    page_rows = [(cs, ce) for s, p, cs, ce in chunk_rows if (s, p) == (src, page)]
    if not page_rows:
        return "page-not-in-corpus"
    if not any(_overlaps((unit["char_start"], unit["char_end"]), (cs, ce))
               for cs, ce in page_rows):
        return "no-chunk-overlaps-the-unit-span (chunking gap on the page)"
    if any(_overlaps((unit["char_start"], unit["char_end"]),
                     (int(c["char_start"]), int(c["char_end"])))
           for c in deep_chunks
           if c.get("source") == src and c.get("page") == page):
        return "retrieved-deeper-than-max-k (overlap exists beyond tested k)"
    return "overlapping-chunk-exists-but-never-retrieved-at-depth-50 (ranking)"


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def measure(args: argparse.Namespace) -> int:
    db = _import_db()
    spec = load_battery()
    k_values = sorted({int(k) for k in args.k.split(",") if k.strip()})
    if not k_values:
        print("REFUSED: no valid k values.")
        return 1

    # --- refusal 1: embedder ---------------------------------------------
    # Two distinct cases, previously conflated (caught 2026-10-06 by the
    # owner's deliberate-substitute test):
    #   (a) a substitute is DETECTED without permission -> REFUSE;
    #   (b) --allow-substitute-embedder is PASSED -> it INSTALLS the
    #       deterministic hash embedder (the spec's wording) and stamps
    #       every result INVALID FOR CLAIMS. Passing the flag with the real
    #       embedder present must NOT silently produce a valid-stamped
    #       figure — that is the dangerous output this guard exists for.
    valid_for_claims = True
    if args.allow_substitute_embedder:
        install_deterministic_embedder(db)
        valid_for_claims = False
        print("INVALID FOR CLAIMS, plumbing check only — deterministic hash")
        print("  embedder INSTALLED by --allow-substitute-embedder. Every result")
        print("  below is stamped valid_for_claims: false. Do not quote it.")
    else:
        real, why = check_embedder_is_real(db)
        if not real:
            print("REFUSED: the configured embedder cannot support a claims-grade")
            print(f"  recall figure ({why}).")
            print("  Pass --allow-substitute-embedder for a plumbing-only run")
            print("  (every result is stamped valid_for_claims: false).")
            return 1

    # --- read-only enforcement (this script MAY target production) ----------
    install_read_only(db)
    if not self_check_read_only(db):
        print("REFUSED: the read-only guard failed its self-check — a no-op")
        print("  UPDATE was NOT rejected by Postgres. A read-only measurement")
        print("  that can write is not read-only. Not measuring.")
        return 1
    print("read-only guard: SELF-CHECKED (no-op UPDATE rejected by Postgres)")

    # --- refusal 2: no expected evidence -----------------------------------
    triples_by_question: Dict[str, List[Tuple[str, str, str]]] = {}
    for q in spec["questions"]:
        t = q.get("expect_triples") or []
        if t:
            triples_by_question[q["id"]] = [tuple(x) for x in t]
    unit_keys, stats = expected_evidence(db, triples_by_question,
                                         include_unreconciled=args.include_unreconciled)
    if not unit_keys["reconciled_only"]:
        print("REFUSED: the expected-evidence set is empty — there is nothing")
        print(f"  to measure. fact_rows={stats['fact_rows_total']}, "
              f"reconciled={stats['reconciled_total']}.")
        print("  prerequisite missing: reconciled facts (seed a stack with")
        print("  scripts/local_stack_bootstrap.py, then populate with")
        print("  scripts/sync_xbrl_and_verify.py).")
        return 1

    # --- refusal 3: mix-declaration mismatch (per-class only) ---------------
    mix_ok, declared, actual = mix_check(spec)
    per_class_allowed = mix_ok or args.ignore_mix_declaration
    per_class_reason = None
    if not mix_ok:
        print("MIX-DECLARATION MISMATCH — per-class recall REFUSED:")
        for cls in sorted(set(declared) | set(actual)):
            print(f"  {cls:36s} declared={declared.get(cls, 0):2d} "
                  f"actual={actual.get(cls, 0):2d}")
        if args.ignore_mix_declaration:
            print("  --ignore-mix-declaration passed: per-class computed against")
            print("  labels that may have moved after registration. Do not")
            print("  quote them as registered-mix results.")
        else:
            print("  Either five questions were re-labelled (invariant violated)")
            print("  or the declaration was never updated (benign) — this script")
            print("  does not assert which. Overall recall is unaffected.")
            per_class_reason = ("mix_declaration != actual class counts; "
                                "pass --ignore-mix-declaration to override")

    # --- header block --------------------------------------------------------
    target = db.get_settings().database_url or ""
    masked = target.split("@")[-1] if "@" in target else "n/a"
    print(f"\nMEASURING: span-level retrieval recall (ONE stage; recall@answerable")
    print( "  in coverage_eval.py is a different metric and is not superseded)")
    print(f"  battery      : {BATTERY_PATH.name} ({len(spec['questions'])} questions, "
          f"{len(triples_by_question)} declaring triples)")
    print(f"  db target    : {masked} (read-only enforced + self-checked)")
    print(f"  embedder     : {'REAL' if valid_for_claims else 'SUBSTITUTE (hash)'}")
    for key_name, key_units in unit_keys.items():
        print(f"  key [{key_name}]: {len(key_units)} units across "
              f"{len({u['question_id'] for u in key_units})} questions "
              f"(fact_rows={stats['fact_rows_total']}, "
              f"reconciled={stats['reconciled_total']})")
    print(f"  k values     : {k_values} (diagnostic depth {DEEP_K})")

    # --- retrieve once per question (deep), score every key -----------------
    questions_retrieved = 0
    questions_returned_zero_chunks: List[str] = []
    chunk_rows: List[Tuple] = []
    with db.get_db_connection(tenant_id="default") as conn:
        cur = conn.cursor()
        cur.execute("SELECT source, page, char_start, char_end FROM multi_agent_chunks;")
        chunk_rows = cur.fetchall()

    results: Dict[str, Dict[str, Any]] = {}
    for key_name, key_units in unit_keys.items():
        expected_pages = {(u["source"], u["page"]) for u in key_units}
        results[key_name] = {
            "units": key_units,
            "expected_pages": expected_pages,
            "units_found": {k: 0 for k in k_values},
            "precision_num": {k: 0 for k in k_values},
            "precision_den": {k: 0 for k in k_values},
            "per_class_found": {}, "per_class_units": {},
            "misses": [],
        }

    for q in spec["questions"]:
        qid, qclass, question_text = q["id"], q["class"], q["q"]
        deep_chunks = db.pgvector_hybrid_search(question_text, top_k=DEEP_K,
                                                 tenant_id="default")
        if deep_chunks:
            questions_retrieved += 1
        else:
            questions_returned_zero_chunks.append(qid)
        for key_name, r in results.items():
            q_units = [u for u in r["units"] if u["question_id"] == qid]
            for k in k_values:
                top = deep_chunks[:k]
                r["precision_den"][k] += len(top)
                if not top:
                    continue
                r["precision_num"][k] += sum(
                    1 for c in top
                    if (c.get("source"), c.get("page")) in r["expected_pages"])
                if not q_units:
                    continue
                found = 0
                for u in q_units:
                    if any(c.get("source") == u["source"]
                           and c.get("page") == u["page"]
                           and _overlaps((u["char_start"], u["char_end"]),
                                          (int(c["char_start"]), int(c["char_end"])))
                           for c in top):
                        found += 1
                r["units_found"][k] += found
                r["per_class_found"].setdefault(qclass, {})[k] = \
                    r["per_class_found"].get(qclass, {}).get(k, 0) + found
                r["per_class_units"].setdefault(qclass, {})[k] = \
                    r["per_class_units"].get(qclass, {}).get(k, 0) + len(q_units)
                if found < len(q_units) and k == max(k_values):
                    for u in q_units:
                        if not any(c.get("source") == u["source"]
                                   and c.get("page") == u["page"]
                                   and _overlaps((u["char_start"], u["char_end"]),
                                                  (int(c["char_start"]),
                                                   int(c["char_end"])))
                                   for c in deep_chunks[:max(k_values)]):
                            r["misses"].append({
                                "question_id": qid, "class": qclass,
                                "unit": f"{u['company']}/{u['metric']}/{u['period']}",
                                "span": f"{u['source']} p.{u['page']} "
                                        f"[{u['char_start']}:{u['char_end']}]",
                                "placement": _place_missed_unit(u, deep_chunks,
                                                                 chunk_rows)})

    # --- assemble + print ------------------------------------------------------
    keys_out: Dict[str, Any] = {}
    for key_name, r in results.items():
        total = len(r["units"])
        contributing = len({u["question_id"] for u in r["units"]})
        keys_out[key_name] = {
            "units_total": total,
            "questions_contributing": contributing,
            "overall": {str(k): {
                f"recall@{k}": round(r["units_found"][k] / total, 4) if total else None,
                f"precision@{k}": (round(r["precision_num"][k] / r["precision_den"][k], 4)
                                    if r["precision_den"][k] else None),
                "units_found": r["units_found"][k],
            } for k in k_values},
            "misses": r["misses"],
        }
        if per_class_allowed:
            per_class: Dict[str, Any] = {}
            for cls in sorted(actual):
                kk = max(k_values)
                tot = r["per_class_units"].get(cls, {}).get(kk, 0)
                hit = r["per_class_found"].get(cls, {}).get(kk, 0)
                per_class[cls] = {f"recall@{kk}": round(hit / tot, 4) if tot else None,
                                  "units": {"found": hit, "total": tot}}
            keys_out[key_name]["per_class"] = per_class

    result = {
        "definition": "span-overlap recall (ONE stage): retrieved chunk must "
                      "share (source, page) AND overlap the fact row's "
                      "transcript span. Strict lower bound on retrieval; NOT "
                      "end-to-end recall@answerable (different metric).",
        "k_values": k_values, "diagnostic_depth": DEEP_K,
        "embedder": {"expected": REAL_EMBEDDER,
                     "valid_for_claims": valid_for_claims},
        "valid_for_claims": valid_for_claims,
        "read_only_enforced": True,
        "n_questions": len(spec["questions"]),
        "n_with_triples": len(triples_by_question),
        "questions_retrieved": questions_retrieved,
        "questions_returned_zero_chunks": questions_returned_zero_chunks,
        "fact_stats": stats,
        "mix_declaration_matches": mix_ok,
        "per_class_refused_reason": per_class_reason,
        "keys": keys_out,
    }

    print("\nOVERALL (valid_for_claims: {})".format(
        "true" if valid_for_claims else "FALSE — DO NOT QUOTE"))
    for key_name, key_out in keys_out.items():
        print(f"\n  key [{key_name}]  "
              f"({key_out['units_total']} units, "
              f"{key_out['questions_contributing']} questions contributing):")
        for k in k_values:
            o = key_out["overall"][str(k)]
            print(f"    recall@{k}    = {o[f'recall@{k}']}  "
                  f"({o['units_found']}/{key_out['units_total']} units)")
            print(f"    precision@{k} = {o[f'precision@{k}']}")
    if not per_class_allowed:
        print("\n  PER-CLASS: REFUSED (mix-declaration mismatch) — see above;")
        print("  --ignore-mix-declaration to override.")
    elif "per_class" in keys_out.get("reconciled_only", {}):
        label_note = ("trusted" if mix_ok
                      else "UNVERIFIED — mix mismatch ignored; do not quote")
        print(f"\n  PER-CLASS (labels {label_note}):")
        for cls, v in sorted(keys_out["reconciled_only"]["per_class"].items()):
            kk = f"recall@{max(k_values)}"
            print(f"    {cls:36s} {kk} = {v[kk]}  "
                  f"({v['units']['found']}/{v['units']['total']} units)")
    print(f"\n  questions_retrieved={questions_retrieved}  "
          f"returned_zero_chunks={len(questions_returned_zero_chunks)}"
          + (f" {questions_returned_zero_chunks}" if questions_returned_zero_chunks else ""))
    misses = keys_out["reconciled_only"]["misses"]
    print(f"  MISSES ({len(misses)} units, reconciled key, WITH PLACEMENT):")
    for m in misses:
        print(f"    [{m['question_id']}] {m['unit']} @ {m['span']}")
        print(f"        -> {m['placement']}")

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
    ap.add_argument("--include-unreconciled", action="store_true",
                    help="also report the widened ground truth (ALL span rows)")
    ap.add_argument("--ignore-mix-declaration", action="store_true",
                    help="emit per-class recall despite the mix-declaration mismatch")
    args = ap.parse_args(argv)
    try:
        return measure(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"REFUSED: measurement failed before producing a result: "
              f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
