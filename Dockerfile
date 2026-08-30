# ===========================================================================
# STAGE 1: BUILDER (Compiles C-extensions in isolation)
# ===========================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies only in builder stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create isolated virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# ===========================================================================
# STAGE 2: RUNTIME (Lean, hardened production image - ZERO COMPILERS)
# ===========================================================================
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install ONLY runtime shared libraries (no compilers) + curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-compiled virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Production Python environment flags
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Security: Create non-root system user (UID 10001) for SOC2 / FedRAMP compliance
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser && \
    chown -R appuser:appgroup /app

# Copy application code with non-root ownership
COPY --chown=appuser:appgroup . .

# Drop privileges to non-root
USER appuser

EXPOSE 8000 8501

# Native Container Health Probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default execution: FastAPI Microservice.
# Single worker is intentional: the per-IP token buckets, Prometheus counters,
# and run semaphore are in-process state — multiple workers would double the
# effective rate limit and split /metrics per scrape. The app is fully async,
# so one worker still saturates the LLM concurrency budget.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]