# Spec: src/agents/orchestrator.py

## 1. Module Purpose

The orchestrator is the top-level entry point that sequences all trading agents across four sessions (evening, morning, monitor, report). It is invoked once per session via `main.py` or cron, uses `time.sleep()` to wait between steps within a session, and loops with `sleep(300)` for the monitor agent during market hours. It never crashes -- any agent exception is caught, logged, alerted, and the orchestrator continues with remaining agents. It auto-detects the appropriate session based on current IST time when no explicit session is specified.

## 2. Architectural Decisions

**Session detection**: Based on current IST hour. 18:00-23:59 = evening. 06:00-09:14 = morning. 09:15-15:44 = monitor. 15:45-17:59 = report. Outside these ranges = error logged, no session runs.

**Not a daemon**: Single invocation per session. The monitor session loops internally via `time.sleep(300)` until 15:44 IST, then exits. Other sessions run sequentially and exit.

**Import-only-what-exists**: All agent imports at module level. For not-yet-built agents (`data_collector_agent`, `morning_validator_agent`), placeholder functions log a skip and return None. No conditional imports.

**Error isolation**: Each agent call is wrapped in a try/except that catches `Exception` (not bare -- catches Exception specifically). On failure: log the error, send alert, record the failure in `OrchestratorResult`, continue to next agent. The orchestrator itself only raises `OrchestratorError` if session detection fails or db_path is invalid.

**Sleep strategy**: Evening session sleeps between agents only when running live (not in tests). Morning session has no sleeps -- agents are called sequentially. Monitor session sleeps 300 seconds between ticks.

**Pipeline deadline**: If the signal agent has not completed by 08:50 IST (its own internal deadline), it returns `late_start=True`. The orchestrator detects this and sets `safe_mode=True` on the `OrchestratorResult`, which causes the execution agent to be called but it will find no approved signals and enter safe mode naturally.

**Weekday guard**: Monitor and morning sessions only run on weekdays (Monday-Friday). Evening session runs Sunday-Thursday (produces watchlist for next trading day). Report runs Monday-Friday.

**Watchlist timeout**: Morning session calls `check_watchlist_timeout(run_date)` at the start (before signal agent) to mark any unapproved watchlist entries.

## 3. Public API

### Dataclasses

```python
@dataclass(frozen=True)
class AgentStepResult:
    """Result of a single agent invocation within a session."""
    agent_name: str
    success: bool
    error_message: str | None  # None on success, exception string on failure
    started_at: datetime.datetime  # IST
    completed_at: datetime.datetime  # IST

@dataclass(frozen=True)
class OrchestratorResult:
    """Aggregated result of a full session run."""
    session: str  # 'evening', 'morning', 'monitor', 'report'
    run_date: datetime.date
    safe_mode: bool
    safe_mode_reason: str | None
    steps: list[AgentStepResult]
    started_at: datetime.datetime  # IST
    completed_at: datetime.datetime  # IST

class OrchestratorError(Exception):
    """Raised only for orchestrator-level failures (invalid session, bad db_path)."""
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
```

### Entry point

```python
def run_orchestrator(
    session: str | None = None,
    run_date: datetime.date | None = None,
    db_path_override: str | None = None,
) -> OrchestratorResult:
    """Run the specified trading session or auto-detect based on IST time.

    Args:
        session: One of 'evening', 'morning', 'monitor', 'report', or None for auto-detect.
        run_date: Trading date. Defaults to today (IST) for morning/monitor/report,
                  or next trading day for evening session.
        db_path_override: Override database path for testing.

    Returns:
        OrchestratorResult with per-step success/failure and safe_mode flag.

    Raises:
        OrchestratorError: If session is invalid or auto-detection finds no matching session.
    """
```

## 4. Session Flows

### Evening Session

Scheduled time: 22:00 IST (Sunday-Thursday).

| Step | Time | Agent call | On failure |
|------|------|-----------|------------|
| 1 | 22:00 | `_run_data_collection(run_date, db_path_override)` -- TODO placeholder, logs skip | Continue |
| 2 | 22:20 | `run_screener_agent(run_date=run_date)` | Continue -- research/watchlist will find no screener_results and produce empty output |
| 3 | 22:40 | `run_research_agent(run_date=run_date)` | Continue -- watchlist will skip symbols with no completed research |
| 4 | 23:30 | `run_watchlist_agent(run_date=run_date)` | Log + alert, evening ends |

`run_date` for evening session: the NEXT trading day. If today is Sunday, run_date = Monday. If today is Thursday, run_date = Friday. The orchestrator computes this via `_next_trading_day(today_ist)`.

No `time.sleep()` between steps -- agents already have internal delays (Tavily rate limiting, etc). The scheduled times in the table are approximate. Each agent runs as soon as the previous finishes.

