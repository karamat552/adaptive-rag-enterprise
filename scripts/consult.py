"""Multi-model consultation tool — adversarial design reviews at crossroads.

Usage:
  python scripts/consult.py --prompt-file PATH [--models "gemini-3.7-flash,claude"]
  python scripts/consult.py --prompt-file PATH --all       # every reachable lane
  cat brief.md | python scripts/consult.py                  # stdin (default, no flag)

Lanes — fail-soft: a lane whose key is unset or quota-exhausted prints
[UNAVAILABLE — reason] and the rest still run (exit 1 only if EVERY lane
fails):
  gemini-<model>     GEMINI_API_KEY     Generative Language API
  groq               GROQ_API_KEY       model pinned (openai/gpt-oss-20b)
  nim                NIM_API_KEY        nemotron-120b — reasoning model, slow
  claude[:<model>]   ANTHROPIC_API_KEY  default claude-3-7-sonnet-latest (paid)
  openai[:<model>]   OPENAI_API_KEY     default gpt-4o (paid)

This standing practice follows the project convention established in
ARCHITECTURE_DECISIONS.md (ADR-006): independent model opinions at design
crossroads catch blind spots (the table-chunking gap was found this way).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

_GEMINI_MODELS = [
    "gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
]

_CLAUDE_DEFAULT_MODEL = "claude-3-7-sonnet-latest"
_OPENAI_DEFAULT_MODEL = "gpt-4o"

# Script default stays on the free lanes; the paid lanes (claude/openai) are
# named explicitly via --models or the /consult workspace command.
DEFAULT_PANEL = ["gemini-3.7-flash", "groq"]
ALL_LANES = ["gemini-3.7-flash", "groq", "nim", "claude", "openai:gpt-4o"]


def _ask_gemini(model: str, prompt: str, timeout: int = 120) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts
                     if p.get("text") and not p.get("thought"))
    if not text.strip():
        raise RuntimeError(f"empty response from {model}")
    return text.strip()


def _ask_groq(prompt: str, timeout: int = 60) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set")
    payload = json.dumps({
        # 2026-09 catalog rotation retired llama-3.1-8b-instant.
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4, "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def _ask_nim(prompt: str, timeout: int = 180) -> str:
    """NVIDIA NIM (nemotron-120b). Long timeout: it is a REASONING model —
    its thinking consumes output tokens before the answer lands, so we also
    give it a generous max_tokens budget (ADR-008 war story 4)."""
    key = os.environ.get("NIM_API_KEY")
    if not key:
        raise RuntimeError("NIM_API_KEY not set")
    payload = json.dumps({
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4, "max_tokens": 6000,
    }).encode()
    req = urllib.request.Request(
        "https://integrate.api.nvidia.com/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    text = data["choices"][0]["message"]["content"].strip()
    if not text:
        raise RuntimeError("empty NIM response (reasoning budget exhausted?)")
    return text


def _ask_claude(model: str, prompt: str, timeout: int = 120) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    payload = json.dumps({
        "model": model,
        "max_tokens": 4096, "temperature": 0.4,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    text = "\n".join(b.get("text", "") for b in data.get("content", [])
                     if b.get("type") == "text")
    if not text.strip():
        raise RuntimeError(f"empty response from {model}")
    return text.strip()


def _ask_openai(model: str, prompt: str, timeout: int = 120) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4, "max_tokens": 4096,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def consult(prompt: str, models=None):
    """Returns [(model_name, text | None, error | None)] for each target."""
    out = []
    targets = models or DEFAULT_PANEL
    for t in targets:
        try:
            if t.startswith("gemini"):
                out.append((t, _ask_gemini(t, prompt), None))
            elif t.startswith("groq"):
                out.append((t, _ask_groq(prompt), None))
            elif t.startswith("nim"):
                out.append((t, _ask_nim(prompt), None))
            elif t.startswith("claude"):
                model = t.split(":", 1)[1] if ":" in t else _CLAUDE_DEFAULT_MODEL
                out.append((t, _ask_claude(model, prompt), None))
            elif t.startswith("openai"):
                model = t.split(":", 1)[1] if ":" in t else _OPENAI_DEFAULT_MODEL
                out.append((t, _ask_openai(model, prompt), None))
            else:
                out.append((t, None, f"unknown provider: {t}"))
        except urllib.error.HTTPError as e:
            out.append((t, None, f"HTTP {e.code}: {e.read()[:200]!r}"))
        except Exception as e:
            out.append((t, None, str(e)[:200]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-model adversarial consult")
    ap.add_argument("--prompt-file", help="path to a markdown/text brief")
    ap.add_argument("--stdin", action="store_true",
                    help="read prompt from stdin (default when no --prompt-file)")
    ap.add_argument("--all", action="store_true",
                    help="consult every reachable lane (overrides --models)")
    ap.add_argument("--models", default=",".join(DEFAULT_PANEL),
                    help="comma list: gemini-<model> | groq | nim | "
                         "claude[:<model>] | openai[:<model>]")
    args = ap.parse_args()

    prompt = (open(args.prompt_file, encoding="utf-8").read()
              if args.prompt_file else sys.stdin.read()).strip()
    if not prompt:
        print("empty prompt (no --prompt-file and stdin was empty)",
              file=sys.stderr)
        return 2
    targets = (ALL_LANES if args.all else
               [m.strip() for m in args.models.split(",") if m.strip()])
    results = consult(prompt, targets)

    failed = 0
    for name, text, err in results:
        print("=" * 78)
        print(f"MODEL: {name}")
        print("=" * 78)
        if err:
            failed += 1
            print(f"[UNAVAILABLE — {err}]")
        else:
            print(text)
        print()
    return 1 if failed == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
