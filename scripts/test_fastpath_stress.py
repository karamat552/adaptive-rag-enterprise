"""ADR-017 Phase-2 STRESS HARNESS — the accelerated zero-token battery.

Gemini-accelerated-test suggestion, rebuilt to respect the architecture's
own physics: the deterministic layers CAN be stress-tested to hundreds of
permutations in seconds (0 tokens, cached fact rows, pure functions); the
V1-agreement leg CANNOT (it is token-bound — one battery per quota window,
which is what the nightly A.2 run is for). This harness stresses
everything that is legitimately accelerable:

  1. FACT-CLASS VARIANTS (the fastpath's serving surface): every
     in-coverage battery question x alias/casing/noise/period-rephrase
     variants -> must resolve the SAME triple, build a candidate, pass
     the citation gate, and VERIFY against the fact store.
  2. FUZZ CONTRACT (A.5): every fuzz operator (entity/metric/period
     swap, qualifier inject) must NEVER resolve to the original triple
     — resolve differently or demote, never silently re-bind.
  3. DEMOTE-CLASS VARIANTS: out-of-coverage/wrong-period/qualifier/
     interpretive questions (originals + mutations) must ALWAYS demote
     to the fleet — a single false 'fact' would serve a guess.
  4. CORRUPTED-CLAIM INJECTIONS (X01-X15): caught, with a positive
     named reason (never an unjudged decline).
  5. CONTROLS: every covered triple's template-rendered claim verifies.
  6. --e2e N: additionally runs N questions through the LIVE graph node
     (flag on in-process) — full receipts, real DB writes, 0 LLM calls.

Exit codes: 0 all green · 1 any violation. Deterministic — no RNG.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("Stress")

BATTERY = Path(__file__).resolve().parent.parent / \
    "tests" / "battery_phase1_preregistered.json"

# alias + figure machinery: fact_extract OWNS these dictionaries — the
# harness imports them so it can never drift (the first version kept a
# copy and the sync check caught it missing 'apple inc' et al.)
from fact_extract import _ENTITY_ALIASES
from fact_claims import verify_claim_against_facts

_METRIC_ALIASES = {"revenue": ["revenue", "total revenue", "total revenues",
                               "total net sales", "net sales", "top line"],
                   "net_income": ["net income", "bottom line"],
                   "eps_diluted": ["diluted eps", "earnings per share",
                                   "earn per share", "earned per share",
                                   "eps"]}
_PERIOD_VARIANTS = {"Q4-2023": ["Q4 2023", "Q4-2023", "q4 2023",
                                "fourth quarter of 2023"]}

_FIG_RE = None       # compiled lazily (Decimal-safe $-figure matcher)


def _figures_in_store(sentence: str, rows: List[Dict[str, Any]]) -> bool:
    """The weaker multi-figure fallback: every $-figure in the sentence
    must reconstruct from SOME fact row's value within the proven 0.5%
    tolerance (scale word parsed from the sentence). For comparative /
    multi-entity sentences this is still a zero-fabrication-surface
    check — no figure can appear that the store does not contain."""
    import re
    from decimal import Decimal
    mult = {"thousand": Decimal("1e3"), "million": Decimal("1e6"),
            "billion": Decimal("1e9")}
    row_usds = [Decimal(str(r["value_usd"])) for r in rows]
    for m in re.finditer(
            r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
            r"(?:\s*(million|billion|thousand)s?\b)?", sentence,
            re.IGNORECASE):
        claimed = Decimal(m.group(1).replace(",", "")) * \
            mult.get((m.group(2) or "").lower(), Decimal("1"))
        if not any(abs(claimed - ru) / abs(ru) <= Decimal("0.005")
                   for ru in row_usds if ru != 0):
            return False
    return True


def _verify_answer(answer: str, rows: List[Dict[str, Any]]) \
        -> Tuple[bool, str]:
    """Per-SENTENCE verification — the claim verifier is single-triple by
    design, so a multi-claim answer (multi-entity comparisons, prior-year
    comparatives) verifies clause by clause; multi-figure sentences fall
    back to the all-figures-in-store check."""
    import re
    # clause-level split: multi-entity answers separate companies with
    # ';' — each clause is then a single-triple claim the verifier can
    # judge. The lead-in ('For Q4-2023:') carries the shared period; a
    # clause with no period of its own inherits the lead-in's — the
    # context is in the ANSWER, just not repeated per clause.
    lead = None
    m = re.match(r"^For ((?:Q[1-4]|FY)-(?:20\d{2}))[:.]", answer)
    if m:
        lead = m.group(1)
    for sent in re.split(r"(?<=[.!?;])\s+", answer):
        sent = sent.rstrip(";").strip()
        if not sent:
            continue
        vc = verify_claim_against_facts(sent, rows)
        if vc["verified"]:
            continue
        if vc["reason"] == "unjudged_no_period" and lead:
            vc = verify_claim_against_facts(
                f"For {lead}: {sent}", rows)
            if vc["verified"]:
                continue
        if vc["reason"].startswith("unjudged_multi_figure"):
            if _figures_in_store(sent, rows):
                continue
            return False, f"figures not in store: {sent[:60]!r}"
        return False, f"{sent[:50]!r} -> {vc['reason']}"
    return True, ""


def _variants(question: str, expect_triples: List[List[str]]) -> List[str]:
    """Deterministic phrasing variants for an in-coverage question. Every
    variant must resolve to the SAME triples (alias/casing/noise must
    never change the parse)."""
    out = [question]
    low = question.lower()
    for alias, canon in _ENTITY_ALIASES.items():
        if canon.lower() in low and alias != canon.lower():
            out.append(low.replace(canon.lower(), alias))
    for m, phrases in _METRIC_ALIASES.items():
        if any(t[1] == m for t in expect_triples):
            for p in phrases:
                for cur in _METRIC_ALIASES[m]:
                    if f" {cur} " in f" {low} " and p != cur:
                        out.append(low.replace(cur, p, 1))
                        break
    for canon, forms in _PERIOD_VARIANTS.items():
        for f in forms:
            if f in low:
                for v in _PERIOD_VARIANTS[canon]:
                    if v != f:
                        out.append(low.replace(f, v, 1))
                break
    out.append(question.upper())                       # casing noise
    out.append("  " + question + "  ?? ")              # whitespace noise
    out.append("hey, quick question: " + question)     # prefix noise
    out.append(question.replace("What was", "How much was"))
    return list(dict.fromkeys(out))                    # stable dedupe


def run_matrix() -> Dict[str, Any]:
    """The zero-token stress core. One read-only fact fetch; every check
    is a pure function over the cached rows."""
    from db import get_fact_rows
    from fact_claims import (control_claims, verify_claim_against_facts)
    from fact_extract import (FUZZ_OPERATORS, canonical_metric,
                              canonical_entities, path_a_decision)
    from fact_templates import build_path_a_answer
    from adaptive_rag import citation_pre_audit

    spec = json.loads(BATTERY.read_text(encoding="utf-8"))
    rows = get_fact_rows()
    keys = {(r["company"], r["metric_key"], r["period"]) for r in rows}
    stats = {"pass": 0, "fail": 0, "checks": 0, "failures": []}

    def _check(ok: bool, label: str, detail: str = "") -> None:
        stats["checks"] += 1
        if ok:
            stats["pass"] += 1
        else:
            stats["fail"] += 1
            stats["failures"].append(f"{label}: {detail}")
            logger.error("FAIL %s %s", label, detail)

    # --- 1 + 3: every question, original + variants, against the guard
    for q in spec["questions"]:
        base_triples = {tuple(t) for t in (q.get("expect_triples") or [])}
        pool = _variants(q["q"], q.get("expect_triples") or []) \
            if q["expect_path"] == "fact" else [q["q"]]
        for v in pool:
            d = path_a_decision(v, keys)
            if q["expect_path"] == "fact":
                _check(d["path"] == "fact", "guard",
                       f"{v[:60]!r} -> {d['path']} ({d.get('reason')})")
                if d["path"] == "fact":
                    got = {tuple(t) for t in d["resolved"]}
                    _check(got == base_triples, "triples",
                           f"{v[:60]!r} -> {sorted(got)} != "
                           f"{sorted(base_triples)}")
                    cand = build_path_a_answer(v, d, rows)
                    _check(cand is not None, "builder",
                           f"{v[:60]!r} declined after exact match")
                    if cand:
                        _check(citation_pre_audit(
                            cand["answer"], len(cand["evidence_records"]))
                            is None, "citation-gate", cand["answer"][:70])
                        ok, why = _verify_answer(cand["answer"], rows)
                        _check(ok, "verify",
                               f"{v[:50]!r} -> {why}")
            else:
                _check(d["path"] == "fleet", "demote",
                       f"{v[:60]!r} -> {d['path']} ({d.get('reason')})")
                if d["path"] == "fact":
                    # a false 'fact' on a demote-class question would
                    # SERVE a guess — worst class, shout it
                    for t in d["resolved"]:
                        _check(tuple(t) not in keys, "false-serve",
                               f"{v[:60]!r} resolved uncovered {t}")

    # --- 2: the A.5 fuzz contract — resolve DIFFERENTLY or demote,
    # never IDENTICALLY to the original triple set (a multi-entity
    # swap legitimately keeps the un-swapped entity; only a resolution
    # EQUAL to the original is a re-bind).
    for q in spec["questions"]:
        if not q.get("expect_triples"):
            continue
        original = {tuple(t) for t in q["expect_triples"]}
        for op in FUZZ_OPERATORS:
            mutated = op(q["q"])
            if mutated == q["q"]:
                continue          # operator declined (no target) — fine
            d = path_a_decision(mutated, keys)
            if d["path"] == "fact":
                got = {tuple(t) for t in d["resolved"]}
                _check(got != original, "fuzz-rebind",
                       f"{op.__name__}: {mutated[:60]!r} -> {sorted(got)}")

    # --- 4 + 5: corrupted injections + template controls
    for x in spec["corrupted_claim_injections"]:
        vc = verify_claim_against_facts(x["corrupted_claim"], rows)
        caught = (not vc["verified"]) and not vc["reason"].startswith(
            "unjudged")
        _check(caught, "corrupted", f"{x['id']} verified={vc['verified']} "
               f"reason={vc['reason']}")
    for name, claim in control_claims(rows):
        vc = verify_claim_against_facts(claim, rows)
        _check(vc["verified"], "control", f"{name} -> {vc['reason']}")

    # alias table sanity: fact_extract owns the truth, import above
    _check(len(_ENTITY_ALIASES) >= 7, "alias-table",
           f"unexpected alias count {len(_ENTITY_ALIASES)}")
    return stats


async def run_e2e(n: int) -> Dict[str, Any]:
    """N live graph runs with the flag on IN-PROCESS: full node path,
    real receipts, 0 LLM calls expected."""
    import os
    os.environ["RAG_FACT_FASTPATH"] = "1"
    os.environ["RAG_DISABLE_CACHE_READ"] = "1"
    spec = json.loads(BATTERY.read_text(encoding="utf-8"))
    covered = [q for q in spec["questions"] if q["expect_path"] == "fact"]
    from adaptive_rag import arun_query
    out = {"runs": 0, "zero_token": 0, "served": 0, "total_tokens": 0,
           "latencies": [], "failures": []}
    for q in covered[:n]:
        r = await arun_query(q["q"])
        out["runs"] += 1
        toks = sum(u.get("input", 0) + u.get("output", 0)
                   for u in (r.get("per_model") or {}).values())
        out["total_tokens"] += toks
        out["latencies"].append(r.get("latency_s") or 0)
        if toks == 0:
            out["zero_token"] += 1
        if r.get("outcome") == "vectorstore" and r.get("grounded"):
            out["served"] += 1
        else:
            out["failures"].append(f"{q['id']}: outcome={r.get('outcome')}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="fastpath stress harness")
    ap.add_argument("--e2e", type=int, default=0,
                    help="also run N live node runs (flag on, real receipts)")
    ns = ap.parse_args()
    t0 = time.perf_counter()
    stats = run_matrix()
    dt = time.perf_counter() - t0
    print(f"\n{'=' * 66}")
    print(f"FASTPATH STRESS — {stats['checks']} checks in {dt:.1f}s "
          f"(0 LLM tokens)")
    print(f"  PASS {stats['pass']} · FAIL {stats['fail']}")
    for f in stats["failures"][:15]:
        print(f"  ✗ {f}")
    if ns.e2e:
        e2e = asyncio.run(run_e2e(ns.e2e))
        print(f"\nE2E LIVE NODE ({e2e['runs']} runs, flag on): "
              f"served={e2e['served']} zero_token={e2e['zero_token']} "
              f"total_tokens={e2e['total_tokens']} "
              f"latencies={e2e['latencies']}")
        for f in e2e["failures"]:
            print(f"  ✗ {f}")
        stats["fail"] += len(e2e["failures"])
    print(f"{'=' * 66}")
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
