"""
Adaptive RAG — Grounding Evaluation & Canary Calibrator
========================================================
Offline quality gate for the frozen orchestrator (adaptive_rag.py) and the
persistence layer (db.py). Mirrors ingest.py CLI conventions (argparse, exit
codes, JSON report artifact).

Suites
------
canary  Deterministic auditor liveness proof (the "Canary Calibrator Method"):
        take a VERIFIED baseline answer for Apple's Q4-2023 Services revenue
        ($22.314B — mathematically confirmed against the 10-Q), programmatically
        tamper it (+$1M shift, +$1B shift, fabricated [99] citation, cross-
        company transplant), and re-audit each variant through the LIVE
        fact_checker_guard with the real retrieved evidence. Every tampered
        draft MUST come back grounded=False and MUST NOT enter the semantic
        cache; the untampered control MUST pass and MUST enter the cache.
        This is what proves "Empty Result Audit Pollution" stays fixed.
gold    Gold-set factual accuracy (figures verified against source filings).
        Refusals are honest skips, not failures — a lie is the only failure.
ragas   RAGAS scoring: Faithfulness, Context Precision, Answer Relevancy.
        Runs the graph cache-clean (evict-first, writes disabled), scores the
        certified answer + retrieved contexts against a reference. Uses the
        ragas library when it imports cleanly; otherwise an equivalent native
        judge (same metrics, formulas, thresholds) built on the project's own
        LLM seam. Skips only if neither engine can be constructed.

Quota discipline: the canary suite costs ~4 audit LLM calls (plus one pipeline
run only if the baseline question is not already cached); gold/ragas default to
--limit 3 questions each. Token usage is recorded in the report.

Run:  python eval.py --suite canary
      python eval.py --suite all --limit 3 --report eval_report.json
Exit: 0 = all requested suites passed, 1 = failures/skips, 2 = harness error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_TOTAL_FAILURE = 0, 1, 2

logger = logging.getLogger("RagEval")

# Heavy imports are guarded: a missing DB URL / provider key must produce an
# actionable CLI error, not an import traceback.
try:
    from adaptive_rag import (
        _build_engine,
        _format_record,
        arun_query,
        canonicalize_documents,
        detect_company_scope,
        fact_checker_guard,
        get_graph,
        get_health,
        get_stage_model,
    )
    from db import (
        check_semantic_cache,
        evict_from_semantic_cache,
        health_check,
        pgvector_hybrid_search,
    )
    _BOOTSTRAP_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover - environment-dependent
    _BOOTSTRAP_ERROR = _exc


# ============================== REPORT ARTIFACTS ===========================
@dataclass
class SuiteResult:
    suite: str
    status: str                                   # pass | fail | skip
    checks: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalReport:
    started_at: str
    provider: str = "?"
    models: Dict[str, str] = field(default_factory=dict)
    corpus_lineage: Dict[str, Any] = field(default_factory=dict)
    corpus_epoch: Optional[int] = None
    tokens_spent: int = 0
    suites: List[SuiteResult] = field(default_factory=list)
    harness_error: Optional[str] = None
    duration_s: float = 0.0

    def write(self, path: str) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2,
                                          ensure_ascii=False, default=str),
                              encoding="utf-8")


# ============================== EVAL DATA ==================================
# Figures verified against the source filings (same provenance as
# tests/test_answer_accuracy.py — do not edit casually).
EVAL_SET: List[Tuple[str, Optional[List[str]]]] = [
    ("What were Tesla's total automotive revenues in Q4 2023?", ["21,563"]),
    ("What was Tesla's energy generation and storage revenue in Q4 2023?", ["1,438"]),
    ("What was Apple's total net sales in Q4 2023?", ["89,498"]),
    ("What was Meta's total revenue in Q4 2023?", ["40,111"]),
    ("What was Meta's advertising revenue in Q4 2023?", ["38,706", "38.7"]),
    ("What was Meta's revenue growth in Q4 2023 compared to Q4 2022?", ["24.7", "25"]),
    ("What was Apple's revenue change in Q4 2023 versus Q4 2022?", ["-0.7", "decline", "decreased"]),
    ("Compare the revenue growth and net income performance between Apple and Meta in Q4 2023.",
     ["40,111", "89,498"]),
    ("What was Tesla's dividend per share in Q4 2023?", None),   # honest-gap probe
]

# RAGAS reference answers are built ONLY from gold-verified figures.
RAGAS_SET: List[Tuple[str, str]] = [
    ("What was Apple's total net sales in Q4 2023?",
     "Apple's total net sales in Q4 FY2023 were $89,498 million, comprised of "
     "Products revenue of $67,684 million and Services revenue of $22,314 million."),
    ("What were Tesla's total automotive revenues in Q4 2023?",
     "Tesla's total automotive revenues in Q4 2023 were $21,563 million; energy "
     "generation and storage contributed $1,438 million."),
    ("What was Meta's total revenue in Q4 2023?",
     "Meta's total revenue in Q4 2023 was $40,111 million, of which advertising "
     "revenue was $38,706 million."),
]

CANARY_QUESTION = "What were Apple's Products revenue versus Services revenue in Q4 2023?"
CANARY_FIGURE = ("22,314", "22.314")     # Apple Q4-2023 Services revenue ($B / $M-forms)
CANARY_PLUS_1M = ("22", "314", "315")    # int-part, dec-part, tampered dec-part (+$1M)
CANARY_PLUS_1B = ("22", "314", "23")     # int-part shifted +$1B, dec-part preserved

RAGAS_THRESHOLDS = {"faithfulness": 0.70, "context_precision": 0.60,
                    "answer_relevancy": 0.60}


# ============================== HELPERS ====================================
def _contains_any(text: str, needles: Tuple[str, ...]) -> Optional[str]:
    for n in needles:
        if n in text:
            return n
    return None


def _shift_figure(text: str, int_part: str, dec_part: str,
                  new_int: str, new_dec: str) -> Tuple[str, int]:
    """Shift every `int_part<sep>dec_part` figure (sep = '.' or ','). Returns
    (tampered text, replacements made)."""
    pat = re.compile(rf"(?<!\d){int_part}([.,]){dec_part}(?!\d)")
    count = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal count
        count += 1
        return f"{new_int}{m.group(1)}{new_dec}"

    return pat.sub(_sub, text), count


def _tamper_variants(answer: str) -> List[Dict[str, Any]]:
    """Programmatic tampering of the VERIFIED baseline. Each variant must be
    caught by the live fact_checker_guard."""
    variants: List[Dict[str, Any]] = []

    t1, n1 = _shift_figure(answer, *CANARY_PLUS_1M[:2], CANARY_PLUS_1M[0], CANARY_PLUS_1M[2])
    variants.append({"name": "plus_1m", "text": t1,
                     "detail": f"+$1M shift on the verified figure ({n1} replacements)"})
    t2, n2 = _shift_figure(answer, CANARY_PLUS_1B[0], CANARY_PLUS_1B[1],
                           CANARY_PLUS_1B[2], CANARY_PLUS_1B[1])
    variants.append({"name": "plus_1b", "text": t2,
                     "detail": f"+$1B shift on the verified figure ({n2} replacements)"})
    variants.append({
        "name": "fake_citation",
        "text": answer + ("\n\nAdditionally, Apple's Services gross margin expanded "
                          "to 91.7% in Q4 2023, a record for the segment [99]."),
        "detail": "fabricated figure + out-of-range [99] evidence citation"})
    variants.append({
        "name": "cross_company",
        "text": re.sub(r"\bApple\b", "Tesla", answer),
        "detail": "verified Apple figures transplanted onto Tesla"})
    return variants


async def _retrieve_docs(question: str, company_filter: str) -> List[str]:
    rows = await asyncio.to_thread(pgvector_hybrid_search, question,
                                   company_filter=company_filter, top_k=20)
    return [_format_record(r) for r in canonicalize_documents(rows)]


def _canary_state(question: str, docs: List[str], reports: Dict[str, Any],
                  draft: str, tenant: str) -> Dict[str, Any]:
    """Minimal MultiAgentState slice accepted by fact_checker_guard."""
    return {
        "original_question": question,
        "search_query": question,
        "documents": docs,
        "financial_report": reports.get("financial_report"),
        "risk_report": reports.get("risk_report"),
        "product_report": reports.get("product_report"),
        "final_executive_report": draft,
        "degraded_agents": [],
        "retry_count": 0,
        "run_id": f"canary-{uuid.uuid4().hex[:8]}",
        "tenant_id": tenant,
        "usage_in": 0, "usage_out": 0, "usage_total": 0, "llm_calls": 0,
    }


# ============================== SUITE: CANARY ==============================
async def _suite_canary(report: EvalReport) -> SuiteResult:
    res = SuiteResult(suite="canary", status="pass")
    tenant = f"eval_canary_{uuid.uuid4().hex[:8]}"   # isolated cache scope (RLS)
    filters = detect_company_scope(CANARY_QUESTION)
    try:
        # -- 1. Baseline: prefer the production cache (a VERIFIED answer); fall
        #          back to one live run (which re-certifies and warms the cache).
        cached = await asyncio.to_thread(check_semantic_cache, CANARY_QUESTION,
                                         filters=filters, tenant_id=None)
        if cached:
            baseline, reports, from_cache = cached.get("answer", ""), dict(cached), True
        else:
            final = await get_graph().ainvoke(
                {"original_question": CANARY_QUESTION, "retry_count": 0,
                 "run_id": f"canarybase-{uuid.uuid4().hex[:8]}",
                 "tenant_id": "default"},
                config={"recursion_limit": 25})
            baseline = final.get("final_executive_report", "")
            reports = {k: final.get(k) for k in
                       ("financial_report", "risk_report", "product_report")}
            from_cache = False
            report.tokens_spent += int(final.get("usage_total", 0))
            if not final.get("grounded"):
                res.checks.append({"check": "baseline_certified", "pass": False,
                                   "detail": f"live baseline run was not grounded "
                                             f"(outcome={final.get('outcome')})"})
                res.status = "fail"
                return res

        needle = _contains_any(baseline, CANARY_FIGURE)
        res.summary["baseline_source"] = "semantic_cache" if from_cache else "live_run"
        if not needle:
            res.checks.append({
                "check": "baseline_figure_present", "pass": False,
                "detail": f"verified figure {'/'.join(CANARY_FIGURE)} not found in "
                          f"baseline answer — cannot calibrate. First 300 chars: "
                          f"{baseline[:300]}"})
            res.status = "fail"
            return res
        res.checks.append({"check": "baseline_figure_present", "pass": True,
                           "detail": f"found '{needle}' in verified baseline "
                                     f"({res.summary['baseline_source']})"})

        docs = await _retrieve_docs(CANARY_QUESTION, company_filter="apple")
        if not docs:
            res.checks.append({"check": "evidence_retrieved", "pass": False,
                               "detail": "hybrid search returned zero chunks"})
            res.status = "fail"
            return res
        res.checks.append({"check": "evidence_retrieved", "pass": True,
                           "detail": f"{len(docs)} real evidence chunks for re-audit"})

        # -- 2. Tampered variants: auditor MUST reject, cache MUST stay clean.
        for var in _tamper_variants(baseline):
            upd = await fact_checker_guard(
                _canary_state(CANARY_QUESTION, docs, reports, var["text"], tenant))
            report.tokens_spent += int(upd.get("usage_total", 0))
            rejected = upd.get("grounded") is False and \
                upd.get("outcome") == "unverified_system"
            leaked = await asyncio.to_thread(check_semantic_cache, CANARY_QUESTION,
                                             filters=filters, tenant_id=tenant)
            poison_blocked = leaked is None
            res.checks.append({
                "check": f"tamper:{var['name']}", "pass": bool(rejected and poison_blocked),
                "detail": f"{var['detail']} | grounded={upd.get('grounded')} "
                          f"outcome={upd.get('outcome')} "
                          f"cache_poison_blocked={poison_blocked}"})
            if not (rejected and poison_blocked):
                res.status = "fail"
            if not poison_blocked:
                # a hallucinated acceptance wrote poison — purge before control
                await asyncio.to_thread(evict_from_semantic_cache,
                                        CANARY_QUESTION, tenant)

        # -- 3. Control: the untampered baseline MUST pass and MUST be cached.
        upd = await fact_checker_guard(
            _canary_state(CANARY_QUESTION, docs, reports, baseline, tenant))
        report.tokens_spent += int(upd.get("usage_total", 0))
        control_ok = upd.get("grounded") is True
        hit = await asyncio.to_thread(check_semantic_cache, CANARY_QUESTION,
                                      filters=filters, tenant_id=tenant)
        hit_ok = bool(hit) and _contains_any((hit or {}).get("answer", ""),
                                             CANARY_FIGURE) is not None
        res.checks.append({"check": "control_certified", "pass": bool(control_ok),
                           "detail": f"clean draft grounded={upd.get('grounded')} "
                                     f"(a False here means the auditor itself "
                                     f"is dead/miscalibrated)"})
        res.checks.append({"check": "control_cache_write", "pass": bool(hit_ok),
                           "detail": f"verified answer entered isolated cache scope: "
                                     f"{hit_ok}"})
        if not (control_ok and hit_ok):
            res.status = "fail"

        res.summary["auditor_alive"] = control_ok
        res.summary["poison_blocked_all_variants"] = res.status == "pass"
        return res
    finally:
        # Never leave canary rows behind, in any outcome.
        try:
            await asyncio.to_thread(evict_from_semantic_cache, CANARY_QUESTION, tenant)
        except Exception as exc:
            logger.warning("Canary tenant cleanup failed: %s", exc)


# ============================== SUITE: GOLD ================================
async def _suite_gold(report: EvalReport, limit: int) -> SuiteResult:
    res = SuiteResult(suite="gold", status="pass")
    passed = failed = skipped = 0
    for question, gold in EVAL_SET[:limit]:
        r = await arun_query(question)
        report.tokens_spent += int(r.get("usage", {}).get("total", 0))
        outcome = r.get("outcome", "unknown")
        if gold is None:   # honest-gap probe: any sane outcome is a pass
            ok = outcome in ("vectorstore", "verified_refusal", "unverified_system")
            detail = f"honest-gap probe outcome={outcome}"
        elif outcome in ("verified_refusal", "unverified_system"):
            ok, detail = True, f"system honestly refused (skip): outcome={outcome}"
            skipped += 1
        else:
            missing = [g for g in gold if g not in r.get("answer", "")]
            ok = outcome == "vectorstore" and not missing
            detail = (f"outcome={outcome} latency={r.get('latency_s')}s "
                      f"missing={missing or 'none'}")
        res.checks.append({"check": question[:70], "pass": ok, "detail": detail})
        passed, failed = passed + (1 if ok else 0), failed + (0 if ok else 1)
        if not ok:
            res.status = "fail"
    res.summary = {"passed": passed, "failed": failed, "honest_skips": skipped}
    return res


# ============================== NATIVE RAGAS JUDGE =========================
# The ragas PyPI package is incompatible with the frozen serving stack:
#   * 0.4.x pins openai<2 (conflicts with openai 3.3.1 / langchain-openai 1.6)
#   * 0.3.x imports langchain_community.chat_models.vertexai (removed in
#     langchain-community 1.x)
# The three RAGAS metrics are therefore also implemented natively with the
# project's judge-LLM seam (same executive model the ground-truth auditor uses)
# and the persistence layer's bge-small embedder — same methodology, formulas,
# and thresholds. The library path is still attempted first and used when it
# imports cleanly.
#
#   faithfulness       = supported_atomic_claims / total_atomic_claims
#   context_precision  = mean(precision@k over relevant chunks)  (AP, ragas-style)
#   answer_relevancy   = mean cosine(question, questions regenerated from answer)


class _Claims(BaseModel):
    claims: List[str] = Field(
        description="Every atomic factual claim (numbers, dates, comparisons) "
                    "stated in the report, one string each")


class _Verdicts(BaseModel):
    supported: List[bool] = Field(
        description="One verdict per claim in the SAME order: True only if the "
                    "claim is strictly supported by the evidence")


class _Relevance(BaseModel):
    relevant: List[bool] = Field(
        description="One verdict per evidence chunk in the SAME order: True if "
                    "the chunk contains information needed to answer the question")


class _QuestionGen(BaseModel):
    questions: List[str] = Field(
        description="Exactly 3 distinct questions this text fully answers")


_CLAIM_SYS = ("Decompose the report into atomic factual claims. Each claim must "
              "be independently verifiable (a number, date, comparison, or fact). "
              "No commentary, no duplicates.")
_VERDICT_SYS = ("You are a strict SEC Compliance Auditor. For each numbered claim, "
                "decide if it is strictly supported by the evidence excerpts. "
                "Paraphrase and unit-conversion count as supported; invention does not.")
_RELEVANCE_SYS = ("For each numbered evidence chunk, decide whether it contains "
                  "information needed to answer the question. Ignore redundancy.")
_QGEN_SYS = ("Read the text and write exactly 3 distinct, specific questions it "
             "fully answers. No meta-commentary.")


def _build_native_judges() -> Tuple[Any, Any, Any, Any]:
    base = _build_engine(get_stage_model("executive"))
    return (base.with_structured_output(_Claims),
            base.with_structured_output(_Verdicts),
            base.with_structured_output(_Relevance),
            base.with_structured_output(_QuestionGen))


def _cosine(a: List[float], b: List[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return max(0.0, min(1.0, num / (na * nb)))


async def _native_faithfulness(j_claims: Any, j_verdicts: Any,
                               answer: str, contexts: List[str]) -> float:
    if not answer.strip():
        return 0.0
    claims = (await j_claims.ainvoke(
        [("system", _CLAIM_SYS), ("human", answer)])).claims
    clist = [c.strip() for c in claims if c.strip()][:12]   # bound the audit cost
    if not clist:
        return 1.0
    ctx = "\n---\n".join(f"<evidence>\n{c}\n</evidence>" for c in contexts)
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(clist, 1))
    verdicts = (await j_verdicts.ainvoke(
        [("system", _VERDICT_SYS),
         ("human", f"[EVIDENCE]\n{ctx}\n\n[CLAIMS]\n{numbered}")])).supported
    ok = [v for v in verdicts[:len(clist)] if v]
    return len(ok) / len(clist)


async def _native_context_precision(j_rel: Any, question: str,
                                    contexts: List[str]) -> float:
    if not contexts:
        return 0.0
    numbered = "\n".join(f"[{i}] {c[:400]}" for i, c in enumerate(contexts, 1))
    verdicts = (await j_rel.ainvoke(
        [("system", _RELEVANCE_SYS),
         ("human", f"[QUESTION]\n{question}\n\n[EVIDENCE CHUNKS]\n{numbered}")])).relevant
    v = list(verdicts)[:len(contexts)]
    relevant = sum(1 for x in v if x)
    if not relevant:
        return 0.0
    hits, ap = 0, 0.0
    for k, vk in enumerate(v, 1):
        if vk:
            hits += 1
            ap += hits / k          # precision@k, averaged over relevant items
    return ap / relevant


async def _native_answer_relevancy(j_qgen: Any, question: str,
                                   answer: str, embed_fn) -> float:
    if not answer.strip():
        return 0.0
    qs = (await j_qgen.ainvoke(
        [("system", _QGEN_SYS), ("human", answer[:2500])])).questions[:3]
    if not qs:
        return 0.0
    q_emb = await asyncio.to_thread(embed_fn, question)
    sims = []
    for gq in qs:
        g_emb = await asyncio.to_thread(embed_fn, gq)
        sims.append(_cosine(g_emb, q_emb))
    return sum(sims) / len(sims)


async def _native_ragas_scores(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    from db import embed_query
    j_claims, j_verdicts, j_rel, j_qgen = _build_native_judges()
    per_question = []
    for rec in records:
        f = await _native_faithfulness(j_claims, j_verdicts,
                                       rec["answer"], rec["contexts"])
        cp = await _native_context_precision(j_rel, rec["question"], rec["contexts"])
        ar = await _native_answer_relevancy(j_qgen, rec["question"],
                                            rec["answer"], embed_query)
        per_question.append({"question": rec["question"][:70],
                             "faithfulness": round(f, 3),
                             "context_precision": round(cp, 3),
                             "answer_relevancy": round(ar, 3)})
    means = {}
    if per_question:
        for m in ("faithfulness", "context_precision", "answer_relevancy"):
            means[m] = round(sum(p[m] for p in per_question) / len(per_question), 3)
    return {"per_question": per_question, "means": means}


# ============================== SUITE: RAGAS ===============================
def _build_ragas_dependencies() -> Tuple[Any, Any, Any, List[Any]]:
    """Import ragas + wrap judge LLM (provider seam) and fastembed embeddings.
    Raises with an actionable message when ragas isn't installed."""
    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import answer_relevancy, context_precision, faithfulness
    except Exception as exc:
        raise RuntimeError(f"ragas/datasets unavailable ({exc}) — "
                           f"pip install ragas datasets") from exc
    try:
        from ragas.llms import LangchainLLM
    except ImportError:
        from ragas.llms.base import LangchainLLM  # type: ignore[no-redef]
    try:
        from ragas.embeddings import LangchainEmbeddings
    except ImportError:
        from ragas.embeddings.base import LangchainEmbeddings  # type: ignore[no-redef]

    from langchain_core.embeddings import Embeddings as LcEmbeddings
    from db import embed_passages, embed_query

    class _Fastembed(LcEmbeddings):
        """bge-small-en-v1.5 — the same embedder the persistence layer uses."""

        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            return [list(map(float, v)) for v in embed_passages(list(texts))]

        def embed_query(self, text: str) -> List[float]:
            return list(map(float, embed_query(text)))

    judge = LangchainLLM(_build_engine(get_stage_model("executive")))
    embeddings = LangchainEmbeddings(_Fastembed())
    return evaluate, judge, embeddings, [faithfulness, context_precision,
                                         answer_relevancy]


