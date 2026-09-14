"""ADR-017 Phase 0 — the span-anchored fact store extractor.

PURE extraction logic: no DB imports, no I/O — fully unit-testable.
The persistence wrapper (sync_fact_rows) lives in db.py; it groups
current-epoch chunks by (source, page), feeds each page here, and
writes the reconciled rows to fact_rows.

GROUND TRUTH — the three real statement layouts in the corpus
(dumped verbatim from production multi_agent_chunks, 2026-09-14):

Apple (page 1) and Meta (page 6): line-sequential layout — a label
line, then one value PER LINE, with noise lines between ("$", "!"):

    Total net sales (1)
    89,498
    90,146
    383,285
    394,328
    Year headers: 2023 2022 2023 2022 (one per line) -> the
    [CY, PY, CY, PY] sequence binds rows to [Q4-CY, Q4-PY, FY-CY, FY-PY].

Tesla (pages 4/5/25): quarter- or year-column headers, also one value
per line, plus a YoY/margin column ("%"/"bp") that must NEVER bind:

    Q4-2022 / Q1-2023 / Q2-2023 / Q3-2023 / Q4-2023     (p4, p25)
    2019 / 2020 / 2021 / 2022 / 2023                    (p5)
    Total revenues
    24,318
    23,329
    ...
    1%          <- YoY column: rejected as a value line

    Tesla's reconciliation pages (28/29/30) are letter-spaced
    ("R E C O N C I L I A T I O N ...") and excluded whole (B.1.3).

PROVENANCE MODEL: a page's chunks are joined with the SAME "\\n\\n"
separator and offsets ingest.py uses to build page_transcripts, so a
candidate's absolute (char_start, char_end) are transcript coordinates
the receipt chain slices. Rows whose span crosses a chunk boundary are
declined (the corpus re-states table heads per chunk, so real rows are
chunk-local by construction); db.sync_fact_rows then re-verifies every
row against the STORED transcript before writing — that is the
Phase-0 exit gate.

BINDING AMENDMENTS IMPLEMENTED (Appendix A.4/B.5, binding for Phase 0):
  B.1.1  Only (revenue, net_income, eps_diluted) can ever reconcile;
         every other extracted metric is 'unreconciled_fact' forever —
         absence of an XBRL counterpart is NEVER agreement.
  B.1.3  Context-marker pages (pro-forma / as-previously-reported /
         restatement / sensitivity / GAAP-to-non-GAAP reconciliation)
         and arithmetic-flagged chunks (arithmetic_ok=False) are
         excluded from extraction. 'Excluded' removes the KNOWN
         mistagging categories — never "eliminated" (B.6.1 wording).
  B.1.5  Derived XBRL facts (Q4 = FY - 9mo) gain authority only through
         the PDF-column agreement recorded here; the consumption-side
         gate (check_xbrl_figures) declines disconfirmed facts.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

# ===========================================================================
# B.1.3 — context-marker exclusions (verbatim-correct-but-wrong-context)
# ===========================================================================
_CONTEXT_EXCLUSIONS = re.compile(
    r"pro[\s\-]?forma|as\s+previously\s+reported|restat\w*|"
    r"sensitivity\s+analysis|supplemental\s+financial|hypothetical",
    re.IGNORECASE)

# Tesla's GAAP-to-non-GAAP reconciliation pages carry a letter-spaced
# title ("R E C O N C I L I A T I O N  O F  G A A P ...") — match it
# with whitespace squished out.
_RECON_MARK_RE = re.compile(r"reconciliationofgaap", re.IGNORECASE)

# Statements whose rows are extraction-eligible (Apple "OPERATIONS",
# Meta "INCOME"). Tesla's statement pages have no title at all and are
# admitted by their ALL-CAPS income-statement section lines instead —
# which also EXCLUDES the near-miss pages: Tesla's cash-flow "Net
# income" (7,943, includes NCI) sits within the 0.5% tolerance of the
# attributable XBRL figure (7,928) and would FALSELY reconcile; the
# press-release highlights and segment tables duplicate statement rows.
# Fail-closed: one canonical income statement per company, nothing else.
_TITLE_MARKERS = re.compile(
    r"CONDENSED\s+CONSOLIDATED\s+STATEMENTS?\s+OF\s+(OPERATIONS|INCOME)",
    re.IGNORECASE)
_INCOME_SECTION_MARKERS = re.compile(
    r"^(REVENUES|COST OF REVENUES|INCOME FROM OPERATIONS|NET INCOME"
    r"( ATTRIBUTABLE TO COMMON STOCKHOLDERS)?)\s*$", re.MULTILINE)

_SCALE_RE = re.compile(r"\bin\s+(thousands|millions|billions)\b",
                       re.IGNORECASE)
_SCALE_MULT = {"thousands": Decimal("1e3"), "millions": Decimal("1e6"),
               "billions": Decimal("1e9")}

_NUM_TOKEN = re.compile(r"\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?")
_YEAR_LINE = re.compile(r"^20\d{2}$")
_QUARTER_LINE = re.compile(r"^Q[1-4]-20\d{2}$", re.IGNORECASE)
# header census for page eligibility — MULTILINE so ^...$ hit every line
_YEAR_ANYWHERE = re.compile(r"^20\d{2}$", re.MULTILINE)
_QUARTER_ANYWHERE = re.compile(r"^Q[1-4]-20\d{2}$", re.IGNORECASE | re.MULTILINE)
_BP_LINE = re.compile(r"\bbp\b", re.IGNORECASE)

_EPS_SECTION = re.compile(
    r"^(earnings\s+per\s+share|net\s+income\s+per\s+share)", re.IGNORECASE)
_SHARES_SECTION = re.compile(
    r"^(shares\s+used\s+in\s+computing|weighted[-\s]?average\s+shares\s+used)",
    re.IGNORECASE)

_FOOTNOTE_RE = re.compile(r"\s*\(\d+\)\s*$")   # trailing "(1)" markers

# Filing dates per source (A.3.2 valid-time column): the date the PDF's
# parent filing hit EDGAR — the 10-K for Apple, the Q4 earnings 8-K
# exhibits for Meta and Tesla.
FILING_DATES: Dict[str, str] = {
    "Apple_Q4_2023.pdf": "2023-11-02",
    "Meta_Q4_2023.pdf": "2024-02-01",
    "Tesla_Q4_2023.pdf": "2024-01-24",
}

# Canonical row labels (cleaned lowercase) -> metric_key. Consolidated
# statements-of-operations lines only; Tesla's "(GAAP)" suffixes stay in
# the key so "(non-GAAP)" siblings can never collide (they are dropped
# by the row-level non-GAAP filter before map lookup).
_ROW_LABELS: Dict[str, str] = {
    "total net sales": "revenue",
    "total revenues": "revenue",
    "total revenue": "revenue",
    "revenue": "revenue",
    "revenues": "revenue",
    "net sales": "revenue",            # heading row — carries no values
    "net income": "net_income",
    "net income (loss)": "net_income",
    "net income attributable to common stockholders": "net_income",
    "net income attributable to common stockholders (gaap)": "net_income",
    "net (loss) income attributable to common stockholders (gaap)":
        "net_income",
    "eps attributable to common stockholders, diluted (gaap)": "eps_diluted",
}

# Tracked-only rows (no XBRL twin at Phase 0 — B.1.1 fail-closed:
# stored unreconciled for the V3 cross-filing study, never Path-A
# eligible). Bare "NET INCOME" on a page that ALSO carries an
# attributable variant is renamed net_income_total post-pass — Tesla's
# total-including-NCI (7,943) must never masquerade as the attributable
# net income the XBRL NetIncomeLoss tag means (7,928).
_TRACKED_LABELS: Dict[str, str] = {
    "products": "segment_products_revenue",
    "services": "segment_services_revenue",
    "services and other": "segment_services_revenue",
    "services and other revenue": "segment_services_revenue",
    "automotive sales": "segment_automotive_sales",
    "total automotive revenues": "segment_automotive_revenue",
    "energy generation and storage": "segment_energy_revenue",
    "energy generation and storage revenue": "segment_energy_revenue",
    "total cost of sales": "total_cost_of_sales",
    "total cost of revenues": "total_cost_of_sales",
    "total operating expenses": "total_operating_expenses",
    "gross margin": "gross_margin",
    "gross profit": "gross_margin",
    "operating income": "operating_income",
    "operating income (loss)": "operating_income",
    "income from operations": "operating_income",
    "(loss) income from operations": "operating_income",
    "income before provision for income taxes": "pretax_income",
    "income before income taxes": "pretax_income",
    "provision for income taxes": "tax_provision",
    "provision for (benefit from) income taxes": "tax_provision",
    "research and development": "rd_expense",
    "selling, general and administrative": "sga_expense",
}

# Metrics an XBRL fact exists for at Phase 0 (B.1.1 — consolidated only).
PATH_A_METRICS = frozenset({"revenue", "net_income", "eps_diluted"})

_NOISE_CHARS = frozenset("!$\u2014\u2013-. ")


def _clean_label(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s*!\s*", " ", s)
    s = _FOOTNOTE_RE.sub("", s).strip()
    s = s.rstrip(":").strip()
    return re.sub(r"\s+", " ", s).lower()


def _is_noise_line(s: str) -> bool:
    """'$' placeholders, '!' markers, em-dash cells, blank rules —
    layout scaffolding inside a row, never a value and never a label."""
    return not s or set(s) <= _NOISE_CHARS


def _is_numeric_line(s: str) -> bool:
    if not any(ch.isdigit() for ch in s):
        return False
    return bool(re.fullmatch(r"[\(\)\$!\d,.\s]+", s))


def _parse_num(tok: str) -> Optional[Decimal]:
    neg = tok.startswith("(") and tok.endswith(")")
    t = tok.strip("()").replace(",", "")
    try:
        v = Decimal(t)
    except Exception:
        return None
    return -v if neg else v


# ===========================================================================
# COLUMN BINDING — fail-closed (misbinding is attacked at write time)
# ===========================================================================
def bind_periods(years: List[int], quarters: List[str],
                 n_values: int) -> Optional[List[str]]:
    """Binds a row's value columns to period labels from the page's own
    headers. Exactly three shapes are accepted; anything else declines
    the row:

      1. Quarter columns (Tesla): ['Q4-2022', 'Q1-2023', ...] verbatim.
      2. Distinct year columns (Tesla 5-year): FY-YYYY each.
      3. The [CY, PY, CY, PY] Apple/Meta pattern:
         [Q4-CY, Q4-PY, FY-CY, FY-PY].
    """
    if len(quarters) >= 3:
        return list(quarters) if n_values == len(quarters) else None
    if len(years) >= 3 and len(set(years)) == len(years):
        return [f"FY-{y}" for y in years] if n_values == len(years) else None
    if len(years) >= 4:
        cy, py = years[0], years[1]
        if years[2] == cy and years[3] == py and n_values == 4:
            return [f"Q4-{cy}", f"Q4-{py}", f"FY-{cy}", f"FY-{py}"]
    return None


def _binding_width(quarters: List[str], years: List[int]) -> Optional[int]:
    """The row width the page's headers imply, or None while headers are
    still incomplete (rows then flush label-driven and decline unless
    the width happens to match)."""
    if len(quarters) >= 3:
        return len(quarters)
    if len(years) >= 3 and len(set(years)) == len(years):
        return len(years)
    if len(years) >= 4 and years[0] == years[2] and years[1] == years[3]:
        return 4
    return None


# ===========================================================================
# PAGE EXTRACTION — the pure core
# ===========================================================================
def extract_facts_from_page(
    company: str,
    page: int,
    page_chunks: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """Extract period-bound fact candidates from ONE statement page.

    page_chunks: the page's chunks (dicts with content/char_start/
    char_end/chunk_hash/arithmetic_ok), unsorted. Returns (candidates,
    excluded) where excluded counts B.1.3 drops (flagged chunks,
    context-marker pages) and candidates are dicts with absolute
    transcript-coordinate span anchors plus row_text (the exact span
    text, re-verified against page_transcripts by sync before writing).
    """
    excluded = 0
    usable = [c for c in page_chunks
              if c.get("char_start") is not None and c.get("content")]
    # B.1.3: flagged chunks (arithmetic_ok=False) stay IN the join —
    # transcript coordinates must match the stored page_transcripts,
    # which contains every chunk — but no row may anchor inside them
    # (checked against the owning chunk at span-anchor time).
    excluded += sum(1 for c in usable
                    if c.get("arithmetic_ok") is False)
    if not usable:
        return [], excluded
    usable.sort(key=lambda c: c["char_start"])

    # Join with the SAME "\n\n" separator + running offsets ingest.py
    # uses to build page_transcripts — joined == transcript byte-for-
    # byte, so candidate spans are transcript coordinates by identity.
    parts: List[str] = []
    offsets: List[Tuple[int, int, Dict[str, Any]]] = []  # (jstart, jend, chunk)
    pos = 0
    for c in usable:
        text = c["content"]
        offsets.append((pos, pos + len(text), c))
        parts.append(text)
        parts.append("\n\n")
        pos += len(text) + 2
    joined = "".join(parts)
    source = usable[0].get("source")

    # ---- page-level eligibility + B.1.3 exclusions -------------------
    squished = re.sub(r"\s+", "", joined[:400])
    if _RECON_MARK_RE.search(squished):          # non-GAAP reconciliation
        return [], excluded + len(usable)
    if _CONTEXT_EXCLUSIONS.search(joined):
        return [], excluded + len(usable)
    head = joined[:2000]
    has_title = bool(_TITLE_MARKERS.search(head))
    # Tesla's statement pages have no title — admitted by their
    # ALL-CAPS income-statement section lines (case-SENSITIVE on
    # purpose: title-case "Net income" on cash-flow pages must not
    # qualify). Searched on the RAW head — the ^...$ anchors need
    # the newlines.
    has_income_sections = bool(_INCOME_SECTION_MARKERS.search(head))
    if not (has_title or has_income_sections):
        return [], excluded        # not an income statement — nothing
    n_quarters = len(_QUARTER_ANYWHERE.findall(head))
    n_years = len(_YEAR_ANYWHERE.findall(head))
    if not (n_quarters >= 3 or n_years >= 4):
        return [], excluded
    sm = _SCALE_RE.search(head)
    if not sm:
        return [], excluded + len(usable)   # unverifiable scale — fail-closed
    scale_name = sm.group(1).lower()
    scale_mult = _SCALE_MULT[scale_name]

    # ---- line loop with absolute (joined-) coordinates ---------------
    line_starts: List[int] = [0]
    raw_lines = joined.split("\n")
    for ln in raw_lines[:-1]:
        line_starts.append(line_starts[-1] + len(ln) + 1)

    years: List[int] = []
    quarters: List[str] = []
    section: Optional[str] = None          # 'eps' | 'shares' | None
    current: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = []
    prev_owner: Optional[int] = None

    def _flush() -> None:
        nonlocal current
        if current is None:
            return
        current = None

    def _emit() -> None:
        """Width-complete row -> period-bound candidates."""
        nonlocal current
        toks = current["tokens"]
        periods = bind_periods(years, quarters, len(toks))
        if periods is None:
            current = None
            return
        is_per_share = current["metric_key"].startswith(("eps_", "shares_"))
        for i, (tok_text, tok_s, tok_e) in enumerate(toks):
            val = _parse_num(tok_text)
            if val is None:
                continue
            mult = Decimal("1") if is_per_share else scale_mult
            candidates.append({
                "company": company,
                "source": source,
                "page": page,
                "metric_key": current["metric_key"],
                "label": current["label_orig"],
                "period": periods[i],
                "value_raw": str(val),
                "scale": "1" if is_per_share else scale_name,
                "value_usd": str(val * mult),
                "basis": "GAAP",
                "chunk_hash": None,
                # tight span: label start -> THIS value's own end
                # (one span per period, never label->last-value)
                "char_start": current["label_start"],
                "char_end": tok_e,
                "row_text": None,
                "filing_date": FILING_DATES.get(source),
                "reconciled": False,
                "verifier_class": "unreconciled_fact",
            })
        current = None

    for li, raw in enumerate(raw_lines):
        s = raw.strip()
        base = line_starts[li]
        owner = _owner_index(offsets, base)
        if owner != prev_owner:
            # chunk boundary crossed: a row's label and its values must
            # live in ONE chunk. The corpus re-states table heads per
            # chunk, so a trailing label at a chunk's end + the next
            # chunk's re-stated values would MISBIND — decline the
            # pending row at the boundary.
            _flush()
            current = None
            prev_owner = owner
        if _is_noise_line(s):
            continue
        if _YEAR_LINE.fullmatch(s):
            _flush()
            years.append(int(s))
            continue
        if _QUARTER_LINE.fullmatch(s):
            _flush()
            quarters.append(s.upper())
            continue
        if "%" in s or _BP_LINE.search(s):
            continue                 # YoY / margin column — never a value
        if _is_numeric_line(s):
            if current is not None:
                for m in _NUM_TOKEN.finditer(raw):
                    current["tokens"].append(
                        (m.group(0), base + m.start(), base + m.end()))
                width = _binding_width(quarters, years)
                n = len(current["tokens"])
                if width is not None:
                    if n == width:
                        _emit()
                    elif n > width:
                        current = None     # layout confusion — decline row
            continue
        # label / section line
        cleaned = _clean_label(s)
        if _EPS_SECTION.match(cleaned):
            section = "eps"
            _flush()
            continue
        if _SHARES_SECTION.match(cleaned):
            section = "shares"
            _flush()
            continue
        if "non-gaap" in cleaned:
            _flush()                    # B.1.3 row-level exclusion
            continue
        metric = _ROW_LABELS.get(cleaned) or _TRACKED_LABELS.get(cleaned)
        if cleaned in ("basic", "diluted"):
            if section == "eps":
                metric = "eps_" + cleaned
            elif section == "shares":
                metric = "shares_" + cleaned
            else:
                metric = None            # bare Basic/Diluted — no context
        _flush()
        if metric:
            stripped = base + (len(raw) - len(raw.lstrip()))
            current = {"metric_key": metric, "label_orig": s,
                       "tokens": [], "label_start": stripped}
    _flush()

    # ---- Tesla disambiguation: bare "NET INCOME" vs attributable -----
    has_attributable = any(
        c["metric_key"] == "net_income"
        and "attributable" in (c["label"] or "").lower()
        for c in candidates)
    if has_attributable:
        for c in candidates:
            if (c["metric_key"] == "net_income"
                    and "attributable" not in (c["label"] or "").lower()):
                c["metric_key"] = "net_income_total"

    # ---- span anchoring: every row must live inside ONE ELIGIBLE chunk
    # and slice it byte-exactly (receipt-grade guarantee at write time).
    # Joined coordinates -> absolute transcript coordinates.
    verified: List[Dict[str, Any]] = []
    for c in candidates:
        row_s, row_e = c["char_start"], c["char_end"]
        if row_e <= row_s:
            continue
        oi = _owner_index(offsets, row_s)
        oe = _owner_index(offsets, row_e - 1)
        if oi is None or oi != oe:
            continue                 # row crosses a chunk boundary — drop
        jstart, _, owner = offsets[oi]
        if owner.get("arithmetic_ok") is False:
            continue                 # row anchors inside a flagged window
        rel_s = row_s - jstart
        rel_e = row_e - jstart
        chunk_text = owner.get("content") or ""
        if not (0 <= rel_s <= rel_e <= len(chunk_text)):
            continue
        row_text = chunk_text[rel_s:rel_e]
        if row_text != joined[row_s:row_e]:
            continue
        chunk_abs = owner["char_start"] or 0
        c["chunk_hash"] = owner["chunk_hash"]
        c["char_start"] = chunk_abs + rel_s
        c["char_end"] = chunk_abs + rel_e
        c["row_text"] = row_text
        verified.append(c)
    return verified, excluded


def _owner_index(offsets: List[Tuple[int, int, Dict[str, Any]]],
                 join_pos: int) -> Optional[int]:
    for i, (jstart, jend, _ch) in enumerate(offsets):
        if jstart <= join_pos < jend:
            return i
    return None


# ===========================================================================
# RECONCILIATION — dual-key, exact or <=0.5% cross-scale (Amendment 4)
# ===========================================================================
_TOLERANCE = Decimal("0.5")     # percent


def reconcile_rows(candidate: Dict[str, Any],
                   xbrl_facts: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """B.1.1: no XBRL counterpart -> unreconciled (fail-closed; absence
    of contradiction is NEVER agreement). Agreement (exact or within
    0.5% — the proven rounding-slack rule, e.g. $91.7B vs $91,650M)
    flips the row to 'span_xbrl_reconciled', which is also the B.1.5
    three-way confirmation that grants a derived Q4 fact authority."""
    key_metric = candidate["metric_key"]
    if key_metric not in PATH_A_METRICS:
        return False, "unreconciled_fact"
    comp = (candidate.get("company") or "").lower()
    fact = next((f for f in xbrl_facts
                 if (f.get("company") or "").lower() == comp
                 and f.get("metric") == key_metric
                 and f.get("period") == candidate["period"]), None)
    if fact is None:
        return False, "unreconciled_fact"
    try:
        a = Decimal(str(candidate["value_usd"]))
        b = Decimal(str(fact["value"]))
    except Exception:
        return False, "unreconciled_fact"
    if b == 0:
        return False, "unreconciled_fact"
    diff_pct = abs(a - b) / abs(b) * 100
    if diff_pct <= _TOLERANCE:
        return True, "span_xbrl_reconciled"
    return False, "unreconciled_fact"              # mismatch — never serve


# ===========================================================================
# A.4 / B.1.2 — THE EXACT-MATCH ROUTING GUARD (Phase-1 primitive, shipped
# as pure functions + regression tests BEFORE Path A goes live)
# ===========================================================================
_ENTITY_ALIASES: Dict[str, str] = {
    "apple": "Apple", "aapl": "Apple", "apple inc": "Apple",
    "meta": "Meta", "meta platforms": "Meta", "fb": "Meta",
    "facebook": "Meta",
    "tesla": "Tesla", "tsla": "Tesla", "tesla inc": "Tesla",
}

# metric phrases, longest-first so "total net sales" beats "net sales"
_METRIC_PHRASES: List[Tuple[str, str]] = [
    ("earnings per share", "eps_diluted"),
    ("diluted eps", "eps_diluted"),
    ("total net sales", "revenue"),
    ("total revenues", "revenue"),
    ("total revenue", "revenue"),
    ("net sales", "revenue"),
    ("top line", "revenue"),
    ("net income", "net_income"),
    ("bottom line", "net_income"),
    ("revenues", "revenue"),
    ("revenue", "revenue"),
    ("eps", "eps_diluted"),
    ("gross margin", "gross_margin"),
    ("gross profit", "gross_margin"),
    ("operating income", "operating_income"),
]

# A qualifier immediately before a metric noun makes it a SEGMENT /
# derived metric, not the consolidated one ("ad revenue", "iPhone
# revenue") — Amendment 2: ambiguity demotes the ENTIRE query.
_METRIC_QUALIFIERS = frozenset({
    "ad", "advertising", "segment", "segments", "product", "products",
    "service", "services", "iphone", "mac", "ipad", "wearables",
    "automotive", "energy", "digital", "apps", "app", "labs", "other",
    "us", "u.s.", "international", "overseas", "china", "retail",
})

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def canonical_entities(text: str) -> List[str]:
    """Deterministic entity resolution: alias/ngram dictionary only,
    ordered by FIRST APPEARANCE in the text, no fuzzy matching."""
    low = " " + re.sub(r"[^a-z0-9.,'\s]", " ", text.lower()) + " "
    found: Dict[str, int] = {}       # canonical -> first match position
    for alias, canon in _ENTITY_ALIASES.items():
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(alias)
                         + r"(?![a-z0-9])")
        m = pat.search(low)
        if m and canon not in found:
            found[canon] = m.start()
    return [c for c, _ in sorted(found.items(), key=lambda kv: kv[1])]


def canonical_metric(text: str) -> Tuple[str, Optional[str]]:
    """Returns ('metric', key) | ('segment', key) | ('ambiguous', None) |
    ('none', None). A metric noun preceded by a qualifier ("ad revenue",
    "iPhone revenue") is a segment metric — Amendment 2 demotes it;
    a query mixing qualified and unqualified mentions ("ad revenue vs
    total revenue") is ambiguous and demotes too."""
    low = " " + re.sub(r"[^a-z0-9\s]", " ", text.lower()) + " "
    hits: Dict[str, List[bool]] = {}
    for phrase, key in _METRIC_PHRASES:
        pat = re.compile(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])")
        for m in pat.finditer(low):
            before = low[:m.start()].strip()
            prev = before.split(" ")[-1] if before else ""
            hits.setdefault(key, []).append(prev in _METRIC_QUALIFIERS)
    if not hits:
        return "none", None
    if len(hits) > 1:
        return "ambiguous", None           # multi-metric question
    key, quals = next(iter(hits.items()))
    if all(quals):
        return "segment", key
    if any(quals):
        return "ambiguous", None           # segment + consolidated mixed
    return "metric", key


