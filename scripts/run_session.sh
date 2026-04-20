#!/bin/bash
# Wrapper for orchestrator sessions. Sends Telegram alert on failure.
set -euo pipefail

SESSION="${1:?Usage: run_session.sh <morning|monitor|report|evening>}"
PROJ=/home/hitaish/projects/indian-trader
PY=$PROJ/.venv/bin/python
LOG=$PROJ/logs/orchestrator_$(date +%Y%m%d).log

mkdir -p "$PROJ/logs"
cd "$PROJ"

if $PY -c "
from src.agents.orchestrator import run_orchestrator
result = run_orchestrator(session='$SESSION')
print(f'session=$SESSION safe_mode={result.safe_mode} steps={len(result.steps)}')
" >> "$LOG" 2>&1; then
    exit 0
else
    EXIT=$?
    TAIL=$(tail -5 "$LOG" 2>/dev/null || echo "no log")
    $PY -c "
from src.utils.notifier import send_notification
send_notification(
    subject='TRADER: session $SESSION FAILED',
    body='Exit $EXIT\n\nLast log lines:\n$TAIL\n\nFull log: $LOG'
)
" 2>/dev/null || true
    exit $EXIT
fi