def _ragas_evaluate_sync(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Runs in a worker thread (ragas needs to own its event loop)."""
    evaluate, judge, embeddings, metrics = _build_ragas_dependencies()
    from datasets import Dataset
    ds = Dataset.from_list(records)
    result = evaluate(ds, metrics=metrics, llm=judge, embeddings=embeddings)
    df = result.to_pandas()
    per_question = []
    for _, row in df.iterrows():
        entry = {"question": str(row.get("question", ""))[:70]}
        for m in metrics:
            v = row.get(m.name)
            try:
                entry[m.name] = None if v != v else round(float(v), 3)  # v != v => NaN
            except (TypeError, ValueError):
                entry[m.name] = None
        per_question.append(entry)
    means = {m.name: float(df[m.name].dropna().mean()) for m in metrics
             if m.name in df and df[m.name].notna().any()}
    return {"per_question": per_question, "means": means}


async def _suite_ragas(report: EvalReport, limit: int) -> SuiteResult:
    res = SuiteResult(suite="ragas", status="pass")
    engine = "ragas-library"
    try:
        _build_ragas_dependencies()
    except Exception as ragas_exc:
        try:
            _build_native_judges()
            engine = "native-judge (ragas methodology)"
            logger.warning("ragas library unavailable (%s) — using the native "
                           "judge implementation with the same metrics/formulas.",
                           ragas_exc)
        except Exception as judge_exc:
            res.status, res.summary = "skip", {"reason": str(judge_exc)}
            logger.warning("RAGAS suite skipped: %s", judge_exc)
            return res

    prev_kill = os.environ.get("RAG_DISABLE_CACHE_WRITE")
    os.environ["RAG_DISABLE_CACHE_WRITE"] = "1"   # eval runs never pollute cache
    records: List[Dict[str, Any]] = []
    try:
        for question, reference in RAGAS_SET[:limit]:
            await asyncio.to_thread(evict_from_semantic_cache, question, "default")
            final = await get_graph().ainvoke(
                {"original_question": question, "retry_count": 0,
                 "run_id": f"ragas-{uuid.uuid4().hex[:8]}", "tenant_id": "default"},
                config={"recursion_limit": 25})
            report.tokens_spent += int(final.get("usage_total", 0))
            records.append({"question": question,
                            "answer": final.get("final_executive_report", ""),
                            "contexts": final.get("documents", []),
                            "reference": reference})
        if engine == "ragas-library":
            scores = await asyncio.to_thread(_ragas_evaluate_sync, records)
        else:
            scores = await _native_ragas_scores(records)
    except Exception as exc:
        logger.exception("RAGAS evaluation failed")
        res.status, res.summary = "fail", {"reason": str(exc)}
        return res
    finally:
        if prev_kill is None:
            os.environ.pop("RAG_DISABLE_CACHE_WRITE", None)
        else:
            os.environ["RAG_DISABLE_CACHE_WRITE"] = prev_kill

    res.summary["engine"] = engine
    res.summary["means"] = scores["means"]
    res.checks.extend(scores["per_question"])
    for metric, threshold in RAGAS_THRESHOLDS.items():
        value = scores["means"].get(metric)
        ok = value is not None and value >= threshold
        res.checks.append({"check": f"threshold:{metric}", "pass": ok,
                           "detail": f"mean={value if value is None else round(value, 3)} "
                                     f">= {threshold}"})
        if not ok:
            res.status = "fail"
    return res


# ============================== ORCHESTRATION ==============================
async def _run_suites(args: argparse.Namespace, report: EvalReport) -> None:
    if args.suite in ("canary", "all"):
        logger.info("=== SUITE: canary (auditor liveness calibration) ===")
        report.suites.append(await _suite_canary(report))
    if args.suite in ("gold", "all"):
        logger.info("=== SUITE: gold (factual accuracy, limit=%d) ===", args.limit)
        report.suites.append(await _suite_gold(report, args.limit))
    if args.suite in ("ragas", "all"):
        logger.info("=== SUITE: ragas (limit=%d) ===", args.limit)
        report.suites.append(await _suite_ragas(report, args.limit))


def _collect_lineage(report: EvalReport) -> None:
    manifest = Path(__file__).resolve().parent / "corpus_chunks.manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            report.corpus_lineage = {
                "run_id": m.get("run_id"),
                "sha256": (m.get("corpus") or {}).get("sha256"),
                "chunks": (m.get("corpus") or {}).get("chunks"),
            }
        except Exception as exc:
            report.corpus_lineage = {"error": str(exc)}
    try:
        h = get_health()
        report.provider = h.get("provider", "?")
        report.models = h.get("models", {})
    except Exception as exc:
        logger.warning("Provider lineage unavailable: %s", exc)
    try:
        db_health = health_check()
        report.corpus_epoch = db_health.get("epoch")
    except Exception as exc:
        logger.warning("DB health unavailable: %s", exc)


def _log_report(report: EvalReport) -> None:
    print("\n" + "=" * 78)
    print(f"EVAL REPORT | provider={report.provider} | epoch={report.corpus_epoch} "
          f"| tokens={report.tokens_spent} | {report.duration_s:.1f}s")
    if report.corpus_lineage.get("sha256"):
        print(f"  corpus sha256={report.corpus_lineage['sha256'][:16]}...")
    for s in report.suites:
        n_pass = sum(1 for c in s.checks if c.get("pass"))
        print(f"  [{s.status.upper():4}] {s.suite:7} | {n_pass}/{len(s.checks)} checks "
              f"| {json.dumps(s.summary, ensure_ascii=False, default=str)[:100]}")
        for c in s.checks:
            if not c.get("pass"):
                print(f"          FAIL {c['check']}: {c['detail'][:160]}")
    print("=" * 78)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Adaptive RAG grounding evaluation "
                                            "harness (canary / gold / ragas)")
    p.add_argument("--suite", choices=("canary", "gold", "ragas", "all"),
                   default="all", help="which suite to run (default: all)")
    p.add_argument("--limit", type=int, default=3,
                   help="max questions for gold/ragas suites (quota control)")
    p.add_argument("--report", default="eval_report.json",
                   help="path for the JSON report artifact")
    p.add_argument("--json-logs", action="store_true",
                   help="emit structured JSON log lines on stdout")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    if args.json_logs:
        import logging as _logging
        h = _logging.StreamHandler(sys.stdout)
        h.setFormatter(_JsonLineFormatter())
        _logging.getLogger().handlers = [h]

    if _BOOTSTRAP_ERROR is not None:
        print(f"BOOTSTRAP ERROR: {_BOOTSTRAP_ERROR}\n"
              f"eval.py needs a live database (DB_DATABASE_URL) and a provider "
              f"key (GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY) in .env.",
              file=sys.stderr)
        return EXIT_TOTAL_FAILURE

    report = EvalReport(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    t0 = time.perf_counter()
    _collect_lineage(report)
    try:
        asyncio.run(_run_suites(args, report))
    except Exception as exc:
        logger.exception("Harness failure")
        report.harness_error = f"{type(exc).__name__}: {exc}"
        report.duration_s = round(time.perf_counter() - t0, 2)
        report.write(args.report)
        return EXIT_TOTAL_FAILURE
    report.duration_s = round(time.perf_counter() - t0, 2)
    report.write(args.report)
    _log_report(report)

    if any(s.status == "fail" for s in report.suites):
        return EXIT_PARTIAL_FAILURE
    if report.suites and all(s.status == "skip" for s in report.suites):
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                           "level": record.levelname, "logger": record.name,
                           "message": record.getMessage()}, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
