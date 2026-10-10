# RUNBOOK — The Operator's Playbook

Written for the person debugging at 2am: when X breaks, do Y. Everything
below is grounded in live incidents and the ADRs — this is the organized
version, not new knowledge. Companions: `ARCHITECTURE.md` (how it works) ·
`KNOWN_ISSUES.md` (the failure ledger) · `ARCHITECTURE_DECISIONS.md` (why).

## 0. What runs where

| Piece | Where | What it is |
|---|---|---|
| API (main.py) | Render `adaptive-rag-enterprise` (srv-dab44c142hec739q3f7g) | FastAPI, port 8000, ONE uvicorn worker |
| Console | Vercel | the React/Next UI |
| Demo | adaptive-rag-enterprise.streamlit.app | Streamlit (free tier — sleeps between visits) |
| Database | Neon `ep-young-brook-…neondb` | Postgres + pgvector + pg_trgm, RLS multi-tenancy |
| CI | GitHub Actions `ci.yml` | unit / integration / docker / audit, on every push |
| Heartbeat | GitHub Actions `heartbeat.yml` | health-only poll every 15 min (zero tokens) |
| Canary + shadow battery | GitHub Actions — **PAUSED** (owner decision) | the nightly LLM batteries |

---

## 1. The API is down / unhealthy

**Detect:** the heartbeat workflow emails you (GitHub Actions failure);
or Render's own health check (Health Check Path = `/live`, already set)
fails a deploy and rolls it back automatically.

**Do, in order:**
1. `curl https://adaptive-rag-enterprise.onrender.com/health` — the payload
   names the graph state, llm_circuit, provider, and the resolved per-stage
   models. `200` with `graph: lazy` is NORMAL after a cold start (Render
   free tier spins down; the first query warms it).
2. `/live` is a liveness probe (lightweight) — if it fails, the service is
   genuinely down: check the Render dashboard → the service → **Logs** tab.
3. Bad deploy? The dashboard → the service → **Events** — the deploy list;
   the **Rollback** button on the last-good deploy restores it in ~1 min.
   (Or locally: `git revert <commit> && git push` — Render redeploys.)
4. The deploy was HEALTHY but queries fail? See §2 (model/lane death) —
   the service being up and the LLM lanes being dead are different outages.

**Render free-tier spin-down is NOT an outage.** The first request after
an idle period pays the ~30-60s cold start; the Streamlit demo's health
pill shows it. Nothing to fix.

---

## 2. A model or lane dies

### The failover chain (automatic — know it, don't fight it)

```
PRIMARY (Groq) ──429/timeout──▶ APInex ──▶ NIM ──▶ OpenRouter   (fleet lanes)
EXECUTIVE (Groq gpt-oss-120b) ──quota-class──▶ vetted peer pool:
                                    NIM nemotron-120b → Gemini 3.5-flash
```

A quota wall at the EXECUTIVE ends in a **verified refusal** — never a
weaker model certifying a financial answer (the pinned-primary rule,
ADR-008). The vetted peers only rescue the day-capped-TPD class.

### Groq walls (the most common)

- **Symptom:** `HTTP/1.1 429 Too Many Requests` in the logs; "Retrying
  request in 31s"; refusals naming the quota.
- **Mechanics:** per-MODEL budgets — 30 RPM / 1K RPD / 200K TPD each for
  gpt-oss-20b, qwen3.8-27b, gpt-oss-120b (separate pools). The TPD drains
  by mid-morning under load. The endpoint cooldown PARSES Groq's own
  "try again in 10m44s" hint and waits exactly that long.
- **Do:** nothing, usually — the fleet fails over to NIM; the executive's
  peer pool (NIM nemotron) rescues audit-stage walls. A refusal under a
  wall is the CORRECT outcome (fail-closed), receipted.
- **The A.2-gate warning:** do not run the canary + the shadow battery in
  the same fresh window — the 09-17 run showed the canary draining the
  window ~40 min before the shadow battery needed it (13/22 questions
  429-aborted). This is WHY both schedules are paused.

### NIM (the owner's 1000 credits)

