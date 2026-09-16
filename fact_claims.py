"""ADR-017 Phase 1 — the deterministic per-claim verifier for Path A.

A.2's corrupted-claim instrument: the battery's X01-X15 injections
simulate what a REGRESSED Path A could emit — a right figure bound to
the wrong noun / entity / period / scale / basis, or a true figure
wrapped in a wrong context. This module answers ONE question,
deterministically, zero tokens: would the fact store CERTIFY this
claim? A corrupted claim that verifies is a FALSE PASS — under A.2 it
resets the 7-green-nights clock.

This is also the seed of the Phase-2 per-claim verifier class (B.1.4):
when RAG_FACT_FASTPATH serves Path A, every receipt claim will be
verified by this predicate, so the proof carries its own provenance.

FAIL-CLOSED: any ambiguity (multi-entity claim, multiple figures,
segment metrics, unresolvable nouns) DECLINES — unjudged, never
certified. A claim verifies only when EVERY check passes:

  1. context markers  — B.1.3 vocabulary (pro-forma / as previously
     reported / restated ...): the same words that exclude chunks at
     ingest void certification at claim time. X10.
  2. basis            — a non-GAAP claim against a GAAP fact row never
     certifies (the per-share/basis collision class). X15.
  3. triple resolution — the SAME canonicalizer dictionaries the
     routing guard uses (a guard that routes and a verifier that
     certifies must never disagree about what words mean).
  4. triple in store   — reconciled rows only (B.1.1: absence of
     contradiction is never reconciliation). X03 / X09.
  5. figure binding   — the claimed figure must reconstruct from the
     triple's own row within the proven 0.5% tolerance. A figure that
     belongs to a DIFFERENT metric of the same company+period is
     metric_misbinding (Amendment 1); to the same metric of another
     company, entity_misbinding; ~1000x off, scale_mismatch; anything
     else, value_mismatch. X01/02/04/05/06/07/08/11/12/13/14.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fact_extract import (
    _CONTEXT_EXCLUSIONS,
    canonical_entities,
    canonical_metric,
    canonical_periods,
)

# Non-GAAP marker (V1's Gate-4 vocabulary, applied to claims): a basis
# claim contradicts every fact row the store holds (basis=GAAP columns).
_NON_GAAP_RE = re.compile(r"non[\s\-]?gaap", re.IGNORECASE)

# A $-figure with its own scale word: "$89,498 million", "$2.72".
# $-anchored only — a verifier declines what it cannot bound precisely.
_CLAIM_FIGURE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(million|billion|thousand)s?\b)?",
    re.IGNORECASE)
_CLAIM_SCALE_MULT = {"thousand": Decimal("1e3"),
                     "million": Decimal("1e6"),
                     "billion": Decimal("1e9")}

# The proven cross-scale tolerance (Amendment 4): $91.7B == $91,650M.
_TOLERANCE = Decimal("0.005")       # 0.5% relative
_SCALE_RATIO_BAND = (Decimal("500"), Decimal("2000"))   # ~1000x either way


def _fig_usd(m: "re.Match[str]") -> Tuple[Decimal, Optional[str]]:
    raw = Decimal(m.group(1).replace(",", ""))
    scale_word = (m.group(2) or "").lower() or None
    mult = _CLAIM_SCALE_MULT[scale_word] if scale_word else Decimal("1")
    return raw * mult, scale_word


def _within(claimed: Decimal, actual: Decimal) -> bool:
    """0.5% relative tolerance — the rounding-slack rule, not exactness:
    legitimate renderings ('$21.6 billion' for 21,563) must verify."""
    if actual == 0:
        return claimed == 0
    return abs(claimed - actual) / abs(actual) <= _TOLERANCE


def _row_usd(row: Dict[str, Any]) -> Decimal:
    v = row["value_usd"]
    return Decimal(v) if not isinstance(v, Decimal) else v


def verify_claim_against_facts(
        claim: str,
        rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic claim certification against the span-anchored fact
    store. `rows` are reconciled fact rows (get_fact_rows() default).

    Returns {"verified": bool, "reason": str, "detail": dict} — reason
    is the NAMED catch on failure; verified claims carry the triple."""
    out: Dict[str, Any] = {"verified": False, "reason": "", "detail": {}}
    if not claim or not claim.strip():
        out["reason"] = "unjudged_empty"
        return out

    # 1. context markers — B.1.3: wrong-context truth is still wrong.
    m = _CONTEXT_EXCLUSIONS.search(claim)
    if m:
        out["reason"] = "context_marker"
        out["detail"] = {"marker": m.group(0)}
        return out

    # 3a. resolution — exactly one entity; the canonicalizers are the
    # routing guard's own, so routing and verification cannot disagree.
    entities = canonical_entities(claim)
    if len(entities) != 1:
        out["reason"] = "unjudged_multi_entity" if entities \
            else "unjudged_no_entity"
        out["detail"] = {"entities": entities}
        return out
    kind, metric = canonical_metric(claim)
    if kind != "metric":
        # none / segment / ambiguous — outside Path A's certification
        # surface; DECLINE, never certify.
        out["reason"] = f"unjudged_metric_{kind}"
        out["detail"] = {"metric": metric}
        return out
    periods = canonical_periods(claim)
    if len(periods) != 1:
        out["reason"] = "unjudged_multi_period" if periods \
            else "unjudged_no_period"
        out["detail"] = {"periods": periods}
        return out

    company, period = entities[0], periods[0]
    out["triple"] = [company, metric, period]

    # 4. the triple must exist as a RECONCILED row (B.1.1 fail-closed).
    triple_rows = [r for r in rows if r["company"] == company
                   and r["metric_key"] == metric and r["period"] == period]
    if not triple_rows:
        out["reason"] = "triple_not_in_fact_store"
        out["detail"] = {"triple": out["triple"]}
        return out

    # 2. basis — a non-GAAP claim against GAAP rows is a basis corruption
    # even when the figure is right (X15: re-labeled truth is misbinding).
    if _NON_GAAP_RE.search(claim) and \
            str(triple_rows[0].get("basis", "GAAP")).upper() != "NONGAAP":
        out["reason"] = "basis_mismatch"
        out["detail"] = {"claim_basis": "non-GAAP",
                         "row_basis": triple_rows[0].get("basis", "GAAP")}
        return out

    # 5. figure binding — exactly one $-figure; multiples decline.
    figs = list(_CLAIM_FIGURE_RE.finditer(claim))
    if len(figs) != 1:
        out["reason"] = "unjudged_multi_figure" if figs \
            else "unjudged_no_figure"
        return out
    claimed_usd, scale_word = _fig_usd(figs[0])
    out["detail"]["claimed_usd"] = str(claimed_usd)

    row_usds = [_row_usd(r) for r in triple_rows]
    # duplicate spans re-state the same statement row — ANY of them may
    # match (the template rule: shortest span wins at answer time, but
    # verification is over the VALUES, which agree by construction).
    if any(_within(claimed_usd, ru) for ru in row_usds):
        out["verified"] = True
        out["reason"] = "verified"
        return out

    # misbinding forensics: does the figure belong to a DIFFERENT row?
    for r in rows:
        if r["company"] == company and r["period"] == period \
                and r["metric_key"] != metric \
                and any(_within(claimed_usd, ru)
                        for ru in [_row_usd(r)]):
            out["reason"] = "metric_misbinding"
            out["detail"].update({"matched_metric": r["metric_key"],
                                  "claim_metric": metric})
            return out
    for r in rows:
        if r["company"] != company and r["metric_key"] == metric \
                and r["period"] == period \
                and _within(claimed_usd, _row_usd(r)):
            out["reason"] = "entity_misbinding"
            out["detail"].update({"matched_company": r["company"],
                                  "claim_company": company})
            return out

    # scale class: ~1000x off in EITHER direction is the millions-vs-
    # billions lie (ADR-009's class — there is deliberately no freestanding
    # x1000 verification branch; the direction of error is recorded).
    for ru in row_usds:
        if ru != 0:
            ratio = claimed_usd / ru
            if _SCALE_RATIO_BAND[0] < abs(ratio) < _SCALE_RATIO_BAND[1] \
                    or _SCALE_RATIO_BAND[0] < abs(1 / ratio) \
                    < _SCALE_RATIO_BAND[1]:
                out["reason"] = "scale_mismatch"
                out["detail"].update({"row_usd": str(ru),
                                      "ratio": str(round(ratio, 3)),
                                      "claim_scale_word": scale_word})
                return out

    out["reason"] = "value_mismatch"
    out["detail"].update({"row_usd": str(row_usds[0]),
                          "claim_scale_word": scale_word})
    return out


def control_claims(rows: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """The self-validation controls: render one TRUE claim per distinct
    triple using the Path-A template surface itself — what Phase 2
    would actually serve must verify, or the instrument false-rejects
    (a corrupted pass is meaningless against a blanket rejector)."""
    from fact_templates import template_single_metric
    seen: set = set()
    out: List[Tuple[str, str]] = []
    # shortest-span row per triple — the same deterministic choice
    # build_path_a_answer makes (the statement's own row, not a
    # re-serialized duplicate)
    by_triple: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        t = (r["company"], r["metric_key"], r["period"])
        if t not in by_triple or (r["char_end"] - r["char_start"]) < \
                (by_triple[t]["char_end"] - by_triple[t]["char_start"]):
            by_triple[t] = r
    for t, row in sorted(by_triple.items()):
        sent = template_single_metric(row, cite=1)
        if not sent:
            continue      # a row the templates decline is not a control
        # strip the citation index — claims here carry no [n]
        claim = re.sub(r"\s*\[\d+\]\.$", ".", sent).strip()
        out.append((f"{'/'.join(t)}", claim))
    return out
