"""A/B v3 — clean-window interleaved measurement (the ADR-015 deliverable).
Run from project root: venv/Scripts/python.exe scripts/ab_clean.py"""
import asyncio, os, sys, time, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))
os.environ.update({"RAG_DISABLE_CACHE_WRITE": "1", "RAG_DISABLE_CACHE_READ": "1"})

import adaptive_rag as ar

QUESTIONS = [
    ("tesla", "What was Tesla's total automotive revenues in Q4 2023?"),
    ("apple", "What was Apple's total net sales in Q4 2023?"),
    ("rl", "Did Reality Labs record an operating loss in Q4 2023, and how large was it?"),
]

_orig = ar._llm_call
_calls = {"n": 0, "expand": 0}

async def _counting(runnable, messages, stage, allow_failover=False):
    _calls["n"] += 1
    if stage == "query-expand":
        _calls["expand"] += 1
    return await _orig(runnable, messages, stage, allow_failover=allow_failover)

ar._llm_call = _counting

async def one(q, conf):
    os.environ["RAG_EXPANSION_CONFIDENCE"] = conf
    _calls["n"] = 0; _calls["expand"] = 0
    t0 = time.perf_counter()
    r = await ar.arun_query(q)
    u = r.get("usage") or {}
    return {"outcome": r.get("outcome"), "tin": u.get("input", 0),
            "tout": u.get("output", 0), "wall": round(time.perf_counter() - t0, 1),
            "llm": _calls["n"], "expand": _calls["expand"],
            "grounded": r.get("grounded")}

async def main():
    a, b = [], []
    for tag, q in QUESTIONS:
        ra = await one(q, "1.01")       # baseline: always expand (pre-fix)
        await asyncio.sleep(4)
        rb = await one(q, "0.55")       # optimized: conditional
        await asyncio.sleep(4)
        a.append(ra); b.append(rb)
        print(f"{tag:>6}: A in={ra['tin']:>6,} out={ra['tout']:>5,} wall={ra['wall']:>6.1f}s "
              f"llm={ra['llm']:>2} exp={ra['expand']} ({ra['outcome'][:14]}) || "
              f"B in={rb['tin']:>6,} out={rb['tout']:>5,} wall={rb['wall']:>6.1f}s "
              f"llm={rb['llm']:>2} exp={rb['expand']} ({rb['outcome'][:14]})", flush=True)

    def agg(rows):
        return dict(tin=sum(r["tin"] for r in rows), tout=sum(r["tout"] for r in rows),
                    wall=sum(r["wall"] for r in rows), llm=sum(r["llm"] for r in rows),
                    expand=sum(r["expand"] for r in rows),
                    cert=sum(1 for r in rows if r["outcome"] == "vectorstore" and r["grounded"]))
    A, B = agg(a), agg(b)
    print("\n" + "=" * 84)
    print(f"A (always-expand): tokens in={A['tin']:,} out={A['tout']:,} total={A['tin']+A['tout']:,} "
          f"| wall={A['wall']:.0f}s | llm-calls={A['llm']} | expand={A['expand']} | certified={A['cert']}/3")
    print(f"B (0.55 bar)    : tokens in={B['tin']:,} out={B['tout']:,} total={B['tin']+B['tout']:,} "
          f"| wall={B['wall']:.0f}s | llm-calls={B['llm']} | expand={B['expand']} | certified={B['cert']}/3")
    dt = (B['tin']+B['tout']) - (A['tin']+A['tout'])
    dw = B['wall'] - A['wall']
    dl = B['llm'] - A['llm']
    denom = A['tin']+A['tout'] or 1
    print(f"CLEAN DELTA: tokens {dt:+,} ({dt/denom:+.1%}) | wall {dw:+.0f}s ({dw/A['wall'] if A['wall'] else 0:+.1%}) | llm-calls {dl:+d}")
    json.dump({"A": A, "B": B}, open("ab_clean_report.json", "w"), indent=1)

asyncio.run(main())
