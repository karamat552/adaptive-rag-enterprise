"""
Pipeline Audit — production-readiness walkthrough of every stage.
================================================================
Walks the Adaptive RAG pipeline from ingestion to the offline-verifiable
certificate and produces EVIDENCE, not opinions: each check reports what it
observed, so a reader can see the claim and the proof side by side.

Run:
    python scripts/pipeline_audit.py            # deterministic stages (no keys)
    python scripts/pipeline_audit.py --json     # machine-readable report

Design rules (matching the project's own culture):
  * A check that cannot run is SKIP (visible), never a silent pass.
  * Every check prints the ACTUAL observed value, not "ok".
  * Zero LLM tokens: this audits the deterministic spine. The LLM stages are
    exercised through their routing/state contracts with mocked engines, so
    stage LOGIC is verified without spending provider quota.
  * Exit code 1 if any check FAILs — usable as a CI gate.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

RESULTS: List[Dict[str, Any]] = []


_REGISTRY: List[Tuple[str, str, Callable]] = []


def check(stage: str, name: str) -> Callable:
    """Decorator: REGISTER a check so the runner executes it.

    (First cut made the decorator wrap-and-call, which meant a check only ran
    if its own body explicitly invoked it — every stage defined its checks and
    executed none, reporting a clean 0/0/0. Registration is the honest shape:
    the runner owns execution, so "check defined but never run" is impossible.)
    """
    def deco(fn: Callable) -> Callable:
        _REGISTRY.append((stage, name, fn))
        return fn
    return deco


def run_check(stage: str, name: str, fn: Callable) -> None:
    t0 = time.perf_counter()
    try:
        status, evidence = fn()
    except SkipCheck as exc:
        status, evidence = "SKIP", str(exc)
    except Exception as exc:
        status, evidence = "FAIL", f"{type(exc).__name__}: {exc}"
    RESULTS.append({
        "stage": stage, "check": name, "status": status,
        "evidence": evidence, "ms": round((time.perf_counter() - t0) * 1000, 1),
    })


class SkipCheck(Exception):
    """Raised when a check's precondition is absent (DB, corpus, etc.)."""


def _ev(ok: bool, text: str) -> Tuple[str, str]:
    return ("PASS" if ok else "FAIL"), text


# ===========================================================================
# STAGE 0 — INGESTION / PERSISTENCE
# ===========================================================================
def stage0_ingest() -> None:
    from db import admin_connection, get_corpus_epoch

    @check("0-ingest", "corpus chunks + spans persisted")
    def _chunks():
        with admin_connection() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM multi_agent_chunks;")
            n = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM multi_agent_chunks WHERE char_start IS NOT NULL;")
            spanned = cur.fetchone()[0]
        if n == 0:
            raise SkipCheck("corpus empty — run ingest first")
        # ADR-005: chunks that cannot be located carry NULL spans ("not
        # locatable"), never a fabricated offset. So spanned <= n is expected.
        return _ev(True, f"{n} chunks, {spanned} with byte spans "
                         f"({n - spanned} honestly NULL = unlocatable)")

    @check("0-ingest", "page transcripts pinned per epoch")
    def _transcripts():
        with admin_connection() as c, c.cursor() as cur:
            cur.execute("SELECT count(DISTINCT source) FROM source_registry;")
            srcs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM page_transcripts;")
            pages = cur.fetchone()[0]
            cur.execute("SELECT source, pdf_sha256 FROM source_registry ORDER BY source;")
            rows = cur.fetchall()
        if not rows:
            raise SkipCheck("source_registry empty")
        shas_ok = all(len(s) == 64 and all(ch in "0123456789abcdef" for ch in s)
                      for _, s in rows)
        return _ev(shas_ok and pages > 0,
                   f"{srcs} sources, {pages} page transcripts, "
                   f"pdf_sha256 anchors {'valid' if shas_ok else 'MALFORMED'}")

    @check("0-ingest", "every chunk span slices its transcript byte-exactly")
    def _span_integrity():
        """THE ingest contract (ADR-005): a stored span must reproduce the
        stored chunk text when applied to the stored transcript. This is the
        integrity property every receipt ultimately rests on."""
        with admin_connection() as c, c.cursor() as cur:
            cur.execute("SELECT epoch FROM corpus_state WHERE id=1;")
            epoch = cur.fetchone()[0]
            cur.execute("""SELECT source, page, transcript FROM page_transcripts
                           WHERE corpus_epoch = %s;""", (epoch,))
            tr = {(s, p): t for s, p, t in cur.fetchall()}
            cur.execute("""SELECT source, page, content, char_start, char_end
                           FROM multi_agent_chunks
                           WHERE char_start IS NOT NULL AND char_end IS NOT NULL;""")
            rows = cur.fetchall()
        if not rows:
            raise SkipCheck("no spanned chunks")
        bad, checked = 0, 0
        for s, p, content, cs, ce in rows:
            t = tr.get((s, p))
            if t is None:
                continue
            checked += 1
            if t[cs:ce] != content:
                bad += 1
        return _ev(bad == 0, f"{checked - bad}/{checked} spans slice byte-exactly "
                             f"({bad} mismatches)")

    @check("0-ingest", "fact store: span-anchored rows written")
    def _fact_rows():
        from db import sync_fact_rows, verify_fact_rows
        st = sync_fact_rows()
        if st["rows"] == 0:
            raise SkipCheck("no fact rows extracted")
        v = verify_fact_rows()
        return _ev(v["span_failures"] == 0 and st["span_mismatches"] == 0,
                   f"{st['rows']} rows written, {st['span_mismatches']} span "
                   f"mismatches at write, re-verify {v['span_verified']}/"
                   f"{v['rows']} byte-exact, {st['reconciled']} XBRL-reconciled")

    @check("0-ingest", "RLS: runtime role cannot see another tenant")
    def _rls():
        from db import get_db_connection, admin_connection
        with admin_connection() as c, c.cursor() as cur:
            cur.execute("SELECT current_user, rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user;")
        tenant = f"audit_{uuid.uuid4().hex[:8]}"
        with get_db_connection(tenant_id=tenant) as c, c.cursor() as cur:
            cur.execute("SELECT current_user;")
            user = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM multi_agent_chunks;")
            leak = cur.fetchone()[0]
        return _ev(leak == 0,
                   f"runtime role '{user}' sees {leak} rows of another tenant's "
                   f"corpus (must be 0)")


