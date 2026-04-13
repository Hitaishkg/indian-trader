#!/bin/bash
# One-time evening+morning pipeline run for April 14, 2026
# Self-deletes its cron entry after execution.
# System is UTC; cron entry is set for 00:30 UTC = 06:00 IST.

set -euo pipefail

cd /home/hitaish/projects/indian-trader
source .venv/bin/activate

LOG_FILE="logs/orchestrator_$(date +%Y%m%d).log"
mkdir -p logs

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') — Starting evening session (override_time=22:00)" >> "$LOG_FILE"

python -c "
from src.agents.orchestrator import run_orchestrator
result = run_orchestrator(session='evening', override_time='22:00')
print(f'Evening: safe_mode={result.safe_mode}, steps={len(result.steps)}')
" >> "$LOG_FILE" 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') — Evening done. Sleeping 10 minutes before morning..." >> "$LOG_FILE"
sleep 600

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') — Starting morning session" >> "$LOG_FILE"

python -c "
from src.agents.orchestrator import run_orchestrator
result = run_orchestrator(session='morning')
print(f'Morning: safe_mode={result.safe_mode}, steps={len(result.steps)}')
" >> "$LOG_FILE" 2>&1

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') — Both sessions complete. Removing cron entry." >> "$LOG_FILE"

# Self-cleanup: remove this one-time cron entry
crontab -l 2>/dev/null | grep -v "run_tomorrow" | crontab -

echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') — Cron entry removed." >> "$LOG_FILE"
