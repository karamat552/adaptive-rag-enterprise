"""Generate Karamat Shirgaonkar's one-page ATS-compliant resume PDF (A4).

Usage:  python scripts/generate_resume.py
Output: Karamat_Shirgaonkar_AI_Resume.pdf (repo root)

Single-column, standard fonts, no tables/graphics — parses cleanly in
ATS extractors. Not committed to the repo (personal job-search artifact).
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

OUT = "Karamat_Shirgaonkar_AI_Resume.pdf"

PAGE_W, PAGE_H = A4
ML, MR, MT, MB = 0.5 * inch, 0.5 * inch, 0.42 * inch, 0.38 * inch
TEXT_W = PAGE_W - ML - MR

INK = HexColor("#111111")
RULE = HexColor("#333333")

F_BODY = "Helvetica"
F_BOLD = "Helvetica-Bold"

SZ_NAME, SZ_TAG, SZ_CONTACT = 15.5, 9.5, 8.4
SZ_SECT, SZ_BODY, SZ_META = 10.2, 8.9, 8.3
LD = 10.7          # body leading


def wrap(text, font, size, width):
    """Greedy word-wrap; returns list of lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class Resume:
    def __init__(self, path):
        self.c = canvas.Canvas(path, pagesize=A4)
        self.y = PAGE_H - MT

    # ---- primitives -------------------------------------------------
    def text(self, s, font=F_BODY, size=SZ_BODY, x=ML, y=None, color=INK):
        self.c.setFont(font, size)
        self.c.setFillColor(color)
        self.c.drawString(x, self.y if y is None else y, s)

    def para(self, s, size=SZ_BODY, font=F_BODY, indent=0, leading=LD,
             space_after=2.0):
        for ln in wrap(s, font, size, TEXT_W - indent):
            self.y -= leading
            self.text(ln, font, size, x=ML + indent)
        self.y -= space_after

    def title(self, s, meta=None):
        """Bold entry title (advances a full line first — never overlaps)."""
        self.y -= 10.4
        self.text(s, F_BOLD, SZ_BODY)
        if meta:
            self.y -= 9.7
            for ln in wrap(meta, F_BODY, SZ_META, TEXT_W):
                self.text(ln, F_BODY, SZ_META)
                self.y -= 9.4
            self.y -= 1.4

    def section(self, title):
        self.y -= 2.0
        self.c.setStrokeColor(RULE)
        self.c.setLineWidth(0.8)
        self.c.line(ML, self.y - 4.2, PAGE_W - MR, self.y - 4.2)
        self.y -= 10.2
        self.text(title, F_BOLD, SZ_SECT)
        self.y -= 2.4

    def bullet(self, s, size=SZ_BODY):
        lines = wrap(s, F_BODY, size, TEXT_W - 11)
        self.y -= LD
        self.text("\u2022", F_BODY, size, x=ML + 2)
        for i, ln in enumerate(lines):
            if i:
                self.y -= LD
            self.text(ln, F_BODY, size, x=ML + 11)
        self.y -= 2.2

    def save(self):
        self.c.save()


# =====================================================================
# Content
# =====================================================================
r = Resume(OUT)

# ---- Header ----
r.y -= 2
r.text("KARAMAT NIJAMUDDIN SHIRGAONKAR", F_BOLD, SZ_NAME,
       x=(PAGE_W - stringWidth("KARAMAT NIJAMUDDIN SHIRGAONKAR",
                               F_BOLD, SZ_NAME)) / 2)
r.y -= 12.5
tag = "AI / LLM Systems Engineer  \u2022  Data Engineer"
r.text(tag, F_BODY, SZ_TAG, x=(PAGE_W - stringWidth(tag, F_BODY, SZ_TAG)) / 2)
r.y -= 11.5
contact = ("karamatsh19@gmail.com | +91 90829 86540 | Mumbai, India | "
           "github.com/karamat552 | linkedin.com/in/karamat552 | "
           "Live AI demo: adaptive-rag-enterprise.streamlit.app")