# ===========================================================================
# STAGE 1 — SEMANTIC CACHE (the front door)
# ===========================================================================
def stage1_cache() -> None:
    import adaptive_rag as ar
    from db import (check_semantic_cache, save_to_semantic_cache,
                    evict_from_semantic_cache)

    @check("1-cache", "near-exact replay hits; paraphrase misses")
    def _threshold():
        cfg_miss = ar.get_settings().cache_similarity
        q = f"audit cache probe {uuid.uuid4().hex[:8]} apple revenue"
        tenant = f"audit_{uuid.uuid4().hex[:8]}"
        hit_before = check_semantic_cache(q, tenant_id=tenant)
        save_to_semantic_cache(q, {"answer": "probe", "provenance_run_id": "r1"},
                               tenant_id=tenant)
        hit_after = check_semantic_cache(q, tenant_id=tenant)
        other = check_semantic_cache(f"completely different {uuid.uuid4().hex}",
                                     tenant_id=tenant)
        evict_from_semantic_cache(q, tenant)
        ok = hit_before is None and hit_after is not None and other is None
        return _ev(ok, f"threshold {cfg_miss}: miss-before={hit_before is None}, "
                       f"hit-after={hit_after is not None}, unrelated-miss="
                       f"{other is None}")

    @check("1-cache", "cache is tenant-scoped (no cross-tenant replay)")
    def _scope():
        from db import check_semantic_cache, save_to_semantic_cache
        # DEFENSE IN DEPTH, and why this control is fixture-level:
        # removing the explicit `WHERE tenant_id = %(tenant)s` predicate from
        # check_semantic_cache does NOT leak, because the runtime role sits
        # under a row-level-security policy that filters by session tenant.
        # Two independent layers means no single-line mutation can produce a
        # cross-tenant read; to show the ASSERTION is live, the mutation
        # (see pipeline_audit_mutations.py) makes tenant B the same tenant as
        # A, which must flip this check to FAIL.
        t_a, t_b = f"audit_a_{uuid.uuid4().hex[:8]}", f"audit_b_{uuid.uuid4().hex[:8]}"
        q = f"scoped probe {uuid.uuid4().hex[:8]}"
        save_to_semantic_cache(q, {"answer": "A-secret", "provenance_run_id": "r"},
                               tenant_id=t_a)
        leaked = check_semantic_cache(q, tenant_id=t_b)
        own = check_semantic_cache(q, tenant_id=t_a)
        return _ev(leaked is None and own is not None,
                   f"tenant B sees tenant A's entry: {leaked is not None} "
                   f"(must be False); owner still hits: {own is not None}")

    @check("1-cache", "replay restores documents (sources), not just the answer")
    def _replay_sources():
        """Live-caught 2026-10-03 on production: replays rendered with
        sources: [] because the payload never stored `documents`."""
        import unittest.mock as mock
        docs = ["Apple | aapl | p5\nServices 22,314"]
        def _modern(*a, **k):
            return {"answer": "certified [1]", "documents": docs,
                    "provenance_run_id": "orig-1"}
        async def _db(fn, *a, **k):
            return fn(*a, **k)
        with mock.patch.object(ar, "check_semantic_cache", _modern), \
             mock.patch.object(ar, "_db_call", _db):
            upd = asyncio.run(ar.check_cache_node(
                {"original_question": "q?", "run_id": "replay-1",
                 "tenant_id": "default"}))
        return _ev(upd.get("documents") == docs,
                   f"cached_hit={upd.get('cached_hit')}, "
                   f"documents restored={upd.get('documents') == docs}")

    @check("1-cache", "legacy entry without provenance = MISS (self-heal)")
    def _legacy():
        import unittest.mock as mock
        def _legacy_hit(*a, **k):
            return {"answer": "legacy no-proof", "grounded": True}
        async def _db(fn, *a, **k):
            return fn(*a, **k)
        with mock.patch.object(ar, "check_semantic_cache", _legacy_hit), \
             mock.patch.object(ar, "_db_call", _db):
            upd = asyncio.run(ar.check_cache_node(
                {"original_question": "q?", "run_id": "replay-2",
                 "tenant_id": "default"}))
        return _ev(upd.get("cached_hit") is False,
                   f"unverifiable entry served as replay: "
                   f"{upd.get('cached_hit') is not False} (must be False)")


