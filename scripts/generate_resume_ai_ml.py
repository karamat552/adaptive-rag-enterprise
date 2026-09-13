"""Generate Karamat Shirgaonkar's AI/ML-targeted resume PDF (A4).

Usage:  python scripts/generate_resume_ai_ml.py
Output: Karamat_Shirgaonkar_AI_ML_Resume.pdf (repo root)

Replicates the v1 resume's design system (extracted from the original
PDF's content stream):
  name   20.5pt Helvetica-Bold   ink   #202220   centered
  tagline 11pt                   orange #D4572D  centered
  contact  grey #7B8287 with blue #0000EE underlined clickable links
  section 12pt bold orange + orange rule 0.8w
  body    ink; meta lines grey
Not committed to the repo (personal job-search artifact).
"""
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

OUT = "Karamat_Shirgaonkar_AI_ML_Resume.pdf"

PAGE_W, PAGE_H = A4
ML = MR = 45.7
TEXT_W = PAGE_W - ML - MR          # 503.9 — same as v1

INK = HexColor("#202220")
ORANGE = HexColor("#D4572D")
GREY = HexColor("#7B8287")
LINK = HexColor("#0000EE")

F, FB = "Helvetica", "Helvetica-Bold"

SZ_BODY, LD = 9.3, 10.5
SZ_META, LD_META = 9.2, 10.4


def wrap_runs(runs, size, width):
    """Word-wrap a list of (text, style) runs.

    style: 'ink' | 'grey' | 'bold' | ('link', url)
    Returns list of lines, each a list of (token, style).
    """
    toks = []
    for text, style in runs:
        for piece in re.split(r"(\s+)", text):
            if piece:
                toks.append((piece, style))
    sp_w = stringWidth(" ", F, size)
    lines, cur, cur_w = [], [], 0.0

    def tok_font(style):
        return FB if style == "bold" else F

    for tok, style in toks:
        if tok.isspace():
            if cur:
                cur.append((" ", style))
                cur_w += sp_w
            continue
        w = stringWidth(tok, tok_font(style), size)
        if cur and cur_w + w > width:
            while cur and cur[-1][0] == " ":
                cur.pop()
                cur_w -= sp_w
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur.append((tok, style))
        cur_w += w
    if cur:
        lines.append(cur)
    return lines


class Resume:
    def __init__(self, path):
        self.c = canvas.Canvas(path, pagesize=A4)
        self.y = PAGE_H - 58

    def _draw(self, line, size, x0, centered_within=None):
        """Draw one wrapped line; registers link rects + blue underlines."""
        line_w = sum(stringWidth(t, FB if s == "bold" else F, size)
                     for t, s in line)
        x = x0 if centered_within is None else \
            x0 + max((centered_within - line_w) / 2, 0)
        cx, base = x, self.y
        for tok, style in line:
            if tok == " ":
                cx += stringWidth(" ", F, size)
                continue
            font = FB if style == "bold" else F
            is_link = isinstance(style, tuple)
            color = LINK if is_link else {"ink": INK, "grey": GREY,
                                          "bold": INK}[style]
            self.c.setFont(font, size)
            self.c.setFillColor(color)
            self.c.drawString(cx, base, tok)
            w = stringWidth(tok, font, size)
            if is_link:
                url = style[1]
                self.c.setStrokeColor(LINK)
                self.c.setLineWidth(0.6)
                self.c.line(cx, base - 2.1, cx + w, base - 2.1)
                self.c.linkURL(url, (cx - 1, base - 2.8, cx + w + 1,
                                     base + size * 0.3), relative=0)
            cx += w

    def rich(self, runs, size=SZ_BODY, leading=LD, indent=0, space_after=2.0,
             center=False):
        lines = wrap_runs(runs, size, TEXT_W - indent)
        for line in lines:
            self.y -= leading
            self._draw(line, size, ML + indent,
                       centered_within=(TEXT_W - indent) if center else None)
        self.y -= space_after

    def section(self, title):
        self.y -= 13
        self.c.setStrokeColor(ORANGE)
        self.c.setLineWidth(0.8)
        self.c.line(ML, self.y - 4.3, PAGE_W - MR, self.y - 4.3)
        self.c.setFont(FB, 12)
        self.c.setFillColor(ORANGE)
        self.c.drawString(ML, self.y, title)
        self.y -= 12

    def bullet(self, runs, size=SZ_BODY, leading=LD):
        lines = wrap_runs(runs, size, TEXT_W - 12)
        for i, line in enumerate(lines):
            self.y -= leading
            if i == 0:
                self.c.setFont(F, size)
                self.c.setFillColor(INK)
                self.c.drawString(ML + 2, self.y, "\u2022")
            self._draw(line, size, ML + 12)
        self.y -= 1.6

    def project_title(self, title, meta_runs):
        self.y -= 11.5
        self.c.setFont(FB, 9.6)
        self.c.setFillColor(INK)
        self.c.drawString(ML, self.y, title)
        self.rich(meta_runs, size=SZ_META, leading=LD_META, space_after=1.6)

    def save(self):
        self.c.save()


