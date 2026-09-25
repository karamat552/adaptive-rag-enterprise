import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import Header from "./components/Header.jsx";
import QueryConsole from "./components/QueryConsole.jsx";
import PipelineRail from "./components/PipelineRail.jsx";
import AnswerPanel from "./components/AnswerPanel.jsx";
import ReceiptExplorer from "./components/ReceiptExplorer.jsx";
import { fetchHealth, fetchReceipt, streamQuery } from "./api.js";

const EXAMPLES = [
  "What was Apple's total net sales in Q4 2023?",
  "Compare Apple's and Tesla's revenue in Q4 2023.",
  "What was Tesla's diluted EPS in Q4 2023?",
  "What are the main risks Tesla faces in its energy business?",
];

export default function App() {
  const [phase, setPhase] = useState("idle"); // idle | streaming | done | error
  const [question, setQuestion] = useState("");
  const [transitions, setTransitions] = useState([]);
  const [result, setResult] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [error, setError] = useState(null);
  const [health, setHealth] = useState(null);
  const abortRef = useRef(null);

  const refreshHealth = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  useEffect(() => {
    refreshHealth();
    const t = setInterval(refreshHealth, 30000);
    return () => clearInterval(t);
  }, [refreshHealth]);

  const ask = useCallback(async (q) => {
    if (!q || q.trim().length < 3 || phase === "streaming") return;
    abortRef.current?.abort();
    setQuestion(q.trim());
    setPhase("streaming");
    setTransitions([]);
    setResult(null);
    setReceipt(null);
    setError(null);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    await streamQuery({
      question: q.trim(),
      signal: ctrl.signal,
      onTransition: (t) => setTransitions((prev) => [...prev, t]),
      onResult: async (r) => {
        setResult(r);
        setPhase("done");
        const rid = r.provenance_run_id || r.run_id;
        try { setReceipt(await fetchReceipt(rid)); } catch { setReceipt(null); }
      },
      onError: (e) => {
        if (e.name === "AbortError") return;
        setError(e.message || "stream failed");
        setPhase("error");
      },
    }).catch((e) => {
      if (e.name !== "AbortError") { setError(e.message || "stream failed"); setPhase("error"); }
    });
  }, [phase]);

  const fastpathFlash = useMemo(
    () => transitions.some((t) => t.node === "fact_fastpath"),
    [transitions]
  );

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-6 sm:px-6">
      <Header health={health} />

      <main className="flex flex-1 flex-col gap-6">
        <QueryConsole
          onAsk={ask}
          streaming={phase === "streaming"}
          examples={EXAMPLES}
          fastpathFlash={fastpathFlash}
        />

        {(phase !== "idle" || transitions.length > 0) && (
          <PipelineRail
            transitions={transitions}
            streaming={phase === "streaming"}
            fastpath={fastpathFlash}
          />
        )}

        {phase === "error" && (
          <div className="glass animate-rise border-rose-450/40 p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-rose-450">
              <span className="dot" style={{ background: "#f43f5e" }} />
              Request failed — the gateway refused or could not be reached
            </div>
            <p className="mt-2 font-mono text-xs text-mist-400">{error}</p>
            <p className="mt-2 text-xs text-mist-400">
              The system fails closed: it refuses rather than serve an unverified answer.
              Set a valid API key in the header if the gateway enforces tenant auth.
            </p>
          </div>
        )}

        {result && (
          <>
            <AnswerPanel result={result} question={question} />
            <ReceiptExplorer receipt={receipt} runId={result.provenance_run_id || result.run_id} />
          </>
        )}

        {phase === "idle" && (
          <section className="grid animate-rise gap-4 md:grid-cols-3">
            {[
              {
                t: "Provably grounded",
                d: "Every certified answer carries a cryptographic receipt — claim, evidence span, page transcript, SHA-256 — re-verifiable offline with zero trust.",
                icon: "M9 12l2 2 4-4m5 2a9 9 0 11-18 0 9 9 0 0118 0z",
              },
              {
                t: "Deterministic fast path",
                d: "Covered financial figures are looked up from a dual-key fact store — SEC XBRL reconciled — at zero LLM tokens, with template-verified citations.",
                icon: "M13 10V3L4 14h7v7l9-11h-7z",
              },
              {
                t: "Fail-closed, always",
                d: "Five zero-token gates run before the auditor. If anything cannot be verified, the system refuses — it never serves an unverified answer.",
                icon: "M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4",
              },
            ].map((c) => (
              <div key={c.t} className="glass glass-hover p-5">
                <svg className="h-5 w-5 text-brand-400" fill="none" viewBox="0 0 24 24"
                     stroke="currentColor" strokeWidth="1.8">
                  <path strokeLinecap="round" strokeLinejoin="round" d={c.icon} />
                </svg>
                <h3 className="mt-3 text-sm font-semibold text-mist-200">{c.t}</h3>
                <p className="mt-1.5 text-xs leading-relaxed text-mist-400">{c.d}</p>
              </div>
            ))}
          </section>
        )}
      </main>

      <footer className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-ink-700 pt-4 text-[11px] text-mist-400">
        <span>
          Adaptive RAG Enterprise · multi-agent RAG over Q4-2023 SEC filings ·
          every refusal receipted
        </span>
        <span className="font-mono">
          {health ? `backend ${health.service} · epoch ${health.db?.epoch}` : "backend offline"}
        </span>
      </footer>
    </div>
  );
}