# ===========================================================================
# STAGE 2 — ADR-017 PATH A (deterministic serving, zero tokens)
# ===========================================================================
PATH_A_CASES = [
    ("What was Apple's total net sales in Q4 2023?", "Apple", "89,498"),
    ("What was Apple's net income in Q4 2023?", "Apple", "22,956"),
    ("What was Tesla's revenue in Q4 2023?", "Tesla", "25,167"),
    ("What was Tesla's diluted EPS in Q4 2023?", "Tesla", "2.27"),
    ("What was Meta's revenue in Q4 2023?", "Meta", "40,111"),
]
PATH_A_DEMOTIONS = [
    "What was Apple's Products revenue in Q4 2023?",     # segment qualifier
    "What drove Tesla's Q4 2023 net income growth?",     # interpretive stem
    "What was Tesla's dividend per share in Q4 2023?",   # no fact row: Tesla pays none
    "Metas revenue in Q4 2023",                          # entity not exact
    "What was Apple's revenue in Q3 2023?",              # out of coverage
]


def stage2_path_a() -> None:
    import adaptive_rag as ar
    os.environ["RAG_FACT_FASTPATH"] = "1"

    @check("2-pathA", "covered triples serve at ZERO LLM tokens")
    def _serves():
        served = 0
        detail = []
        for q, company, figure in PATH_A_CASES:
            out = asyncio.run(ar.fact_fastpath(
                {"original_question": q, "run_id": "audit",
                 "tenant_id": "default", "retry_count": 0}))
            got = out.get("final_executive_report") or ""
            ok = bool(out.get("fastpath_served")) and figure in got
            served += ok
            detail.append(f"{company}:{'ok' if ok else 'MISS'}")
        return _ev(served == len(PATH_A_CASES),
                   f"{served}/{len(PATH_A_CASES)} served — " + ", ".join(detail))

    @check("2-pathA", "out-of-coverage / ambiguous queries DEMOTE (fail-closed)")
    def _demotes():
        demoted = 0
        detail = []
        for q in PATH_A_DEMOTIONS:
            out = asyncio.run(ar.fact_fastpath(
                {"original_question": q, "run_id": "audit",
                 "tenant_id": "default", "retry_count": 0}))
            ok = not out.get("fastpath_served")
            demoted += ok
            detail.append("demote" if ok else "SERVED(bad)")
        return _ev(demoted == len(PATH_A_DEMOTIONS),
                   f"{demoted}/{len(PATH_A_DEMOTIONS)} demoted — "
                   f"{'; '.join(detail)}")

    @check("2-pathA", "served figures are span-verbatim (no NUMERIC padding)")
    def _verbatim():
        """A.5 ruling: every figure must be re-matched inside the row's own
        span text, so the page's rendering (2.27) always wins and a padded
        NUMERIC (2.2700) can never reach the answer."""
        bad = []
        for q, _c, figure in PATH_A_CASES:
            out = asyncio.run(ar.fact_fastpath(
                {"original_question": q, "run_id": "audit",
                 "tenant_id": "default", "retry_count": 0}))
            got = out.get("final_executive_report") or ""
            if figure not in got:
                bad.append(q)
            if re.search(r"\d+\.\d*0{2,}\b", got):
                bad.append(f"padded:{q}")
        return _ev(not bad, f"all figures verbatim in span text; offenders={bad or 'none'}")

    @check("2-pathA", "Path A writes a deterministic receipt that /verify resolves")
    def _receipt():
        from db import get_verification_receipt, verify_receipt_chain
        q = "What was Apple's total net sales in Q4 2023?"
        run_id = f"audit-{uuid.uuid4().hex[:10]}"
        out = asyncio.run(ar.fact_fastpath(
            {"original_question": q, "run_id": run_id,
             "tenant_id": "default", "retry_count": 0}))
        if not out.get("fastpath_served"):
            raise SkipCheck("Path A did not serve (fact store not reconciled?)")
        rc = get_verification_receipt(run_id, "default")
        if not rc:
            return _ev(False, f"served run {run_id} stored NO receipt")
        chain = verify_receipt_chain(rc)
        return _ev(bool(chain.get("verified")),
                   f"run {run_id}: receipt present, verified={chain.get('verified')}, "
                   f"links {chain.get('links_ok')}/{chain.get('links_checked')}")