# =====================================================================
r = Resume(OUT)

# ---- Header (v1 style: centered ink name / orange tagline / grey contact) ----
r.c.setFont(FB, 20.5)
r.c.setFillColor(INK)
r.c.drawCentredString(PAGE_W / 2, r.y, "KARAMAT NIJAMUDDIN SHIRGAONKAR")
r.y -= 15
r.c.setFont(F, 11)
r.c.setFillColor(ORANGE)
r.c.drawCentredString(PAGE_W / 2, r.y,
                      "AI / ML Engineer  \u2022  LLM Systems Engineer")
r.y -= 14
GH = ("link", "https://github.com/karamat552")
LI = ("link", "https://linkedin.com/in/karamat552")
DEMO = ("link", "https://adaptive-rag-enterprise.streamlit.app")
r.rich([("karamatsh19@gmail.com | +91 90829 86540 | Mumbai, India | ", "grey"),
        ("github.com/karamat552", GH),
        (" | ", "grey"),
        ("linkedin.com/in/karamat552", LI),
        (" | Live AI demo: ", "grey"),
        ("adaptive-rag-enterprise.streamlit.app", DEMO)],
       leading=11.2, space_after=3, center=True)

# ---- Summary (AI/ML targeting) ----
r.section("PROFESSIONAL SUMMARY")
r.rich([
    ("Computer Engineering graduate who builds and ships production AI "
     "systems: a live-deployed, self-correcting multi-agent RAG platform "
     "(LangGraph, FastAPI, PostgreSQL/pgvector) gated by a ", "ink"),
    ("311-test adversarial regression suite", "bold"),
    (", five deterministic hallucination gates, and cryptographic "
     "offline-verifiable provenance chains \u2014 zero fabricated certified "
     "answers across every evaluation battery. Builds LLM and ML systems "
     "engineered to fail safely: zero-trust auditing, refusal over "
     "hallucination, token-cost discipline. Targeting AI/ML Engineer and "
     "LLM Systems roles.", "ink"),
])

# ---- Skills (AI/ML ordering: RAG + ML first) ----
r.section("TECHNICAL SKILLS")
skills = [
    ("AI/LLM & RAG: ", "LangGraph, LangChain, RAG (hybrid RRF search, "
     "pgvector HNSW, FlashRank reranking), FastEmbed/bge ONNX embeddings, "
     "semantic caching, structured LLM outputs (Pydantic), multi-provider "
     "failover (Groq/NVIDIA/Gemini)"),
    ("ML & Evaluation: ", "Scikit-learn (Logistic Regression, Random "
     "Forest, XGBoost), RAGAS-methodology faithfulness/relevancy/precision, "
     "hallucination detection, adversarial fuzzing, "
     "chaos engineering, EDA, feature engineering"),
    ("Languages: ", "Python, SQL (PostgreSQL), PySpark"),
    ("Backend & APIs: ", "FastAPI, Uvicorn, REST, SSE streaming, API-key "
     "tenant authentication, token-bucket rate limiting, Prometheus metrics"),
    ("Data & Cloud: ", "PostgreSQL (Row-Level Security, CTEs), pgvector, "
     "Neon, Pandas; Azure Data Factory, Databricks, ADLS Gen2, Synapse; "
     "Docker, GitHub Actions CI, pytest (311-test suite), Git, Streamlit"),
]
for lead, rest in skills:
    r.rich([(lead, "bold"), (rest, "ink")], space_after=1.2)

# ---- Projects ----
r.section("FEATURED PROJECTS")
R1 = ("link", "https://github.com/karamat552/adaptive-rag-enterprise")
r.project_title(
    "Adaptive RAG \u2014 Enterprise Financial Intelligence Platform",
    [("LangGraph \u2022 FastAPI \u2022 PostgreSQL/pgvector (RLS) \u2022 "
      "Docker \u2022 GitHub Actions CI \u2022 Streamlit  \u2014  ", "grey"),
     ("github.com/karamat552/adaptive-rag-enterprise", R1),
     (" | Live app + public API (green CI badge)", "grey")])