- **Symptom:** 503s; or the credits exhausted.
- **Mechanics:** `integrate.api.nvidia.com/v1`, `NIM_API_KEY`. NIM serves
  BOTH the fleet failover (lane) AND the executive peer rescue. The
  vetting verdict (ADR-026): nemotron is NOT fit as the synthesis or
  audit PRIMARY (reasoning burn → empty drafts; stricter audit →
  coverage collapse) — do NOT promote it; the credits are for the lane +
  the peer rescue.
- **Do:** when credits exhaust, the lane 403s/503s and the fleet falls to
  OpenRouter; the peer pool falls to Gemini. Fail-closed holds. Top up
  or accept the reduced redundancy.

### APInex — DEAD (removed 2026-10-09)

- Live evidence: `402 Payment Required` — "available only with a
  subscription". The lane was removed from `RAG_FAILOVER_ENDPOINTS`
  (local + Render env) the same day; the fleet now fails over
  Groq → NIM → OpenRouter. **The dead-lane rule: the assembly cannot
  ship a dead lane** — a dead lane wastes a failover attempt per cycle.

### Provider catalog rotation

- **Symptom:** a model 404s or the boot smoke fails after a quiet period.
- **Mechanics:** free catalogs are NOT contracts — providers rotate them
  (llama-3.1-8b-instant and llama-4-scout were retired; TokenRouter's
  glm-5.3-free vanished).
- **Do:** `python scripts/list_models.py <provider> <base_url> <key_env>`
  to enumerate; then the 16-point gauntlet
  (`scripts/vet_exec_candidate.py`) BEFORE assigning any stage model.
  Never assign without the gauntlet (ADR-008).

---

## 3. The auth postures (what each guarantees)

- `QUERY_API_KEYS` SET (e.g. `acme:sk-…,globex:sk-…`) → the X-API-Key
  header is required; **the key IS the identity** — a declared tenant_id
  that disagrees is impersonation (403).
- `ALLOW_OPEN_MODE=true` + keys configured → a MISSING/empty key is an
  anonymous caller on the `default` tenant (the public-demo posture);
  a present-but-INVALID key is still 403 (anonymous is admitted, spoofed
  is not).
- `QUERY_API_KEYS` unset AND `ALLOW_OPEN_MODE` unset → **503
  fail-closed** (the server refuses to run open by omission).
- `ADMIN_API_KEY` → enables POST /feedback eviction; fail-closed if unset.
- **Never** run production without `QUERY_API_KEYS` + `ALLOW_OPEN_MODE=true`
  unless the public-demo posture is intended.

---

## 4. The database (Neon)

- **The pooler trap (hit live):** session GUCs (`app.rls_bypass`,
  `app.tenant_id`) require the DIRECT hostname
  (`ep-…neon.tech`), not the `-pooler` hostname. A pooled connection's
  GUC-scoped query fails with "Tenant binding failed". db.py uses the
  direct host for the admin identity and the pooler for the runtime
  pool — keep that split if you ever re-provision.
- **Identities:** `app_rag` is least-privilege + RLS-enforced (runtime);
  `neondb_owner` is the unpooled admin (schema sync, eviction, purge).
  Never merge them.
- **Restore:** Neon's console → the branch → Point-in-Time Restore to a
  snapshot. After restore, RE-VERIFY with the receipt chain:
  `/verify/<run_id>` recomputes the deterministic chain; a restored DB
  that fails verification is a wrong restore, not a code bug.
  **The drill (PRACTICED 2026-10-10):** `scripts/drill_restore.py
  --target-url <disposable>` simulates the recovery end to end — dumps
  the 8 data tables from the .env DB (READ-ONLY enforced), restores into
  a disposable target (the migrations fresh, TRUNCATE-then-COPY for the
  seeded tables, the id sequences reset), and VERIFIES with the receipt
  chain: every evidence chunk-hash must re-derive from the restored
  row's own fields, the transcript spine must hold byte-exact, the fact
  rows must be consistent. First run: PASSED — 8/8 tables, 693 receipts,
  8,772 evidence chunk-hashes re-derived. The procedure is known-good,
  not just written.
- **target_guard (ADR-025):** write-capable scripts refuse the production
  (host, database) pair — fail-closed, no override flag. Read-only
  measurements may target production only with Postgres-enforced
  READ ONLY. Don't weaken either.

