"""Local environment pre-warm (2026-10-08): Windows App Control policy
blocks jiter's DLL when the openai SDK loads it DEFERRED (at ChatOpenAI
client init) AFTER onnxruntime's DLLs are resident — and db.py imports
fastembed (onnxruntime) before any engine build. Once jiter is loaded
BEFORE onnxruntime, the module caches and the whole process is immune.

This conftest pre-warms exactly that chain (module import + one client
instantiation) before any test module imports db. A ChatOpenAI build with
a throwaway key makes NO network call (openai validates keys at invoke
time, not init), so this is harmless in CI too. Best-effort: if the
policy still blocks it, engine-building tests fail loudly on their own —
the real machine-level fix is an OS exclusion for jiter's DLL."""
import os

try:
    from langchain_openai import ChatOpenAI
    _ = ChatOpenAI(model="prewarm", api_key=os.getenv("GROQ_API_KEY")
                   or "prewarm-key",
                   base_url="https://prewarm.invalid/v1",
                   timeout=1, max_retries=1)
except Exception:
    pass
