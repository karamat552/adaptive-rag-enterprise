"""
Adaptive RAG — Executive Client (Streamlit v2)
==============================================
Chat-style production UI over the FastAPI gateway (main.py):
  GET  /query/stream  -> SSE events: start | transition | result | error
  POST /feedback      -> poison-pill cache eviction (X-Admin-Key)
  POST /search        -> raw hybrid-search debugger (fusion_score)
  GET  /health        -> sidebar telemetry

Production discipline:
  * LLM fires ONLY on an explicit chat submit / suggested-question click.
    Every other widget interaction re-renders from st.session_state.
  * Live pipeline visualization (st.status) driven by SSE node transitions.
  * Connection-aware: degraded-gateway banner + friendly per-error messaging.
  * Evidence [x] fold-downs are index-aligned with the report's [x] footnotes.
  * Mobile-responsive via theme config + targeted CSS; no server-side coupling.
"""

import json
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Adaptive RAG — Financial Intelligence",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
QUERY_API_KEY = os.getenv("QUERY_API_KEY", "")
DEFAULT_TENANT = os.getenv("RAG_TENANT_ID", "default")


def _auth_headers() -> Dict[str, str]:
    """X-API-Key for the gateway's query-key endpoints (/query/stream,
    /export). Empty dict in open mode (QUERY_API_KEYS unset server-side)."""
    return {"X-API-Key": QUERY_API_KEY} if QUERY_API_KEY else {}

SUGGESTED_QUESTIONS = [
    "What were Apple's Products revenue versus Services revenue in Q4 2023?",
    "Compare revenue growth and net income: Apple vs Meta in Q4 2023.",
    "What supply-chain and regulatory risks did Tesla disclose in Q4 2023?",
    "What did Meta report about AI infrastructure and GPU investments?",
]

# --- Responsive polish (defensive selectors only — safe across versions) ----
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 4rem; max-width: 1100px; }
      [data-testid="stMetric"] {
          background: rgba(128,128,128,0.08); border: 1px solid rgba(128,128,128,0.18);
          border-radius: 10px; padding: 10px 14px;
      }
      [data-testid="stChatMessage"] { border-radius: 12px; }
      [data-testid="stStatusWidget"] { border-radius: 10px; }
      h1 { font-size: 1.9rem !important; letter-spacing: -0.02em; }
      @media (max-width: 640px) {
          h1 { font-size: 1.4rem !important; }
          .block-container { padding-left: 0.8rem; padding-right: 0.8rem; }
      }
      .rag-footer { opacity: 0.6; font-size: 0.82rem; text-align: center; margin-top: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📡 C-Suite Financial Intelligence")
st.caption("Multi-agent RAG over Q4-2023 SEC 10-Q filings — Tesla · Apple · Meta. "
           "Every figure is audited against source evidence before display.")


# ============================== NETWORK LAYER ==============================
@st.cache_resource
def _client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE_URL, timeout=httpx.Timeout(300.0))


@st.cache_data(ttl=30)
def fetch_health() -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    """Cached telemetry poll — sidebar refreshes at most every 30s."""
    t0 = time.perf_counter()
    try:
        r = _client().get("/health", timeout=5.0)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if r.status_code == 200:
            return r.json(), latency_ms
        return {"_error": f"HTTP {r.status_code}"}, latency_ms
    except httpx.RequestError:
        return None, None