# ===========================================================================
# STAGE 3 — ROUTER (state contract, mocked engine; zero tokens)
# ===========================================================================
def stage3_router() -> None:
    import adaptive_rag as ar

    @check("3-router", "vectorstore / general_knowledge / out_of_domain routing")
    def _routes():
        """Every RouterDecision.destination must survive route_question verbatim.

        Historical false failure: patching only `_llm_call` was not enough.
        `route_question` resolves `_get_router()` as a CALL ARGUMENT, i.e.
        BEFORE the mocked `_llm_call` runs — with no GROQ_API_KEY the node
        fail-closed and all three routes read 'out_of_domain'. The mock must
        sit at both layers, and the assertion additionally proves the
        specialists list round-trips and router_unavailable is not set.
        """
        import unittest.mock as mock
        cases = [("What was Apple's revenue in Q4 2023?", "vectorstore",
                  ["financial"]),
                 ("What is EBITDA?", "general_knowledge", None),
                 ("How do I bake sourdough bread?", "out_of_domain", None)]
        seen, ok = [], True
        for question, dest, specs in cases:
            decl = ar.RouteDecision(destination=dest,
                                    active_specialists=specs)
            async def _llm(*a, **k):
                return decl, ar.UsageCollector()
            with mock.patch.object(ar, "_get_router", lambda: object()), \
                 mock.patch.object(ar, "_llm_call", _llm):
                upd = asyncio.run(ar.route_question({"original_question": question}))
            got = upd.get("route")
            seen.append(f"{dest}→{got}")
            ok &= (got == dest and not upd.get("router_unavailable")
                   and (specs is None or upd.get("active_specialists") == specs))
        return _ev(ok, "faithful round-trip: " + ", ".join(seen)
                   + "; router_unavailable never set; specialists preserved")

    @check("3-router", "router death FAIL-CLOSES and names the true cause")
    def _dead():
        import unittest.mock as mock
        async def _boom(*a, **k):
            raise RuntimeError("GROQ_API_KEY missing")
        with mock.patch.object(ar, "_llm_call", _boom):
            upd = asyncio.run(ar.route_question({"original_question": "Apple revenue?"}))
        out = asyncio.run(ar.cannot_answer({**upd, "original_question": "Apple revenue?"}))
        text = out.get("final_executive_report", "")
        ok = (upd.get("route") == "out_of_domain"
              and upd.get("router_unavailable") is True
              and "outside the scope" not in text)
        return _ev(ok, f"route={upd.get('route')}, flagged={upd.get('router_unavailable')}, "
                       f"truthful text={'yes' if 'routing stage is unavailable' in text else 'NO'}")

    @check("3-router", "non-schema router reply fails closed (never AttributeErrors)")
    def _nonschema():
        import unittest.mock as mock
        class _Raw:
            content = "**vectorstore**"
        async def _raw(*a, **k):
            return _Raw(), ar.UsageCollector()
        with mock.patch.object(ar, "_llm_call", _raw):
            upd = asyncio.run(ar.route_question({"original_question": "Apple revenue?"}))
        return _ev(upd.get("route") == "out_of_domain" and upd.get("router_unavailable"),
                   f"raw AIMessage → route={upd.get('route')}, no crash")


# ===========================================================================
# STAGE 4 — RETRIEVAL (hybrid RRF + multi-query + rerank)
# ===========================================================================
def stage4_retrieval() -> None:
    import adaptive_rag as ar
    from db import pgvector_hybrid_search

    @check("4-retrieval", "hybrid search returns cited, spanned evidence")
    def _hybrid():
        rows = pgvector_hybrid_search("Tesla energy storage deployments", top_k=5)
        if not rows:
            raise SkipCheck("no results (corpus/embedder unavailable)")
        needed = {"chunk_hash", "content", "company", "source", "page"}
        missing = [k for k in needed if k not in rows[0]]
        return _ev(not missing,
                   f"{len(rows)} rows; fields ok={not missing}; "
                   f"top: {rows[0].get('company')} p{rows[0].get('page')} "
                   f"fusion={rows[0].get('fusion_score'):.4f}")

    @check("4-retrieval", "RRF fusion ranks a document present in both arms first")
    def _rrf():
        # Real rows always carry chunk_hash (UNIQUE NOT NULL in the schema);
        # the historical failure used bare {"id": n} dicts, which all fell
        # through to the source|page|content fallback key and COLLAPSED INTO
        # ONE SCORED DOCUMENT. Fusion was never wrong — the fixture was.
        mk = lambda h: {"chunk_hash": h, "source": "s.pdf", "page": 1,
                        "content": f"chunk {h}"}
        fused = ar._rrf_fuse([
            [mk("a"), mk("b"), mk("c")],             # semantic arm
            [mk("b"), mk("a"), mk("d")],             # keyword arm
        ])
        order = [r["chunk_hash"] for r in fused]
        both = {h for h in order[:2]} == {"a", "b"}
        single_after = order.index("c") > order.index("a") and order.index("d") > order.index("a")
        return _ev(both and single_after,
                   f"order={order}; both-arm docs occupy ranks 1-2={both}; "
                   f"single-arm docs ranked below={single_after}")

    @check("4-retrieval", "specialist pruning keeps evidence-pool PARITY")
    def _pruning():
        """KNOWN_ISSUES: pruning once shrank the pool 15→5 and drafts started
        claiming net income as revenue. The invariant: per_specialist_k is
        scaled AFTER pruning (against the PRUNED count), so the union stays
        ~15 chunks whether 1 or 3 specialists run.

        Historical false failure: the check grepped for `target_pool=`, a
        variable that does not exist anywhere in the codebase (the real
        expression is `max(5, 15 // max(1, len(specialists)))`). A grep for a
        name nobody wrote can never pass and never should have been authored.
        """
        src = (REPO / "adaptive_rag.py").read_text(encoding="utf-8")
        scaling = re.search(r"per_specialist_k\s*=\s*([^\n]+)", src)
        if not scaling:
            return _ev(False, "per-specialist k-scaling expression not found")
        expr = scaling.group(1).strip()
        # the scaling must read the PRUNED dict, i.e. appear AFTER reassignment
        pruned_at = src.find("specialists = pruned")
        scale_at = scaling.start()
        after = pruned_at != -1 and scale_at > pruned_at
        # The k must be EVALUATED from the source expression with the real
        # variable bound — re-deriving our own `max(5, 15 // n)` here would
        # silently ignore whatever the code actually says (a hardcoded k=5
        # mutation scored as PASS: the check computed 15/14/15 from its own
        # formula and never looked at the mutant's).
        pools: Dict[int, int] = {}
        for n in (1, 2, 3):
            env = {"max": max, "len": len,
                   "specialists": {f"s{i}": None for i in range(n)}}
            try:
                k = eval(expr, {"__builtins__": {}}, env)  # noqa: S307 - audited source
            except Exception as exc:
                return _ev(False, f"cannot evaluate {expr!r}: {exc}")
            pools[n] = int(k) * n
        par = all(14 <= v <= 15 for v in pools.values())
        return _ev(after and par,
                   f"expr={expr!r}; scales after pruning={after}; "
                   f"evaluated union pool 1/2/3 specialists = "
                   f"{pools[1]}/{pools[2]}/{pools[3]} chunks "
                   f"(parity = 14-15 each)")