### Morning Session

Scheduled time: 07:00-09:05 IST (Monday-Friday).

| Step | Time | Agent call | On failure |
|------|------|-----------|------------|
| 1 | start | `check_watchlist_timeout(run_date)` | Log + continue |
| 2 | start | `_run_morning_validator(run_date)` -- TODO placeholder, logs skip | Continue |
| 3 | after step 2 | `run_signal_agent(run_date=run_date)` | safe_mode=True, continue to execution |
| 4 | after step 3 | `run_risk_agent(run_date=run_date, db_path_override=db_path_override)` | safe_mode=True, skip execution |
| 5 | after step 4 | `run_execution_agent(run_date=run_date, db_path_override=db_path_override)` | Log + alert |

If signal_agent returns `late_start=True`, set safe_mode=True on the result and log `"signal_agent_late_start"`. Execution agent still runs (it reads risk_approvals which will be empty, entering safe mode naturally).

If risk_agent raises or returns `kill_switch_fired=True`, log the kill switch reason and send alert. Execution agent still runs (reads risk_approvals -- all REJECTED).

### Monitor Session

Runs from current IST time until 15:44 IST. Loop:

```
while now_ist < 15:45:
    run_monitor_agent(run_date=run_date, current_time=now_ist, db_path_override=db_path_override)
    if now_ist >= 15:44:
        break
    sleep(300)  # 5 minutes
    now_ist = current IST time
```

Each monitor tick failure is logged and alerted but does NOT break the loop. The loop continues.

### Report Session

Single call:

```
run_reporter_agent(report_date=run_date, db_path_override=db_path_override)
```

## 5. Internal Helper Functions

```python
def _detect_session(now_ist: datetime.datetime) -> str:
    """Auto-detect session from IST time. Raises OrchestratorError if outside all windows."""

def _next_trading_day(today: datetime.date) -> datetime.date:
    """Return the next weekday. Does not account for market holidays (Phase 5 improvement)."""

def _is_weekday(d: datetime.date) -> bool:
    """True if Monday-Friday."""

def _run_step(
    agent_name: str,
    callable_fn: Callable[..., Any],
    **kwargs: Any,
) -> AgentStepResult:
    """Execute a single agent step with timing and error capture."""

def _run_data_collection(run_date: datetime.date, db_path_override: str | None) -> None:
    """TODO placeholder for data_collector_agent. Logs skip."""

def _run_morning_validator(run_date: datetime.date) -> None:
    """TODO placeholder for morning_validator_agent. Logs skip."""
```

## 6. Constants

```python
AGENT_NAME: str = "orchestrator"
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

# Session detection windows (IST hours)
EVENING_START_HOUR: int = 18
EVENING_END_HOUR: int = 24  # midnight exclusive
MORNING_START_HOUR: int = 6
MORNING_END_HOUR: int = 9
MORNING_END_MINUTE: int = 14
MONITOR_START_HOUR: int = 9
MONITOR_START_MINUTE: int = 15
MONITOR_END_HOUR: int = 15
MONITOR_END_MINUTE: int = 44
REPORT_START_HOUR: int = 15
REPORT_START_MINUTE: int = 45
REPORT_END_HOUR: int = 17
REPORT_END_MINUTE: int = 59

# Monitor loop
MONITOR_SLEEP_SECONDS: int = 300

# Valid sessions
VALID_SESSIONS: frozenset[str] = frozenset({"evening", "morning", "monitor", "report"})
```

## 7. Logging

All logging via `log_agent_action(agent_name="orchestrator", ...)`.

| Action | Level | When |
|--------|-------|------|
| `"session_started: {session}"` | INFO | Session begins |
| `"session_auto_detected: {session}"` | INFO | Auto-detection resolved |
| `"step_started: {agent_name}"` | INFO | Before each agent call |
| `"step_completed: {agent_name}"` | INFO | Agent returned successfully |
| `"step_failed: {agent_name}: {error}"` | ERROR | Agent raised exception |
| `"safe_mode_activated: {reason}"` | WARNING | Any safe mode trigger |
| `"monitor_tick: {tick_number}"` | INFO | Each monitor loop iteration |
| `"monitor_loop_ended"` | INFO | Monitor exits at 15:44 |
| `"session_completed: {session}"` | INFO | Session ends normally |
| `"placeholder_skip: {agent_name}"` | WARNING | TODO agent not yet built |
| `"weekday_guard: {day_name}"` | WARNING | Session skipped on wrong day |
| `"kill_switch_fired: {reason}"` | ERROR | Risk agent detected kill switch |

## 8. Error Handling

