import React, { useState } from "react";
import { getAdminKey, postFeedback, downloadExportBundle } from "../api.js";
import { useToast } from "../toast.jsx";

// Markdown-lite renderer for answers: bold, inline code, bullet lists,
// paragraphs. Deliberately dependency-free — the answer surface is bounded
// (synthesis mandate: bullets + bold + [n] citations), so a 30-line renderer
// beats pulling a full markdown lib into the bundle.
function renderInline(text) {
  const parts = [];
  const re = /(\*\*[^*]+\*\*|\[[0-9,]+\]|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) parts.push(<strong key={parts.length}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) parts.push(<code key={parts.length}>{tok.slice(1, -1)}</code>);
    else parts.push(
      <sup key={parts.length}
           className="mx-0.5 rounded bg-brand-500/15 px-1 font-mono text-[10px] text-brand-400">
        {tok.slice(1, -1)}
      </sup>
    );
    last = m.index + tok.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function MarkdownLite({ text }) {
  const lines = (text || "").split(/\r?\n/);
  const blocks = [];
  let list = null;
  const flush = () => {
    if (list) { blocks.push(<ul key={blocks.length}>{list}</ul>); list = null; }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*[-•*]\s+/.test(line)) {
      const item = <li key={(list?.length) || 0}>{renderInline(line.replace(/^\s*[-•*]\s+/, ""))}</li>;
      list = list ? [...list, item] : [item];
    } else {
      flush();
      if (line.trim()) blocks.push(<p key={blocks.length}>{renderInline(line)}</p>);
    }
  }
  flush();
  return <div className="prose-answer">{blocks}</div>;
}