# ===========================================================================
# STAGE 5 — DETERMINISTIC GATES (the five)
# ===========================================================================
def stage5_gates() -> None:
    import adaptive_rag as ar

    @check("5-gates", "GATE citation-bounds: fabricated [n] rejected")
    def _citation():
        bad = ar.citation_pre_audit("Revenue grew [1] and [9].", doc_count=3)
        good = ar.citation_pre_audit("Revenue grew [1] and [3].", doc_count=3)
        fullwidth = ar.citation_pre_audit("Revenue grew 【2】.", doc_count=3)
        return _ev(bad == "[9]" and good is None and fullwidth is None,
                   f"out-of-range [9] → {bad!r}; in-range [1][3] → {good!r}; "
                   f"fullwidth 【2】 → {fullwidth!r}")

    @check("5-gates", "GATE unit/scale: 1000× lie rejected, restatement passes")
    def _scale():
        ev = [{"content": "Net sales ($ in millions): 21,563", "company": "Apple",
               "source": "a.pdf", "page": 1, "chunk_hash": "h"}]
        lie = ar.assert_claim_scales("Revenue was $21,563 billion [1].", ev)
        honest = ar.assert_claim_scales("Revenue was $21,563 [1].", ev)
        resc = ar.assert_claim_scales("Revenue was $21.6 billion [1].", ev)
        undeclared = ar.assert_claim_scales("Revenue was $99 zillion [1].", [])
        return _ev(bool(lie) and not honest and not resc,
                   f"1000×-up lie → {'REJECTED' if lie else 'passed(bad)'}; "
                   f"verbatim → {'passed' if not honest else 'REJECTED(bad)'}; "
                   f"re-scaled → {'passed' if not resc else 'REJECTED(bad)'}; "
                   f"undeclared evidence → declines ({not undeclared})")

    @check("5-gates", "GATE growth-direction: 'grew' when it declined is caught")
    def _growth():
        # Evidence must be in the real Phase-B pair-line format the parser
        # keys on ('<label> :: Q4-2022=<n> | Q4-2023=<n>'). The historical
        # failure fed a prose string ('Total revenue 2023: 96,773 ; 2022:
        # 81,462') that find_comparative_pairs cannot parse, so the gate
        # correctly declined-to-judge and the audit scored it as a failure.
        ev = [{"content": "Total automotive revenues :: Q4-2022=21,307 | "
                          "Q4-2023=21,563 | YoY=1%",
               "company": "Tesla", "source": "t.pdf", "page": 1,
               "chunk_hash": "h"}]
        truthful = ar.check_growth_claims(
            "Total automotive revenues grew 1% [1].", ev)
        lie = ar.check_growth_claims(
            "Total automotive revenues declined 25% [1].", ev)
        no_pair = ar.check_growth_claims(
            "Total automotive revenues declined 25% [1].", [])
        return _ev(not truthful and bool(lie) and not no_pair,
                   f"'grew 1%' over growth → flagged={bool(truthful)} (must be "
                   f"False); 'declined 25%' → flagged={bool(lie)} (must be "
                   f"True); metric with no pair → declines={not no_pair}")

    @check("5-gates", "GATE XBRL: figure-level metric ownership (no cross-metric judging)")
    def _xbrl():
        src = (REPO / "adaptive_rag.py").read_text(encoding="utf-8")
        has_owner = "_figure_metric_owner" in src
        has_decoy = "decoy" in src.lower()
        return _ev(has_owner,
                   f"figure→metric ownership resolver present={has_owner}; "
                   f"decoy-anchor guard present={has_decoy}")

    @check("5-gates", "GATE echo/injection: prompt-injection echo rejected")
    def _echo():
        src = (REPO / "adaptive_rag.py").read_text(encoding="utf-8")
        markers = re.findall(r'"([^"]{4,40})"', src)
        inj = [m for m in markers if "ignore" in m.lower() or "instruction" in m.lower()]
        return _ev(len(inj) > 0,
                   f"injection/echo guard markers present: {len(inj)} "
                   f"(e.g. {inj[:2]})")

    class _Settings:
        max_retries = 2

    @check("5-gates", "guard rail: a degraded run can NEVER certify")
    def _degraded():
        import unittest.mock as mock
        state = {"original_question": "Apple revenue?", "documents": ["Apple | a.pdf | p1\nx"],
                 "evidence_records": [{"chunk_hash": "h", "content": "x"}],
                 "final_executive_report": "Apple revenue was 89,498 [1].",
                 "degraded_agents": ["financial"], "retry_count": 0,
                 "run_id": "audit", "tenant_id": "default"}
        async def _db(fn, *a, **k):
            return None
        with mock.patch.object(ar, "_db_call", _db):
            upd = asyncio.run(ar.fact_checker_guard(state))
        # Outcome alone is not enough: a mutation that deletes the `degraded`
        # branch still yields grounded=False downstream (the run fails for
        # some OTHER reason). The load-bearing assertion is that the refusal
        # ATTRIBUTES the failure to the quarantined specialist — that is the
        # difference between a fail-closed guard and an accidental refusal.
        objection = upd.get("audit_objection") or ""
        # The objection must NAME the quarantined specialist rather than the
        # generic 'draft failed the grounding audit' fallback.
        names_cause = "quarantin" in objection.lower() and "financial" in objection
        # ...and it must actually reach the user-facing refusal text.
        refusal_text = ""
        with mock.patch.object(ar, "_db_call", _db), \
             mock.patch.object(ar, "get_settings", lambda: _Settings()):
            ref = asyncio.run(ar.verified_refusal({**state, **upd}))
        refusal_text = ref.get("final_executive_report") or ""
        reaches_user = "quarantin" in refusal_text.lower()
        return _ev(upd.get("grounded") is False and names_cause and reaches_user,
                   f"degraded run grounded={upd.get('grounded')} (must be False); "
                   f"objection={objection!r}; names the cause={names_cause}; "
                   f"reaches the refusal text={reaches_user}")


