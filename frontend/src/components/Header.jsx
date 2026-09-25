import React, { useState } from "react";
import { getApiKey, setApiKey } from "../api.js";

export default function Header({ health }) {
  const [key, setKey] = useState(getApiKey());
  const [editing, setEditing] = useState(false);

  const save = () => { setApiKey(key.trim()); setEditing(false); };
  const live = !!health;
  const db = health?.db || {};

  return (
    <header className="flex flex-wrap items-center justify-between gap-4">
      <div className="flex items-center gap-3">
        <div className="relative flex h-10 w-10 items-center justify-center rounded-xl
                        bg-gradient-to-br from-brand-600 to-cyan-350/70 shadow-lg
                        shadow-brand-600/30">
          <svg viewBox="0 0 24 24" className="h-5 w-5 text-ink-950" fill="none"
               stroke="currentColor" strokeWidth="2.4">
            <path d="M12 3l9 9-9 9-9-9z" strokeLinejoin="round" />
            <circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none" />
          </svg>
        </div>
        <div>
          <h1 className="text-[15px] font-bold tracking-tight text-mist-200">
            Adaptive RAG <span className="shimmer-text">Enterprise</span>
          </h1>
          <p className="text-[11px] text-mist-400">
            Provably-grounded financial intelligence · Apple · Meta · Tesla · Q4 2023
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="pill">
          <span className={`dot ${live ? "dot-live" : "dot-pending"}`} />
          {live ? "backend live" : "backend offline"}
        </span>
        {live && (
          <>
            <span className="pill font-mono">{health.provider}</span>
            <span className="pill font-mono">epoch {db.epoch ?? "–"}</span>
            <span className="pill font-mono">{db.chunks ?? "–"} chunks</span>
            <span className="pill font-mono">
              {health.failover?.endpoints ?? 0} failover lanes
            </span>
            {health.failover?.executive_pinned && (
              <span className="pill border-emerald-450/40 text-emerald-450">
                exec pinned
              </span>
            )}
          </>
        )}
        {editing ? (
          <span className="pill gap-1.5">
            <input
              autoFocus
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && save()}
              placeholder="X-API-Key"
              className="w-32 bg-transparent font-mono text-[11px] text-mist-200
                         outline-none placeholder:text-mist-500"
            />
            <button onClick={save}
                    className="text-brand-400 hover:text-brand-500 font-semibold">save</button>
          </span>
        ) : (
          <button onClick={() => setEditing(true)}
                  className="pill hover:border-brand-500/50 transition-colors"
                  title="Tenant API key (stored locally; required when the gateway enforces auth)">
            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"
                 strokeWidth="2">
              <path strokeLinecap="round" strokeLinejoin="round"
                    d="M15 7a2 2 0 012 2m3-2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {getApiKey() ? "key set" : "api key"}
          </button>
        )}
      </div>
    </header>
  );
}
