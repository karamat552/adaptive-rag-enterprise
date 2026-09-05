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

# Generic seam: any OpenAI-compatible endpoint.
#   python scripts/list_models.py openai_compatible <base_url> [KEY_ENV]
#     e.g. python scripts/list_models.py openai_compatible https://api-inference.modelscope.cn/v1 MODELSCOPE_TOKEN
#          python scripts/list_models.py openai_compatible https://api.deepseek.com/v1 DEEPSEEK_API_KEY
if provider == "openai_compatible" and len(sys.argv) > 2:
    url, key_env = sys.argv[2].rstrip("/") + "/models", "RAG_API_KEY"
    if len(sys.argv) > 3:
        key_env = sys.argv[3]

if url is None:
    raise SystemExit(f"unknown provider: {provider}")
key = os.getenv(key_env)
if not key:
    raise SystemExit(f"{key_env} not set")

r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
for m in sorted(r.json().get("data", []), key=lambda x: x["id"]):
    print(m["id"])