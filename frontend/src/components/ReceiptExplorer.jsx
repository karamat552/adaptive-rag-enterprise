import React, { useState } from "react";
import { useToast } from "../toast.jsx";

const verifierClass = (v) =>
  v === "deterministic" || v === "python_certified"
    ? { label: v === "deterministic" ? "DETERMINISTIC" : "PYTHON-CERTIFIED",
        cls: "border-cyan-350/40 text-cyan-350 bg-cyan-350/5" }
    : { label: "LLM AUDIT", cls: "border-brand-500/40 text-brand-400 bg-brand-500/5" };

// The claimed span highlighted inside the FULL page transcript — the trust
// centerpiece: the reader watches the exact bytes the claim rests on.
// Bounds-defensive (mirrors app.py's _highlight_transcript).
function HighlightedTranscript({ transcript, start, end }) {
  const t = transcript || "";
  if (typeof start !== "number" || typeof end !== "number" ||
      !(0 <= start && start < end && end <= t.length)) {
    return <p className="whitespace-pre-wrap font-mono text-[11px] text-mist-500">{t}</p>;
  }
  return (
    <p className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-mist-400">
      {t.slice(0, start)}
      <mark className="hl-mark">{t.slice(start, end)}</mark>
      {t.slice(end)}
    </p>
  );
}

function Expander({ title, children, defaultOpen = false, tone = "" }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`rounded-lg border bg-ink-850/60 ${tone || "border-ink-600"}`}>
      <div className="flex items-center gap-1 px-3 py-2">
        <button onClick={() => setOpen(!open)}
                className="flex flex-1 items-center gap-2 text-left
                           text-xs font-medium text-mist-300 transition-colors
                           hover:text-mist-200">
          <svg className={`h-3.5 w-3.5 shrink-0 text-mist-500 transition-transform
                           ${open ? "rotate-90" : ""}`}
               viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          <span className="flex-1">{title}</span>
        </button>
      </div>
      <div className="expander-grid" data-open={open}>
        <div className="px-3 pb-2.5 text-xs text-mist-400">
          {children}
        </div>
      </div>
    </div>
  );
}

