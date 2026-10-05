// Adaptive RAG console API layer.
// SSE is consumed via fetch + manual stream parsing (NOT EventSource) so the
// X-API-Key auth header rides every request — the gateway is fail-closed and
// production requires tenant identity.

const ENV = (typeof import.meta !== "undefined" && import.meta.env) || {};

// Resolution order:
//   1. VITE_API_BASE           — explicit override (production builds set it)
//   2. "/api" in dev           — same-origin, proxied to the gateway by Vite
//                                (vite.config.js). Works behind ANY host or
//                                proxy with no CORS config, because the
//                                browser never makes a cross-origin request.
//   3. localhost:8000          — production build with no override: unchanged
//                                from the previous behaviour.
const API_BASE = ENV.VITE_API_BASE || (ENV.DEV ? "/api" : "http://localhost:8000");

const KEY_STORAGE = "arag.apikey";
const ADMIN_STORAGE = "arag.adminkey";

export const getApiKey = () =>
  (typeof localStorage !== "undefined" && localStorage.getItem(KEY_STORAGE)) || "";

export const setApiKey = (k) => {
  if (typeof localStorage === "undefined") return;
  if (k) localStorage.setItem(KEY_STORAGE, k);
  else localStorage.removeItem(KEY_STORAGE);
};

export const getAdminKey = () =>
  (typeof localStorage !== "undefined" && localStorage.getItem(ADMIN_STORAGE)) || "";

export const setAdminKey = (k) => {
  if (typeof localStorage === "undefined") return;
  if (k) localStorage.setItem(ADMIN_STORAGE, k);
  else localStorage.removeItem(ADMIN_STORAGE);
};

function headers(json = true) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  const key = getApiKey();
  if (key) h["X-API-Key"] = key;
  return h;
}

export async function fetchHealth(signal) {
  const r = await fetch(`${API_BASE}/health`, { headers: headers(false), signal });
  if (!r.ok) throw new Error(`health ${r.status}`);
  return r.json();
}

export async function fetchReceipt(runId) {
  const r = await fetch(`${API_BASE}/verify/${runId}`, { headers: headers(false) });
  if (!r.ok) throw new Error(`verify ${r.status}`);
  return r.json();
}

/**👎 Poison-pill cache eviction: POST /feedback (X-Admin-Key). Returns
 * {status, evicted} — evicted = semantic-cache entries purged so the next
 * ask of this question re-verifies from the filings. */
export async function postFeedback(question) {
  const admin = getAdminKey();
  const h = { "Content-Type": "application/json" };
  if (admin) h["X-Admin-Key"] = admin;
  const r = await fetch(`${API_BASE}/feedback`, {
    method: "POST", headers: h,
    body: JSON.stringify({ question, tenant_id: "default" }),
  });
  let evicted = 0;
  try { evicted = (await r.json()).evicted || 0; } catch { /* keep 0 */ }
  return { status: r.status, evicted };
}

/**🔏 Compliance Audit Bundle: GET /export/{run_id} streams the offline-
 * verifiable zip (receipt + evidence + transcripts + Ed25519 attestation
 * + stdlib verifier). Downloaded client-side into a Blob. */
export async function downloadExportBundle(runId) {
  const r = await fetch(`${API_BASE}/export/${runId}`, { headers: headers(false) });
  if (!r.ok) throw new Error(`export ${r.status}`);
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `compliance_bundle_${runId}.zip`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/**
 * Streams GET /query/stream (SSE: start -> transition{node,message} xN
 * -> result | error). Parses the stream by hand: events separated by
 * blank lines, "event:"/"data:" fields, ":" comment lines are keep-alives.
 */
export async function streamQuery({ question, onTransition, onResult, onError, signal }) {
  const url = `${API_BASE}/query/stream?question=${encodeURIComponent(question)}&tenant_id=default`;
  const res = await fetch(url, { headers: headers(false), signal });
  if (!res.ok || !res.body) {
    let detail = `stream ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
    onError(new Error(detail));
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const handle = (raw) => {
    let event = "message";
    const dataLines = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith(":")) return; // keep-alive comment
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    let payload = {};
    try { payload = JSON.parse(dataLines.join("\n")); } catch { /* keep {} */ }
    if (event === "transition") onTransition(payload);
    else if (event === "result") onResult(payload);
    else if (event === "error") onError(new Error(payload.detail || "stream error"));
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      handle(buf.slice(0, idx));
      buf = buf.slice(idx + 2);
    }
  }
  if (buf.trim()) handle(buf);
}
