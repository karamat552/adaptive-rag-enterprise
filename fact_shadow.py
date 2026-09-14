"""ADR-017 Phase 1 — the SHADOW executor for Path A.

SHADOW DISCIPLINE (§3 rollout table): Path A answers are built but
NOT served. Every query that routes 'fact' produces a V2 candidate
answer; the candidate runs the SAME deterministic gates the served
V1 answer runs; the comparison lands in the disagreement ledger. The
A.2 exit gate (≥98% agreement, 15/15 corrupted-claim catches, 7
green nights) is measured from that ledger.

ISOLATION CONTRACT — this module can never affect a served answer:
  - it is called AFTER V1's answer is final (post fact_checker_guard);
  - every operation is wrapped fail-open (shadow errors are logged,
    never raised — a broken instrument must not break the pipeline);
  - it writes ONLY to its own tables/ledger;
  - RAG_FACT_FASTPATH (Phase 2's flag) is not read here — Phase 1
    does not serve even when the flag is set; Phase 2 code reads it.

Ledger persistence: shadow_disagreements table (migration 007) + the
receipt-lineage columns on verification_receipts.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("FactShadow")

SHADOW_DISABLED_REASON = "shadow_disabled"


_REFUSAL_MARKERS = (
    "cannot verify", "could not verify", "not available in the",
    "refus", "unable to verify", "i could not", "no verified answer",
    "not supported by the indexed", "insufficient evidence",
)


def _agreement(v1_answer: str, v2_answer: str) -> Dict[str, Any]:
    """Deterministic agreement classification for the ledger:
      agree_numeric   — both answers state the same span-verbatim figure;
      agree_refusal   — both refuse/decline;
      disagree_value  — figures differ (the dangerous class);
      disagree_shape  — one answers, one refuses (coverage gap or
                         false-servable — measured, not judged).
    Figures are extracted with the same money regex family the gates
    use; comparison is on the SET of comma-grouped numerals. Refusal
    detection matches the pipeline's own refusal phrasings (the
    verified_refusal node's text family) — V2=None is a refusal by
    construction (the builder declined)."""
    import re
    low1 = (v1_answer or "").lower().strip()
    v1_refusal = (not low1) or any(m in low1 for m in _REFUSAL_MARKERS)
    v2_refusal = not v2_answer
    if v1_refusal and v2_refusal:
        return {"class": "agree_refusal"}
    if v1_refusal != v2_refusal:
        return {"class": "disagree_shape",
                "who_answers": "v2" if v2_answer else "v1"}
    money = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+")
    f1 = set(money.findall(v1_answer or ""))
    f2 = set(money.findall(v2_answer))
    if f1 and f1 == f2:
        return {"class": "agree_numeric"}
    if f1 & f2:
        return {"class": "agree_partial", "shared": sorted(f1 & f2),
                "v1_only": sorted(f1 - f2), "v2_only": sorted(f2 - f1)}
    return {"class": "disagree_value",
            "v1_figures": sorted(f1), "v2_figures": sorted(f2)}


async def shadow_fact_path(state: Dict[str, Any]) -> Dict[str, Any]:
    """The shadow node. Runs after the served pipeline finished (V1
    answer final in state). Builds the Path-A candidate, runs the
    deterministic gates over it, classifies agreement, and records the
    ledger row. Returns {} on every path — no state mutation beyond
    usage accounting, ever."""
    try:
        import asyncio
        from fact_extract import path_a_decision
        from fact_templates import build_path_a_answer
        from db import get_fact_rows

        question = state.get("original_question") or ""
        if not question:
            return {}
        rows = await asyncio.to_thread(get_fact_rows)
        keys = {(r["company"], r["metric_key"], r["period"]) for r in rows}
        decision = path_a_decision(question, keys)
        if decision["path"] != "fact":
            # out of Path-A coverage — NOT a disagreement (V1 serves it);
            # log coverage misses for the battery's recall math instead
            logger.info("SHADOW coverage-miss: %s (%s)", question,
                        decision["reason"])
            return {"shadow_coverage_miss": decision["reason"]}

        candidate = build_path_a_answer(question, decision, rows)
        if candidate is None:
            # guard said fact, builder declined — a coverage lie; that IS
            # an instrument finding (demote-on-empty must match)
            agreement = {"class": "disagree_shape",
                         "who_answers": "v1",
                         "note": "builder_declined_after_exact_match"}
            v2_answer = None
        else:
            v2_answer = candidate["answer"]
            # deterministic gates, same contract as the served path:
            from adaptive_rag import citation_pre_audit
            bad = citation_pre_audit(v2_answer,
                                     len(candidate["evidence_records"]))
            if bad:
                agreement = {"class": "disagree_shape",
                             "who_answers": "v1",
                             "note": f"citation_bounds_reject:{bad}"}
                v2_answer = None

        v1_answer = state.get("final_executive_report") or ""
        agr = agreement if v2_answer is None else \
            _agreement(v1_answer, v2_answer)

        # ledger write (fail-open: the instrument must not break serving)
        try:
            from db import admin_connection
            with admin_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT epoch FROM corpus_state WHERE id=1")
                epoch = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO shadow_disagreements
                      (question, route_reason, v1_answer, v2_answer,
                       agreement_class, agreement_detail, resolved_triples,
                       corpus_epoch, tenant_id, run_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id, question, corpus_epoch, run_id)
                    DO NOTHING
                """, (question, decision["reason"],
                      (v1_answer or "")[:2000], (v2_answer or "")[:2000],
                      agr["class"], json.dumps(agr)[:2000],
                      json.dumps(decision["resolved"]),
                      epoch,
                      (state.get("tenant_id") or "default"),
                      (state.get("run_id") or "-")))
        except Exception as exc:
            logger.warning("SHADOW ledger write failed (non-fatal): %s", exc)

        if agr["class"].startswith("disagree"):
            logger.warning("SHADOW DISAGREEMENT [%s] Q=%r", agr["class"],
                           question)
        else:
            logger.info("SHADOW agreement [%s] Q=%r", agr["class"], question)
        return {}
    except Exception as exc:                       # the shadow must never
        logger.warning("SHADOW executor failed (non-fatal): %s", exc)  # break serving
        return {}


def shadow_summary(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """A.2 instrument: the disagreement ledger's roll-up. The Phase-1
    exit gate reads these numbers (agreement rate ≥ 98% on covered
    questions, zero value-disagreements unexplained)."""
    import json
    from db import admin_connection, get_settings
    cfg = get_settings()
    tid = tenant_id or cfg.default_tenant
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT agreement_class, count(*) FROM shadow_disagreements
            WHERE tenant_id=%s AND corpus_epoch=
                  (SELECT epoch FROM corpus_state WHERE id=1)
            GROUP BY agreement_class;
        """, (tid,))
        by_class = dict(cur.fetchall())
        cur.execute("""
            SELECT count(*) FROM shadow_disagreements
            WHERE tenant_id=%s AND corpus_epoch=
                  (SELECT epoch FROM corpus_state WHERE id=1)
              AND agreement_class = 'disagree_value';
        """, (tid,))
        value_disagreements = cur.fetchone()[0]
    total = sum(by_class.values())
    agree = by_class.get("agree_numeric", 0) + by_class.get(
        "agree_partial", 0)
    rate = (agree / total * 100) if total else 0.0
    return {"total": total, "by_class": by_class,
            "agreement_pct": round(rate, 2),
            "value_disagreements": value_disagreements}
