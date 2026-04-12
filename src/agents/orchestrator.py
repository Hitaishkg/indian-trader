"""Orchestrator — top-level session sequencer for all trading agents.

Sequences evening / morning / monitor / report sessions.
Auto-detects session from IST time when none is specified.
Never crashes — agent exceptions are caught, logged, and alerted.
"""

from __future__ import annotations

import datetime
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.agents.execution_agent import run_execution_agent
from src.agents.monitor_agent import run_monitor_agent
from src.agents.reporter_agent import run_reporter_agent
from src.agents.research_agent import run_research_agent
from src.agents.risk_agent import run_risk_agent
from src.agents.screener_agent import run_screener_agent
from src.agents.signal_agent import run_signal_agent
from src.agents.watchlist_agent import check_watchlist_timeout, run_watchlist_agent
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "orchestrator"
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

EVENING_START_HOUR: int = 18
EVENING_END_HOUR: int = 24
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

MONITOR_SLEEP_SECONDS: int = 300

VALID_SESSIONS: frozenset[str] = frozenset({"evening", "morning", "monitor", "report"})


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


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
        """Initialise with a descriptive message."""
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_session(now_ist: datetime.datetime) -> str:
    """Auto-detect session from IST time.

    Raises:
        OrchestratorError: If outside all session windows.
    """
    h, m = now_ist.hour, now_ist.minute

    if EVENING_START_HOUR <= h < EVENING_END_HOUR:
        return "evening"
    if MORNING_START_HOUR <= h < MORNING_END_HOUR or (
        h == MORNING_END_HOUR and m <= MORNING_END_MINUTE
    ):
        return "morning"
    if h == MONITOR_START_HOUR and m >= MONITOR_START_MINUTE:
        return "monitor"
    if MONITOR_START_HOUR < h < MONITOR_END_HOUR:
        return "monitor"
    if h == MONITOR_END_HOUR and m <= MONITOR_END_MINUTE:
        return "monitor"
    if h == REPORT_START_HOUR and m >= REPORT_START_MINUTE:
        return "report"
    if REPORT_START_HOUR < h < REPORT_END_HOUR:
        return "report"
    if h == REPORT_END_HOUR and m <= REPORT_END_MINUTE:
        return "report"

    raise OrchestratorError(
        f"No session window matches current IST time {now_ist.strftime('%H:%M')}. "
        "Valid windows: evening 18:00-23:59, morning 06:00-09:14, "
        "monitor 09:15-15:44, report 15:45-17:59."
    )


def _next_trading_day(today: datetime.date) -> datetime.date:
    """Return the next weekday (Mon-Fri). Does not account for market holidays."""
    candidate = today + datetime.timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate += datetime.timedelta(days=1)
    return candidate


def _is_weekday(d: datetime.date) -> bool:
    """True if Monday-Friday."""
    return d.weekday() < 5


def _run_step(
    agent_name: str,
    callable_fn: Callable[..., Any],
    **kwargs: Any,
) -> AgentStepResult:
    """Execute a single agent step with timing and error capture."""
    started_at = datetime.datetime.now(IST)
    log_agent_action(agent_name=AGENT_NAME, action=f"step_started: {agent_name}", level="INFO")
    try:
        callable_fn(**kwargs)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_completed: {agent_name}",
            level="INFO",
        )
        return AgentStepResult(
            agent_name=agent_name,
            success=True,
            error_message=None,
            started_at=started_at,
            completed_at=completed_at,
        )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: {agent_name}: {exc}",
            level="ERROR",
        )
        return AgentStepResult(
            agent_name=agent_name,
            success=False,
            error_message=str(exc),
            started_at=started_at,
            completed_at=completed_at,
        )


def _run_data_collection(run_date: datetime.date, db_path_override: str | None) -> None:
    """TODO placeholder for data_collector_agent. Logs skip."""
    log_agent_action(
        agent_name=AGENT_NAME,
        action="placeholder_skip: data_collector_agent",
        level="WARNING",
    )


def _run_morning_validator(run_date: datetime.date) -> None:
    """TODO placeholder for morning_validator_agent. Logs skip."""
    log_agent_action(
        agent_name=AGENT_NAME,
        action="placeholder_skip: morning_validator_agent",
        level="WARNING",
    )


def _safe_send_alert(subject: str, message: str) -> None:
    """Send alert, swallowing any exception so orchestrator never crashes over it."""
    try:
        send_alert(subject=subject, message=message)
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"send_alert_failed: {exc}",
            level="ERROR",
        )


