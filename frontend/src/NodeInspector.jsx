import React, { useEffect } from "react";

// The node's architectural content — purpose + the actual rules enforced,
// from the ADRs (003, 005-007, 009-011, 015, 017). This is the
// "click the demo and learn the architecture" feature.
export const INSPECTOR = {
  cache: {
    title: "Semantic cache",
    purpose: "Near-exact replay: previously CERTIFIED answers served again at zero LLM tokens. Replays carry the original run's provenance receipt.",
    rules: [
      "Replay requires cosine similarity ≥ 0.985 — near-exact only; paraphrases pay full price and can never be served someone else's answer",
      "Same tenant, same filters, same corpus epoch — any corpus bump invalidates every cached row (soft invalidation, zero downtime)",
      "Legacy provenance-less entries are evicted on lookup (self-heal)",
    ],
  },
  router: {
    title: "Intent router",
    purpose: "One structured LLM call classifies the query — corpus / general knowledge / out-of-domain — and prunes the specialist fleet to the relevant domain.",
    rules: [
      "Fail-open: an unsure router dispatches the full fleet (the cost of a misroute is tokens, never coverage)",
      "Out-of-domain and jailbreak attempts exit here as verified refusals",
      "Pruning is default-on: single-domain questions dispatch 1 specialist at top_k≈15 — evidence-pool parity (calls shrink, evidence never does)",
    ],
  },
  premise: {
    title: "Premise probe",
    purpose: "A cheap scoped probe refuses unanswerable questions in <1s instead of burning the full pipeline on them.",
    rules: [
      "Zero corpus hits on the question's own metric terms → immediate specific refusal",
      "Scoped to the question's company — other companies' headlines can't trigger it",
      "Infrastructure errors fall through to the full pipeline (never refuse on infrastructure)",
    ],
  },
  fastpath: {
    title: "Fact FastPath",
    purpose: "The deterministic serving path (ADR-017 Phase 2): numbers are looked up from the span-anchored fact store, not generated — 0 LLM tokens for covered (entity, metric, period) triples.",
    rules: [
      "Exact-match only over dual-key reconciled fact_rows — every figure agreed by BOTH the PDF span and SEC XBRL",
      "Interpretive stems ('what drove…'), segment qualifiers ('advertising revenue'), or missing triples demote ATOMICALLY to the fleet",
      "Figures are span-verbatim by construction; citations bind to the row's own span; receipts stamp verifier=deterministic",
    ],
  },
  fleet: {
    title: "Specialist fleet",
    purpose: "Parallel specialist extraction (financial · risk · product) with [n] citations over hybrid RRF search (HNSW cosine + trigram keyword).",
    rules: [
      "Quota-class failures fail over to the armed lanes (APInex → NIM → glm-5.2); non-quota errors fail closed",
      "Multi-query expansion is conditional — fires only when the direct retrieval's confidence < 0.55",
      "Degraded agents are quarantined, surfaced on the result, and never silently trusted",
    ],
  },
  crosscheck: {
    title: "Cross-check",
    purpose: "Deterministic contradiction sweep over the specialist reports — zero tokens.",
    rules: [
      "Mentions group by (metric family, company, period, unit, basis); a >2% relative gap beyond rounding slack flags",
      "A first-seen conflict triggers exactly ONE sharpened re-search + fleet re-run",
      "Surviving conflicts are surfaced with BOTH figures and their sources — never averaged",
    ],
  },
  synthesis: {
    title: "Synthesis",
    purpose: "The executive draft with [n] inline citations — produced by the pinned executive model.",
    rules: [
      "% -Change figures are quoted verbatim from the table's own column — never self-computed",
      "Abort-on-hint: a 429 announcing ≥900s skips the doomed retry loop and refuses verified",
      "The executive is PINNED — no weaker-model failover; quota walls escalate to vetted 120B-class peers",
    ],
  },
  gates: {
    title: "5 gates + audit",
    purpose: "Five deterministic zero-token gates run before the LLM auditor sees a single token — each born from a live-caught fabrication.",
    rules: [
      "Citation bounds: every [n] must address the evidence set — fabricated by construction",
      "Unit/scale: every $ figure must reconstruct from the cited table's declared units (the millions-vs-billions guard)",
      "Growth direction: 'grew/declined' must agree with the table's own comparative columns",
      "XBRL: claims reconcile against SEC-published ground truth; disconfirmed facts → authority declined (fail-closed)",
      "Echo/injection: prompt-echo and deliberation markers reject the draft",
      "Then the LLM auditor cross-examines; rejection → bounded rewrite loop → verified refusal",
    ],
  },
  terminal: {
    title: "Certify / refuse",
    purpose: "The receipted terminal: both outcomes leave tamper-evident proof.",
    rules: [
      "Certified: SHA-256 chain (claim → evidence span → transcript → hash → PDF anchor) + Ed25519 offline bundle",
      "Refused: the receipt names the exact audit objection — refusals are receipted too",
      "The shadow ledger records the V1-vs-V2 comparison either way",
    ],
  },
};