for ln in wrap(contact, F_BODY, SZ_CONTACT, TEXT_W):
    r.text(ln, F_BODY, SZ_CONTACT,
           x=(PAGE_W - stringWidth(ln, F_BODY, SZ_CONTACT)) / 2)
    r.y -= 10

# ---- Summary ----
r.section("PROFESSIONAL SUMMARY")
r.para("Computer Engineering graduate who builds and ships production AI "
       "systems: a live-deployed, self-correcting multi-agent RAG platform "
       "(LangGraph, FastAPI, PostgreSQL/pgvector) guarded by a 311-test "
       "adversarial regression suite, five deterministic hallucination gates, "
       "and cryptographic offline-verifiable provenance chains \u2014 with a "
       "zero-fabrication invariant across every evaluation battery \u2014 plus "
       "an Azure Medallion lakehouse pipeline. Specializes in LLM systems "
       "engineered to fail safely: zero-trust auditing, refusal over "
       "hallucination, token-cost discipline. Targeting AI/LLM Engineer and "
       "Data Engineer roles.")

# ---- Skills ----
r.section("TECHNICAL SKILLS")
skills = [
    ("Languages: ", "Python, SQL (PostgreSQL), PySpark"),
    ("AI/LLM & RAG: ", "LangGraph, LangChain, hybrid RRF search, pgvector "
     "HNSW, FlashRank reranking, FastEmbed/bge ONNX embeddings, semantic "
     "caching, structured outputs (Pydantic), multi-provider failover, "
     "RAGAS evaluation, hallucination detection"),
    ("Backend & APIs: ", "FastAPI, Uvicorn, REST, SSE streaming, API-key "
     "tenant authentication, token-bucket rate limiting, Prometheus metrics"),
    ("Verification: ", "SHA-256 receipt chains, Ed25519 attestation, "
     "adversarial fuzzing, chaos engineering, zero-trust LLM auditing"),
    ("Cloud & Big Data: ", "Azure Data Factory, Databricks, ADLS Gen2, "
     "Synapse, Medallion architecture"),
    ("Data & BI: ", "PostgreSQL (Row-Level Security, CTEs), pgvector, Neon, "
     "Power BI star schema, Pandas"),
    ("DevOps & Testing: ", "Docker, GitHub Actions CI, pytest (311-test "
     "suite), Git, Streamlit, httpx"),
    ("Classic ML: ", "Scikit-learn (Logistic Regression, Random Forest, "
     "XGBoost), EDA, feature engineering"),
]
for lead, rest in skills:
    r.para(lead + rest)

# ---- Projects ----
r.section("FEATURED PROJECTS")
r.title("Adaptive RAG \u2014 Enterprise Financial Intelligence Platform",
        meta="LangGraph \u2022 FastAPI \u2022 PostgreSQL/pgvector (RLS) \u2022 "
             "Docker \u2022 GitHub Actions CI \u2022 Streamlit  \u2014  "
             "github.com/karamat552/adaptive-rag-enterprise | Live app + "
             "public API (green CI badge)")