def canonical_periods(text: str) -> List[str]:
    """Deterministic period labels; 'Q4' alone (no year) resolves to
    nothing — a bare quarter is ambiguous across years, so it demotes."""
    low = text.lower()
    periods: List[str] = []

    def _add(p: str) -> None:
        if p not in periods:
            periods.append(p)

    for m in re.finditer(r"\bq([1-4])[\s,\-.]?(20\d{2})\b", low):
        _add(f"Q{m.group(1)}-{m.group(2)}")
    for m in re.finditer(
            r"\b(first|second|third|fourth)[\s\-]*quarter(?:\s+of|"
            r"\s+in|,)?\s*(20\d{2})?\b", low):
        if m.group(2):
            _add(f"Q{_ORDINALS[m.group(1)]}-{m.group(2)}")
    for m in re.finditer(r"\bfy[\s\-]*(20\d{2})\b", low):
        _add(f"FY-{m.group(1)}")
    for m in re.finditer(r"\bfiscal(?:\s+year)?\s+(20\d{2})\b", low):
        _add(f"FY-{m.group(1)}")
    for m in re.finditer(r"\bfull[\s\-]year(?:\s+(?:of|in))?\s*(20\d{2})\b",
                         low):
        _add(f"FY-{m.group(1)}")
    if not periods:
        m = re.search(r"\b(20\d{2})\b", low)
        if m:
            _add(f"FY-{m.group(1)}")     # bare year — full-year reading
    return periods


