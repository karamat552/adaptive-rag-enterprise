import React, { useState } from "react";
import { getApiKey, setApiKey, getAdminKey, setAdminKey } from "../api.js";

export default function Header({ health, pingMs }) {
  const [key, setKey] = useState(getApiKey());
  const [admin, setAdmin] = useState(getAdminKey());
  const [editing, setEditing] = useState(null); // "query" | "admin" | null

  const live = !!health;
  const db = health?.db || {};
  const models = health?.models || {};

  return (
    <header className="flex flex-wrap items-center justify-between gap-3">
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

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="pill">
          <span className={`dot ${live ? "dot-live" : "dot-pending"}`} />
          {live ? "live" : "offline"}
        </span>
        {live && (
          <>
            <span className="pill font-mono" title={`router ${models.router} · fleet ${models.fleet} · executive ${models.executive}`}>
              {health.provider}
            </span>
            <span className="pill font-mono" title={`Semantic cache: ${db.live_cache_entries} live entries · circuit failures: ${health.circuit_failures}`}>
              epoch {db.epoch ?? "–"} · {db.chunks ?? "–"} chunks
            </span>
            <span className="pill font-mono" title="Failover lanes armed behind the primary">
              {health.failover?.endpoints ?? 0} lanes
            </span>
            {pingMs != null && (
              <span className="pill font-mono" title="Client-measured gateway round-trip">
                {Math.round(pingMs)}ms
              </span>
            )}
            {health.failover?.executive_pinned && (
              <span className="pill border-emerald-450/40 text-emerald-450">
                exec pinned
              </span>
            )}
          </>
        )}

        {editing === "query" ? (
          <span className="pill gap-1.5">
            <input autoFocus type="password" value={key}
                   onChange={(e) => setKey(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" &&
                     (setApiKey(key.trim()), setEditing(null))}
                   placeholder="X-API-Key"
                   className="w-32 bg-transparent font-mono text-[11px] text-mist-200
                              outline-none placeholder:text-mist-500" />
            <button onClick={() => (setApiKey(key.trim()), setEditing(null))}
                    className="font-semibold text-brand-400 hover:text-brand-500">save</button>
          </span>
        ) : (
          <button onClick={() => setEditing("query")}
                  className="pill transition-colors hover:border-brand-500/50"
                  title="Query API key — stored locally; optional when the gateway allows anonymous demo access">
            🔑 {getApiKey() ? "key set" : "api key"}
          </button>
        )}

        {editing === "admin" ? (
          <span className="pill gap-1.5">
            <input autoFocus type="password" value={admin}
                   onChange={(e) => setAdmin(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" &&
                     (setAdminKey(admin.trim()), setEditing(null))}
                   placeholder="ADMIN_API_KEY"
                   className="w-36 bg-transparent font-mono text-[11px] text-mist-200
                              outline-none placeholder:text-mist-500" />
            <button onClick={() => (setAdminKey(admin.trim()), setEditing(null))}
                    className="font-semibold text-amber-450 hover:text-amber-450/80">save</button>
          </span>
        ) : (
          <button onClick={() => setEditing("admin")}
                  className="pill transition-colors hover:border-amber-450/50"
                  title="Admin key — enables 👎 Flag Inaccurate cache eviction (stored locally, never sent except to /feedback)">
            🛡️ {getAdminKey() ? "admin set" : "admin key"}
          </button>
        )}
      </div>
    </header>
  );
}
