"""Enumerate live model IDs for the active provider. Run BEFORE assigning models."""
import os, sys
import requests
from dotenv import load_dotenv

load_dotenv()
provider = sys.argv[1] if len(sys.argv) > 1 else "groq"

url, key_env = {
    "groq": ("https://api.groq.com/openai/v1/models", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/models", "OPENROUTER_API_KEY"),
}.get(provider, (None, None))

if url is None:
    raise SystemExit(f"unknown provider: {provider}")
key = os.getenv(key_env)
if not key:
    raise SystemExit(f"{key_env} not set")

r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
for m in sorted(r.json().get("data", []), key=lambda x: x["id"]):
    print(m["id"])