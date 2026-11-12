import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import Header from "./components/Header.jsx";
import QueryConsole from "./components/QueryConsole.jsx";
import PipelineRail from "./components/PipelineRail.jsx";
import AnswerPanel from "./components/AnswerPanel.jsx";
import ReceiptExplorer from "./components/ReceiptExplorer.jsx";
import VerifyViewer from "./components/VerifyViewer.jsx";
import { fetchHealth, fetchReceipt, streamQuery } from "./api.js";
import { ToastProvider } from "./toast.jsx";

const EXAMPLES = [
  "What was Apple's total net sales in Q4 2023?",
  "Compare Apple's and Tesla's revenue in Q4 2023.",
  "What was Tesla's diluted EPS in Q4 2023?",
  "What are the main risks Tesla faces in its energy business?",
];

// a real production run, pre-filled so a first-time visitor can verify a
// receipt without hunting for an id (the README's demo receipt)
const SAMPLE_RUN_ID = "250f578be128";

export default function App() {
  const [view, setView] = useState("console"); // console | verify
  const [phase, setPhase] = useState("idle"); // idle | streaming | done | error
  const [question, setQuestion] = useState("");
  const [transitions, setTransitions] = useState([]);
  const [result, setResult] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [error, setError] = useState(null);
  const [health, setHealth] = useState(null);
  const [pingMs, setPingMs] = useState(null);
  const abortRef = useRef(null);

  const refreshHealth = useCallback(() => {
    const t0 = performance.now();
    fetchHealth().then((h) => {
      setHealth(h);
      setPingMs(performance.now() - t0);
    }).catch(() => setHealth(null));
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

  // RESULT-ANCHORED TRUTH (run 521bec0cf161 lesson): the fact_fastpath
  // node executes — and emits a transition — on every flag-on query, even
  // when it demotes to the fleet or the run refuses. Node presence in the
  // stream never means "served"; only the result's fastpath_served does.
  const fastpathFlash = useMemo(
    () => result?.fastpath_served === true,
    [result]
  );

  return (
    <ToastProvider>
    <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-6 sm:px-6">
      <Header health={health} pingMs={pingMs} view={view} onView={setView} />

      <p
        role="note"
        className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-center text-xs text-amber-200/90"
      >
        Not financial advice — this console is a technical demonstration of a
        retrieval-and-verification pipeline over public SEC filings.
      </p>

      {view === "verify" ? (
        <main className="flex flex-1 flex-col gap-6">
          <VerifyViewer />
        </main>
      ) : (
      <>
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
            result={result}
            receipt={receipt}
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

        {phase === "streaming" && (
          <section className="glass animate-rise p-5">
            <div className="flex items-center gap-2 text-xs text-mist-300">
              <span className="dot dot-pending" />
              <span className="font-medium">
                {transitions.length
                  ? (transitions[transitions.length - 1].message ||
                     transitions[transitions.length - 1].node)
                  : "Routing query…"}
              </span>
              <span className="font-mono text-[10px] text-mist-500">
                — watch the rail
              </span>
            </div>
            <div className="mt-4 space-y-2.5">
              <div className="skeleton-bar h-3 w-11/12" />
              <div className="skeleton-bar h-3 w-8/12" />
              <div className="skeleton-bar h-3 w-9/12" />
            </div>
          </section>
        )}

        {result && (
          <>
            <AnswerPanel result={result} question={question} />
            <ReceiptExplorer receipt={receipt} runId={result.provenance_run_id || result.run_id} />
          </>
        )}

        {phase === "idle" && (
          <>
          <section className="bento animate-rise">
            {/* WIDE — the system's promise, measured */}
            <div className="glass glass-hover bento-tile-wide p-5">
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 font-mono text-[11.5px]">
                <span className="text-emerald-450">0 fabricated answers</span>
                <span className="text-mist-300">~100ms instant answers</span>
                <span className="text-mist-300">20 verified metrics</span>
                <span className="text-mist-300">every refusal receipted</span>
              </div>
              <p className="mt-2 text-[11px] text-mist-400">
                Measured on the live pipeline — not aspirational copy.
              </p>
              {health && (
                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-ink-700 pt-3 font-mono text-[10.5px] text-mist-400">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="dot dot-live" /> LIVE
                  </span>
                  <span>epoch {health.db?.epoch ?? "–"}</span>
                  <span>{health.db?.chunks ?? "–"} chunks indexed</span>
                  <span>boot smoke: {(health.boot_smoke || {}).status || "—"}</span>
                  <span className="text-emerald-450/80">
                    verify any receipt in the 🔍 tab
                  </span>
                </div>
              )}
            </div>

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

            {/* A certified example — show, don't tell */}
            <div className="glass glass-hover p-5">
              <span className="pill animate-pop">✓ CERTIFIED · DETERMINISTIC</span>
              <div className="mt-3 font-mono text-3xl font-bold tracking-tight text-mist-200">
                $89,498M
              </div>
              <div className="mt-1 font-mono text-[10px] uppercase tracking-wider text-mist-400">
                Apple · Total net sales · Q4 FY2023
              </div>
              <p className="mt-2 text-xs text-mist-400">
                Served from the dual-verified fact store — no AI, no tokens,
                citation bound to the filing page.
              </p>
            </div>

            <div className="glass glass-hover p-5">
              <svg className="h-5 w-5 text-brand-400" fill="none" viewBox="0 0 24 24"
                   stroke="currentColor" strokeWidth="1.8">
                <path strokeLinecap="round" strokeLinejoin="round"
                      d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4" />
              </svg>
              <h3 className="mt-3 text-sm font-semibold text-mist-200">Fail-closed, always</h3>
              <p className="mt-1.5 text-xs leading-relaxed text-mist-400">
                Five zero-token gates run before the auditor. If anything cannot
                be verified, the system refuses — it never serves an unverified answer.
              </p>
            </div>

            {/* WIDE — the coverage, the real reconciled triples */}
            <div className="glass glass-hover bento-tile-wide p-5">
              <h3 className="text-sm font-semibold text-mist-200">What it answers today</h3>
              <div className="mt-3 space-y-2 font-mono text-[10.5px] text-mist-400">
                <p><span className="text-mist-200">APPLE</span> · revenue · net income · gross margin · operating income · R&D · pretax income · SG&A</p>
                <p><span className="text-mist-200">META</span> · revenue · net income · operating income · R&D · pretax income</p>
                <p><span className="text-mist-200">TESLA</span> · revenue · net income · EPS · gross margin · operating income · R&D · pretax income · SG&A</p>
              </div>
              <p className="mt-2.5 text-[11px] text-mist-400">
                20 dual-verified fact triples — PDF text reconciled against SEC
                XBRL — and the corpus grows deterministically.
              </p>
            </div>
          </section>

          {/* THE HOW-IT-WORKS JOURNEY — the landing section a first-time
              visitor reads before they trust the console with a question */}
          <section className="glass animate-rise p-5" aria-labelledby="how-title">
            <h2 id="how-title" className="text-sm font-semibold text-mist-200">
              How an answer earns its badge
            </h2>
            <p className="mt-1 text-xs text-mist-400">
              The same four steps, every question, no exceptions.
            </p>
            <ol className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {[
                {
                  n: "01",
                  t: "You ask",
                  d: "A natural-language question over the indexed Q4-2023 filings — no keywords, no query language.",
                },
                {
                  n: "02",
                  t: "The gate decides",
                  d: "A deterministic coverage check routes covered metrics to the zero-token fact store; everything else goes to the retrieval fleet.",
                },
                {
                  n: "03",
                  t: "Evidence binds",
                  d: "Claims anchor to filing pages and are reconciled against SEC XBRL. Five zero-token gates and the auditor judge every one.",
                },
                {
                  n: "04",
                  t: "You get proof",
                  d: "A certified answer with its cryptographic receipt — or a refusal that is receipted too. Nothing in between.",
                },
              ].map((s) => (
                <li key={s.n} className="rounded-xl border border-ink-700 bg-ink-950/40 p-3.5">
                  <span className="font-mono text-[10px] tracking-widest text-brand-400">{s.n}</span>
                  <h3 className="mt-1 text-xs font-semibold text-mist-200">{s.t}</h3>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-mist-400">{s.d}</p>
                </li>
              ))}
            </ol>
          </section>

          {/* THE FAQ — the questions every skeptical visitor actually has */}
          <section className="glass animate-rise p-5" aria-labelledby="faq-title">
            <h2 id="faq-title" className="text-sm font-semibold text-mist-200">
              The honest questions
            </h2>
            <div className="mt-3 space-y-2">
              {[
                {
                  q: "Can the system make a number up?",
                  a: "A certified answer is bound to evidence before it reaches you: claims anchor to filing-page spans, reconcile against SEC XBRL where a twin exists, and pass the auditor. If any gate cannot verify, the system refuses instead. The zero-fabrication invariant is measured nightly by an automated battery — it is not a slogan.",
                },
                {
                  q: "What happens when it doesn't know?",
                  a: "You get a verified refusal: a plain statement that the answer could not be verified, with the reason (retrieval gap, out-of-period question, failed audit) — and a receipt for the refusal itself. Refusing is the designed behavior, not an error.",
                },
                {
                  q: "How fast is an instant answer?",
                  a: "Covered metrics (revenue, net income, EPS, margins, and more) are looked up from the dual-verified fact store at zero LLM tokens — the measured p50 on the live pipeline is ~94ms. Uncovered questions run the full retrieval-and-audit path and take tens of seconds; the honest ones refuse in seconds.",
                },
                {
                  q: "Which filings does it cover today?",
                  a: "Apple, Meta and Tesla, Q4-2023, with 20 metrics dual-verified against SEC XBRL. The corpus and metric coverage grow through the same deterministic ingest pipeline — every expansion is reconciled at ingest, never hand-entered.",
                },
                {
                  q: "How do I verify an answer without trusting you?",
                  a: "Paste the answer's run ID in the 🔍 tab — no key, no login — and this console re-fetches the stored certification: the claims, their verifier stamps, and the exact evidence attribution. For full offline verification, download the compliance bundle (Ed25519-signed) and re-derive every hash with the included standard-library verifier.",
                },
              ].map((f) => (
                <details key={f.q} className="group rounded-xl border border-ink-700 bg-ink-950/40">
                  <summary className="cursor-pointer list-none px-3.5 py-2.5 text-xs font-medium text-mist-300 transition-colors hover:text-mist-200 [&::-webkit-details-marker]:hidden">
                    <span className="mr-2 inline-block text-brand-400 transition-transform group-open:rotate-90">▸</span>
                    {f.q}
                  </summary>
                  <p className="px-3.5 pb-3 pl-9 text-[11px] leading-relaxed text-mist-400">{f.a}</p>
                </details>
              ))}
            </div>
          </section>
          </>
        )}
      </main>
      </>
      )}

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
    </ToastProvider>
  );
}
