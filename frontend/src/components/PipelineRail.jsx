import React, { useEffect, useMemo, useState } from "react";

// The visual stage rail, mapped from the graph's real node names. The SSE
// stream reports every node the run touches; the rail lights stages in order.
const STAGES = [
  { key: "cache",      label: "Semantic cache",  nodes: ["cache_check"], hint: "Near-exact replay check" },
  { key: "router",     label: "Intent router",   nodes: ["gateway"], hint: "Corpus / general / out-of-domain" },
  { key: "premise",    label: "Premise probe",   nodes: ["premise"], hint: "Answerable-at-all check (<1s refusals)" },
  { key: "fastpath",   label: "Fact FastPath",   nodes: ["fact_fastpath"], hint: "Deterministic 0-token lookup", fast: true },
  { key: "fleet",      label: "Specialist fleet", nodes: ["exec_db"], hint: "Parallel extraction agents" },
  { key: "crosscheck", label: "Cross-check",     nodes: ["cross_check", "sharpen"], hint: "Deterministic contradiction sweep" },
  { key: "synthesis",  label: "Synthesis",       nodes: ["csuite_synth"], hint: "Executive draft with [n] citations" },
  { key: "gates",      label: "5 gates + audit", nodes: ["validate", "rewrite"], hint: "Zero-token gates, then the LLM auditor" },
  { key: "terminal",   label: "Certify / refuse", nodes: ["verified_refusal", "shadow_fact", "abandon"], hint: "Receipted either way" },
];

export default function PipelineRail({ transitions, streaming, fastpath }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!streaming) return;
    const t0 = performance.now();
    const t = setInterval(() => setElapsed((performance.now() - t0) / 1000), 100);
    return () => clearInterval(t);
  }, [streaming]);

  const touched = useMemo(
    () => new Set(transitions.map((t) => t.node)),
    [transitions]
  );

  const activeIdx = useMemo(() => {
    let last = -1;
    STAGES.forEach((s, i) => { if (s.nodes.some((n) => touched.has(n))) last = i; });
    return last;
  }, [touched]);

  const seen = new Set();
  const messages = transitions.filter((t) => {
    if (seen.has(t.message) || !t.message) return false;
    seen.add(t.message);
    return true;
  }).slice(-6);

  return (
    <section className="glass animate-rise p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist-400">
          Live pipeline
        </h2>
        {streaming && (
          <span className="pill font-mono text-brand-400">
            <span className="dot dot-pending" /> {elapsed.toFixed(1)}s
          </span>
        )}
      </div>

      <ol className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-5 lg:grid-cols-9">
        {STAGES.map((s, i) => {
          const done = touched.size > 0 && i < activeIdx;
          const active = i === activeIdx && streaming;
          const passed = touched.size > 0 && i === activeIdx && !streaming;
          return (
            <li key={s.key} title={s.hint} className="group relative">
              <div
                className={[
                  "rounded-lg border px-2 py-2.5 text-center transition-all duration-300",
                  done || passed
                    ? "border-emerald-450/40 bg-emerald-450/10"
                    : active
                      ? "border-brand-500/70 bg-brand-500/10 shadow-lg shadow-brand-600/20"
                      : "border-ink-600 bg-ink-850/60 opacity-60",
                ].join(" ")}
              >
                <div className="mx-auto flex h-5 w-5 items-center justify-center">
                  {done || passed ? (
                    <svg className="h-3.5 w-3.5 text-emerald-450" viewBox="0 0 24 24"
                         fill="none" stroke="currentColor" strokeWidth="3">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                    </svg>
                  ) : active ? (
                    <span className="dot dot-pending" style={{ background: "#818cf8" }} />
                  ) : (
                    <span className="h-1.5 w-1.5 rounded-full bg-ink-500" />
                  )}
                </div>
                <div className={[
                  "mt-1.5 text-[10px] font-medium leading-tight",
                  done || passed ? "text-emerald-450/90"
                    : active ? "text-brand-400"
                      : "text-mist-500",
                ].join(" ")}>
                  {s.label}
                </div>
              </div>
              {active && (
                <span className="absolute -bottom-1 left-1/2 h-[2px] w-8 -translate-x-1/2
                                 rounded-full bg-gradient-to-r from-transparent via-brand-400 to-transparent
                                 animate-flow"
                      style={{ backgroundSize: "200% 100%" }} />
              )}
            </li>
          );
        })}
      </ol>

      {fastpath && (
        <div className="mt-3 flex items-center gap-2 rounded-lg border border-cyan-350/30
                        bg-cyan-350/5 px-3 py-2 text-[11px] text-cyan-350">
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
          Deterministic FastPath engaged — answered from the span-anchored fact
          store at zero LLM tokens.
        </div>
      )}

      {messages.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5 border-t border-ink-700 pt-3">
          {messages.map((m, i) => (
            <span key={`${m.message}-${i}`}
                  className="pill font-mono text-[10px] text-mist-400">
              {m.message}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