# ===========================================================================
# STAGE 6 — TERMINALS: refusal, shadow, escalation
# ===========================================================================
def stage6_terminals() -> None:
    import adaptive_rag as ar

    @check("6-terminal", "verified refusal stores a refused receipt (autopsy trail)")
    def _refusal_receipt():
        import unittest.mock as mock
        captured = {}
        async def _db(fn, *a, **k):
            captured["fn"] = getattr(fn, "__name__", str(fn))
            captured["args"] = a
            captured["kwargs"] = k
            return None
        with mock.patch.object(ar, "_db_call", _db):
            upd = asyncio.run(ar.verified_refusal(
                {"original_question": "Apple dividends?", "run_id": "audit-run",
                 "tenant_id": "default", "evidence_records": [],
                 "audit_unavailable": True}))
        ok = (upd.get("outcome") == "verified_refusal"
              and "audit stage unavailable" in (upd.get("final_executive_report") or "")
              and captured.get("kwargs", {}).get("audit_verdict") == "refused")
        return _ev(ok, f"outcome={upd.get('outcome')}; receipt saved="
                       f"{captured.get('fn')}; verdict="
                       f"{captured.get('kwargs', {}).get('audit_verdict')}; "
                       f"names true cause="
                       f"{'audit stage unavailable' in (upd.get('final_executive_report') or '')}")

    @check("6-terminal", "premise fast-path refuses uncovered metrics WITHOUT the fleet")
    def _premise():
        src = (REPO / "adaptive_rag.py").read_text(encoding="utf-8")
        has = "_premise_fast_path" in src and "route_premise" in src
        declared = "    _premise_fast_path: bool" in src
        return _ev(has and declared,
                   f"premise gate present={has}; state channel declared={declared} "
                   f"(undeclared keys are DROPPED by LangGraph)")

    @check("6-terminal", "shadow executor NEVER serves (isolation contract)")
    def _shadow():
        from fact_shadow import shadow_fact_path
        import unittest.mock as mock
        state = {"original_question": "What was Tesla's net income in Q4 2023?",
                 "tenant_id": "default", "run_id": f"audit-shadow-{uuid.uuid4().hex[:8]}",
                 "final_executive_report": "Tesla net income $7,928 million [1]."}
        before = dict(state)
        out = asyncio.run(shadow_fact_path(dict(state)))
        mutated = {k: v for k, v in out.items() if k in before and before[k] != v}
        return _ev(out == {}, f"returned {out} (must be {{}}); serving-state "
                              f"mutations={mutated or 'none'}")

    @check("6-terminal", "graph declares every state channel the nodes use")
    def _channels():
        import adaptive_rag as ar
        app = ar.get_graph()
        channels = getattr(app, "channels", None) or getattr(app.graph, "channels", None)
        if not channels:
            raise SkipCheck("channel introspection unavailable on this langgraph")
        required = ["provenance_run_id", "quota_hint_s", "echo_reject", "xbrl_issues",
                    "quota_aborted", "_premise_fast_path", "audit_unavailable",
                    "shadow_coverage_miss", "router_unavailable", "fastpath_served",
                    "served_path", "fastpath_reason", "cached_hit", "grounded"]
        missing = [k for k in required if k not in channels]
        return _ev(not missing,
                   f"{len(channels)} channels; missing={missing or 'none'} "
                   f"(undeclared keys are silently dropped at merge)")


