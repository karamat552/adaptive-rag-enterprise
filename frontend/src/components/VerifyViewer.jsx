import React, { useEffect, useState } from "react";
import { fetchReceipt } from "../api.js";

/** PUBLIC VERIFY VIEW — the trust story as an experience. Anyone pastes a
 * run ID; the console re-fetches the receipt, re-renders the deterministic
 * chain verdict (verified / chain-broken), the claims with their verifier
 * stamps, and the evidence attribution (source · page · chunk hash). No
 * key, no login — zero-trust by construction (the /verify endpoint is
 * read-only and anonymous-friendly). */
export default function VerifyViewer({ initialRunId = "" }) {
  const [runId, setRunId] = useState(initialRunId);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const verify = async (id) => {
    const rid = (id || "").trim();
    if (rid.length < 4 || loading) return;
    setLoading(true);
    setError(null);
    setData(null);
    try {
      setData(await fetchReceipt(rid));
    } catch (e) {
      setError(e.message || "verify failed — no receipt for this run id?");
    }
  };

  useEffect(() => {
    if (initialRunId) verify(initialRunId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRunId]);

  const v = data?.verification || {};
  const rcpt = data?.receipt || {};
  const verified = v.verified === true;
  // THREE-STATE VERDICT (live-tested 2026-11-12, run e946d9f9c3a5): a
  // refusal receipt carries 0 claims — the chain verifier correctly
  // returns verified=false ("no locatable evidence spans"), but rendering
  // that as "✗ CHAIN BROKEN" indicts the system for doing exactly what it
  // promises: refusing rather than serve. A refusal is a SUCCESS of the
  // fail-closed posture and gets its own honest badge; only a receipt
  // WITH claims that fail re-derivation is a broken chain.
  const refused = rcpt.audit_verdict === "refused";

  const parseJson = (x) => {
    try {
      return typeof x === "string" ? JSON.parse(x) : (x || []);
    } catch {
      return [];
    }
  };
  const claims = parseJson(rcpt.claims_json);
  const evidence = parseJson(rcpt.evidence_json);

  return (
    <section className="flex animate-rise flex-col gap-6">
      <div className="glass p-5">
        <h2 className="text-sm font-semibold text-mist-200">
          Verify any answer — zero trust required
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-mist-400">
          Paste a run ID from any answer's receipt. The console re-fetches the
          stored certification, re-renders the deterministic chain verdict, and
          shows the claims with their verifier stamps and the exact evidence
          attribution. No key, no login.
        </p>
        <div className="mt-3 flex gap-2">
          <input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && verify(runId)}
            placeholder="run id — e.g. 250f578be128"
            className="flex-1 rounded-xl border border-ink-600 bg-ink-950/60
                       px-3.5 py-2.5 font-mono text-sm text-mist-200 outline-none
                       placeholder:text-mist-500 focus:border-brand-500/60"
          />
          <button
            onClick={() => verify(runId)}
            disabled={loading || runId.trim().length < 4}
            className="rounded-xl bg-emerald-450 px-5 py-2.5 text-sm font-bold
                       text-ink-950 transition-opacity
                       disabled:cursor-not-allowed disabled:opacity-40
                       enabled:hover:opacity-90">
            {loading ? "Verifying…" : "Verify"}
          </button>
        </div>
        {error && (
          <p className="mt-3 text-xs text-rose-450">{error}</p>
        )}
      </div>

      {data && (
        <>
          <div className={`glass animate-pop p-5 ${verified && !refused ? "seal-trace" : ""}`}>
            <span className={`pill font-mono ${refused
              ? "border-amber-450/50 text-amber-450"
              : verified
                ? "border-emerald-450/50 text-emerald-450"
                : "border-rose-450/50 text-rose-450"}`}>
              {refused
                ? "✓ HONEST REFUSAL — nothing served, nothing to break"
                : verified
                  ? `✓ CHAIN VERIFIED · ${v.links_ok}/${v.links_checked} links`
                  : "✗ CHAIN BROKEN"}
            </span>
            {refused && (
              <p className="mt-2 text-xs leading-relaxed text-amber-200/80">
                The system declined to answer rather than serve an unverified
                one — the fail-closed posture working as designed. This is a
                pass of the zero-fabrication invariant, not a failure.
              </p>
            )}
            <div className="mt-3 font-mono text-[11px] text-mist-400">
              claims: {v.claims_total} · evidence: {v.evidence_total} ·
              run: {data.requested_run_id || runId}
            </div>
            <div className="mt-3 text-xs text-mist-300">
              <span className="text-mist-400">Question:</span> {rcpt.question}
            </div>
            <div className="prose-answer mt-2 text-mist-200">
              {rcpt.answer}
            </div>
          </div>

          {claims.length > 0 && (
            <div className="glass p-5">
              <h3 className="text-sm font-semibold text-mist-200">
                Claims — each stamped with its verifier
              </h3>
              <div className="mt-3 space-y-3">
                {claims.map((c, i) => (
                  <div key={i} className="rounded-xl border border-ink-700 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="pill font-mono">[{(c.citations || []).join(",")}]</span>
                      <span className={`pill font-mono ${c.verifier === "deterministic"
                        ? "border-emerald-450/40 text-emerald-450"
                        : ""}`}>
                        {c.verifier || "unverified"}
                      </span>
                      {c.deterministic_certification && (
                        <span className="pill font-mono text-[10px] text-mist-400">
                          {c.deterministic_certification}
                        </span>
                      )}
                    </div>
                    <p className="mt-2 text-xs leading-relaxed text-mist-300">
                      {c.claim}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {evidence.length > 0 && (
            <div className="glass p-5">
              <h3 className="text-sm font-semibold text-mist-200">
                Evidence — where the numbers live
              </h3>
              <div className="mt-3 space-y-2">
                {evidence.map((e, i) => (
                  <div key={i} className="rounded-xl border border-ink-700 p-3">
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[10.5px]">
                      <span className="pill font-mono">[{i + 1}]</span>
                      <span className="text-mist-300">
                        {e.company} · {e.source} · p.{e.page}
                      </span>
                      <span className="text-mist-500">
                        {String(e.chunk_hash || "").slice(0, 16)}…
                      </span>
                    </div>
                    {e.content && (
                      <p className="mt-2 font-mono text-[11px] leading-relaxed text-mist-400">
                        {String(e.content).slice(0, 220)}
                        {String(e.content).length > 220 ? "…" : ""}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