export default function NodeInspector({ stageKey, result, receipt, onClose }) {
  const info = INSPECTOR[stageKey];
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!info) return null;
  const usage = result?.usage || {};
  const rec = receipt?.receipt || {};
  const verifierCounts = (rec.claims_json || []).reduce((acc, c) => {
    const k = c.verifier === "deterministic" ? "deterministic" : "llm_audit";
    acc[k] = (acc[k] || 0) + 1;
    return acc;
  }, {});

  const stageTelemetry = {
    cache: result?.cached ? "⚡ replayed (0 tokens)" : "miss — fresh run",
    fastpath: result?.fastpath_served
      ? "⚡ SERVED — 0 LLM tokens"
      : "demoted to fleet (uncovered or flag off)",
    crosscheck: `${(rec.contradictions_json || []).length} contradiction(s) surfaced`,
    gates: Object.entries(verifierCounts).length
      ? Object.entries(verifierCounts)
          .map(([k, n]) => `${n} ${k === "deterministic" ? "DETERMINISTIC" : "LLM-AUDIT"}`)
          .join(" · ")
      : "awaiting a run",
    terminal: result
      ? result.grounded ? "CERTIFIED · GROUNDED" : (result.outcome || "—")
      : "awaiting a run",
  }[stageKey];

  return (
    <>
      <div className="fixed inset-0 z-40 bg-ink-950/60 backdrop-blur-[2px]
                      opacity-0 transition-opacity duration-300
                      [&[data-open='true']]:opacity-100
                      pointer-events-none"
           data-open={stageKey ? "true" : "false"}
           onClick={onClose} />
      <aside
        data-open={stageKey ? "true" : "false"}
        className="drawer fixed right-0 top-0 z-50 flex h-full w-[24rem]
                   max-w-[92vw] flex-col border-l border-ink-600
                   bg-ink-900/95 shadow-2xl backdrop-blur-xl
                   data-[open='false']:pointer-events-none"
        aria-hidden={stageKey ? "false" : "true"}>
        <div className="flex items-center justify-between border-b border-ink-700 px-5 py-4">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-mist-500">
              Node inspector
            </p>
            <h3 className="text-sm font-bold text-mist-200">{info.title}</h3>
          </div>
          <button onClick={onClose}
                  className="rounded-lg p-1.5 text-mist-400 transition-colors
                             hover:bg-ink-700 hover:text-mist-200"
                  aria-label="Close inspector">
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
          <section>
            <h4 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-mist-500">
              Purpose
            </h4>
            <p className="mt-1.5 text-xs leading-relaxed text-mist-300">
              {info.purpose}
            </p>
          </section>

          <section>
            <h4 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-mist-500">
              Rules enforced
            </h4>
            <ul className="mt-1.5 space-y-2">
              {info.rules.map((r, i) => (
                <li key={i} className="flex gap-2 text-xs leading-relaxed text-mist-400">
                  <span className="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full
                                   bg-brand-500/70" />
                  {r}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h4 className="text-[10px] font-semibold uppercase tracking-[0.14em] text-mist-500">
              This run
            </h4>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              <span className="pill font-mono">{usage.llm_calls ?? 0} LLM calls</span>
              <span className="pill font-mono">{usage.total ?? 0} tokens</span>
              {result && <span className="pill font-mono">{result.latency_s}s</span>}
              {stageTelemetry && (
                <span className="pill border-brand-500/40 font-mono text-brand-400">
                  {stageTelemetry}
                </span>
              )}
              {!result && (
                <span className="pill">ask a question to populate</span>
              )}
            </div>
          </section>
        </div>
      </aside>
    </>
  );
}