def _stream_query(question: str, tenant: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield (event, payload) from the gateway's SSE endpoint."""
    with _client().stream("GET", "/query/stream",
                          params={"question": question, "tenant_id": tenant},
                          headers={"Accept": "text/event-stream",
                                   **_auth_headers()}) as resp:
        if resp.status_code != 200:
            resp.read()
            if resp.status_code == 401 and not QUERY_API_KEY:
                yield "error", {"detail": (
                    "Gateway rejected the query: HTTP 401 — query auth is "
                    "enforced but this app has no QUERY_API_KEY secret. "
                    "Add it in Streamlit Cloud → App → Settings → Secrets.")}
            elif resp.status_code in (401, 403):
                yield "error", {"detail": (
                    f"Gateway rejected the query: HTTP {resp.status_code} — "
                    f"{resp.text[:200]} (check QUERY_API_KEY / tenant scope)")}
            else:
                yield "error", {"detail": f"Gateway returned HTTP {resp.status_code}"}
            return
        event: Optional[str] = None
        for raw in resp.iter_lines():
            line = raw.decode("utf-8", "replace").strip() \
                if isinstance(raw, bytes) else raw.strip()
            if not line:
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                try:
                    payload = json.loads(line.split(":", 1)[1].strip())
                except json.JSONDecodeError:
                    continue
                yield event, payload
                if event in ("result", "error"):
                    return


def _post_feedback(question: str, tenant: str) -> Tuple[int, str]:
    try:
        r = _client().post("/feedback", json={"question": question,
                                              "tenant_id": tenant},
                           headers={"X-Admin-Key": ADMIN_API_KEY})
        return r.status_code, r.text[:200]
    except httpx.RequestError as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _fetch_verification(run_id: str, tenant: str,
                        provenance_run_id: Optional[str] = None
                        ) -> Optional[Dict[str, Any]]:
    """GET /verify — receipt + deterministic chain recompute + transcripts.
    For cached answers, run_id is a REPLAY id whose proof lives under the
    original certification's provenance_run_id — pass it so /verify can
    resolve the true receipt."""
    params = {"tenant_id": tenant}
    if provenance_run_id and provenance_run_id != run_id:
        params["provenance_run_id"] = provenance_run_id
    try:
        r = _client().get(f"/verify/{run_id}", params=params)
        if r.status_code == 200:
            return r.json()
        return None
    except httpx.RequestError:
        return None


def _highlight_transcript(transcript: str, start: Optional[int],
                          end: Optional[int]) -> str:
    """Transcript -> HTML with the claimed span highlighted (mark tag).
    Bounds-defensive: any invalid span renders unhighlighted."""
    if not isinstance(start, int) or not isinstance(end, int):
        return transcript
    if not (0 <= start < end <= len(transcript)):
        return transcript
    pre, span, post = (transcript[:start], transcript[start:end],
                       transcript[end:])
    # escape AFTER slicing so offsets stay byte-true to the stored transcript
    import html as _html
    return (_html.escape(pre)
            + f"<mark style='background:#ffe08a;color:#111;"
              f"padding:1px 3px;border-radius:3px;'>{_html.escape(span)}</mark>"
            + _html.escape(post))


def _render_receipt_explorer(run_id: str, tenant: str,
                             provenance_run_id: Optional[str] = None) -> None:
    """🧾 PROVE-IT view: every claim, its citations, and the exact evidence
    span in the page transcript — with the recomputed hash chain and source
    PDF SHA-256s. Zero LLM tokens; the gateway recomputes everything."""
    data = _fetch_verification(run_id, tenant, provenance_run_id)
    if not data:
        st.error(f"No verification receipt for `{run_id}` — only grounded "
                 "runs store receipts (refusals are honest, not receipted).")
        return
    receipt = data.get("receipt", {})
    ver = data.get("verification", {})
    if data.get("resolved_from_provenance"):
        st.info(f"⚡ Cached answer — its proof lives under the original "
                f"certification run `{data['resolved_from_provenance']}`, "
                f"resolved automatically from this replay's provenance.")
    sources = {s.get("source"): s for s in (data.get("sources") or [])}
    claims = receipt.get("claims_json") or []
    evidence = receipt.get("evidence_json") or []
    transcripts = (ver.get("transcripts") or {})
    attribution = ver.get("attribution") or {}
    contradictions = receipt.get("contradictions_json") or []

    st.markdown(_escape_dollars(f"**Question:** {receipt.get('question', '—')}"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Chain verdict", "✅ verified" if ver.get("verified")
              else "❌ BROKEN")
    c2.metric("Links", f"{ver.get('links_ok', 0)}/{ver.get('links_checked', 0)}")
    c3.metric("Claims", len(claims))
    c4.metric("Evidence", len(evidence))
    if not ver.get("verified"):
        st.error("⚠️ The deterministic chain REJECTED this receipt — at least "
                 "one claim cannot be traced to source bytes. Do not trust it.")
    else:
        st.success("Every claim traces to an exact transcript span whose "
                    "sha256(company⊣source⊣page⊣slice) recomputes identically. "
                    "This proof cost zero LLM tokens.")

    st.subheader("🔗 Provenance spine")
    prov_cols = st.columns(len(sources) or 1)
    for col, (src, meta) in zip(prov_cols, sources.items()):
        sha = (meta.get("pdf_sha256") or "")[:16]
        col.caption(f"**{src}**\n\npdf sha256 `{sha}…` · {meta.get('chunks', '?')} chunks")

    if contradictions:
        with st.expander(f"⚠️ Contradictions surfaced ({len(contradictions)}) — "
                         "both figures shown, never averaged", expanded=True):
            for c in contradictions:
                st.markdown(_escape_dollars(
                    f"- **{c.get('company')} · {c.get('family')}** "
                    f"({c.get('period') or 'period n/a'}): "
                    f"conflicting values {c.get('values')} — "
                    f"gap {round(float(c.get('rel_gap', 0)) * 100, 1)}%"))

    st.subheader("🧾 Claims → Evidence → Source bytes")
    for i, claim in enumerate(claims, 1):
        cites = claim.get("citations") or []
        chips = " ".join(f"`[{n}]`" for n in cites) if cites else "`uncited`"
        with st.expander(f"Claim {i} {chips} — {str(claim.get('claim', ''))[:90]}",
                         expanded=(i <= 1)):
            if not cites:
                st.warning("This claim rests on no cited evidence.")
            for n in cites:
                if not (1 <= n <= len(evidence)):
                    st.error(f"Citation [{n}] points outside the evidence set "
                             "— forged by construction.")
                    continue
                e = evidence[n - 1]
                tkey = f"{e.get('source')}\x1f{e.get('page')}"
                tr = (transcripts.get(tkey) or {}).get("transcript", "")
                truth = attribution.get(e.get("chunk_hash")) or {}
                badges = []
                if e.get("contains_table"):
                    ao = e.get("arithmetic_ok")
                    badges.append("📊 table" + (" · ✅ sums verified" if ao
                                 else (" · ⚠️ arithmetic FLAG" if ao is False
                                       else "")))
                badge_str = f" · {' · '.join(badges)}" if badges else ""
                st.markdown(f"**Evidence [{n}]** — `{e.get('company')}` · "
                            f"`{e.get('source')}` · p.{e.get('page')}"
                            f"{badge_str}")
                st.caption(f"chunk sha256 `{str(e.get('chunk_hash'))[:20]}…` · "
                           f"span {e.get('char_start')}–{e.get('char_end')} · "
                           f"DB-truth company `{truth.get('company', '—')}`")
                if tr:
                    st.markdown("**Page transcript (claimed span highlighted):**")
                    st.markdown(
                        f"<div style='max-height:220px;overflow:auto;"
                        f"border:1px solid #8884;border-radius:8px;"
                        f"padding:8px 12px;font-size:0.86rem;"
                        f"white-space:pre-wrap;font-family:inherit;'>"
                        f"{_highlight_transcript(tr, e.get('char_start'), e.get('char_end'))}"
                        f"</div>", unsafe_allow_html=True)
                else:
                    st.info("No stored transcript for this page "
                            "(pre-2.1 evidence — chunk-level tracing only).")


# ============================== RESULT RENDERING ===========================
def _escape_dollars(text: str) -> str:
    r"""Streamlit treats $...$ spans as inline LaTeX math and mangles any
    answer containing two dollar figures ('$67,184M ... $22,314M' renders
    as scrambled math glyphs). Answers are plain prose — escape every $."""
    return text.replace("$", r"\$")


def _render_result(result: Dict[str, Any], question: str, tenant: str) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Outcome", result.get("outcome", "?"))
    c2.metric("Audit", "✅ verified" if result.get("grounded") else "⚠️ unverified")
    c3.metric("Latency", f"{result.get('latency_s', '—')}s")
    c4.metric("Tokens", f"{(result.get('usage') or {}).get('total', 0):,}")

    st.markdown(_escape_dollars(result.get("answer", "")))
    if result.get("cached"):
        st.caption("⚡ Served from the verified semantic cache — evict below to force re-verification.")

    # Evidence fold-downs: index i == footnote [i] in the report above.
    sources = result.get("sources") or []
    if sources:
        st.divider()
        st.subheader(f"📚 Evidence Ledger ({len(sources)} chunks)")
        st.caption("Indices align with the inline [n] footnotes above.")
        for i, src in enumerate(sources, 1):
            header, _, body = str(src).partition("\n")
            with st.expander(f"Evidence [{i}] — {header.replace(' | ', ' · ')}"):
                st.markdown(_escape_dollars(body.strip() or header))

    st.divider()
    ac1, ac2, ac3, ac4 = st.columns([1, 1, 1, 2])
    flagged = bool(result.get("_flagged"))
    if ac1.button("👎 Flag Inaccurate" + (" (flagged)" if flagged else ""),
                  disabled=not ADMIN_API_KEY or flagged,
                  key=f"flag_{result.get('run_id', 'x')}",
                  help="Evicts the semantic-cache entries nearest this question "
                       "so the next ask re-verifies from filings. Requires ADMIN_API_KEY."):
        status, text = _post_feedback(question, tenant)
        if status == 200:
            try:
                evicted = json.loads(text).get("evicted", 0)
            except json.JSONDecodeError:
                evicted = 0
            result["_flagged"] = True
            st.toast(f"🗑 Evicted {evicted} cache entr"
                     f"{'y' if evicted == 1 else 'ies'} — next ask re-verifies.", icon="✅")
        elif status == 0:
            st.error(f"Eviction failed — gateway unreachable: {text}")
        else:
            st.error(f"Eviction rejected: HTTP {status} — {text}")
    if ac2.button("⬇️ Export .md", key=f"dl_{result.get('run_id', 'x')}"):
        _flagged_note = "" if not result.get("_flagged") else " (FLAGGED INACCURATE)"
        st.download_button(
            label="Save report",
            data=(f"# Executive Intelligence Report{_flagged_note}\n\n"
                  f"- **run_id:** {result.get('run_id')}\n"
                  f"- **question:** {question}\n"
                  f"- **outcome:** {result.get('outcome')} · grounded: "
                  f"{result.get('grounded')} · latency: {result.get('latency_s')}s\n\n"
                  f"---\n\n{result.get('answer', '')}\n"),
            file_name=f"report_{result.get('run_id', 'run')}.md",
            mime="text/markdown", key=f"save_{result.get('run_id', 'x')}")
    if ac3.button("🔏 Prove it", key=f"prove_{result.get('run_id', 'x')}",
                  help="Open the verification receipt: every claim traced to "
                       "its exact source span, recomputed deterministically "
                       "(zero LLM tokens)."):
        st.session_state.prove_run_id = result.get("run_id")
        st.session_state.prove_prov_id = result.get("provenance_run_id")
        st.rerun()
    ac4.caption(f"run_id `{result.get('run_id', '—')}` · "
                f"degraded agents: {result.get('degraded_agents') or 'none'}")

    # The Prove-It receipt explorer (opened via the 🔏 button above).
    if st.session_state.get("prove_run_id") == result.get("run_id"):
        st.divider()
        _render_receipt_explorer(st.session_state.prove_run_id, tenant,
                                 st.session_state.get("prove_prov_id"))


# ============================== SIDEBAR ====================================
with st.sidebar:
    st.header("📊 System Telemetry")
    health, ping_ms = fetch_health()
    gateway_up = bool(health) and not health.get("_error")
    if gateway_up:
        db = health.get("db", {}) or {}
        db_ok = db.get("status") != "unreachable"
        (st.success if db_ok else st.error)(
            f"{'🟢' if db_ok else '🔴'} API · Neon/pgvector {db.get('status', '?')}")
        c1, c2 = st.columns(2)
        c1.metric("API ping", f"{ping_ms:.0f} ms" if ping_ms else "—")
        c2.metric("LLM circuit", str(health.get("llm_circuit", "?")))
        c1.metric("Chunks", f"{db.get('chunks', 0):,}")
        c2.metric("Epoch", db.get("epoch", "—"))
        c1.metric("Cache entries", db.get("live_cache_entries", 0))
        c2.metric("CB failures", health.get("circuit_failures", 0))
        models = health.get("models", {}) or {}
        st.caption(f"provider **{health.get('provider', '?')}** · "
                   f"router `{models.get('router')}` · fleet `{models.get('fleet')}` · "
                   f"executive `{models.get('executive')}`")
    else:
        st.error(f"🔴 Gateway unreachable at `{API_BASE_URL}`")
        st.caption("Start it with: `uvicorn main:app --port 8000`")

    st.divider()
    tenant = st.text_input("Tenant scope", value=DEFAULT_TENANT,
                           help="RLS cache/search isolation scope.")
    if st.button("🧹 New session", use_container_width=True):
        st.session_state.messages = []
        st.session_state.history = []
        st.rerun()
    st.caption(f"gateway `{API_BASE_URL}`")
    if not ADMIN_API_KEY:
        st.caption("ℹ️ ADMIN_API_KEY not set — 👎 feedback eviction disabled.")
    st.caption("ℹ️ Figures are audited against source filings; this tool is not "
               "investment advice.")


# ============================== SESSION STATE ==============================
if "messages" not in st.session_state:
    st.session_state.messages = []        # [{role, content} | {role, result, question, tenant}]
if "history" not in st.session_state:
    st.session_state.history = []         # recent-runs rows for the dataframe
if "busy" not in st.session_state:
    st.session_state.busy = False
if "prove_run_id" not in st.session_state:
    st.session_state.prove_run_id = None


def _run_pipeline(question: str, tenant: str) -> None:
    """The ONLY place an LLM run is triggered. Streams SSE transitions into an
    st.status panel, stores the final result in session state, rerenders."""
    st.session_state.busy = True
    result: Optional[Dict[str, Any]] = None
    try:
        with st.status("Dispatching to the multi-agent engine...", expanded=True) as status:
            status.write(f"run dispatched · tenant `{tenant}`")
            nodes: List[str] = []
            try:
                for ev, payload in _stream_query(question, tenant):
                    if ev == "start":
                        status.update(label=f"Run `{payload.get('run_id', '')}` — "
                                            f"watching pipeline...")
                    elif ev == "transition":
                        nodes.append(payload.get("node", "?"))
                        status.write(f"→ {payload.get('message') or payload.get('node')}")
                    elif ev == "result":
                        result = payload
                    elif ev == "error":
                        result = {"_error": payload.get("detail", "unknown gateway error")}
            except httpx.RequestError as exc:
                result = {"_error": f"Gateway unreachable: {exc}"}

        if result and not result.get("_error"):
            st.session_state.messages.append({
                "role": "assistant", "result": result,
                "question": question, "tenant": tenant})
            st.session_state.history.insert(0, {
                "question": question[:80], "outcome": result.get("outcome"),
                "grounded": bool(result.get("grounded")),
                "cached": bool(result.get("cached")),
                "latency_s": result.get("latency_s"),
                "tokens": (result.get("usage") or {}).get("total", 0),
                "run_id": result.get("run_id")})
            del st.session_state.history[8:]
        else:
            err = (result or {}).get("_error", "no result event from gateway")
            st.session_state.messages.append({
                "role": "assistant", "result": {"_error": err},
                "question": question, "tenant": tenant})
    finally:
        st.session_state.busy = False
        st.rerun()


# ============================== CHAT FLOW ==================================
for m in st.session_state.messages:
    if m["role"] == "user":
        with st.chat_message("user"):
            st.markdown(m["content"])
    else:
        with st.chat_message("assistant", avatar="🧠"):
            if m["result"].get("_error"):
                st.error(f"Query failed: {m['result']['_error']}")
                st.caption("Check the gateway (`uvicorn main:app --port 8000`) and try again — "
                           "failed runs are never cached.")
            else:
                _render_result(m["result"], m["question"], m["tenant"])

# Input: chat_input fires ONLY on submit — typing never triggers a rerun.
prompt = st.chat_input("Ask the intelligence desk...", disabled=st.session_state.busy)

if prompt and len(prompt.strip()) >= 3:
    q = prompt.strip()
    st.session_state.messages.append({"role": "user", "content": q})
    with st.chat_message("user"):
        st.markdown(q)
    _run_pipeline(q, tenant.strip() or DEFAULT_TENANT)
elif prompt:
    st.warning("Question must be at least 3 characters.")

# Empty state: onboarding + one-click suggested questions (also great for demos).
if not st.session_state.messages:
    st.info("Ask a question and a **3-agent specialist fleet** extracts from the filings, "
            "a **CIO synthesizer** drafts the brief with inline citations, and a "
            "**zero-trust compliance auditor** verifies every figure before you see it. "
            "Unverified answers are refused — never guessed.")
    s1, s2 = st.columns(2)
    clicked = None
    for i, sq in enumerate(SUGGESTED_QUESTIONS):
        col = s1 if i % 2 == 0 else s2
        if col.button(sq, use_container_width=True, key=f"sq_{i}"):
            clicked = sq
    if clicked:
        st.session_state.messages.append({"role": "user", "content": clicked})
        with st.chat_message("user"):
            st.markdown(clicked)
        _run_pipeline(clicked, tenant.strip() or DEFAULT_TENANT)

# ============================== RECENT RUNS ================================
if st.session_state.history:
    with st.expander("🕘 Recent runs (this session)", expanded=False):
        st.dataframe(pd.DataFrame(st.session_state.history), hide_index=True,
                     use_container_width=True)

# ============================== RECEIPT LOOKUP =============================
with st.expander("🔏 Receipt Explorer — verify any run (zero LLM tokens)",
                 expanded=False):
    rid = st.text_input("run_id", placeholder="e.g. 44c14e8374dc",
                        help="Every grounded run stores a tamper-evident "
                             "receipt: claims → citations → evidence spans → "
                             "transcript slices → recomputed hashes.")
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        do_verify = st.button("Verify receipt", disabled=len(rid.strip()) < 3)
    with bcol2:
        do_export = st.button("⬇ Download Audit Certificate 🔏",
                              disabled=len(rid.strip()) < 3,
                              help="Air-gapped compliance bundle: receipt + "
                                   "evidence spans + transcripts + Ed25519 "
                                   "attestation + the stdlib-only offline "
                                   "verifier. A regulator can re-verify the "
                                   "proof without this API.")
    if do_export and len(rid.strip()) >= 3:
        try:
            r = _client().get(f"/export/{rid.strip()}",
                              headers=_auth_headers())
            if r.status_code == 200:
                st.download_button(
                    "⬇ Save audit_bundle zip", data=r.content,
                    file_name=f"audit_bundle_{rid.strip()}.zip",
                    mime="application/zip")
                st.success(f"Bundle assembled ({len(r.content):,} bytes) — "
                           "verify offline with: python verify_certificate.py")
            else:
                st.error(f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.RequestError as exc:
            st.error(f"Export failed: {exc}")
    elif do_verify and len(rid.strip()) >= 3:
        _render_receipt_explorer(rid.strip(), tenant.strip() or DEFAULT_TENANT)

# ============================== SEARCH CONSOLE =============================
with st.expander("🔍 Vector Search Console (bypasses the LLM entirely)", expanded=False):
    with st.form("search_form"):
        srow1, srow2, srow3 = st.columns([2, 1, 1])
        sq = srow1.text_input("Query", placeholder="e.g. AI infrastructure GPU deployments")
        company = srow2.selectbox("Company", ["*", "Apple", "Meta", "Tesla"])
        category = srow3.selectbox("Category", ["*", "financial", "risk", "product"])
        top_k = st.slider("Top K", 1, 20, 7)
        run_search = st.form_submit_button("Run hybrid search (RRF)", type="secondary")
    if run_search and len(sq.strip()) >= 3:
        try:
            r = _client().post("/search", json={
                "query": sq.strip(),
                "company_filter": None if company == "*" else company,
                "category_filter": None if category == "*" else category,
                "top_k": top_k, "tenant_id": tenant.strip() or DEFAULT_TENANT})
            if r.status_code == 200:
                rows = r.json().get("results", [])
                if rows:
                    st.dataframe(pd.DataFrame(rows)[[
                        "company", "category", "page", "section_title",
                        "fusion_score", "contains_table"]], hide_index=True)
                    for i, row in enumerate(rows[:5], 1):
                        with st.expander(f"[{i}] {row['company']} · "
                                         f"{row['source']} · p.{row['page']}"):
                            st.markdown(row["content"])
                else:
                    st.info("No chunks matched — try broader filters.")
            else:
                st.error(f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.RequestError as exc:
            st.error(f"Search failed: {exc}")
    elif run_search:
        st.warning("Search query must be at least 3 characters.")

st.markdown("<div class='rag-footer'>Adaptive RAG · LangGraph maker–checker · "
            "Neon/pgvector + RLS · figures verified against SEC 10-Q evidence · "
            "not investment advice</div>", unsafe_allow_html=True)