# ---------------------------------------------------------------------------
# Session runners
# ---------------------------------------------------------------------------


def _run_evening_session(
    run_date: datetime.date,
    db_path_override: str | None,
    steps: list[AgentStepResult],
) -> None:
    """Run the evening session (Sunday-Thursday)."""
    # Step 1: data collection (placeholder)
    step = _run_step(
        "data_collector_agent",
        _run_data_collection,
        run_date=run_date,
        db_path_override=db_path_override,
    )
    steps.append(step)

    # Step 2: screener
    step = _run_step("screener_agent", run_screener_agent, run_date=run_date)
    steps.append(step)

    # Step 3: research
    step = _run_step("research_agent", run_research_agent, run_date=run_date)
    steps.append(step)

    # Step 4: watchlist — log + alert on failure
    started_at = datetime.datetime.now(IST)
    log_agent_action(agent_name=AGENT_NAME, action="step_started: watchlist_agent", level="INFO")
    try:
        run_watchlist_agent(run_date=run_date)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME, action="step_completed: watchlist_agent", level="INFO"
        )
        steps.append(
            AgentStepResult(
                agent_name="watchlist_agent",
                success=True,
                error_message=None,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: watchlist_agent: {exc}",
            level="ERROR",
        )
        _safe_send_alert(
            subject="Orchestrator: watchlist_agent failed",
            message=str(exc),
        )
        steps.append(
            AgentStepResult(
                agent_name="watchlist_agent",
                success=False,
                error_message=str(exc),
                started_at=started_at,
                completed_at=completed_at,
            )
        )


def _run_morning_session(
    run_date: datetime.date,
    db_path_override: str | None,
    steps: list[AgentStepResult],
) -> tuple[bool, str | None]:
    """Run the morning session (Monday-Friday).

    Returns:
        (safe_mode, safe_mode_reason)
    """
    safe_mode = False
    safe_mode_reason: str | None = None

    # Step 1: watchlist timeout check
    started_at = datetime.datetime.now(IST)
    log_agent_action(
        agent_name=AGENT_NAME, action="step_started: check_watchlist_timeout", level="INFO"
    )
    try:
        check_watchlist_timeout(run_date)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action="step_completed: check_watchlist_timeout",
            level="INFO",
        )
        steps.append(
            AgentStepResult(
                agent_name="check_watchlist_timeout",
                success=True,
                error_message=None,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: check_watchlist_timeout: {exc}",
            level="ERROR",
        )
        steps.append(
            AgentStepResult(
                agent_name="check_watchlist_timeout",
                success=False,
                error_message=str(exc),
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    # Step 2: morning validator (placeholder)
    step = _run_step(
        "morning_validator_agent",
        _run_morning_validator,
        run_date=run_date,
    )
    steps.append(step)

    # Step 3: signal agent — if exception or late_start: safe_mode=True
    started_at = datetime.datetime.now(IST)
    log_agent_action(
        agent_name=AGENT_NAME, action="step_started: signal_agent", level="INFO"
    )
    try:
        signal_result = run_signal_agent(run_date=run_date)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME, action="step_completed: signal_agent", level="INFO"
        )
        steps.append(
            AgentStepResult(
                agent_name="signal_agent",
                success=True,
                error_message=None,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
        if signal_result.late_start:
            safe_mode = True
            safe_mode_reason = "signal_agent_late_start"
            log_agent_action(
                agent_name=AGENT_NAME,
                action="signal_agent_late_start",
                level="WARNING",
            )
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"safe_mode_activated: {safe_mode_reason}",
                level="WARNING",
            )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: signal_agent: {exc}",
            level="ERROR",
        )
        _safe_send_alert(
            subject="Orchestrator: signal_agent failed",
            message=str(exc),
        )
        safe_mode = True
        safe_mode_reason = str(exc)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"safe_mode_activated: {safe_mode_reason}",
            level="WARNING",
        )
        steps.append(
            AgentStepResult(
                agent_name="signal_agent",
                success=False,
                error_message=str(exc),
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    # Step 4: risk agent
    skip_execution = safe_mode  # already in safe mode from signal agent failure
    started_at = datetime.datetime.now(IST)
    log_agent_action(
        agent_name=AGENT_NAME, action="step_started: risk_agent", level="INFO"
    )
    try:
        risk_result = run_risk_agent(run_date=run_date, db_path_override=db_path_override)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME, action="step_completed: risk_agent", level="INFO"
        )
        steps.append(
            AgentStepResult(
                agent_name="risk_agent",
                success=True,
                error_message=None,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
        if risk_result.kill_switch_fired:
            reason = risk_result.kill_switch_reason or "unknown"
            safe_mode = True
            safe_mode_reason = reason
            skip_execution = True
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"kill_switch_fired: {reason}",
                level="ERROR",
            )
            log_agent_action(
                agent_name=AGENT_NAME,
                action="execution_skipped: kill_switch_active",
                level="ERROR",
            )
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"safe_mode_activated: {reason}",
                level="WARNING",
            )
            _safe_send_alert(
                subject="Orchestrator: kill switch fired",
                message=f"Kill switch: {reason}. Execution agent skipped.",
            )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: risk_agent: {exc}",
            level="ERROR",
        )
        _safe_send_alert(
            subject="Orchestrator: risk_agent failed",
            message=str(exc),
        )
        safe_mode = True
        safe_mode_reason = str(exc)
        skip_execution = True
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"safe_mode_activated: {safe_mode_reason}",
            level="WARNING",
        )
        steps.append(
            AgentStepResult(
                agent_name="risk_agent",
                success=False,
                error_message=str(exc),
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    # Step 5: execution agent — skip if kill switch fired
    if skip_execution:
        return safe_mode, safe_mode_reason

    started_at = datetime.datetime.now(IST)
    log_agent_action(
        agent_name=AGENT_NAME, action="step_started: execution_agent", level="INFO"
    )
    try:
        run_execution_agent(run_date=run_date, db_path_override=db_path_override)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME, action="step_completed: execution_agent", level="INFO"
        )
        steps.append(
            AgentStepResult(
                agent_name="execution_agent",
                success=True,
                error_message=None,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: execution_agent: {exc}",
            level="ERROR",
        )
        _safe_send_alert(
            subject="Orchestrator: execution_agent failed",
            message=str(exc),
        )
        steps.append(
            AgentStepResult(
                agent_name="execution_agent",
                success=False,
                error_message=str(exc),
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    return safe_mode, safe_mode_reason


def _run_monitor_session(
    run_date: datetime.date,
    db_path_override: str | None,
    steps: list[AgentStepResult],
) -> None:
    """Run the monitor session loop until 15:44 IST."""
    tick = 0
    now_ist = datetime.datetime.now(IST)

    while now_ist.hour < MONITOR_END_HOUR or (
        now_ist.hour == MONITOR_END_HOUR and now_ist.minute <= MONITOR_END_MINUTE
    ):
        tick += 1
        started_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"monitor_tick: {tick}",
            level="INFO",
        )
        try:
            run_monitor_agent(
                run_date=run_date,
                current_time=now_ist,
                db_path_override=db_path_override,
            )
            completed_at = datetime.datetime.now(IST)
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"step_completed: monitor_agent (tick {tick})",
                level="INFO",
            )
            steps.append(
                AgentStepResult(
                    agent_name=f"monitor_agent_tick_{tick}",
                    success=True,
                    error_message=None,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        except Exception as exc:
            completed_at = datetime.datetime.now(IST)
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"step_failed: monitor_agent (tick {tick}): {exc}",
                level="ERROR",
            )
            _safe_send_alert(
                subject=f"Orchestrator: monitor_agent tick {tick} failed",
                message=str(exc),
            )
            steps.append(
                AgentStepResult(
                    agent_name=f"monitor_agent_tick_{tick}",
                    success=False,
                    error_message=str(exc),
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )

        # Check if next tick would be past the window before sleeping
        next_time = datetime.datetime.now(IST) + datetime.timedelta(seconds=MONITOR_SLEEP_SECONDS)
        if next_time.hour > MONITOR_END_HOUR or (
            next_time.hour == MONITOR_END_HOUR and next_time.minute > MONITOR_END_MINUTE
        ):
            break

        time.sleep(MONITOR_SLEEP_SECONDS)
        now_ist = datetime.datetime.now(IST)

    log_agent_action(
        agent_name=AGENT_NAME,
        action="monitor_loop_ended",
        level="INFO",
    )


def _run_report_session(
    run_date: datetime.date,
    db_path_override: str | None,
    steps: list[AgentStepResult],
) -> None:
    """Run the report session."""
    started_at = datetime.datetime.now(IST)
    log_agent_action(
        agent_name=AGENT_NAME, action="step_started: reporter_agent", level="INFO"
    )
    try:
        run_reporter_agent(report_date=run_date, db_path_override=db_path_override)
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME, action="step_completed: reporter_agent", level="INFO"
        )
        steps.append(
            AgentStepResult(
                agent_name="reporter_agent",
                success=True,
                error_message=None,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    except Exception as exc:
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"step_failed: reporter_agent: {exc}",
            level="ERROR",
        )
        _safe_send_alert(
            subject="Orchestrator: reporter_agent failed",
            message=str(exc),
        )
        steps.append(
            AgentStepResult(
                agent_name="reporter_agent",
                success=False,
                error_message=str(exc),
                started_at=started_at,
                completed_at=completed_at,
            )
        )


# ---------------------------------------------------------------------------
# Dashboard auto-start (Amendment 2)
# ---------------------------------------------------------------------------


def _maybe_start_dashboard() -> None:
    """Start dashboard/server.py as background subprocess if port 8765 is free."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", 8765))
        if result == 0:
            # Port is in use — dashboard already running
            log_agent_action(
                agent_name=AGENT_NAME,
                action="dashboard_already_running",
                level="INFO",
            )
        else:
            subprocess.Popen([sys.executable, "dashboard/server.py"])
            log_agent_action(
                agent_name=AGENT_NAME,
                action="dashboard_started",
                level="INFO",
            )
    except Exception:
        pass  # Never crash orchestrator over dashboard


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


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
    # Amendment 2: start dashboard before anything else
    _maybe_start_dashboard()

    now_ist = datetime.datetime.now(IST)
    started_at = now_ist

    # Resolve session
    if session is not None:
        if session not in VALID_SESSIONS:
            raise OrchestratorError(
                f"Invalid session '{session}'. Must be one of {sorted(VALID_SESSIONS)}."
            )
        resolved_session = session
    else:
        resolved_session = _detect_session(now_ist)
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"session_auto_detected: {resolved_session}",
            level="INFO",
        )

    # Resolve run_date
    if run_date is None:
        today_ist = now_ist.date()
        if resolved_session == "evening":
            run_date = _next_trading_day(today_ist)
        else:
            run_date = today_ist

    # Weekday guard
    if resolved_session in {"morning", "monitor", "report"} and not _is_weekday(run_date):
        day_name = run_date.strftime("%A")
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"weekday_guard: {day_name}",
            level="WARNING",
        )
        return OrchestratorResult(
            session=resolved_session,
            run_date=run_date,
            safe_mode=False,
            safe_mode_reason=None,
            steps=[],
            started_at=started_at,
            completed_at=datetime.datetime.now(IST),
        )

    if resolved_session == "evening" and not _is_weekday(
        run_date - datetime.timedelta(days=1)
    ):
        # Evening runs Sun-Thu (so the previous day being a weekday isn't the right check).
        # The spec says "Sunday-Thursday" for evening. run_date is next trading day.
        # Check that today (the day the session runs) is Sun-Thu.
        today_dow = now_ist.date().weekday()  # 0=Mon ... 6=Sun
        if today_dow == 4:  # Friday
            # Friday evening → skip (next trading day is Monday, but we still guard)
            pass  # Friday is actually OK per spec (produces Monday watchlist)
        # Spec: evening Sunday-Thursday. weekday() 0=Mon,1=Tue,2=Wed,3=Thu,6=Sun
        # Friday(4) and Saturday(5) → no evening session
        if today_dow in {4, 5}:
            day_name = now_ist.date().strftime("%A")
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"weekday_guard: {day_name}",
                level="WARNING",
            )
            return OrchestratorResult(
                session=resolved_session,
                run_date=run_date,
                safe_mode=False,
                safe_mode_reason=None,
                steps=[],
                started_at=started_at,
                completed_at=datetime.datetime.now(IST),
            )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"session_started: {resolved_session}",
        level="INFO",
    )

    steps: list[AgentStepResult] = []
    safe_mode = False
    safe_mode_reason: str | None = None

    if resolved_session == "evening":
        _run_evening_session(run_date, db_path_override, steps)

    elif resolved_session == "morning":
        safe_mode, safe_mode_reason = _run_morning_session(run_date, db_path_override, steps)

    elif resolved_session == "monitor":
        _run_monitor_session(run_date, db_path_override, steps)

    elif resolved_session == "report":
        _run_report_session(run_date, db_path_override, steps)

    completed_at = datetime.datetime.now(IST)
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"session_completed: {resolved_session}",
        level="INFO",
    )

    return OrchestratorResult(
        session=resolved_session,
        run_date=run_date,
        safe_mode=safe_mode,
        safe_mode_reason=safe_mode_reason,
        steps=steps,
        started_at=started_at,
        completed_at=completed_at,
    )
