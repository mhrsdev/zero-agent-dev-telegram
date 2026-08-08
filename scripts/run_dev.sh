#!/usr/bin/env bash
# Convenience launcher for local development.
#
# Usage:
#   ./scripts/run_dev.sh                # run with reload
#   ZERO_ENV=development ./scripts/run_dev.sh
#
# This script is for development convenience only. Production deploys
# should use a proper process manager (systemd, supervisord, or a
# container orchestrator) — added in a later milestone.

set -euo pipefail

cd "$(dirname "$0")/.."

# Default to development if ZERO_ENV is not set.
export ZERO_ENV="${ZERO_ENV:-development}"
export ZERO_LOG_LEVEL="${ZERO_LOG_LEVEL:-INFO}"

# In development, default the database to a local file if not set.
export ZERO_DATABASE_URL="${ZERO_DATABASE_URL:-sqlite:///./zero_develop.db}"

echo "Starting Zero Develop in ${ZERO_ENV} mode..."
echo "  database: ${ZERO_DATABASE_URL}"
echo "  log level: ${ZERO_LOG_LEVEL}"
echo "  docs:     http://127.0.0.1:8000/docs"
echo "  health:   http://127.0.0.1:8000/healthz"
echo ""

exec uvicorn zero.main:app --reload --host 127.0.0.1 --port 8000