export default function ReceiptExplorer({ receipt, runId }) {
  const v = receipt?.verification || {};
  const rec = receipt?.receipt || {};
  const claims = rec.claims_json || [];
  const evidence = rec.evidence_json || [];
  const transcripts = v.transcripts || {};
  const attribution = v.attribution || {};
  const sources = receipt?.sources || [];
  const contradictions = rec.contradictions_json || [];
  const verified = v.verified === true;
  const resolvedFrom = receipt?.resolved_from_provenance;
  const notify = useToast();
  const copyEv = async (e, n) => {
    try {
      await navigator.clipboard.writeText(String(e.content || ""));
      notify(`Evidence [${n}] copied to clipboard`, "cyan");
    } catch {
      notify("Clipboard unavailable in this context", "amber");
    }
  };

  if (!receipt) {
    return (
      <section className="glass p-4 text-[11px] text-mist-400">
        Receipt for run <span className="font-mono text-mist-300">{runId}</span> is not
        available (refusals 404 at /verify; cache replays resolve via their
        provenance run).
      </section>
    );
  }

  return (
    <section className={`glass animate-rise overflow-hidden ${verified ? "seal-trace" : ""}`}>
      <div className="border-b border-ink-700 px-5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist-300">
            🔏 Verification receipt — Prove-it view
          </span>
          <span className={`pill font-mono ${verified
              ? "border-emerald-450/40 text-emerald-450"
              : "border-rose-450/40 text-rose-450"}`}>
            {verified ? `chain verified · ${v.links_ok}/${v.links_checked} links` : "CHAIN BROKEN"}
          </span>
          <span className="pill font-mono">{claims.length} claims · {evidence.length} evidence</span>
          {rec.model_id && <span className="pill font-mono">model {rec.model_id}</span>}
          {rec.model_id === null && claims.some((c) => c.verifier === "deterministic") && (
            <span className="pill border-cyan-350/40 text-cyan-350">no-LLM answer</span>
          )}
        </div>
        {resolvedFrom && (
          <p className="mt-2 text-[11px] text-brand-400/90">
            ⚡ Cached answer — its proof lives under the original certification
            run <span className="font-mono">{resolvedFrom}</span>, resolved
            automatically from this replay's provenance.
          </p>
        )}
        <p className={`mt-2 text-[11px] ${verified ? "text-emerald-450/80" : "text-rose-450"}`}>
          {verified
            ? "Every claim traces to an exact transcript span whose sha256(company⊣source⊣page⊣slice) recomputes identically. This proof cost zero LLM tokens."
            : "⚠️ The deterministic chain REJECTED this receipt — at least one claim cannot be traced to source bytes. Do not trust it."}
        </p>
      </div>

      <div className="space-y-4 px-5 py-4">
        {sources.length > 0 && (
          <div>
            <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-mist-500">
              🔗 Provenance spine
            </h4>
            <div className="grid gap-2 sm:grid-cols-3">
              {sources.map((s) => (
                <div key={s.source} className="rounded-lg border border-ink-600 bg-ink-850/60 px-3 py-2">
                  <p className="text-xs font-semibold text-mist-300">{s.source}</p>
                  <p className="mt-0.5 font-mono text-[10px] text-mist-500">
                    pdf sha256 {(s.pdf_sha256 || "").slice(0, 16)}… · {s.chunks ?? "?"} chunks
                  </p>
                </div>
              ))}
            </div>
          </div>
        )}

        {contradictions.length > 0 && (
          <div>
            <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-amber-450/90">
              ⚠️ Contradictions surfaced ({contradictions.length}) — both figures shown, never averaged
            </h4>
            <div className="space-y-1.5">
              {contradictions.map((c, i) => (
                <p key={i} className="rounded-lg border border-amber-450/25 bg-amber-450/5
                                     px-3 py-1.5 text-[11px] text-amber-450/90">
                  {c.company} · {c.family} ({c.period || "period n/a"}): conflicting
                  values {JSON.stringify(c.values)} — gap{" "}
                  {Math.round((parseFloat(c.rel_gap) || 0) * 1000) / 10}%
                </p>
              ))}
            </div>
          </div>
        )}

        <div>
          <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-mist-500">
            🧾 Claims → Evidence → Source bytes
          </h4>
          <div className="space-y-2">
            {claims.map((claim, i) => {
              const cites = claim.citations || [];
              const vc = verifierClass(claim.verifier);
              return (
                <Expander
                  key={i}
                  defaultOpen={i === 0}
                  title={`Claim ${i + 1} ${cites.length
                      ? cites.map((n) => `[${n}]`).join(" ")
                      : "(uncited)"} — ${String(claim.claim || "").slice(0, 84)}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`pill ${vc.cls}`}>{vc.label}</span>
                    {claim.deterministic_certification && (
                      <span className="font-mono text-[10px] text-cyan-350/80">
                        deterministic_certification: {claim.deterministic_certification}
                      </span>
                    )}
                  </div>
                  <p className="mt-2 text-xs leading-relaxed text-mist-300">
                    {claim.claim}
                  </p>
                  {cites.length === 0 && (
                    <p className="mt-2 rounded border border-amber-450/25 bg-amber-450/5
                                 px-2 py-1 text-[11px] text-amber-450/90">
                      ⚠️ This claim rests on no cited evidence.
                    </p>
                  )}
                  {cites.map((n) => {
                    if (!(1 <= n && n <= evidence.length)) {
                      return (
                        <p key={n} className="mt-2 rounded border border-rose-450/30
                                            bg-rose-450/5 px-2 py-1 text-[11px] text-rose-450">
                          Citation [{n}] points outside the evidence set — forged by construction.
                        </p>
                      );
                    }
                    const e = evidence[n - 1];
                    const tkey = `${e.source}\u001f${e.page}`;
                    const tr = (transcripts[tkey] || {}).transcript || "";
                    const truth = attribution[e.chunk_hash] || {};
                    return (
                      <div key={n} className="mt-3 rounded-lg border border-ink-600 bg-ink-900/60 p-3">
                        <div className="flex flex-wrap items-center gap-2 text-[11px]">
                          <span className="font-semibold text-mist-300">Evidence [{n}]</span>
                          <span className="font-mono text-brand-400">{e.company}</span>
                          <span className="text-mist-500">{e.source} · p.{e.page}</span>
                          <button onClick={() => copyEv(e, n)}
                                  title="Copy evidence chunk"
                                  className="pill hover:border-cyan-350/50 hover:text-cyan-350">
                            ⧉ copy
                          </button>
                          {e.contains_table && (
                            <span className={`pill ${e.arithmetic_ok === true
                                ? "border-emerald-450/30 text-emerald-450/80"
                                : e.arithmetic_ok === false
                                  ? "border-amber-450/30 text-amber-450/80" : ""}`}>
                              📊 table{e.arithmetic_ok === true
                                ? " · ✅ sums verified"
                                : e.arithmetic_ok === false ? " · ⚠️ arithmetic FLAG" : ""}
                            </span>
                          )}
                        </div>
                        <p className="mt-1 font-mono text-[10px] text-mist-500">
                          chunk sha256 {String(e.chunk_hash).slice(0, 20)}… ·
                          span {e.char_start}–{e.char_end} ·
                          DB-truth company {truth.company || "—"}
                        </p>
                        <div className="mt-2 max-h-56 overflow-y-auto rounded-lg border
                                    border-ink-700 bg-ink-950/60 p-3">
                          {tr ? (
                            <>
                              <p className="mb-1 text-[10px] text-mist-500">
                                Page transcript — claimed span highlighted:
                              </p>
                              <HighlightedTranscript transcript={tr}
                                                     start={e.char_start}
                                                     end={e.char_end} />
                            </>
                          ) : (
                            <p className="text-[11px] text-mist-500">
                              No stored transcript for this page (pre-2.1 evidence —
                              chunk-level tracing only).
                            </p>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </Expander>
              );
            })}
            {claims.length === 0 && (
              <p className="text-xs text-mist-500">
                No claims recorded (refusal receipts carry none).
              </p>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
