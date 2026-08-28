#!/usr/bin/env bash
# S2 — start the REAL Zero server (uvicorn zero.main:app) against the
# wizard-configured real home, with the real provider env, real secrets,
# and the real Telegram polling worker. Idempotent: kills a previous
# instance first.
set -u

REAL_HOME=/home/z/my-project/zero-real-home
PROJECT=/home/z/my-project/zero/zero-agent-dev-telegram
LOG=/home/z/my-project/scripts/realrun/server.log
PIDFILE=/home/z/my-project/scripts/realrun/server.pid

# Stop a previous instance
if [ -f "$PIDFILE" ]; then
  OLD=$(cat "$PIDFILE" 2>/dev/null || true)
  if [ -n "${OLD:-}" ] && kill -0 "$OLD" 2>/dev/null; then
    kill "$OLD" 2>/dev/null
    sleep 2
    kill -9 "$OLD" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi
# Also clear a stale zero.pid inside the real home (zero stop contract)
rm -f "$REAL_HOME/zero.pid"

export ZERO_ENV=development
export ZERO_HOME="$REAL_HOME"
export ZERO_DATABASE_URL="sqlite:///${REAL_HOME}/engine.db"
export ZERO_OPENAI_API_KEY="sk-BlwjB2GhsGBwFLjQBBAhKK7FpmfJYP9usqGfrImaLaA1JOKW"
export ZERO_OPENAI_BASE_URL="https://api.justwoker.icu/v1"
export ZERO_OPENAI_MODEL="claude-opus-5"
# Model-level fallback routing (Hermes parity): mirrors routing.fallback_models
# in the wizard-written config.yaml. A primary-model outage now routes to
# the next model on the same gateway instead of failing the task.
export ZERO_OPENAI_FALLBACK_MODELS="claude-opus-4-8,claude-opus-4-8-thinking"
export ZERO_OPENAI_TIMEOUT_SECONDS="180"
# Transient-failure retry budget (Hermes parity): gateways behind CDN
# edges occasionally emit 524/502/503; 4 attempts with backoff gives a
# real chance of riding out a blip instead of failing the task.
export ZERO_PROVIDER_MAX_ATTEMPTS="4"
export ZERO_DECOMPOSITION_ENABLED="1"
export ZERO_WORKTREE_ROOT="${REAL_HOME}/worktrees"
export ZERO_WORKTREE_ISOLATION_MODE="host_bounded"
export ZERO_WORKTREE_ALLOWED_COMMANDS="python3,pip3,ls,cat,git,echo,wc,grep,find,touch,head,tail"
# Runtime evidence verification: stdlib unittest (python3 is allowlisted).
export ZERO_EVIDENCE_TEST_COMMAND="python3 -m unittest discover -s tests -v"
export ZERO_TELEGRAM_WEBHOOK_SECRET="realrun-webhook-secret-2026"
# ZERO_SECRET_KEY: read from the wizard-written .env so the encrypted
# store opens exactly the same key material as every prior real run.
export ZERO_SECRET_KEY="$(grep '^ZERO_SECRET_KEY=' "$REAL_HOME/.env" | cut -d= -f2-)"
# NOTE: ZERO_TELEGRAM_API_BASE intentionally NOT set → real api.telegram.org
unset ZERO_TELEGRAM_API_BASE 2>/dev/null || true

cd "$PROJECT"
nohup "$PROJECT/.venv/bin/python" -m uvicorn zero.main:app \
  --host 127.0.0.1 --port 8000 >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "server starting pid=$(cat "$PIDFILE") log=$LOG"