function Expander({ title, children, defaultOpen = false, onCopy, copyLabel }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg border border-ink-600 bg-ink-850/60">
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
          <span className="flex-1 truncate">{title}</span>
        </button>
        {onCopy && (
          <button onClick={onCopy} title={copyLabel || "Copy to clipboard"}
                  className="shrink-0 rounded p-1 text-mist-500 transition-colors
                             hover:text-brand-400"
                  aria-label={copyLabel || "Copy"}>
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="2">
              <rect x="9" y="9" width="11" height="11" rx="2" />
              <path d="M5 15V5a2 2 0 012-2h10" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>
      <div className="expander-grid" data-open={open}>
        <div className="px-3 pb-2.5 text-xs leading-relaxed text-mist-400">
          {children}
        </div>
      </div>
    </div>
  );
}

export default function AnswerPanel({ result, question }) {
  const usage = result.usage || {};
  const refused = result.outcome === "verified_refusal" || result.outcome === "out_of_domain";
  const grounded = !!result.grounded;
  const zeroToken = (usage.llm_calls || 0) === 0 && grounded;
  const sources = result.sources || [];
  const [flagged, setFlagged] = useState(false);
  const [flagMsg, setFlagMsg] = useState(null);
  const notify = useToast();

  const copyText = async (text, label) => {
    try {
      await navigator.clipboard.writeText(text);
      notify(`${label} copied to clipboard`, "cyan");
    } catch {
      notify("Clipboard unavailable in this context", "amber");
    }
  };

  const flag = async () => {
    if (flagged) return;
    const { status, evicted } = await postFeedback(question);
    if (status === 200) {
      setFlagged(true);
      setFlagMsg(`🗑 Evicted ${evicted} cache entr${evicted === 1 ? "y" : "ies"} — the next ask re-verifies from filings.`);
      notify(`Cache evicted (${evicted}) — next ask re-verifies`, "emerald");
    } else if (status === 403 || status === 401) {
      setFlagMsg(`Eviction rejected (HTTP ${status}) — a valid admin key is required.`);
      notify("Eviction rejected — admin key required", "rose");
    } else {
      setFlagMsg(`Eviction failed — HTTP ${status}`);
      notify(`Eviction failed — HTTP ${status}`, "rose");
    }
  };

  const exportMd = () => {
    const md = `# Executive Intelligence Report\n\n` +
      `- **run_id:** ${result.run_id}\n` +
      `- **question:** ${question}\n` +
      `- **outcome:** ${result.outcome} · grounded: ${result.grounded} · ` +
      `latency: ${result.latency_s}s\n\n---\n\n${result.answer || ""}\n`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
    a.download = `report_${result.run_id}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
    notify("📄 Report downloaded (.md)", "emerald");
  };

  const exportBundle = async () => {
    try {
      await downloadExportBundle(result.provenance_run_id || result.run_id);
      notify("🔏 Offline-verifiable compliance bundle downloaded", "emerald");
    } catch (e) {
      notify(`Bundle unavailable (${e.message})`, "rose");
    }
  };

  return (
    <section className="glass animate-rise overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-5 py-3">
        <div className="flex items-center gap-2">
          {grounded ? (
            <span className="pill animate-pop border-emerald-450/40 text-emerald-450">
              <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="2.6">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4" />
              </svg>
              CERTIFIED · GROUNDED
            </span>
          ) : (
            <span className="pill animate-pop border-amber-450/40 text-amber-450">
              <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="2.2">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v4m0 4h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6A2 2 0 0022 18L13.7 3.9a2 2 0 00-3.4 0z" />
              </svg>
              {refused ? "VERIFIED REFUSAL" : "UNVERIFIED"}
            </span>
          )}
          {zeroToken && (
            <span className="pill border-cyan-350/40 text-cyan-350">
              0 LLM TOKENS
            </span>
          )}
          {result.cached && <span className="pill">cache replay</span>}
          {usage.llm_calls > 0 && (
            <span className="pill font-mono">
              {usage.llm_calls} LLM calls · {usage.total ?? (usage.input || 0) + (usage.output || 0)} tok
            </span>
          )}
          <span className="pill font-mono">{result.latency_s}s</span>
        </div>
        <span className="font-mono text-[10px] text-mist-500">run {result.run_id}</span>
      </div>

      <div className="px-5 py-4">
        <p className="mb-2 font-mono text-[11px] text-mist-500">Q: {question}</p>
        <MarkdownLite text={result.answer || ""} />
        {result.cached && (
          <p className="mt-3 rounded-lg border border-brand-500/25 bg-brand-500/5 px-3 py-2 text-[11px] text-brand-400/90">
            ⚡ Served from the verified semantic cache — flag below to evict
            and force re-verification.
          </p>
        )}
        {refused && (
          <p className="mt-3 rounded-lg border border-amber-450/25 bg-amber-450/5 px-3 py-2 text-[11px] text-amber-450/90">
            Fail-closed by design: the system refuses rather than serve an
            unverified answer — and the refusal itself is receipted.
          </p>
        )}

        {(sources.length > 0 || grounded) && (
          <>
            <div className="mt-5 border-t border-ink-700 pt-3 text-[11px] font-semibold
                           uppercase tracking-[0.14em] text-mist-400">
              📚 Evidence Ledger {sources.length > 0 && `(${sources.length} chunks)`}
            </div>
            <p className="mt-1 text-[10px] text-mist-500">
              Indices align with the inline [n] footnotes above.
            </p>
            <div className="mt-2 space-y-1.5">
              {sources.map((src, i) => {
                const s = String(src);
                const nl = s.indexOf("\n");
                const header = (nl > 0 ? s.slice(0, nl) : s)
                  .replace(" | ", " · ");
                const body = (nl > 0 ? s.slice(nl + 1) : "").trim();
                return (
                  <Expander key={i} title={`Evidence [${i + 1}] — ${header}`}>
                    <p className="whitespace-pre-wrap font-mono text-[11px] text-mist-400">
                      {body || header}
                    </p>
                  </Expander>
                );
              })}
            </div>
          </>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-ink-700 px-5 py-3">
        <button onClick={flag} disabled={flagged || !getAdminKey()}
                title={getAdminKey()
                  ? "Evicts the semantic-cache entries nearest this question so the next ask re-verifies"
                  : "Requires the admin key (header pill) — set it to enable cache eviction"}
                className={`pill transition-colors
                  ${flagged ? "border-emerald-450/40 text-emerald-450"
                    : getAdminKey() ? "hover:border-rose-450/50 hover:text-rose-450"
                      : "opacity-50"}`}>
          👎 Flag Inaccurate{flagged ? " (flagged)" : ""}
        </button>
        <button onClick={exportMd}
                className="pill transition-colors hover:border-brand-500/60 hover:text-mist-200">
          ⬇️ Export .md
        </button>
        {grounded && (
          <button onClick={exportBundle}
                  title="Download the offline-verifiable compliance bundle: receipt, evidence spans, page transcripts, Ed25519 attestation + a stdlib-only verifier"
                  className="pill transition-colors hover:border-cyan-350/50 hover:text-cyan-350">
            🔏 Proof bundle (.zip)
          </button>
        )}
        <span className="ml-auto font-mono text-[10px] text-mist-500">
          run_id {result.run_id} · degraded agents: {result.degraded_agents?.length ? result.degraded_agents.join(", ") : "none"}
        </span>
      </div>

      {flagMsg && (
        <div className="border-t border-ink-700 px-5 py-2 text-[11px] text-mist-300">
          {flagMsg}
        </div>
      )}
    </section>
  );
}