bullets = [
    [("Architected and deployed a self-correcting multi-agent RAG platform "
      "(3 parallel specialist agents \u2192 CIO synthesizer \u2192 "
      "zero-trust compliance auditor) over 186 indexed SEC-filing chunks, "
      "fronted by five deterministic zero-token gates (citation bounds, "
      "unit/scale, growth direction, XBRL cross-check, echo-guard) that "
      "intercept fabrications before the LLM auditor runs; unverified "
      "answers trigger bounded rewrites or verified refusal.", "ink")],
    [("Holds a zero-fabrication invariant across every adversarial battery, "
      "provider, and outage storm \u2014 90% gold-set recall with every miss "
      "root-caused in a public 22-class failure-mode ledger and 15+ "
      "Architecture Decision Records.", "ink")],
    [("Built a cryptographic receipt chain (claim \u2192 evidence span "
      "\u2192 page transcript \u2192 SHA-256 \u2192 source PDF anchor) with "
      "Ed25519-signed offline compliance bundles \u2014 auditors re-verify "
      "any certified answer air-gapped through a stdlib-only verifier that "
      "trusts nothing about the producing system.", "ink")],
    [("Engineered multi-provider LLM failover (Groq \u2192 NVIDIA NIM "
      "\u2192 TokenRouter + executive peer rescue) with quota-aware "
      "cooldowns, abort-on-hint self-pacing, and reasoning-headroom caps; "
      "cut LLM calls 37% and expansion calls 94% via conditional routing "
      "and specialist pruning.", "ink")],
    [("Built fail-closed multi-tenant persistence: least-privilege runtime "
      "role under Row-Level Security, hybrid RRF search (HNSW cosine + "
      "trigram keyword, k=60) with local ONNX embeddings, and corpus-epoch "
      "cache invalidation for zero-downtime re-ingestion.", "ink")],
    [("Hardened the FastAPI serving layer \u2014 API-key tenant "
      "authentication live-verified on production (401/403), per-IP "
      "token-bucket rate limiting, SSE streaming with keep-alive "
      "heartbeats, client-disconnect cancellation, and request-ID-correlated "
      "structured logs.", "ink")],
    [("Grew the regression suite 43 \u2192 311 tests through five "
      "adversarial trace audits and 50+ fixed production defects: a "
      "1,000-mutation fuzz harness (11 forgery operators, zero false "
      "accepts), a 17-vector tamper suite on the offline path, and 7 chaos "
      "contracts proving every dependency kill fails closed \u2014 shipped "
      "in a 3-job CI pipeline (unit \u2192 RLS-bound pgvector integration "
      "\u2192 Docker build), where verification passes exposed real infra "
      "bugs (pgvector bootstrap deadlock, cold-start-induced LLM 429s).",
      "ink")],
]
for b in bullets:
    r.bullet(b)

R2 = ("link", "https://github.com/karamat552/Azure-Data-Engineering-Lakehouse")
r.project_title(
    "Azure End-to-End Medallion Lakehouse Pipeline",
    [("Azure Data Factory \u2022 Databricks (PySpark) \u2022 ADLS Gen2 "
      "\u2022 Synapse Serverless SQL \u2022 Power BI  \u2014  ", "grey"),
     ("github.com/karamat552/Azure-Data-Engineering-Lakehouse", R2)])
r.bullet([("Architected a Bronze-Silver-Gold pipeline: ADF ingestion from "
           "REST sources into ADLS Gen2, then PySpark cleaning, schema "
           "enforcement, and type transformations in Databricks via Managed "
           "Identity mounts \u2014 publishing Gold-layer Fact/Dimension "
           "tables via Synapse Serverless SQL (OPENROWSET + CETAS) into a "
           "Power BI star schema.", "ink")])

R3 = ("link", "https://github.com/karamat552/loan-approval-prediction")
r.project_title(
    "Loan Approval Prediction",
    [("Python \u2022 Scikit-learn \u2022 Pandas  \u2014  ", "grey"),
     ("github.com/karamat552/loan-approval-prediction", R3)])
r.bullet([("Benchmarked 6 Scikit-learn classifiers on a real-world loan "
           "dataset with evidence-based selection (confusion-matrix driven) "
           "and domain-specific null imputation.", "ink")])

# ---- Education ----
r.section("EDUCATION & CERTIFICATIONS")
edu = [
    ("B.E., Computer Engineering",
     "Rizvi College of Engineering, Mumbai | 2022 \u2013 2025"),
    ("Diploma, Computer Engineering",
     "Vidyalankar Polytechnic, Mumbai | 2019 \u2013 2022"),
    ("Complete Data Science Course", "Grow Data Skills | June 2026"),
]
for deg, right in edu:
    r.y -= 11
    r.c.setFont(FB, 9.3)
    r.c.setFillColor(INK)
    r.c.drawString(ML, r.y, deg)
    r.c.setFont(F, 9.3)
    r.c.setFillColor(GREY)
    r.c.drawString(PAGE_W - MR - stringWidth(right, F, 9.3), r.y, right)

r.save()

bottom_gap = r.y - 27.4
print(f"generated {OUT}  |  bottom gap: {bottom_gap:.1f}pt "
      f"({'FITS one page' if bottom_gap > 0 else '!! OVERFLOW'})")
