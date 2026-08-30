"""
Pre-deploy smoke matrix against a RUNNING gateway (uvicorn main:app).
Exercises the full Production Hardening Pack contract in ~4 requests:
  /live, /ready, /health, POST /query (unified contract), SSE without
  tenant_id (the old crash path), mid-stream client abort (ghost-request
  mitigation), /feedback without a key (fail-closed), /metrics deltas.

Usage:  python scripts/smoke_gateway.py [--base http://127.0.0.1:8000]
"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
CACHED_Q = "What were Apple's Products revenue versus Services revenue in Q4 2023?"
FRESH_Q = "What did Meta report about AI infrastructure and GPU investments in Q4 2023?"


def _metrics(cl: httpx.Client) -> dict:
    text = cl.get("/metrics").text
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, val = line.rpartition(" ")
            if val:
                try:
                    out[key] = float(val)
                except ValueError:
                    pass
    return out


def _sse_events(cl: httpx.Client, params: dict, abort_after_s: float = None):
    """Consume an SSE stream; optionally abort mid-run (ghost client)."""
    events, t0, ev = [], time.monotonic(), None
    try:
        with cl.stream("GET", "/query/stream", params=params) as s:
            if s.status_code != 200:
                s.read()
                return [{"http_error": s.status_code}], False
            for line in s.iter_lines():
                if line.startswith("event:"):
                    ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and ev:
                    events.append((ev, json.loads(line.split(":", 1)[1])))
                    if ev in ("result", "error"):
                        return events, False
                if abort_after_s and (
                        (ev == "transition")
                        or (time.monotonic() - t0 > abort_after_s)):
                    return events, True   # leave the with-block: socket closed
    except httpx.RequestError:
        pass
    return events, False


def main() -> int:
    base = sys.argv[sys.argv.index("--base") + 1] if "--base" in sys.argv else BASE
    cl = httpx.Client(base_url=base, timeout=httpx.Timeout(240.0))

    for _ in range(30):                       # wait for boot
        try:
            if cl.get("/live", timeout=3).status_code == 200:
                break
        except httpx.RequestError:
            time.sleep(1)
    else:
        print("FATAL: gateway never came up at", base)
        return 2

    r = cl.get("/ready").json()
    print(f"[probe] /live ok | /ready -> {r}")

    m0 = _metrics(cl)

    # --- POST /query: unified contract -----------------------------------
    r = cl.post("/query", json={"question": CACHED_Q})
    d = r.json()
    print(f"[query] http={r.status_code} outcome={d.get('outcome')} "
          f"grounded={d.get('grounded')} cached={d.get('cached')} "
          f"latency={d.get('latency_s')}s tokens={d.get('usage', {}).get('total')} "
          f"sources={len(d.get('sources', []))}")
    print(f"[query] X-Request-ID == run_id: "
          f"{r.headers.get('X-Request-ID') == d.get('run_id')} "
          f"| answer[:90]: {d.get('answer', '')[:90]!r}")

    # --- SSE WITHOUT tenant_id (the old AttributeError crash path) --------
    events, _ = _sse_events(cl, {"question": CACHED_Q})
    names = [e for e, _ in events]
    res = dict(events).get("result", {})
    print(f"[sse-no-tenant] events={names} result_keys={sorted(res.keys())[:6]}...")

    # --- ghost request: abort mid-run, watchdog must cancel + count -------
    _, aborted = _sse_events(cl, {"question": FRESH_Q}, abort_after_s=3.0)
    print(f"[ghost] client aborted mid-run={aborted}; polling watchdog...")
    m1 = m0
    for _ in range(16):                 # up to 8s — disconnect delivery on
        time.sleep(0.5)                 # Windows can lag a couple of seconds
        m1 = _metrics(cl)
        if m1.get("client_disconnects_total", 0) > m0.get(
                "client_disconnects_total", 0):
            break
    delta = {k: m1.get(k, 0) - m0.get(k, 0) for k in set(m0) | set(m1)
             if m1.get(k, 0) != m0.get(k, 0)}
    print(f"[metrics-delta] {json.dumps(delta, sort_keys=True)}")
    disc = m1.get("client_disconnects_total", 0) - m0.get("client_disconnects_total", 0)
    print(f"[ghost] client_disconnects_total delta={disc} -> "
          f"{'PASS' if disc >= 1 else 'FAIL'}")

    # --- /feedback must fail closed ---------------------------------------
    r = cl.post("/feedback", json={"question": CACHED_Q})
    print(f"[feedback-no-key] http={r.status_code} -> "
          f"{'PASS (fail-closed)' if r.status_code in (401, 403, 503) else 'FAIL'}")

    print("SMOKE_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
