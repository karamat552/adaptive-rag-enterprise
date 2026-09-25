import React, { useState } from "react";

export default function QueryConsole({ onAsk, streaming, examples, fastpathFlash }) {
  const [q, setQ] = useState("");

  const submit = () => {
    if (!streaming) { onAsk(q); }
  };

  return (
    <section className="glass animate-rise relative overflow-hidden p-5 sm:p-7">
      {fastpathFlash && (
        <div className="pointer-events-none absolute inset-x-0 top-0 h-[2px]
                        bg-gradient-to-r from-transparent via-cyan-350 to-transparent" />
      )}
      <label htmlFor="q" className="block text-[11px] font-semibold uppercase tracking-[0.14em] text-mist-400">
        Executive query
      </label>
      <div className="mt-3 flex flex-col gap-3 sm:flex-row">
        <div className="relative flex-1">
          <input
            id="q"
            value={q}
            disabled={streaming}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="Ask for any figure or insight from the Q4 2023 filings…"
            className="w-full rounded-xl border border-ink-600 bg-ink-850/80 px-4 py-3
                       text-sm text-mist-200 shadow-inner outline-none transition-colors
                       placeholder:text-mist-500 focus:border-brand-500/70
                       focus:ring-2 focus:ring-brand-500/25 disabled:opacity-50"
          />
          {streaming && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 font-mono text-[10px]
                             text-brand-400 animate-pulse-soft">streaming…</span>
          )}
        </div>
        <button
          onClick={submit}
          disabled={streaming || q.trim().length < 3}
          className="group inline-flex items-center justify-center gap-2 rounded-xl
                     bg-gradient-to-r from-brand-600 to-brand-500 px-6 py-3 text-sm
                     font-semibold text-white shadow-lg shadow-brand-600/25 transition-all
                     hover:shadow-brand-600/40 hover:brightness-110
                     disabled:cursor-not-allowed disabled:opacity-40"
        >
          {streaming ? (
            <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3"
                      className="opacity-25" />
              <path d="M22 12a10 10 0 00-10-10" stroke="currentColor" strokeWidth="3"
                    strokeLinecap="round" />
            </svg>
          ) : (
            <svg className="h-4 w-4 transition-transform group-hover:translate-x-0.5"
                 viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
              <path strokeLinecap="round" strokeLinejoin="round"
                    d="M13 5l7 7-7 7M5 12h14" />
            </svg>
          )}
          {streaming ? "Running" : "Ask"}
        </button>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-mist-500">Try:</span>
        {examples.map((ex) => (
          <button
            key={ex}
            disabled={streaming}
            onClick={() => { setQ(ex); onAsk(ex); }}
            className="pill transition-colors hover:border-brand-500/60 hover:text-mist-200
                       disabled:opacity-40"
          >
            {ex}
          </button>
        ))}
      </div>
    </section>
  );
}