def path_a_decision(query: str,
                    fact_keys: Set[Tuple[str, str, str]]) -> Dict[str, Any]:
    """Amendment 2 + 3: Path A is honored ONLY when every parsed
    (entity, metric, period) triple maps EXACTLY to a reconciled
    fact_rows key. Any ambiguity, any missing triple (comparatives
    demote ATOMICALLY — no split-brain answers), any segment metric:
    demote the whole query to the fleet."""
    entities = canonical_entities(query)
    kind, metric = canonical_metric(query)
    periods = canonical_periods(query)
    if not entities:
        return {"path": "fleet", "reason": "no_entity_resolved",
                "resolved": []}
    if kind == "none":
        return {"path": "fleet", "reason": "no_metric_resolved",
                "resolved": []}
    if kind == "segment":
        return {"path": "fleet", "reason": "segment_metric_not_in_coverage",
                "resolved": []}
    if kind == "ambiguous":
        return {"path": "fleet", "reason": "metric_ambiguity", "resolved": []}
    if not periods:
        return {"path": "fleet", "reason": "no_period_resolved",
                "resolved": []}
    triples = {(e, metric, p) for e in entities for p in periods}
    resolved = sorted(t for t in triples if t in fact_keys)
    if len(resolved) != len(triples):
        missing = sorted(triples - set(resolved))
        return {"path": "fleet", "reason": "triple_not_in_fact_store",
                "resolved": [], "missing": missing}
    return {"path": "fact", "reason": "exact_match", "resolved": resolved}


