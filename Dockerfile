# syntax=docker/dockerfile:1.7
# Zero Agent v2 — production Docker image
#
# Multi-stage build:
#   1. builder: install deps into a venv
#   2. runtime: copy venv + app, run as non-root
#
# Security hardening:
#   - Non-root user (zero:zero, uid 1001)
#   - No package managers in runtime image
#   - Health check
#   - Tini as init (PID 1) for proper signal handling
#   - Distroless-style minimal runtime (python:3.12-slim)

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder

# Build deps for any C extensions (cryptography already wheels, but just in case).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create venv with up-to-date pip.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /app

# Copy only dependency manifest first (better layer caching).
COPY pyproject.toml ./
COPY zero/__init__.py zero/__init__.py

# Install dependencies (no source yet — just metadata).
# --no-deps would be wrong; we want deps but not the package itself.
# We use --editable after copying the source.
RUN pip install --no-cache-dir \
    aiogram>=3.13,<4.0 \
    pydantic>=2.6,<3.0 \
    pydantic-settings>=2.2,<3.0 \
    structlog>=24.1,<25.0 \
    anyio>=4.3,<5.0 \
    httpx>=0.27,<1.0 \
    sqlalchemy[asyncio]>=2.0,<3.0 \
    aiosqlite>=0.20,<1.0 \
    asyncpg>=0.29,<1.0 \
    alembic>=1.13,<2.0 \
    cryptography>=42.0,<46.0 \
    ulid-py>=1.1,<2.0 \
    pyyaml>=6.0,<7.0 \
    click>=8.1,<9.0 \
    rich>=13.7,<14.0 \
    python-ulid>=2.2,<3.0 \
    aiofiles>=23.0,<25.0 \
    edge-tts>=6.1,<8.0

# Copy source.
COPY zero/ zero/
COPY README.md IMPLEMENTATION_STATUS.md ./

# Install the package itself (editable so source updates don't require rebuild).
RUN pip install --no-cache-dir --editable .

# ---------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime

# Tini for proper PID 1 signal handling (graceful shutdown on SIGTERM).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 zero \
    && useradd --system --uid 1001 --gid zero --create-home --home-dir /home/zero zero

# Copy venv from builder.
COPY --from=builder --chown=zero:zero /opt/venv /opt/venv

# Copy application source.
COPY --from=builder --chown=zero:zero /app /app

# Set up environment.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ZERO_DATABASE__BACKEND=sqlite \
    ZERO_DATABASE__SQLITE_DIR=/home/zero/.zero/db

USER zero
WORKDIR /app
HOME /home/zero

# Create data directories.
RUN mkdir -p /home/zero/.zero/db /home/zero/.zero/logs

# Health check — calls `zero doctor` and checks exit code.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD zero doctor || exit 1

# Use tini as init.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command — start the Telegram bot in polling mode.
# Override with: docker run zero-agent zero mcp serve
CMD ["zero", "serve", "--mode", "polling"]

# Labels (OCI standard).
LABEL org.opencontainers.image.title="Zero Agent v2" \
      org.opencontainers.image.description="Telegram-based AI collaboration platform for development teams" \
      org.opencontainers.image.source="https://github.com/zero/zero-agent" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.version="0.1.0"