---

## 5. Rollback (a bad deploy)

1. **Prefer the dashboard:** the service → Events → the last-good deploy →
   **Rollback** (~1 min, no git state change).
2. **Git route:** `git revert <bad-commit> && git push` — Render redeploys
   from main. The revert is permanent history (the honest route).
3. **The deploy-time safety net:** the Health Check Path (`/live`) means a
   deploy whose new instance fails health checks rolls back
   automatically — a broken push mostly never serves.
4. **After any rollback:** check `/health` (the resolved models +
   circuit state), then one covered query (zero tokens via the FastPath)
   to confirm serving.

---

## 6. Local-dev quirks (Windows)

- **The jiter DLL block (hit 2026-10-08):** Windows app-control flagged
  jiter 0.17.0's DLL — every ChatOpenAI client init died
  (`ImportError: DLL load failed ... Application Control policy`), which
  killed the whole pipeline locally. **Fix: `pip install jiter==0.16.0`**
  (a fresh file hash passes the cloud verdict). Production (Linux) was
  never affected. `tests/conftest.py` pre-warms the langchain import
  (module + one client instantiation) before onnxruntime loads — once
  jiter is cached the process is immune.
- **The retry loop is DEAD** (ADR-023): unverified → refuse. If you see
  multi-pass retry storms in logs, something regressed — check
  `RAG_MAX_LLM_CALLS_PER_RUN` (24) is set.
- **The vetting launcher pattern:** when an environment intermittently
  blocks a module import, pre-warm the import (module + instantiation)
  in a launcher process and relaunch until it passes — retries WITHIN a
  blocked process never succeed (the verdict is per launch).

---

## 7. The schedules (what runs when)

- **heartbeat.yml** — every 15 min, health-only (`/health`, `/ready`,
  `/metrics`), zero tokens, emails on failure. SAFE to keep always-on.
- **canary.yml** — PAUSED. Restore ONLY after the A.2 gate clears
  (7 green nights) or when non-Groq executive capacity lands. It runs
  the 12-question coverage battery against Groq's fresh window.
- **shadow_battery.yml** — PAUSED (the A.2 gate's instrument; it gets the
  fresh window exclusively). The per-question SHADOW-ROW capture means
  the next run leaves data even if killed by timeout.
- Manual triggers: both paused workflows keep `workflow_dispatch` for
  ad-hoc runs.

---

## 8. The measurement instruments (what each proves)

| Script | Measures | Cost |
|---|---|---|
| `scripts/bench_latency.py` | per-node wall-clock, tokens/answer | real tokens; needs a CALM quota window (KNOWN_ISSUES #2) |
| `scripts/token_dig.py` | per-node token/call attribution | real tokens |
| `scripts/measure_recall.py` | span-overlap recall@k, two keys | READ-ONLY-enforced on prod |
| `scripts/generate_answer_eval.py` | judge-scored answer quality (1-5 rubric) | ~60K judge tokens / 12 answers |
| `scripts/sync_xbrl_and_verify.py` | the causal chain walk (fetch→reconcile→audit→mutations) | real tokens; BLOCKED-at-step-1 exits 1 |
| `scripts/vet_exec_candidate.py` | the 16-point stage-model gate | ~200-300K tokens; PACED 50s |
| `scripts/smoke_gateway.py` | the pre-deploy smoke matrix | zero tokens (needs a running gateway) |

Judge caveats: a judge score is an OPINION OF ONE MODEL against a fixed
rubric — quote it with the model named, never as ground truth. The clean
measurement (NIM judge, temperature=0): mean 3.42/5 over 12, 0 errors.

---

## 9. The invariants (never trade these for convenience)

1. **Zero fabricated certified answers** — measured by the gates, not assumed.
2. **Fail-closed everywhere** — auth, eviction, refusal paths, unknown
   remotes, disabled controls. A permissive state is always an explicit
   act, never an omission.
3. **Review models never serve RAG stages** (ADR-008) — stage assignment
   requires the 16-point gauntlet.
4. **Never commit `.env` or real keys** — `.env.example` is the template.
5. **The maker/checker split** (ADR-026) is a capability, not a mandate —
   the default stays same-model until a candidate passes the gauntlet.