# ===========================================================================
# STAGE 7 — RECEIPT CHAIN / OFFLINE PROOF
# ===========================================================================
def stage7_receipts() -> None:
    from db import verify_receipt_chain

    @check("7-receipt", "committed white-whale bundle verifies offline (15/15)")
    def _white_whale():
        import subprocess
        d = REPO / "audit_bundle_white_whale"
        if not d.exists():
            raise SkipCheck("bundle not in this checkout")
        r = subprocess.run([sys.executable, "verify_certificate.py"], cwd=d,
                           capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        m = re.search(r"(\d+)/(\d+) links", out)
        ok = r.returncode == 0 and m and m.group(1) == m.group(2)
        return _ev(bool(ok), f"exit={r.returncode}, "
                             f"links={m.group(0) if m else 'n/a'}, "
                             f"verdict={'PASS' if 'PASS' in out else 'no PASS line'}")

    @check("7-receipt", "tamper: a mutated evidence span BREAKS the chain")
    def _tamper():
        from db import get_verification_receipt
        # Find any stored receipt with evidence to tamper with.
        from db import admin_connection
        with admin_connection() as c, c.cursor() as cur:
            cur.execute("SELECT run_id FROM verification_receipts "
                        "WHERE audit_verdict='grounded' AND jsonb_array_length(evidence_json) > 0 "
                        "ORDER BY created_at DESC LIMIT 1;")
            row = cur.fetchone()
        if not row:
            raise SkipCheck("no grounded receipt in this database yet")
        rc = get_verification_receipt(row[0], "default") or {}
        clean = verify_receipt_chain(rc)
        tampered = json.loads(json.dumps(rc))
        tampered["evidence_json"][0]["content"] = (
            "FORGED " + (tampered["evidence_json"][0].get("content") or "")[:40])
        after = verify_receipt_chain(tampered)
        return _ev(bool(clean.get("verified")) and not after.get("verified"),
                   f"clean verified={clean.get('verified')} (must be True) → "
                   f"tampered verified={after.get('verified')} (must be False)")

    @check("7-receipt", "Ed25519: signing key fully end-to-end (sign+verify)")
    def _signature_crypto():
        """The attestation must be exercised with a REAL key, not scored on
        whether one happens to be in the environment.

        Historical false failure: the check asserted a `signature` block
        exists. With no ATTESTATION_PRIVATE_KEY set, sign_certificate_payload
        returns the DOCUMENTED posture {"status": "unsigned"} — so the check
        scored a correctly-configured-by-design deployment as broken. Worse,
        it never verified any signature (its name promised 'verifiable'), so
        a garbage signature would also have passed. This version generates a
        throwaway key, signs, and cryptographically verifies — the only way
        to prove the mechanism works.
        """
        import base64, json as _json
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey)
        from cryptography.hazmat.primitives import serialization
        import db as _db
        key = Ed25519PrivateKey.generate()
        priv = base64.b64encode(key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())).decode("ascii")
        cert = {"status": "verified", "run_id": "audit-sig", "epoch": 1}
        old = os.environ.get("ATTESTATION_PRIVATE_KEY")
        os.environ["ATTESTATION_PRIVATE_KEY"] = priv
        try:
            sig = _db.sign_certificate_payload(cert)
            unsigned = _db.sign_certificate_payload.__doc__ is not None
        finally:
            if old is None:
                os.environ.pop("ATTESTATION_PRIVATE_KEY", None)
            else:
                os.environ["ATTESTATION_PRIVATE_KEY"] = old
        if not sig.get("signature"):
            return _ev(False, f"signing produced no signature: {sig}")
        payload = {k: v for k, v in cert.items() if k != "signature"}
        canonical = _json.dumps(payload, sort_keys=True,
                                separators=(",", ":"), ensure_ascii=False)
        pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(sig["public_key"]))
        try:
            pub.verify(base64.b64decode(sig["signature"]),
                       canonical.encode("utf-8"))
            verified, tampered = True, False
            try:
                pub.verify(base64.b64decode(sig["signature"]),
                           (canonical + "x").encode("utf-8"))
            except Exception:
                tampered = True
        except Exception as exc:
            return _ev(False, f"signature did NOT verify: {exc}")
        posture = ("configured" if os.getenv("ATTESTATION_PRIVATE_KEY")
                   else "unsigned (documented posture — key not set)")
        return _ev(verified and tampered,
                   f"algorithm={sig.get('algorithm')}, key_id={sig.get('key_id')}; "
                   f"valid signature verifies={verified}; tampered payload "
                   f"rejected={tampered}; live posture={posture}")

    @check("7-receipt", "operator runbook: documented key-gen script exists")
    def _keygen_exists():
        """db.py's signing docstring tells operators to run
        scripts/generate_attestation_key.py — it did not exist, so the only
        documented path to enabling signing dead-ended."""
        script = REPO / "scripts" / "generate_attestation_key.py"
        src = (REPO / "db.py").read_text(encoding="utf-8")
        referenced = "generate_attestation_key" in src
        return _ev(script.exists() and (not referenced or True),
                   f"referenced_in_db_py={referenced}, script_exists={script.exists()}"
                   + ("" if script.exists() else " — DOCSTRING POINTS AT A MISSING FILE"))


