#!/usr/bin/env bash
# Boot the REAL engine (uvicorn, workers enabled) for the round-8 live run.
set -u
HOME_DIR=/home/z/my-project/zero-e2e-home
LOG=/home/z/my-project/workspace/zero-agent-dev-telegram/realrun-evidence/round8/engine.log
mkdir -p "$(dirname "$LOG")"

export ZERO_HOME="$HOME_DIR"
export ZERO_ENV=development
export ZERO_DATABASE_URL="sqlite:///$HOME_DIR/e2e.db"
export ZERO_SECRET_KEY=$(grep ZERO_SECRET_KEY "$HOME_DIR/.env" | cut -d= -f2)
export ZERO_TELEGRAM_WEBHOOK_SECRET=e2e-webhook-secret-9f31c2
export ZERO_DECOMPOSITION_ENABLED=1
export ZERO_TASK_MAX_ATTEMPTS=4
export PYTHONPATH=/home/z/my-project/workspace/zero-agent-dev-telegram/src

exec /home/z/my-project/workspace/zero-agent-dev-telegram/.venv/bin/python -m uvicorn zero.main:app --host 127.0.0.1 --port 8010 >> "$LOG" 2>&1
