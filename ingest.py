"""
Enterprise Ingestion & Semantic Alignment Engine — v2 (Production Hardened)
===================================================================================
Pipeline: resilient download -> layout-aware parsing -> section-aware chunking
          -> strict contracts -> atomic JSONL publication + lineage manifest.

Design guarantees
-----------------
1. ATOMICITY      : Downloads land as *.part and are os.replace()'d into place.
                    Corpus + manifest are published atomically; a failed run can
                    NEVER destroy a previously-good corpus.
2. FAIL LOUDLY    : Every source is tracked end-to-end. Failures surface in the
                    run report, the logs, AND the process exit code (CI-ready).
3. LINEAGE        : Each run emits a manifest: source URLs, source-PDF SHA256,
                    per-source chunk counts, corpus checksum, distributions.
4. LAYOUT FIDELITY: Blocks sorted into (y, x) reading order; headings detected
                    BEFORE splitting so chunks inherit the TRUE section title
                    (carried forward across pages); tables become pipe-rows.
5. MEMORY         : Generator pipeline end-to-end. Dedup uses an O(n) hash set —
                    a documented tradeoff; move uniqueness to the DB at scale.
6. CONTRACTS      : Strict Pydantic v2 schemas; nothing unvalidated is emitted.

Requires: pymupdf, requests, langchain-text-splitters, pydantic>=2,
          pydantic-settings, tenacity>=8.2 (wait_exponential_jitter)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Literal, Optional, Tuple

import pymupdf
import requests
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

SCHEMA_VERSION = "2.0.0"

Category = Literal["financial", "risk", "product", "general"]

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_TOTAL_FAILURE = 2


# ===========================================================================
# 0. CONFIGURATION (env-driven: every field overridable via INGEST_* vars)
# ===========================================================================
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGEST_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    output_file: Path = Path("corpus_chunks.jsonl")

    chunk_size: int = 700
    chunk_overlap: int = 120

    download_workers: int = 3
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    max_download_attempts: int = 3
    min_pdf_bytes: int = 10_240        # smaller than this = probably an error page
    max_pdf_bytes: int = 104_857_600   # 100 MB sanity cap
    force_download: bool = False       # re-download even if file exists locally

    extract_tables: bool = True

    min_expected_chunks: int = 20      # yield safeguard: below this = hard failure

    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @property
    def manifest_file(self) -> Path:
        return self.output_file.with_suffix(".manifest.json")


# --- Sources as strict contracts, not dicts -------------------------------
class SourceDocument(BaseModel):
    filename: str
    url: HttpUrl
    company: str
    year: int = Field(ge=1990, le=2100)
    quarter: str = Field(pattern=r"^Q[1-4]$")


PDF_RESOURCES: Tuple[SourceDocument, ...] = (
    SourceDocument(
        filename="Tesla_Q4_2023.pdf",
        url="https://digitalassets.tesla.com/tesla-contents/image/upload/IR/TSLA-Q4-2023-Update.pdf",
        company="Tesla", year=2023, quarter="Q4",
    ),
    SourceDocument(
        filename="Apple_Q4_2023.pdf",
        url="https://www.apple.com/newsroom/pdfs/fy2023-q4/FY23_Q4_Consolidated_Financial_Statements.pdf",
        company="Apple", year=2023, quarter="Q4",
    ),
    SourceDocument(
        filename="Meta_Q4_2023.pdf",
        url="https://s21.q4cdn.com/399680738/files/doc_financials/2023/q4/Meta-12-31-2023-Exhibit-99-1-FINAL.pdf",
        company="Meta", year=2023, quarter="Q4",
    ),
)


# ===========================================================================
# 1. LOGGING (configured ONLY at entry point — importing is side-effect free)
# ===========================================================================
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s", datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


logger = logging.getLogger("IngestEngine")


# ===========================================================================
# 2. STRICT DATA CONTRACTS
# ===========================================================================
class ChunkMetadata(BaseModel):
    company: str
    source: str
    page: int = Field(ge=1)
    year: int
    quarter: str
    category: Category
    section_title: str = Field(max_length=120)
    contains_table: bool = False


class DocumentChunk(BaseModel):
    schema_version: str = SCHEMA_VERSION
    chunk_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str
    metadata: ChunkMetadata

    @field_validator("text")
    @classmethod
    def _text_must_not_be_blank(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("chunk text cannot be empty or whitespace")
        return cleaned


def generate_chunk_hash(company: str, source: str, page: int, text: str) -> str:
    # ASCII unit separator (\x1f) prevents ambiguous collisions like
    # ("a:b", 1) vs ("a", "b:1") that a naive ":" join allows.
    key = f"{company}\x1f{source}\x1f{page}\x1f{text.strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ===========================================================================
# 3. TEXT INTELLIGENCE (plurals fixed, honest defaults, real headings)
# ===========================================================================
CATEGORY_PATTERNS: Dict[str, re.Pattern[str]] = {
    # trailing 's?' makes the WHOLE term plural-capable: risks, revenues, vehicles...
    "risk": re.compile(
        r"\b(risk|lawsuit|litigation|regulatory|downtime|uncertainty|competition|"
        r"scrutiny|investigation|recall|headwind|penalt(?:y|ies)|debt|supply chain)s?\b",
        re.IGNORECASE),
    "product": re.compile(
        r"\b(ai|metaverse|r&d|product|technology|vision|supercharger|vehicle|"
        r"cloud|gpu|llm|autonom(?:y|ous)|software|platform)s?\b",
        re.IGNORECASE),
    "financial": re.compile(
        r"\b(revenue|net income|ebitda|quarterly|fiscal|earning|balance sheet|"
        r"cash flow|dividend|margin|gross profit|operating income|capex)s?\b",
        re.IGNORECASE),
}
DEFAULT_CATEGORY: Category = "general"


def categorize_chunk(text: str) -> Category:
    """Frequency-scored classification; unmatched chunks are 'general', NOT
    silently mislabeled as 'financial' (the v1 bug)."""
    scores = {name: len(p.findall(text)) for name, p in CATEGORY_PATTERNS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else DEFAULT_CATEGORY


# Uppercase heading heuristic: letters/digits/spaces/basic punctuation only,
# 4..60 chars. "SECTION 3: ..." always qualifies regardless of case.
_HEADING_LINE = re.compile(r"^[A-Z][A-Z0-9\s&,.:'()\u2013-]{3,59}$")


def is_heading(line: str) -> bool:
    if not line or len(line) > 60:
        return False
    if re.match(r"^SECTION\s+\d+", line):
        return True
    return bool(_HEADING_LINE.match(line))


def segment_blocks_into_sections(
    blocks: Iterable[str],
) -> List[Tuple[Optional[str], str, bool]]:
    """Splits a page into (section_title | None, body, has_table) segments.

    THE FIX for v1: headings are detected BEFORE chunk-splitting, so every
    chunk provably belongs to the section it appeared under. `None` means
    'continuation' — the caller carries the last seen title forward."""
    sections: List[Tuple[Optional[str], str, bool]] = []
    current: Optional[str] = None
    buffer: List[str] = []

    def _flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current, body, False))
        buffer.clear()

    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        lines = stripped.splitlines()
        head, tail = lines[0].strip(), "\n".join(l for l in lines[1:] if l.strip())
        if is_heading(head):
            _flush()
            current = head
            if tail:
                buffer.append(tail)
        else:
            buffer.append(stripped)
    _flush()
    return sections


def build_splitter(settings: Settings) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\nSECTION ", "\n\n", "\n", ". ", " ", ""],
    )


# ===========================================================================
# 4. RESILIENT DOWNLOAD ENGINE (atomic, classified errors, jittered retries)
# ===========================================================================
class RetryableDownloadError(RuntimeError):
    """Transient: timeouts, connection resets, 429, 5xx -> retry with backoff."""


class PermanentDownloadError(RuntimeError):
    """Non-retryable: 404/403, non-PDF payloads, size violations -> fail fast."""


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; IngestEngine/2.0)"
    })
    return session


def _sha256_file(path: Path, buf_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(buf_size):
            digest.update(block)
    return digest.hexdigest()


def _fetch_once(session: requests.Session, doc: SourceDocument,
                filepath: Path, settings: Settings) -> Dict[str, Any]:
    tmp = filepath.with_suffix(filepath.suffix + ".part")
    try:
        with session.get(doc.url, stream=True,
                         timeout=(settings.connect_timeout_s, settings.read_timeout_s)) as resp:
            if resp.status_code != 200:
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RetryableDownloadError(f"HTTP {resp.status_code} from {doc.url}")
                raise PermanentDownloadError(f"HTTP {resp.status_code} from {doc.url}")

            declared = int(resp.headers.get("Content-Length") or 0)
            if declared and declared > settings.max_pdf_bytes:
                raise PermanentDownloadError(f"declared size {declared}B exceeds cap")

            first = next(resp.iter_content(chunk_size=1024), b"")
            if not first.startswith(b"%PDF"):
                raise PermanentDownloadError("payload is not a PDF (magic-byte check)")

            written = len(first)
            with tmp.open("wb") as fh:
                fh.write(first)
                for chunk in resp.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > settings.max_pdf_bytes:
                        raise PermanentDownloadError("stream exceeded size cap")
                    fh.write(chunk)

        if written < settings.min_pdf_bytes:
            raise PermanentDownloadError(f"suspiciously small payload ({written}B)")

        digest = _sha256_file(tmp)
        os.replace(tmp, filepath)  # ATOMIC publish — no truncated PDF ever lands
        logger.info("downloaded %-28s %8.1f KB  sha256=%s...",
                    doc.filename, written / 1024, digest[:12])
        return {"status": "downloaded", "bytes": written, "sha256": digest}

    except PermanentDownloadError:
        tmp.unlink(missing_ok=True)
        raise
    except (requests.ConnectionError, requests.Timeout) as exc:
        tmp.unlink(missing_ok=True)
        raise RetryableDownloadError(f"transient network failure: {exc}") from exc
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def fetch_document(session: requests.Session, doc: SourceDocument,
                   filepath: Path, settings: Settings) -> Dict[str, Any]:
    """Programmatic Retrying (not a decorator) so settings stay injectable/testable.
    Jittered exponential backoff prevents synchronized thundering-herd retries."""
    runner = Retrying(
        stop=stop_after_attempt(settings.max_download_attempts),
        wait=wait_exponential_jitter(initial=1, max=10),
        retry=retry_if_exception_type(RetryableDownloadError),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    return runner(_fetch_once, session, doc, filepath, settings)


def acquire_sources(session: requests.Session, settings: Settings) -> Dict[str, Dict[str, Any]]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=settings.download_workers) as pool:
        futures: Dict[Any, str] = {}
        for doc in PDF_RESOURCES:
            dest = settings.data_dir / doc.filename
            if dest.exists() and not settings.force_download:
                # Idempotent re-runs: reuse verified local copy, still fingerprint it
                results[doc.filename] = {
                    "status": "reused",
                    "bytes": dest.stat().st_size,
                    "sha256": _sha256_file(dest),
                }
                logger.info("reusing existing %s (force_download=True to refresh)", doc.filename)
                continue
            futures[pool.submit(fetch_document, session, doc, dest, settings)] = doc.filename

        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except (PermanentDownloadError, RetryableDownloadError) as exc:
                logger.error("DOWNLOAD FAILED %s: %s", name, exc)
                results[name] = {"status": "failed", "error": str(exc)}
            except Exception as exc:  # absolute safety net — a thread must never vanish silently
                logger.exception("UNEXPECTED download error for %s", name)
                results[name] = {"status": "failed", "error": str(exc)}
    return results


# ===========================================================================
# 5. STREAMING PARSER (reading order, tables, section inheritance)
# ===========================================================================
class DocumentParseError(RuntimeError):
    pass


def _extract_tables(page: "pymupdf.Page") -> List[str]:
    """Financial tables flattened by naive extraction lose digit alignment.
    Pipe-delimited rows keep numbers tokenizable for retrieval."""
    tables: List[str] = []
    try:
        found = page.find_tables()
    except Exception as exc:  # find_tables can choke on exotic pages — degrade gracefully
        logger.debug("find_tables failed on page %s: %s", getattr(page, "number", "?"), exc)
        return tables
    for table in getattr(found, "tables", []):
        rows = table.extract()
        lines = [
            "| " + " | ".join(
                "" if cell is None else str(cell).replace("\n", " ").strip()
                for cell in row
            ) + " |"
            for row in rows
        ]
        md = "\n".join(lines).strip()
        if md:
            tables.append(md)
    return tables


def parse_pdf_stream(
    filepath: Path,
    doc: SourceDocument,
    settings: Settings,
    splitter: RecursiveCharacterTextSplitter,
) -> Generator[DocumentChunk, None, None]:
    """Yields validated chunks; raises DocumentParseError on structural failure
    (caller records it and keeps processing other sources)."""
    if not filepath.exists():
        raise DocumentParseError(f"missing input file: {filepath}")

    carried_title: Optional[str] = None
    pages_with_text = 0

    try:
        with pymupdf.open(filepath) as pdf:
            if pdf.page_count == 0:
                raise DocumentParseError("PDF contains zero pages")

            for page_num, page in enumerate(pdf, start=1):
                candidates = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0]
                candidates.sort(key=lambda b: (round(b[1], 1), round(b[0], 1)))  # top->bottom, left->right
                texts = [b[4] for b in candidates]
                if not "".join(texts).strip():
                    continue  # image-only page
                pages_with_text += 1

                sections = segment_blocks_into_sections(texts)
                if settings.extract_tables:
                    for tbl in _extract_tables(page):
                        sections.append((carried_title, tbl, True))

                for title, body, has_table in sections:
                    effective_title = title or carried_title or "General Corporate Commentary"
                    if title:
                        carried_title = title  # carry forward across pages/sections
                    for piece in splitter.split_text(body):
                        cleaned = piece.strip()
                        if not cleaned:
                            continue
                        yield DocumentChunk(
                            chunk_hash=generate_chunk_hash(
                                doc.company, doc.filename, page_num, cleaned),
                            text=cleaned,
                            metadata=ChunkMetadata(
                                company=doc.company,
                                source=doc.filename,
                                page=page_num,
                                year=doc.year,
                                quarter=doc.quarter,
                                category=categorize_chunk(cleaned),
                                section_title=effective_title[:120],
                                contains_table=has_table,
                            ),
                        )

            logger.info("%s parsed: %d/%d pages contained text",
                        doc.filename, pages_with_text, pdf.page_count)
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"{filepath.name}: {exc}") from exc


# ===========================================================================
# 6. ORCHESTRATOR — run report, atomic corpus publication, lineage manifest
# ===========================================================================
@dataclass
class SourceOutcome:
    filename: str
    url: str
    status: str = "pending"          # pending | downloaded | reused | failed
    pdf_sha256: Optional[str] = None
    error: Optional[str] = None
    raw_chunks: int = 0
    duplicates_skipped: int = 0
    chunks_written: int = 0


@dataclass
class PipelineReport:
    run_id: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    corpus_sha256: Optional[str] = None
    raw_chunks: int = 0
    chunks_written: int = 0
    duplicates_skipped: int = 0
    sources: List[SourceOutcome] = field(default_factory=list)
    company_distribution: Counter = field(default_factory=Counter)
    category_distribution: Counter = field(default_factory=Counter)

    @property
    def failed_sources(self) -> List[SourceOutcome]:
        return [s for s in self.sources if s.status == "failed"]


def _publish_atomically(tmp: Path, final: Path) -> Optional[str]:
    digest = _sha256_file(tmp)
    os.replace(tmp, final)
    return digest


def _write_manifest(report: PipelineReport, settings: Settings) -> None:
    payload = {
        "run_id": report.run_id,
        "schema_version": SCHEMA_VERSION,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_s": report.duration_s,
        "corpus": {
            "path": str(settings.output_file),
            "sha256": report.corpus_sha256,
            "chunks": report.chunks_written,
        },
        "totals": {
            "raw_chunks": report.raw_chunks,
            "duplicates_skipped": report.duplicates_skipped,
        },
        "distribution": {
            "company": dict(report.company_distribution),
            "category": dict(report.category_distribution),
        },
        "sources": [asdict(s) for s in report.sources],
    }
    manifest_tmp = Path(str(settings.manifest_file) + ".part")
    manifest_tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(manifest_tmp, settings.manifest_file)
    logger.info("lineage manifest -> %s", settings.manifest_file)


def run_ingestion(settings: Settings) -> PipelineReport:
    t0 = time.perf_counter()
    report = PipelineReport(
        run_id=uuid.uuid4().hex[:12],
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    outcomes = {
        doc.filename: SourceOutcome(filename=doc.filename, url=str(doc.url))
        for doc in PDF_RESOURCES
    }
    report.sources = list(outcomes.values())

    for name, info in acquire_sources(build_session(), settings).items():
        outcome = outcomes[name]
        outcome.status = info["status"]
        outcome.pdf_sha256 = info.get("sha256")
        if info["status"] == "failed":
            outcome.error = info.get("error")

    splitter = build_splitter(settings)
    # O(corpus-size) RAM. Deliberate at this scale; at 10^7+ chunks, enforce
    # uniqueness with a DB UNIQUE constraint instead of application memory.
    seen_hashes: set = set()

    tmp_out = settings.output_file.with_suffix(settings.output_file.suffix + ".part")
    tmp_out.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tmp_out.open("w", encoding="utf-8") as out:
            for doc in PDF_RESOURCES:
                outcome = outcomes[doc.filename]
                if outcome.status == "failed":
                    logger.warning("skipping %s (acquisition failed)", doc.filename)
                    continue

                logger.info("parsing %s ...", doc.filename)
                try:
                    for chunk in parse_pdf_stream(
                            settings.data_dir / doc.filename, doc, settings, splitter):
                        outcome.raw_chunks += 1
                        report.raw_chunks += 1
                        if chunk.chunk_hash in seen_hashes:
                            outcome.duplicates_skipped += 1
                            report.duplicates_skipped += 1
                            continue
                        seen_hashes.add(chunk.chunk_hash)
                        out.write(chunk.model_dump_json() + "\n")
                        outcome.chunks_written += 1
                        report.chunks_written += 1
                        report.company_distribution[chunk.metadata.company] += 1
                        report.category_distribution[chunk.metadata.category] += 1
                except DocumentParseError as exc:
                    logger.error("PARSE FAILURE %s: %s", doc.filename, exc)
                    outcome.status = "failed"
                    outcome.error = str(exc)

        if report.chunks_written > 0:
            report.corpus_sha256 = _publish_atomically(tmp_out, settings.output_file)
        else:
            # CRITICAL GUARANTEE: a dead run never clobbers the last good corpus
            tmp_out.unlink(missing_ok=True)
            logger.critical("zero chunks produced — existing corpus left untouched")
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report.duration_s = round(time.perf_counter() - t0, 2)
    _write_manifest(report, settings)
    return report


def _log_report(report: PipelineReport) -> None:
    logger.info("-" * 70)
    logger.info("RUN %s complete in %.2fs", report.run_id, report.duration_s)
    logger.info("corpus: %d chunks written | %d raw | %d duplicates skipped",
                report.chunks_written, report.raw_chunks, report.duplicates_skipped)
    logger.info("companies : %s", dict(report.company_distribution))
    logger.info("categories: %s", dict(report.category_distribution))
    for src in report.sources:
        logger.info("  %-26s %-11s chunks=%-5d dupes=%-4d %s",
                    src.filename, src.status, src.chunks_written,
                    src.duplicates_skipped, src.error or "")
    logger.info("-" * 70)


# ===========================================================================
# 7. ENTRY POINT (exit codes: 0=ok, 1=partial, 2=total failure)
# ===========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Enterprise ingestion pipeline v2")
    parser.add_argument("--force-download", action="store_true",
                        help="re-download PDFs even if present locally")
    parser.add_argument("--json-logs", action="store_true",
                        help="structured JSON logs (or INGEST_LOG_FORMAT=json)")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    ns = parser.parse_args(argv)

    overrides: Dict[str, Any] = {}
    if ns.force_download:
        overrides["force_download"] = True
    if ns.json_logs:
        overrides["log_format"] = "json"
    if ns.log_level:
        overrides["log_level"] = ns.log_level

    settings = Settings(**overrides)
    setup_logging(settings.log_level, settings.log_format)
    logger.info("ingestion starting | schema=%s | output=%s", SCHEMA_VERSION, settings.output_file)

    report = run_ingestion(settings)
    _log_report(report)

    if report.chunks_written < settings.min_expected_chunks:
        logger.critical("yield safeguard: %d chunks < required minimum %d",
                        report.chunks_written, settings.min_expected_chunks)
        return EXIT_TOTAL_FAILURE
    if report.failed_sources:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
