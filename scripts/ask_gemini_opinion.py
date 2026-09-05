"""One-off: ask Gemini 3.7 Flash for an engineering opinion on killer-feature candidates."""
import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["GEMINI_API_KEY"]
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.7-flash"

PROMPT = """You are being consulted as an independent senior engineer for a second opinion. Be candid and opinionated; do not flatter us.

CONTEXT — the project ("Adaptive RAG"):
An enterprise-grade financial-document Q&A system (SEC filings corpus: Apple/Meta/Tesla 10-Qs). Stack:
- LangGraph pipeline: router/rewriter -> 3x parallel specialist extraction fleet -> executive synthesis -> grounding audit (a second LLM pass that verifies the draft against retrieved evidence before the answer is released).
- Postgres + pgvector hybrid search, flashrank reranking, semantic cache with similarity threshold 0.92.
- Multi-provider compute seam (Groq default, Google, OpenRouter), per-stage models: cheap 8B for routing, mid for fleet extraction, strongest free model (gpt-oss-120b) for synthesis+audit.
- Verification culture already in place: chunk lineage via SHA-256 chunk hashes (company|source|page|text), per-source PDF SHA-256 manifest, citation pre-audit that rejects out-of-bounds [n] tokens, a "canary calibrator" eval that deliberately tampers answers (shifted figures, fabricated citations) and asserts the audit catches every tampered variant, CI runs the full suite against a real pgvector Postgres, plus RAGAS-style gold-set accuracy suites.
- Serving: FastAPI with SSE streaming, per-IP rate limiting, Prometheus metrics, circuit breaker, least-privilege DB role with RLS.

QUESTION — which "killer features" are worth building to make this stand out?

Four candidates from the project owner:
1. Sub-second multi-specialist consensus: run 4-5 specialized micro-extractors in parallel on ultra-fast free endpoints (Groq/Cerebras) and synthesize faster than a single monolithic model turn.
2. Zero-hallucination deterministic audit: a citation/grounding layer so rigorous that financial/legal analysts can trace every claim to the exact chunk ID and document byte (proposed concrete shape: char-span lineage at ingest, claim-level citations, a machine-checkable "verification receipt" JSON per answer: claim -> chunk_hash -> page -> source PDF SHA -> audit verdict, plus a /verify/{run_id} endpoint that recomputes the chain on demand).
3. Self-healing / adaptive pipeline: detect low-confidence retrieval or contradictions between sources and automatically rewire the search strategy on the fly.
4. Resilient zero-cost compute fabric: a self-balancing multi-provider router optimizing speed/cost/context that never hits user-facing downtime or quota limits.

The internal first-opinion (from the project's AI pair-engineer) was:
- #2 is the real moat (the lineage + tamper-tested audit already exists; the receipt endpoint + char spans is a small increment on real infrastructure, and "prove every claim" is the one thing a thin LLM wrapper cannot fake).
- #3 is only worth building in a cheap DETERMINISTIC shape: cross-specialist contradiction detection done in Python (no extra LLM calls) + one bounded targeted re-retrieval retry; if the conflict persists, surface it in the answer instead of silently averaging. Skip the open-ended "agent rewires its own strategy" version.
- #1 is table stakes: fleet already runs in parallel; wall-clock is bounded by executive synthesis; semantic cache already gives sub-second on repeats. Polish, don't lead with it.
- #4 keep boring: generic OpenAI-compatible provider seam + STAGE-AWARE failover (fleet may fail over across endpoints; the audit/synthesis stage stays pinned to the strongest model with hard-fail semantics because it is the only un-backstopped stage).

YOUR TASK: Give your independent engineering opinion.
- Rank the four candidates for THIS project. Where do you agree/disagree with the internal first-opinion, and why?
- Identify anything important the list misses that would be a bigger edge than all four.
- Be specific about what would actually be hard for a competitor or a wrapper to copy, vs. what is just config/infra anyone can replicate.
- Keep it under ~500 words, no marketing fluff.
"""

payload = json.dumps({
    "contents": [{"parts": [{"text": PROMPT}]}],
    "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
}).encode()

req = urllib.request.Request(
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}",
    data=payload, headers={"Content-Type": "application/json"})

with urllib.request.urlopen(req, timeout=120) as resp:
    data = json.load(resp)

cand = (data.get("candidates") or [{}])[0]
parts = (cand.get("content") or {}).get("parts") or []
text = "\n".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
print(f"MODEL: {MODEL}")
print(f"usage: {data.get('usageMetadata', {})}")
print("=" * 78)
print(text.strip() or json.dumps(data)[:2000])
