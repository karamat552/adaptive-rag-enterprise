import React, { useState } from "react";

const verifierClass = (v) =>
  v === "deterministic" || v === "python_certified"
    ? { label: v === "deterministic" ? "DETERMINISTIC" : "PYTHON-CERTIFIED",
        cls: "border-cyan-350/40 text-cyan-350 bg-cyan-350/5" }
    : { label: "LLM AUDIT", cls: "border-brand-500/40 text-brand-400 bg-brand-500/5" };

export default function ReceiptExplorer({ receipt, runId }) {
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState("claims"); // claims | evidence

  const v = receipt?.verification || {};
  const rec = receipt?.receipt || {};
  const claims = rec.claims_json || [];
  const evidence = rec.evidence_json || [];
  const verified = v.verified === true;
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
    <section className="glass animate-rise overflow-hidden">
      <button onClick={() => setOpen(!open)}
              className="flex w-full flex-wrap items-center justify-between gap-2 px-5 py-3
                         text-left transition-colors hover:bg-ink-800/40">
        <div className="flex items-center gap-2">
          <svg className="h-4 w-4 text-brand-400" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="1.8">
            <path strokeLinecap="round" strokeLinejoin="round"
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.6L19 9.4V19a2 2 0 01-2 2z" />
          </svg>
          <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist-300">
            Verification receipt
          </span>
          <span className={`pill font-mono ${verified
              ? "border-emerald-450/40 text-emerald-450"
              : "border-rose-450/40 text-rose-450"}`}>
            {verified ? `chain verified · ${v.links_ok}/${v.links_checked} links` : "not verified"}
          </span>
          <span className="pill font-mono">{claims.length} claims · {evidence.length} evidence</span>
          {rec.model_id && <span className="pill font-mono">model {rec.model_id}</span>}
          {rec.model_id === null && claims.some((c) => c.verifier === "deterministic") && (
            <span className="pill border-cyan-350/40 text-cyan-350">no-LLM answer</span>
          )}
        </div>
        <svg className={`h-4 w-4 text-mist-400 transition-transform ${open ? "rotate-180" : ""}`}
             viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-ink-700 px-5 py-4">
          <div className="mb-3 flex gap-1.5">
            {["claims", "evidence"].map((t) => (
              <button key={t} onClick={() => setTab(t)}
                      className={`rounded-lg px-3 py-1 text-[11px] font-medium transition-colors
                        ${tab === t
                          ? "bg-brand-500/15 text-brand-400"
                          : "text-mist-500 hover:text-mist-300"}`}>
                {t}
              </button>
            ))}
          </div>

          {tab === "claims" && (
            <ul className="space-y-2">
              {claims.map((c, i) => {
                const vc = verifierClass(c.verifier);
                return (
                  <li key={i} className="rounded-lg border border-ink-600 bg-ink-850/60 px-3 py-2">
                    <div className="flex items-start justify-between gap-3">
                      <p className="flex-1 text-xs leading-relaxed text-mist-300">
                        {renderClaim(c.claim)}
                      </p>
                      <div className="flex shrink-0 flex-col items-end gap-1">
                        <span className={`pill ${vc.cls}`}>{vc.label}</span>
                        {(c.citations || []).length > 0 && (
                          <span className="font-mono text-[10px] text-mist-500">
                            cites [{c.citations.join(", ")}]
                          </span>
                        )}
                      </div>
                    </div>
                    {c.deterministic_certification && (
                      <p className="mt-1.5 font-mono text-[10px] text-cyan-350/80">
                        deterministic_certification: {c.deterministic_certification}
                      </p>
                    )}
                  </li>
                );
              })}
              {!claims.length && (
                <p className="text-xs text-mist-500">No claims recorded (refusal receipts carry none).</p>
              )}
            </ul>
          )}

          {tab === "evidence" && (
            <ul className="space-y-2">
              {evidence.map((e, i) => (
                <li key={i}
                    className="rounded-lg border border-ink-600 bg-ink-850/60 px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2 text-[11px]">
                    <span className="font-mono text-brand-400">[{i + 1}]</span>
                    <span className="font-semibold text-mist-300">{e.company}</span>
                    <span className="text-mist-500">{e.source} · p.{e.page}</span>
                    <span className="font-mono text-[10px] text-mist-500">
                      span {e.char_start}–{e.char_end}
                    </span>
                    {e.arithmetic_ok === true && (
                      <span className="pill border-emerald-450/30 text-emerald-450/80">arith ✓</span>
                    )}
                    {e.arithmetic_ok === false && (
                      <span className="pill border-amber-450/30 text-amber-450/80">arith flag</span>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 whitespace-pre-wrap font-mono text-[10px]
                                leading-relaxed text-mist-500">
                    {(e.content || "").slice(0, 220)}…
                  </p>
                </li>
              ))}
              {!evidence.length && (
                <p className="text-xs text-mist-500">No evidence recorded.</p>
              )}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function renderClaim(text) {
  const parts = [];
  const re = /\[([0-9,]+)\]/g;
  let last = 0, m;
  while ((m = re.exec(text || "")) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    parts.push(
      <sup key={parts.length}
           className="mx-0.5 rounded bg-brand-500/15 px-1 font-mono text-[10px] text-brand-400">
        {m[1]}
      </sup>
    );
    last = m.index + m[0].length;
  }
  if (last < (text || "").length) parts.push(text.slice(last));
  return parts;
}
