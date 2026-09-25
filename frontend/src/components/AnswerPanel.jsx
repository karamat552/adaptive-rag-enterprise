import React from "react";

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

export default function AnswerPanel({ result, question }) {
  const usage = result.usage || {};
  const refused = result.outcome === "verified_refusal" || result.outcome === "out_of_domain";
  const grounded = !!result.grounded;
  const zeroToken = (usage.llm_calls || 0) === 0 && grounded;

  return (
    <section className="glass animate-rise overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-700 px-5 py-3">
        <div className="flex items-center gap-2">
          {grounded ? (
            <span className="pill border-emerald-450/40 text-emerald-450">
              <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="2.6">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4" />
              </svg>
              CERTIFIED · GROUNDED
            </span>
          ) : (
            <span className="pill border-amber-450/40 text-amber-450">
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
        {refused && (
          <p className="mt-3 rounded-lg border border-amber-450/25 bg-amber-450/5 px-3 py-2 text-[11px] text-amber-450/90">
            Fail-closed by design: the system refuses rather than serve an
            unverified answer — and the refusal itself is receipted.
          </p>
        )}
      </div>
    </section>
  );
}