bullets = [
    "Architected and deployed a self-correcting multi-agent RAG platform "
    "(3 parallel specialist agents \u2192 CIO synthesizer \u2192 zero-trust "
    "compliance auditor) over 186 indexed SEC-filing chunks, fronted by five "
    "deterministic zero-token gates (citation bounds, unit/scale, growth "
    "direction, XBRL cross-check, echo-guard) that intercept fabrications "
    "before the LLM auditor runs; unverified answers trigger bounded rewrites "
    "or verified refusal.",

    "Hold a zero-fabrication invariant across every adversarial battery, "
    "provider, and outage storm \u2014 90% gold-set recall with every miss "
    "root-caused in a public 22-class failure-mode ledger and 15+ "
    "Architecture Decision Records.",

    "Built a cryptographic receipt chain (claim \u2192 evidence span \u2192 "
    "page transcript \u2192 SHA-256 \u2192 source PDF anchor) with "
    "Ed25519-signed offline compliance bundles \u2014 external auditors "
    "re-verify any certified answer air-gapped through a stdlib-only verifier "
    "that trusts nothing about the producing system.",

    "Engineered multi-provider LLM failover (Groq \u2192 NVIDIA NIM \u2192 "
    "TokenRouter + executive peer rescue) with quota-aware cooldowns, "
    "abort-on-hint self-pacing, and per-endpoint reasoning-headroom caps; cut "
    "LLM calls 37% and expansion calls 94% via conditional routing and "
    "specialist pruning.",

    "Built fail-closed multi-tenant persistence on PostgreSQL + pgvector: "
    "least-privilege runtime role under Row-Level Security, hybrid RRF search "
    "(HNSW cosine + trigram keyword, k=60) with local ONNX embeddings and "
    "reranking, and corpus-epoch cache invalidation for zero-downtime "
    "re-ingestion.",

    "Hardened the FastAPI serving layer \u2014 API-key tenant authentication "
    "live-verified on production (401/403), per-IP token-bucket rate limiting, "
    "SSE streaming with keep-alive heartbeats, client-disconnect cancellation "
    "that halts in-flight LLM token spend, split liveness/readiness probes, "
    "Prometheus histograms, and request-ID-correlated structured logs.",

    "Grew the regression suite 43 \u2192 311 tests through five adversarial "
    "trace audits and 50+ fixed production defects: a 1,000-mutation fuzz "
    "harness (11 forgery operators, zero false accepts), a 17-vector tamper "
    "suite on the offline verification path, and 7 chaos contracts proving "
    "every dependency kill fails closed \u2014 shipped in a 3-job CI pipeline "
    "(unit \u2192 RLS-bound pgvector integration \u2192 Docker build), where "
    "verification passes exposed real infra bugs (pgvector bootstrap "
    "deadlock, cold-start-induced LLM 429s).",
]
for b in bullets:
    r.bullet(b)

r.title("Azure End-to-End Medallion Lakehouse Pipeline",
        meta="Azure Data Factory \u2022 Databricks (PySpark) \u2022 ADLS Gen2 "
             "\u2022 Synapse Serverless SQL \u2022 Power BI  \u2014  "
             "github.com/karamat552/Azure-Data-Engineering-Lakehouse")
for b in [
    "Architected a Bronze-Silver-Gold pipeline: ADF ingestion from REST "
    "sources into ADLS Gen2, then PySpark cleaning, schema enforcement, and "
    "type transformations in Databricks via Managed Identity mounts \u2014 "
    "writing Silver-layer Parquet.",
    "Published Gold-layer Fact/Dimension tables via Synapse Serverless SQL "
    "(OPENROWSET + CETAS) and modeled a Power BI star schema (Import mode) "
    "\u2014 a production-style flow from raw ingestion to business-ready "
    "reporting.",
]:
    r.bullet(b)

r.title("Loan Approval Prediction",
        meta="Python \u2022 Scikit-learn \u2022 Pandas  \u2014  "
             "github.com/karamat552/loan-approval-prediction")
r.bullet("Benchmarked 6 Scikit-learn classifiers on a real-world loan dataset "
         "with evidence-based selection (confusion-matrix driven) and "
         "domain-specific null imputation.")

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
    r.y -= 10.4
    r.text(deg, F_BOLD, SZ_BODY)
    r.text(right, F_BODY, SZ_BODY,
           x=PAGE_W - MR - stringWidth(right, F_BODY, SZ_BODY))

r.save()

bottom_gap = r.y - MB
print(f"generated {OUT}  |  bottom gap: {bottom_gap:.1f}pt "
      f"({'FITS one page' if bottom_gap > 0 else '!! OVERFLOW'})")
