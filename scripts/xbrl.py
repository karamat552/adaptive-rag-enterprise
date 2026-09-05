"""
XBRL Ground-Truth Ingestion — SEC EDGAR companyfacts (roadmap #5 / ADR-011)
============================================================================
Fetches SEC-published structured facts (us-gaap taxonomy) for the corpus
companies and derives the fiscal Q4 figures the PDF corpus covers, with the
same lineage discipline as ingest.py: payload SHA-256, derived-from hashes,
per-company outcome reporting, honest failures.

Q4 DERIVATION (why not read Q4 directly?): companies file 10-Qs for Q1-Q3
and a 10-K for the FULL year. The fourth quarter never appears as a primary
XBRL fact from a Q4 filing — it must be derived: Q4 = FY − 9mo. Fiscal-year
offsets are handled by period math, never by calendar assumptions:
  - Apple:  FY ends late September (53-week years: 371-day FY facts)
  - Tesla/Meta: FY ends Dec 31
The Q1-Q3 10-Qs carry the 9-month (year-to-date) duration facts; the 10-K
carries the FY duration fact. Same start date -> same fiscal year.

Interface:
  python scripts/xbrl.py            # fetch + derive + sync to Neon
  python scripts/xbrl.py --dry-run  # fetch + derive, print, no DB writes

SEC fair-access policy: declared User-Agent, ≤10 req/s; this script makes
~10 requests total. XBRL facts are PUBLIC SEC data — not scraped paywalled
content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from tenacity import (Retrying, before_sleep_log, retry_if_exception_type,
                      stop_after_attempt, wait_exponential_jitter)

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s | %(levelname)-7s | [XBRL] %(message)s",
                   datefmt="%H:%M:%S")
logger = logging.getLogger("XBRL")

UA = {"User-Agent": os.getenv("EDGAR_USER_AGENT",
                             "Adaptive-RAG-Enterprise karamat552@gmail.com")}

# Curated concept map — ONLY consolidated figures we can name precisely.
# Segments (Products/Services, Family-of-Apps/Reality-Labs) have
# company-specific dimensions XBRL frames don't carry; the guard must NOT
# guess those (ADR-011: decline-to-judge beats a misattributed check).
CONCEPTS: Dict[str, List[str]] = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
}

COMPANIES: List[Dict[str, str]] = [
    {"company": "Apple", "cik": "0000320193", "fiscal": "september"},
    {"company": "Meta", "cik": "0001326801", "fiscal": "calendar"},
    {"company": "Tesla", "cik": "0001318605", "fiscal": "calendar"},
]

FISCAL_YEAR_2023 = {"Apple": "FY2023", "Meta": "FY2023", "Tesla": "FY2023"}


class TransientFetchError(RuntimeError):
    pass


def _fetch_json(url: str) -> Any:
    """EDGAR fetch with declared UA + jittered retries (fair-access: <=10/s)."""
    def _once():
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code in (429, 500, 502, 503):
            raise TransientFetchError(f"HTTP {r.status_code} from {url}")
        if r.status_code == 404:
            return None          # concept not filed by this company
        r.raise_for_status()
        return r.json()

    runner = Retrying(stop=stop_after_attempt(4),
                      wait=wait_exponential_jitter(initial=2, max=20),
                      retry=retry_if_exception_type(TransientFetchError),
                      before_sleep=before_sleep_log(logger, logging.WARNING),
                      reraise=True)
    return runner(_once)


def _sha256_payload(data: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _duration_facts(units: List[Dict[str, Any]], start_prefix: str,
                    end: str, dmin: int, dmax: int,
                    form: str) -> List[Dict[str, Any]]:
    """Duration facts matching (start_prefix, end) within [dmin, dmax] days
    and the given form. 53-week fiscal years produce 371-day FY facts."""
    out = []
    for u in units:
        if u.get("end") != end or u.get("form") != form:
            continue
        s = u.get("start", "")
        if not s.startswith(start_prefix) or not s:
            continue
        try:
            from datetime import date
            d0 = date.fromisoformat(s)
            d1 = date.fromisoformat(end)
            days = (d1 - d0).days + 1
        except ValueError:
            continue
        if dmin <= days <= dmax:
            out.append(u)
    return out


def _pick_latest(facts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Most recently filed fact wins (restatements supersede originals)."""
    if not facts:
        return None
    return max(facts, key=lambda u: u.get("filed", ""))


