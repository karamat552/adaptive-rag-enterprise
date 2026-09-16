"""ADR-017 Phase 3 — SEGMENTED AUDIT triage (§2.4, the token slasher).

The V1 auditor cross-examines the WHOLE draft against the WHOLE evidence
pool (~4-6K tokens per call) — even though by the time the audit runs,
five deterministic gates have already vetted every figure (citation
bounds, scale reconstruction, growth direction, XBRL reconciliation,
echo/injection). The segmented audit asks the LLM to judge ONLY what the
deterministic layer cannot:

  Python certifies a claim ONLY IF all four hold (the inverted
  allow-list — anything else routes to the LLM by default):
    1. it cites at least one evidence chunk;
    2. ZERO hedge markers (approximately / roughly / nearly /
       essentially / about / almost / ...) — a hedge is a judgment;
    3. ZERO interpretive connectives (due to / driven by / reflecting /
       because ...) — a causal claim is an interpretation;
    4. EVERY figure token in the claim appears VERBATIM in the cited
       evidence — re-scaled renderings ("$22.3 billion" for a table's
       in-millions "22,314") are legitimate but NOT span-verbatim, so
       they stay with the LLM. Claims containing ANY percentage go to
       the LLM too: the growth gate owns DIRECTION, but percent
       magnitude verification is the auditor's judgment.

Everything else — the flagged clauses and ONLY their cited chunks —
forms one small-context audit call (~600-900 tokens).

B.1.4: receipts record per-claim verifier class (python_certified vs
llm_audit) — the bypass itself is auditable; a compliance reader sees
exactly which guarantee backs each sentence.

FLAG DISCIPLINE: RAG_SEGMENTED_AUDIT=1, default OFF. Phase 3 ships with
the same shadow discipline as Path A (rollout table row 3: disagreement
rate measured before the Python-bypass is trusted); enabling it mid-A.2-
gate would make the measured V1 a moving target. The nightly battery
runs flag-off.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_HEDGE_RE = re.compile(
    r"\b(approx(?:imately)?|roughly|nearly|essentially|about|almost|"
    r"around|just\s+over|just\s+under|close\s+to|nearly|somewhat|"
    r"relatively|largely|broadly|flat)\b", re.IGNORECASE)

_INTERPRETIVE_CONNECTIVE_RE = re.compile(
    r"\b(due\s+to|driven\s+by|drives|reflect(?:s|ing|ed)?|because(?:\s+of)?|"
    r"as\s+a\s+result(?:\s+of)?|attributable\s+to|owing\s+to|"
    r"stem(?:s|ming)?\s+from|fueled\s+by|helped\s+by|hurt\s+by|"
    r"aided\s+by|offset(?:ting|s|ted)?\s+by)\b", re.IGNORECASE)

# figures that must be span-verbatim: comma-grouped numerals or decimals.
# Bare integers are deliberately NOT figures here (citation indices,
# years, counts of things the gates already own); PERCENT claims are
# excluded from Python certification wholesale (magnitude = judgment).
_FIGURE_TOKEN_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+")
_PCT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")


def _figures_verbatim(claim_text: str, cited_texts: List[str]) -> bool:
    """Every comma-grouped or decimal figure in the claim must appear as
    a token in at least one cited chunk. Span-verbatim ONLY — the scale
    gate already validated reconstructability; this is the stricter
    certification bar (§2.4: 'every figure is span-verbatim')."""
    joined = "\n".join(cited_texts)
    for m in _FIGURE_TOKEN_RE.finditer(claim_text):
        tok = m.group(0)
        if not re.search(r"(?<![\d.,])" + re.escape(tok) + r"(?![\d.,])",
                         joined):
            return False
    return True


def triage_claims(claims: List[Dict[str, Any]],
                  docs: List[str]) -> Dict[str, Any]:
    """Split the draft's claims into python_certified / llm_needed with
    a named reason per routing. Pure, zero tokens, never raises on
    malformed input — an unparseable claim is ALWAYS llm_needed."""
    python_certified: List[Dict[str, Any]] = []
    llm_needed: List[Dict[str, Any]] = []
    reasons: Dict[int, str] = {}
    for i, c in enumerate(claims):
        text = c.get("claim") or ""
        cites = c.get("citations") or []
        try:
            if not text.strip():
                reasons[i] = "empty"
                llm_needed.append(c)
            elif not cites:
                reasons[i] = "uncited"            # nothing to verify against
                llm_needed.append(c)
            elif _HEDGE_RE.search(text):
                reasons[i] = "hedge_marker"
                llm_needed.append(c)
            elif _INTERPRETIVE_CONNECTIVE_RE.search(text):
                reasons[i] = "interpretive_connective"
                llm_needed.append(c)
            elif _PCT_RE.search(text):
                reasons[i] = "percent_magnitude"  # direction gate owns
                llm_needed.append(c)              # direction; % is judgment
            elif not _figures_verbatim(
                    text, [docs[n - 1] for n in cites
                           if 1 <= n <= len(docs)]):
                reasons[i] = "figure_not_span_verbatim"
                llm_needed.append(c)
            else:
                reasons[i] = "span_verbatim"
                python_certified.append(c)
        except Exception:
            reasons[i] = "triage_error"
            llm_needed.append(c)
    return {"python_certified": python_certified,
            "llm_needed": llm_needed, "reasons": reasons}


def build_small_context(triage: Dict[str, Any],
                        docs: List[str],
                        max_chunks: int = 8) -> Dict[str, str]:
    """The small-context audit payload: flagged claims + ONLY their cited
    chunks (deduped, capped). Returns the pieces the caller splices
    into the same system prompt the full audit uses."""
    cited = sorted({n for c in triage["llm_needed"]
                    for n in (c.get("citations") or [])
                    if 1 <= n <= len(docs)})
    chunks = [docs[n - 1] for n in cited[:max_chunks]]
    docs_str = "\n---\n".join(f"<evidence>\n{d}\n</evidence>"
                              for d in chunks)
    flagged = "\n".join(f"- {c['claim']}" for c in triage["llm_needed"])
    scope = ("SEGMENTED AUDIT: every claim NOT listed below has been "
             "deterministically certified (span-verbatim figures, no "
             "hedges, no interpretive connectives, citations in bounds) "
             "by the five pre-audit gates plus claim-level triage. You "
             "are auditing ONLY the flagged claims, against ONLY the "
             "evidence they cite. Return grounded=True only if 100% of "
             "the FLAGGED claims are verified.")
    return {"docs_str": docs_str, "flagged": flagged, "scope": scope}
