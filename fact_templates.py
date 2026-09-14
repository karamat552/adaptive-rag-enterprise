"""ADR-017 Phase 1 — Path A answer templates.

A.5 ruling: pure-Python f-strings in reviewed code — NO template
engine (a Jinja-class engine would be a new dependency and a new
audit surface; explicit code is auditable by the same reviewer
process that audits the gates).

THE CONSTRUCTION GUARANTEE (§2.3 of ADR-017): every figure in the
output is byte-identical to its fact_rows span. Templates receive
rows straight from get_fact_rows(reconciled_only=True) and render the
value EXACTLY as the source prints it:

  - value_raw + scale are the span-verbatim rendering ($89,498 in
    millions) — the number that appears on the page;
  - value_usd is the normalized USD figure for context sentences;
  - every sentence carrying a figure carries a [n] citation bound to
    the fact row's OWN span, so the citation-bounds gate passes BY
    CONSTRUCTION and the receipt chain is identical to V1's.

B.6.1 wording: Path-A receipts carry
`deterministic_certification: known-context-exclusions-applied` —
never language implying the mistagging class is eliminated.

Fail-closed: every builder returns None on any missing/ambiguous
input; the caller (shadow executor first, live router in Phase 2)
demotes to the fleet on None. No template ever invents a figure, a
period, or a company.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

# B.6.1 — the wording the three-way review locked for every Path-A
# receipt. "Excluded" (known categories removed), never "eliminated".
DETERMINISTIC_CERTIFICATION = "known-context-exclusions-applied"

# metric_key -> the noun phrase the answer uses. Deliberately small
# and reviewable; grows only with the coverage map.
_METRIC_NOUNS = {
    "revenue": "revenue",
    "net_income": "net income",
    "eps_diluted": "diluted earnings per share (EPS)",
}


def _span_figure(row: Dict[str, Any]) -> str:
    """The span-verbatim figure: the number EXACTLY as the source table
    prints it. Prefer re-matching the digits inside the row's own span
    text (NUMERIC columns pad: 2.2700 -> the page says 2.27); fall back
    to value_raw when no span text is available. This is the ONLY
    rendering that may appear with a citation in a Path-A answer."""
    rt = row.get("row_text")
    v = Decimal(row["value_raw"])
    if rt:
        for m in re.finditer(
                r"\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?", rt):
            try:
                if Decimal(m.group(0).strip("()").replace(",", "")) == v:
                    return m.group(0).strip("()")
            except Exception:
                continue
    q = -v.as_tuple().exponent if v.as_tuple().exponent < 0 else 0
    return f"{v:,.{q}f}"


def _unit_clause(row: Dict[str, Any]) -> str:
    """'million' / 'billion' / '' (per share) — the unit suffix that
    makes the span figure a complete quantity. Matches the source's
    own '(In millions...)' header so the scale claim is anchored to
    the same span."""
    if row["metric_key"] == "eps_diluted":
        return ""
    return f" {row['scale'][:-1]}" if row["scale"].endswith("s") \
        else f" {row['scale']}"


def _metric_phrase(row: Dict[str, Any]) -> str:
    """The noun phrase for the sentence. Revenue/net_income quote the
    row's own label (span-true: 'Total net sales' is what Apple
    prints); EPS uses the canonical phrase because the row label is
    just 'Diluted', meaningless alone."""
    if row["metric_key"] == "eps_diluted":
        return _METRIC_NOUNS["eps_diluted"]
    label = (row.get("label") or "").strip().rstrip(":").strip()
    label = re.sub(r"\s*\(GAAP\)\s*$", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s*\(1\)\s*$", "", label)
    if not label:
        return _METRIC_NOUNS.get(row["metric_key"], row["metric_key"])
    return label[0].lower() + label[1:] if label[0].isupper() else label


def template_single_metric(row: Dict[str, Any],
                           cite: int) -> Optional[str]:
    """'Apple reported total net sales of $89,498 million for Q4-2023
    [1].' — the digits are span-verbatim; the label is the row's
    own; 'reported ... of X' stays grammatical for every metric."""
    try:
        return (f"{row['company']} reported "
                f"{_metric_phrase(row)} of "
                f"${_span_figure(row)}{_unit_clause(row)} "
                f"for {row['period']} [{cite}].")
    except (KeyError, ArithmeticError, ValueError):
        return None


def template_prior_year(row: Dict[str, Any],
                        prior_row: Dict[str, Any],
                        cite: int,
                        prior_cite: int) -> Optional[str]:
    """Comparative: current period + the prior-year figure, each with
    its own citation. The % change is COMPUTED and labeled as such
    (the corpus's own '% Change' column is not stored on fact rows —
    computing and stating the basis is more honest than quoting an
    unbound number)."""
    try:
        cur = Decimal(row["value_usd"])
        prior = Decimal(prior_row["value_usd"])
        if prior == 0:
            pct = None
        else:
            pct = (cur - prior) / abs(prior) * 100
        pct_s = (f", a {abs(pct):.1f}% "
                 f"{'increase' if pct >= 0 else 'decrease'} year over year"
                 if pct is not None else "")
        return (f"{row['company']} reported "
                f"{_metric_phrase(row)} of "
                f"${_span_figure(row)}{_unit_clause(row)} "
                f"for {row['period']} [{cite}], up from "
                f"${_span_figure(prior_row)} in {prior_row['period']} "
                f"[{prior_cite}]{pct_s}.")
    except (KeyError, ArithmeticError, ValueError):
        return None


def template_multi_entity(rows: List[Dict[str, Any]],
                          period: str) -> Optional[str]:
    """Comparative across companies: one clause per company, each with
    its own citation index (assigned in the caller's evidence order).
    rows: [(row, cite), ...] — ALL must share the period and metric."""
    try:
        if not rows or len(rows) < 2:
            return None
        metric = rows[0][0]["metric_key"]
        clauses = []
        for row, cite in rows:
            if row["metric_key"] != metric or row["period"] != period:
                return None            # mixed inputs — fail closed
            clauses.append(f"{row['company']} reported "
                           f"{_metric_phrase(row)} of "
                           f"${_span_figure(row)}{_unit_clause(row)} "
                           f"[{cite}]")
        return f"For {period}: " + "; ".join(clauses) + "."
    except (KeyError, ArithmeticError, ValueError):
        return None


def build_path_a_answer(question: str,
                        decision: Dict[str, Any],
                        rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The Path-A answer builder (Amendment 2 exact-match only).

    decision: path_a_decision() output (path == 'fact', resolved
    triples). rows: the fact rows for those triples from
    get_fact_rows(reconciled_only=True) — the caller passes them in
    evidence order; citation indexes are assigned by position.

    Returns the shadow-executor contract: {answer, evidence_records,
    claims-ready fields, deterministic_certification} — or None on
    ANY ambiguity (demote to fleet; never serve a guess)."""
    try:
        resolved = decision.get("resolved") or []
        if decision.get("path") != "fact" or not resolved:
            return None
        # duplicate spans exist for the same triple (the corpus restates
        # rows across chunks); prefer the SHORTEST span deterministically
        # — the statement's own row, not a re-serialized duplicate
        by_triple: Dict[tuple, Dict[str, Any]] = {}
        for r in rows:
            t = (r["company"], r["metric_key"], r["period"])
            if t not in by_triple or \
                    (r["char_end"] - r["char_start"]) < \
                    (by_triple[t]["char_end"] - by_triple[t]["char_start"]):
                by_triple[t] = r
        if any(t not in by_triple for t in resolved):
            return None               # a triple lost between decision
                                       # and fetch — fail closed

        # group triples by (metric, period) — comparatives share shape
        groups: Dict[tuple, List[tuple]] = {}
        for (comp, metric, period) in resolved:
            groups.setdefault((metric, period), []).append(
                (comp, metric, period))

        evidence: List[Dict[str, Any]] = []
        sentences: List[str] = []
        # one evidence record per DISTINCT fact row (cited spans are
        # the row's own; duplicates across sentences share the index)
        ev_index: Dict[tuple, int] = {}

        def _ev(triple: tuple) -> int:
            if triple in ev_index:
                return ev_index[triple]
            row = by_triple[triple]
            evidence.append({
                "chunk_hash": row["chunk_hash"],
                "company": row["company"],
                "source": row["source"] if "source" in row else None,
                "page": row["page"] if "page" in row else None,
                "content": row["row_text"] if "row_text" in row else None,
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "transcript_version": None,
                "contains_table": True,
                "arithmetic_ok": None,
            })
            ev_index[triple] = len(evidence)
            return len(evidence)

        for (metric, period), triples in sorted(groups.items()):
            if len(triples) == 1:
                row = by_triple[triples[0]]
                cite = _ev(triples[0])
                # prior-year comparative if the row exists in fact_rows
                prior_period = _prior_period(period)
                prior_key = (triples[0][0], metric, prior_period) \
                    if prior_period else None
                prior_row = by_triple.get(prior_key) if prior_key else None
                if prior_row is not None:
                    sentences.append(template_prior_year(
                        row, prior_row, cite, _ev(prior_key)))
                else:
                    sentences.append(
                        template_single_metric(row, cite))
            else:
                pairs = [(by_triple[t], _ev(t)) for t in triples]
                sentences.append(template_multi_entity(pairs, period))

        sentences = [s for s in sentences if s]
        if not sentences:
            return None
        return {
            "answer": " ".join(sentences),
            "evidence_records": evidence,
            "deterministic_certification":
                DETERMINISTIC_CERTIFICATION,
            "path": "fact",
            "resolved_triples": resolved,
        }
    except (KeyError, ArithmeticError, ValueError, TypeError):
        return None


def _prior_period(period: str) -> Optional[str]:
    """Q4-2023 -> Q4-2022; FY-2023 -> FY-2022. Anything else declines
    (comparatives are year-based only)."""
    m = re.match(r"^((?:Q[1-4])|(?:FY))-(20\d{2})$", period)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)) - 1}"