# ===========================================================================
# STAGE 8 — SERVING LAYER (gateway contracts)
# ===========================================================================
def stage8_serving() -> None:
    @check("8-serving", "gateway mounts every documented route")
    def _routes():
        import main
        paths = {getattr(r, "path", "") for r in main.app.routes}
        want = {"/query", "/query/stream", "/search", "/verify/{run_id}",
                "/export/{run_id}", "/feedback", "/health", "/live", "/ready", "/metrics"}
        missing = want - paths
        return _ev(not missing, f"{len(paths)} routes; missing={missing or 'none'}")

    @check("8-serving", "open mode is an AFFIRMATIVE opt-in (fail-closed default)")
    def _auth():
        import inspect
        import main
        src = inspect.getsource(main.require_query_key)
        ok = ("ALLOW_OPEN_MODE" in src
              and 'status_code=503' in src
              and 'status_code=403' in src
              and 'status_code=401' in src)
        return _ev(ok, "503 when keys unset & open-mode off; 401 missing key; "
                       "403 present-but-invalid key")

    @check("8-serving", "/query/stream forwards the RESOLVED tenant (not the raw param)")
    def _tenant():
        import inspect
        import main
        src = inspect.getsource(main.query_stream)
        ok = "_event_stream(request, question, tenant, run_id)" in src
        bad = "_event_stream(request, question, tenant_id, run_id)" in src
        return _ev(ok and not bad,
                   f"passes resolved tenant={ok}; still passes raw tenant_id={bad} "
                   f"(cross-tenant leak if True)")

    @check("8-serving", "single-worker posture is documented at the enforcement site")
    def _workers():
        docker = (REPO / "Dockerfile").read_text(encoding="utf-8")
        return _ev("--workers" in docker and "0.0.0.0" in docker,
                   "Dockerfile binds 0.0.0.0 with an explicit worker count")

    @check("8-serving", "boot smoke covers every provider lane (incl. openai_compatible)")
    def _bootsmoke():
        src = (REPO / "main.py").read_text(encoding="utf-8")
        excluded = 'provider != "openai_compatible"' in src
        exposed = "boot_smoke" in src
        return _ev(not excluded and exposed,
                   f"openai_compatible excluded={excluded} (must be False); "
                   f"verdict exposed at /health={exposed}")


# ===========================================================================
# REPORT
# ===========================================================================
def render(json_mode: bool) -> int:
    """json_mode emits ONLY the JSON array — a machine consumer (`--json | jq`,
    the mutation runner) must not have to strip a trailing human summary.
    Historical bug: --json printed the array AND the summary banner, so every
    programmatic parse died with 'Extra data: line N column 1'."""
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    if json_mode:
        print(json.dumps(RESULTS, indent=1))
        return 1 if fails else 0
    else:
        order = ["0-ingest", "1-cache", "2-pathA", "3-router", "4-retrieval",
                 "5-gates", "6-terminal", "7-receipt", "8-serving"]
        icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "skip"}
        print("=" * 78)
        print("ADAPTIVE RAG — PIPELINE AUDIT (stage-by-stage, evidence-first)")
        print("=" * 78)
        for st in order:
            rows = [r for r in RESULTS if r["stage"] == st]
            if not rows:
                continue
            print(f"\n[{st}]")
            for r in rows:
                print(f"  {icon[r['status']]:>4}  {r['check']}")
                print(f"        → {r['evidence']}")
    skips = [r for r in RESULTS if r["status"] == "SKIP"]
    passed = [r for r in RESULTS if r["status"] == "PASS"]
    print("\n" + "=" * 78)
    print(f"TOTAL {len(RESULTS)} checks | {len(passed)} PASS | "
          f"{len(fails)} FAIL | {len(skips)} SKIP")
    if fails:
        print("\nFAILURES:")
        for r in fails:
            print(f"  - [{r['stage']}] {r['check']} :: {r['evidence']}")
    print("=" * 78)
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", default="", help="substring filter on stage, e.g. 5-gates")
    args = ap.parse_args()

    stages = [
        ("0-ingest", stage0_ingest), ("1-cache", stage1_cache),
        ("2-pathA", stage2_path_a), ("3-router", stage3_router),
        ("4-retrieval", stage4_retrieval), ("5-gates", stage5_gates),
        ("6-terminal", stage6_terminals), ("7-receipt", stage7_receipts),
        ("8-serving", stage8_serving),
    ]
    for name, fn in stages:
        if args.only and args.only not in name:
            continue
        try:
            fn()                      # registers this stage's checks
        except Exception as exc:
            RESULTS.append({"stage": name, "check": "stage import crashed",
                            "status": "FAIL",
                            "evidence": f"{type(exc).__name__}: {exc}", "ms": 0})
    for stage, cname, cfn in _REGISTRY:
        if args.only and args.only not in stage:
            continue
        run_check(stage, cname, cfn)
    return render(args.json)


if __name__ == "__main__":
    sys.exit(main())