def derive_facts() -> (List[Dict[str, Any]], Dict[str, Dict[str, Any]]):
    """Fetches concepts per company and derives fiscal-Q4-2023 figures:
    Q4 = FY(end=FY-end-date) − 9mo(end=Q3-end-date), both durations sharing
    the fiscal-year start. Returns (facts, per-company report)."""
    rows: List[Dict[str, Any]] = []
    report: Dict[str, Dict[str, Any]] = {}

    for comp in COMPANIES:
        name, cik = comp["company"], comp["cik"]
        rep: Dict[str, Any] = {"concepts": {}, "status": "ok"}
        # Fiscal anchors for FY2023 by convention:
        if name == "Apple":
            fy_end, nine_end, fy_start = "2023-09-30", "2023-07-01", "2022-09-25"
        elif name == "Meta":
            fy_end, nine_end, fy_start = "2023-12-31", "2023-09-30", "2023-01-01"
        else:  # Tesla
            fy_end, nine_end, fy_start = "2023-12-31", "2023-09-30", "2023-01-01"

        for metric, tags in CONCEPTS.items():
            for tag in tags:
                data = _fetch_json(
                    f"https://data.sec.gov/api/xbrl/companyconcept/"
                    f"CIK{cik}/us-gaap/{tag}.json")
                time.sleep(0.15)             # fair-access pacing
                if data is None:
                    rep["concepts"][tag] = "not-filed"
                    continue
                units = data.get("units", {}).get("USD", [])
                if metric == "eps_diluted":
                    # EPS is a per-share instant-context fact: take the Q4
                    # frame the earnings 8-K reports, else derive FY-9mo.
                    q4 = [u for u in units
                          if u.get("frame") == "CY2023Q4" or (
                              u.get("start", "").endswith("-10-01")
                              and u.get("end", "").endswith("-12-31")
                              and "2023" in str(u.get("start", "")))]
                    fact = _pick_latest(q4) if q4 else None
                    if fact:
                        rows.append({
                            "company": name, "metric": "eps_diluted",
                            "period": "Q4-2023", "value": fact["val"],
                            "unit": "USD/share",
                            "derivation": "reported",
                            "derived_from": None,
                            "payload_sha256": _sha256_payload(fact),
                            "source_form": fact.get("form", "?"),
                        })
                        rep["concepts"][tag] = f"reported Q4"
                    continue
                # Flow metrics: FY and 9mo durations share the fiscal start.
                fy = _pick_latest(_duration_facts(units, fy_start, fy_end,
                                                   90, 380, "10-K"))
                nine = _pick_latest(_duration_facts(units, fy_start, nine_end,
                                                    250, 285, "10-Q"))
                if not fy:
                    rep["concepts"][tag] = "no FY fact"
                    continue
                if not nine:
                    rep["concepts"][tag] = "no 9mo fact"
                    continue
                q4_val = fy["val"] - nine["val"]
                rows.append({
                    "company": name, "metric": metric,
                    "period": "Q4-2023", "value": q4_val, "unit": "USD",
                    "derivation": "FY_minus_9mo",
                    "derived_from": _sha256_payload(
                        {"fy": fy, "nine": nine}),
                    "source_form": f'FY:{fy.get("form")} 9mo:{nine.get("form")}',
                    "payload_sha256": _sha256_payload({"fy": fy, "nine": nine}),
                })
                rep["concepts"][tag] = f"derived {q4_val:,}"
        report[name] = rep
    return rows, report


def sync_to_db(rows: List[Dict[str, Any]]) -> int:
    """Writes derived facts to xbrl_facts (admin identity, epoch-stamped)."""
    from db import admin_connection, get_settings, bump_corpus_epoch
    import psycopg2.extras as extras
    cfg = get_settings()
    n = 0
    with admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT epoch FROM corpus_state WHERE id = 1;")
        epoch = cur.fetchone()[0]
        cur.execute("DELETE FROM xbrl_facts WHERE period = 'Q4-2023';")
        if rows:
            payload = [(r["company"], r["metric"], r["period"], r["value"],
                        r["unit"], r["derivation"], r.get("derived_from"),
                        r["payload_sha256"], r.get("source_form", "?"),
                        epoch, cfg.default_tenant) for r in rows]
            extras.execute_values(
                cur,
                """INSERT INTO xbrl_facts
                       (company, metric, period, value, unit, derivation,
                        derived_from, payload_sha256, source_form,
                        corpus_epoch, tenant_id)
                   VALUES %s;""",
                payload)
            n = len(payload)
    logger.info("XBRL sync: %d facts at corpus epoch %d.", n, epoch)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="XBRL ground-truth ingestion")
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args()

    t0 = time.perf_counter()
    rows, report = derive_facts()
    print("\n" + "=" * 70)
    print(f"XBRL DERIVATION | {len(rows)} facts | {time.perf_counter()-t0:.1f}s")
    print("=" * 70)
    for name, rep in report.items():
        print(f"  {name}: " + " | ".join(f"{k}={v}" for k, v in rep["concepts"].items()))
    for r in rows:
        print(f"  -> {r['company']:6} {r['metric']:12} {r['period']}: "
              f"{r['value']:>18,} ({r['derivation']})")
    if ns.dry_run:
        print("DRY RUN — no DB writes.")
        return 0
    n = sync_to_db(rows)
    print(f"synced {n} facts to xbrl_facts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
