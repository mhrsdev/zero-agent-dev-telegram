#!/usr/bin/env bash
# Round-9 orchestrator — FULL live verification with the operator's real
# credentials (real bot @SandboxEnvironmentBot, real gateway
# api.justwoker.icu/v1 claude-opus-5, real group -1004406039396).
#
# The sandbox reaps detached processes between tool sessions, so this
# script is structured as small resumable subcommands instead of one
# long-running flow:
#
#   round9_engine.sh boot            # start the real engine (idempotent)
#   round9_engine.sh topo            # agent types / knowledge / RAG / teams / auth / poll
#   round9_engine.sh trigger rag     # plan → REAL approve → execution (RAG proof)
#   round9_engine.sh monitor rag     # wait terminal + validate retrieval evidence
#   round9_engine.sh trigger deleg   # plan → REAL approve → execution (delegate)
#   round9_engine.sh monitor deleg   # wait terminal + validate delegation evidence
#   round9_engine.sh compaction      # in-process REAL compaction + memory deltas
#   round9_engine.sh stop            # stop the engine
set -u
REPO=/home/z/my-project/workspace/zero-agent-dev-telegram
EVIDENCE=$REPO/realrun-evidence/round9
PY=$REPO/.venv/bin/python
HOME_DIR=/home/z/my-project/zero-e2e-home
mkdir -p "$EVIDENCE"

_env() {
  export ZERO_HOME="$HOME_DIR"
  export ZERO_ENV=development
  export ZERO_DATABASE_URL="sqlite:///$HOME_DIR/e2e.db"
  export ZERO_SECRET_KEY=$(grep ZERO_SECRET_KEY "$HOME_DIR/.env" | cut -d= -f2)
  export ZERO_TELEGRAM_WEBHOOK_SECRET=e2e-webhook-secret-9f31c2
  export ZERO_DECOMPOSITION_ENABLED=1
  export ZERO_TASK_MAX_ATTEMPTS=4
  export PYTHONPATH=$REPO/src
}

case "${1:-}" in
  boot)
    _env
    if curl -fsS http://127.0.0.1:8010/healthz >/dev/null 2>&1; then
      echo "engine already healthy"; exit 0
    fi
    nohup $PY -m uvicorn zero.main:app --host 127.0.0.1 --port 8010 \
      >> "$EVIDENCE/engine-boot.log" 2>&1 &
    echo $! > "$EVIDENCE/engine.pid"
    for _ in $(seq 1 60); do
      curl -fsS http://127.0.0.1:8010/healthz >/dev/null 2>&1 && { echo "engine healthy"; exit 0; }
      sleep 1
    done
    echo "ENGINE FAILED TO BOOT — see $EVIDENCE/engine-boot.log"; exit 1
    ;;
  topo)
    E2E_PROFILE=topo E2E_STEP=full $PY $REPO/scripts/e2e_round9_drive.py
    ;;
  trigger)
    E2E_PROFILE="$2" E2E_STEP=trigger $PY $REPO/scripts/e2e_round9_drive.py
    ;;
  monitor)
    E2E_PROFILE="$2" E2E_STEP=monitor E2E_MONITOR_BUDGET=480 \
      $PY $REPO/scripts/e2e_round9_drive.py
    ;;
  compaction)
    _env
    $PY $REPO/scripts/e2e_round9_compaction.py
    ;;
  stop)
    if [ -f "$EVIDENCE/engine.pid" ]; then
      kill "$(cat "$EVIDENCE/engine.pid")" 2>/dev/null
      rm -f "$EVIDENCE/engine.pid"
    fi
    pkill -f "uvicorn zero.main:app" 2>/dev/null
    sleep 2
    echo "engine stopped"
    ;;
  *)
    echo "usage: $0 {boot|topo|trigger rag|monitor rag|trigger deleg|monitor deleg|compaction|stop}"
    exit 2
    ;;
esac
