// Adaptive RAG console API layer.
// SSE is consumed via fetch + manual stream parsing (NOT EventSource) so the
// X-API-Key auth header rides every request — the gateway is fail-closed and
// production requires tenant identity.

const API_BASE =
  (typeof import.meta !== "undefined" && import.meta.env && import.meta.env.VITE_API_BASE) ||
  "http://localhost:8000";

const KEY_STORAGE = "arag.apikey";

export const getApiKey = () =>
  (typeof localStorage !== "undefined" && localStorage.getItem(KEY_STORAGE)) || "";

export const setApiKey = (k) => {
  if (typeof localStorage === "undefined") return;
  if (k) localStorage.setItem(KEY_STORAGE, k);
  else localStorage.removeItem(KEY_STORAGE);
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