# ===========================================================================
# A.5 — FUZZ OPERATORS over the routing guard (the 1,000-mutation
# harness pattern applied to fact-store routing; each mutation must
# resolve differently or demote — NEVER to the original triple)
# ===========================================================================
def op_entity_swap(query: str) -> str:
    ents = canonical_entities(query)
    pool = [c for c in ("Apple", "Meta", "Tesla") if c not in ents]
    if not ents or not pool:
        return query
    other = pool[0].lower()
    low = query.lower()
    i = low.find(ents[0].lower())
    return query[:i] + other + query[i + len(ents[0]):]


def op_metric_noun_swap(query: str) -> str:
    kind, metric = canonical_metric(query)
    swaps = {"revenue": "net income", "net_income": "total net sales",
             "eps_diluted": "net income"}
    if kind != "metric" or metric not in swaps:
        return query
    for phrase, key in _METRIC_PHRASES:
        if key == metric:
            i = query.lower().find(phrase)
            if i >= 0:
                return query[:i] + swaps[metric] + query[i + len(phrase):]
    return query


def op_period_swap(query: str) -> str:
    """Rewrites the FIRST 20xx year to a different one (2023 -> 2024,
    anything else -> 2023) — the mutated query must never resolve to
    the original triple."""
    m = re.search(r"\b20\d{2}\b", query)
    if not m:
        return query
    old = m.group(0)
    new = "2024" if old == "2023" else "2023"
    return query[:m.start()] + new + query[m.end():]


def op_qualifier_inject(query: str) -> str:
    kind, metric = canonical_metric(query)
    if kind != "metric":
        return query
    for phrase, key in _METRIC_PHRASES:
        if key == metric:
            i = query.lower().find(phrase)
            if i >= 0:
                return query[:i] + "advertising " + query[i:]
    return query


FUZZ_OPERATORS = [op_entity_swap, op_metric_noun_swap, op_period_swap,
                  op_qualifier_inject]
