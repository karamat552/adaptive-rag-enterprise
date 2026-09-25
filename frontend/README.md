# Adaptive RAG — Executive Intelligence Console

The production frontend: **React 19 + Vite 6 + Tailwind CSS v4**, ~77 KB
gzipped, zero UI-kit dependencies, dark "AI-product" design.

**What it shows that no slide deck can:**
- **Live pipeline rail** — the query's real LangGraph stages light up as the
  SSE `transition` events arrive (cache → router → premise → FastPath →
  fleet → cross-check → synthesis → gates+audit → certify/refuse), with a
  live elapsed timer and the FastPath flash when the deterministic path
  answers.
- **Verifier-class honesty** — the receipt explorer renders every claim
  with its per-claim badge (`DETERMINISTIC` vs `LLM AUDIT`), the chain
  verification verdict (`links ok/checked`), evidence spans, and the
  `deterministic_certification` wording from the ADR-017 record.
- **Health telemetry** — provider, corpus epoch, chunk count, failover
  lanes, exec-pin status, polled from `/health`.
- **Fail-closed as a feature** — refusals render with the amber
  VERIFIED REFUSAL badge and the "refuses rather than serve an unverified
  answer" framing.

## Develop

```bash
npm install
npm run dev          # http://localhost:5173 (expects the gateway on :8000)
```

The backend base URL comes from `VITE_API_BASE` (default
`http://localhost:8000`). The gateway's CORS list includes the dev
origin; set `ALLOWED_ORIGINS` on the backend to add production origins.

## Build

```bash
npm run build        # -> dist/  (static; serve from any CDN/static host)
npm run preview
```

## Deploy (Vercel — simplest)

- Import the repo, **Root Directory: `frontend`**, Framework: Vite
  (auto-detected), Build: `npm run build`, Output: `dist`.
- Env var: `VITE_API_BASE` = the Render API origin
  (`https://adaptive-rag-enterprise.onrender.com`).
- On the backend, set `ALLOWED_ORIGINS` to include the Vercel domain
  (comma-separated, no trailing slash issues).
- Set `QUERY_API_KEYS` + a console API key in the header pill when the
  gateway enforces tenant auth (the key is stored locally in the browser).

## Visual test artifacts

```bash
node scripts/screenshot-flow.mjs     # P0 journey -> docs/screenshots/
node scripts/layout-audit.mjs        # overflow + WCAG contrast audit
```

Both use headless Edge (`--channel=msedge`) — no browser download needed.
Captured evidence lives in `docs/screenshots/`.