- Each agent call wrapped in `try: ... except Exception as exc:`. Never bare except.
- `OrchestratorError` raised only for: invalid session string, auto-detection failure outside all windows.
- Agent-specific exceptions (`ScreenerAgentError`, `RiskAgentError`, etc.) are caught by the generic `Exception` handler -- no need to import each error class.
- `send_alert()` called on every agent failure. If `send_alert()` itself fails, catch and log -- never crash.
- `log_agent_action()` called OUTSIDE any transaction (per CLAUDE.md SQLite rule).

## 9. Out of Scope

- **Not a daemon/scheduler**: No APScheduler, no cron setup, no systemd. Invoked externally.
- **No Shoonya auth**: Phase 4 uses PaperTrader only.
- **No market holiday calendar**: `_next_trading_day` only skips weekends. Holiday awareness is Phase 5.
- **No data_collector_agent**: Placeholder only. Data collection currently handled by individual agents fetching their own OHLCV.
- **No morning_validator_agent**: Placeholder only. Signal agent reads screener_results directly.
- **No argument parsing**: `main.py` handles CLI args and calls `run_orchestrator()`. The orchestrator does not parse sys.argv.
- **No database migrations**: Tables are created by their respective agents.
- **No concurrent agent execution**: All agents run sequentially within a session.

## 10. Input Contract

- `session`: Must be in `VALID_SESSIONS` or `None`. Any other string raises `OrchestratorError`.
- `run_date`: Must be a `datetime.date`. If None, defaults to today IST (morning/monitor/report) or next trading day (evening).
- `db_path_override`: Passed through to agents that accept it. Orchestrator does not open DB connections itself.

## 11. Output Contract

- `OrchestratorResult.steps`: One `AgentStepResult` per agent call attempted, in execution order. Monitor session has one entry per tick.
- `OrchestratorResult.safe_mode`: True if any condition triggered safe mode (signal_agent late, risk kill switch, agent failure in morning session).
- `OrchestratorResult.safe_mode_reason`: First reason that triggered safe mode. Subsequent reasons logged but not overwritten.
- All timestamps in IST (`ZoneInfo("Asia/Kolkata")`).

## 12. Test Hints (minimum 12)

1. **Auto-detect evening**: Mock IST time to 22:00 Thursday, verify session="evening" and run_date=Friday.
2. **Auto-detect morning**: Mock IST time to 08:00 Monday, verify session="morning" and run_date=Monday.
3. **Auto-detect monitor**: Mock IST time to 11:00 Wednesday, verify session="monitor".
4. **Auto-detect report**: Mock IST time to 16:00 Tuesday, verify session="report".
5. **Auto-detect failure**: Mock IST time to 03:00, verify OrchestratorError raised.
6. **Invalid session string**: Pass session="invalid", verify OrchestratorError.
7. **Evening session calls agents in order**: Mock all 4 agent functions, verify call order: data_collection, screener, research, watchlist.
8. **Morning session safe mode on signal_agent late_start**: Mock signal_agent to return `late_start=True`, verify `safe_mode=True` and `safe_mode_reason` set.
9. **Agent exception does not crash orchestrator**: Mock screener_agent to raise `ScreenerAgentError`, verify orchestrator returns successfully with that step marked `success=False`, and subsequent agents still called.
10. **Monitor loop exits at 15:44**: Mock time progression from 15:40 to 15:45, verify loop runs expected number of ticks then exits.
11. **Monitor tick failure does not break loop**: Mock monitor_agent to raise on first call, succeed on second, verify both ticks recorded.
12. **Weekday guard blocks morning on Saturday**: Mock IST time to 08:00 Saturday, verify morning session logs weekday_guard and returns empty steps.
13. **Evening run_date computes next trading day**: Friday evening should produce run_date=Monday. Sunday evening should produce run_date=Monday.
14. **check_watchlist_timeout called first in morning**: Mock all agents, verify check_watchlist_timeout called before signal_agent.
15. **Kill switch in risk_agent triggers safe_mode**: Mock risk_agent to return `kill_switch_fired=True`, verify `safe_mode=True` on result.
16. **db_path_override passed through**: Verify db_path_override propagated to risk_agent, execution_agent, monitor_agent, reporter_agent.
17. **Report session calls only reporter_agent**: Mock reporter_agent, verify it is the only agent called.

## 13. File Locations

| File | Purpose |
|------|---------|
| `src/agents/orchestrator.py` | Main module |
| `tests/agents/test_orchestrator.py` | Tests |
| `src/agents/__init__.py` | Already exists -- no changes needed |

## 14. pyproject.toml

No new dependencies. All required packages (time, datetime, zoneinfo) are stdlib.

## 15. main.py Changes (out of scope for this spec)

After the orchestrator is built, `main.py` will be updated separately to:
- Parse `--session` CLI arg
- Call `run_orchestrator(session=args.session)`
- Replace the current Phase 1 dry-run logic

This is a separate task, not part of the orchestrator module build.
