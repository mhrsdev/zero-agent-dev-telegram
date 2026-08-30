#!/usr/bin/env bash
# Boot the REAL engine (uvicorn, workers enabled) for the round-5 E2E.
set -u
HOME_DIR=/home/z/my-project/zero-e2e-home
LOG=/home/z/my-project/zero-agent-dev-telegram/realrun-evidence/round5/engine.log
mkdir -p "$(dirname "$LOG")"

export ZERO_HOME="$HOME_DIR"
export ZERO_ENV=development
export ZERO_DATABASE_URL="sqlite:///$HOME_DIR/e2e.db"
export ZERO_SECRET_KEY=$(grep ZERO_SECRET_KEY "$HOME_DIR/.env" | cut -d= -f2)
export ZERO_TELEGRAM_WEBHOOK_SECRET=e2e-webhook-secret-9f31c2
export ZERO_DECOMPOSITION_ENABLED=1
# Bounded auto-retry for failed tasks (GAP-12 machinery: exponential
# backoff, jitter, Retry-After). On the operator's gateway transient
# CDN-edge 403s are a fact of life; without a task-level retry budget
# one blip permanently fails a task and the whole graph dead-ends.
export ZERO_TASK_MAX_ATTEMPTS=4
export ZERO_MCP_SERVERS='[{"name":"e2e-echo","command":["/home/z/my-project/venv/bin/python","/home/z/my-project/zero-agent-dev-telegram/tests/fixtures/fake_mcp_server.py"]}]'
export PYTHONPATH=/home/z/my-project/zero-agent-dev-telegram/src

exec /home/z/my-project/venv/bin/python -m uvicorn zero.main:app --host 127.0.0.1 --port 8010 >> "$LOG" 2>&1
