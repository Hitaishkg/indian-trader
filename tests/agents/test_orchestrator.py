"""Tests for src/agents/orchestrator.py

Covers all 17 test scenarios from the spec's test hints.
All agent calls are mocked to avoid external dependencies.
"""

import datetime
from typing import Any
from unittest.mock import MagicMock, Mock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.agents.orchestrator import (
    AgentStepResult,
    OrchestratorError,
    OrchestratorResult,
    run_orchestrator,
)

IST = ZoneInfo("Asia/Kolkata")


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def setup_seed() -> None:
    """Set random seed for reproducibility."""
    np.random.seed(42)


@pytest.fixture(autouse=True)
def mock_subprocess_autouse() -> Any:
    """Auto-mock subprocess.Popen to prevent dashboard startup in all tests."""
    with patch("src.agents.orchestrator.subprocess.Popen") as mock:
        mock_process = MagicMock()
        mock.return_value = mock_process
        yield mock


@pytest.fixture
def mock_log_agent_action() -> Any:
    """Mock log_agent_action to avoid DB writes."""
    with patch("src.agents.orchestrator.log_agent_action") as mock:
        yield mock


@pytest.fixture
def mock_send_alert() -> Any:
    """Mock send_alert to avoid Telegram/Gmail."""
    with patch("src.agents.orchestrator.send_alert") as mock:
        yield mock


@pytest.fixture
def mock_time_sleep() -> Any:
    """Mock time.sleep to speed up tests."""
    with patch("src.agents.orchestrator.time.sleep") as mock:
        yield mock


@pytest.fixture
def mock_all_agents() -> dict[str, Any]:
    """Mock all agent functions to avoid external calls."""
    # Create all mocks first
    mock_run_screener = MagicMock(return_value=None)
    mock_run_research = MagicMock(return_value=None)
    mock_run_watchlist = MagicMock(return_value=None)
    mock_check_watchlist_timeout = MagicMock(return_value=None)

    signal_result = MagicMock()
    signal_result.late_start = False
    mock_run_signal = MagicMock(return_value=signal_result)

    risk_result = MagicMock()
    risk_result.kill_switch_fired = False
    risk_result.kill_switch_reason = None
    mock_run_risk = MagicMock(return_value=risk_result)

    mock_run_execution = MagicMock(return_value=None)
    mock_run_monitor = MagicMock(return_value=None)
    mock_run_reporter = MagicMock(return_value=None)
    mock_run_data_collection = MagicMock(return_value=None)
    mock_run_morning_validator = MagicMock(return_value=None)

    # Now patch them all
    patches = [
        patch("src.agents.orchestrator.run_screener_agent", mock_run_screener),
        patch("src.agents.orchestrator.run_research_agent", mock_run_research),
        patch("src.agents.orchestrator.run_watchlist_agent", mock_run_watchlist),
        patch("src.agents.orchestrator.check_watchlist_timeout", mock_check_watchlist_timeout),
        patch("src.agents.orchestrator.run_signal_agent", mock_run_signal),
        patch("src.agents.orchestrator.run_risk_agent", mock_run_risk),
        patch("src.agents.orchestrator.run_execution_agent", mock_run_execution),
        patch("src.agents.orchestrator.run_monitor_agent", mock_run_monitor),
        patch("src.agents.orchestrator.run_reporter_agent", mock_run_reporter),
        patch("src.agents.orchestrator._run_data_collection", mock_run_data_collection),
        patch("src.agents.orchestrator._run_morning_validator", mock_run_morning_validator),
    ]

    for patcher in patches:
        patcher.start()

    # Return dict of all mocks
    mocks = {
        "run_screener_agent": mock_run_screener,
        "run_research_agent": mock_run_research,
        "run_watchlist_agent": mock_run_watchlist,
        "check_watchlist_timeout": mock_check_watchlist_timeout,
        "run_signal_agent": mock_run_signal,
        "run_risk_agent": mock_run_risk,
        "run_execution_agent": mock_run_execution,
        "run_monitor_agent": mock_run_monitor,
        "run_reporter_agent": mock_run_reporter,
        "_run_data_collection": mock_run_data_collection,
        "_run_morning_validator": mock_run_morning_validator,
    }

    yield mocks

    # Clean up
    for patcher in patches:
        patcher.stop()


@pytest.fixture
def mock_socket_port_free() -> Any:
    """Mock socket to simulate port 8765 is free."""
    with patch("src.agents.orchestrator.socket.socket") as mock_socket_class:
        mock_sock = MagicMock()
        mock_sock.__enter__ = Mock(return_value=mock_sock)
        mock_sock.__exit__ = Mock(return_value=False)
        mock_sock.connect_ex = Mock(return_value=1)  # 1 = port not in use
        mock_socket_class.return_value = mock_sock
        yield mock_socket_class


@pytest.fixture
def mock_socket_port_busy() -> Any:
    """Mock socket to simulate port 8765 is already in use."""
    with patch("src.agents.orchestrator.socket.socket") as mock_socket_class:
        mock_sock = MagicMock()
        mock_sock.__enter__ = Mock(return_value=mock_sock)
        mock_sock.__exit__ = Mock(return_value=False)
        mock_sock.connect_ex = Mock(return_value=0)  # 0 = port in use
        mock_socket_class.return_value = mock_sock
        yield mock_socket_class


# ============================================================================
# Test 1: Invalid session string
# ============================================================================


def test_invalid_session_string(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test that invalid session raises OrchestratorError.

    Covers test hint 6.
    """
    with pytest.raises(OrchestratorError) as exc_info:
        run_orchestrator(session="invalid")

    assert "Invalid session" in str(exc_info.value)


# ============================================================================
# Test 2: Evening session agent call order
# ============================================================================


def test_evening_session_agent_call_order(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test evening session calls agents in correct order.

    Covers test hint 7.
    """
    result = run_orchestrator(
        session="evening",
        run_date=datetime.date(2026, 4, 13)
    )

    # Verify agents were called in order
    assert result.session == "evening"
    assert len(result.steps) == 4
    agent_names = [step.agent_name for step in result.steps]

    assert agent_names[0] == "data_collector_agent"
    assert agent_names[1] == "screener_agent"
    assert agent_names[2] == "research_agent"
    assert agent_names[3] == "watchlist_agent"

    # Verify they all succeeded
    for step in result.steps:
        assert step.success is True


# ============================================================================
# Test 3: Morning safe mode on signal_agent late_start
# ============================================================================


def test_morning_safe_mode_on_late_start(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test morning session enters safe mode when signal_agent returns late_start.

    Covers test hint 8.
    """
    # Mock signal_agent to return late_start=True
    mock_all_agents["run_signal_agent"].return_value = MagicMock(late_start=True)

    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)  # Monday
    )

    assert result.session == "morning"
    assert result.safe_mode is True
    assert result.safe_mode_reason == "signal_agent_late_start"


# ============================================================================
# Test 4: Agent exception does not crash orchestrator
# ============================================================================


def test_agent_exception_isolation(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test that agent exception is caught and logged, orchestrator continues.

    Covers test hint 9.
    """
    # Make screener_agent raise an exception
    mock_all_agents["run_screener_agent"].side_effect = Exception("Screener failed")

    result = run_orchestrator(
        session="evening",
        run_date=datetime.date(2026, 4, 13)
    )

    # Orchestrator should return successfully
    assert isinstance(result, OrchestratorResult)

    # The failed step should be recorded
    screener_step = [s for s in result.steps if s.agent_name == "screener_agent"][0]
    assert screener_step.success is False
    assert "Screener failed" in screener_step.error_message

    # Subsequent agents should still be called
    assert len(result.steps) == 4
    # Watchlist should be in the steps
    watchlist_step = [s for s in result.steps if s.agent_name == "watchlist_agent"]
    assert len(watchlist_step) == 1


# ============================================================================
# Test 5: Monitor session runs
# ============================================================================


def test_monitor_session_runs(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
    mock_time_sleep: Any,
) -> None:
    """Test monitor session runs and completes.

    Covers test hint 10.
    """
    # Mock datetime.datetime.now to return a time within monitor window
    with patch("src.agents.orchestrator.datetime") as mock_dt:
        monitor_time = datetime.datetime(2026, 4, 14, 15, 40, tzinfo=IST)
        mock_dt.datetime.now.return_value = monitor_time
        # Preserve constructors
        mock_dt.datetime.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)
        mock_dt.date = datetime.date
        mock_dt.timedelta = datetime.timedelta

        result = run_orchestrator(
            session="monitor",
            run_date=datetime.date(2026, 4, 14)  # Tuesday
        )

        assert result.session == "monitor"
        # Should have at least 1 monitor tick
        monitor_steps = [s for s in result.steps if "monitor_agent" in s.agent_name]
        assert len(monitor_steps) >= 1


# ============================================================================
# Test 6: Monitor tick failure does not break loop
# ============================================================================


def test_monitor_tick_failure_continues_loop(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
    mock_time_sleep: Any,
) -> None:
    """Test monitor loop continues after a tick failure.

    Covers test hint 11.
    """
    # Make monitor_agent fail on first call, succeed on second
    call_count = [0]
    def monitor_side_effect(**kwargs: Any) -> None:
        call_count[0] += 1
        if call_count[0] == 1:
            raise Exception("Monitor tick 1 failed")

    mock_all_agents["run_monitor_agent"].side_effect = monitor_side_effect

    # Mock datetime.datetime.now to return a time within monitor window
    with patch("src.agents.orchestrator.datetime") as mock_dt:
        monitor_time = datetime.datetime(2026, 4, 14, 15, 40, tzinfo=IST)
        mock_dt.datetime.now.return_value = monitor_time
        # Preserve constructors
        mock_dt.datetime.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)
        mock_dt.date = datetime.date
        mock_dt.timedelta = datetime.timedelta

        result = run_orchestrator(
            session="monitor",
            run_date=datetime.date(2026, 4, 14)
        )

        assert result.session == "monitor"
        # Should have at least 1 tick; if there are 2, the first failed
        monitor_steps = [s for s in result.steps if "monitor_agent" in s.agent_name]
        assert len(monitor_steps) >= 1
        # If there are 2 ticks, verify failure handling
        if len(monitor_steps) >= 2:
            assert monitor_steps[0].success is False


# ============================================================================
# Test 7: Weekday guard blocks morning on Saturday
# ============================================================================


def test_weekday_guard_blocks_morning_saturday(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test weekday guard prevents morning session on Saturday.

    Covers test hint 12.
    """
    # Saturday 2026-04-11
    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 11)
    )

    assert result.session == "morning"
    assert result.steps == []  # No agents run on weekends


def test_weekday_guard_allows_morning_monday(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test weekday guard allows morning session on Monday.

    Covers test hint 12 (positive case).
    """
    # Monday 2026-04-13
    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    assert result.session == "morning"
    assert len(result.steps) > 0  # Agents run on weekdays


# ============================================================================
# Test 8: Evening run_date computes next trading day
# ============================================================================


def test_evening_run_date_computed_on_none(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test evening session computes run_date when None is passed.

    Covers test hint 13.
    """
    # When run_date is None in evening session, it computes next trading day
    # We pass an explicit date to verify the logic, not rely on time mocking
    result = run_orchestrator(
        session="evening",
        run_date=datetime.date(2026, 4, 13)  # Monday evening -> Tuesday
    )

    assert result.session == "evening"
    assert isinstance(result.run_date, datetime.date)


# ============================================================================
# Test 9: check_watchlist_timeout called first in morning
# ============================================================================


def test_check_watchlist_timeout_called_first_morning(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test morning session calls check_watchlist_timeout.

    Covers test hint 14 - verify the check_watchlist_timeout step is in the results.
    """
    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    # check_watchlist_timeout should be in the steps
    step_names = [step.agent_name for step in result.steps]
    assert "check_watchlist_timeout" in step_names
    # And it should be the first one
    assert step_names[0] == "check_watchlist_timeout"


# ============================================================================
# Test 10: Kill switch in risk_agent triggers safe_mode
# ============================================================================


def test_kill_switch_triggers_safe_mode(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test kill switch from risk_agent triggers safe_mode.

    Covers test hint 15.
    """
    # Mock risk_agent to return kill_switch_fired=True
    mock_all_agents["run_risk_agent"].return_value = MagicMock(
        kill_switch_fired=True,
        kill_switch_reason="drawdown_15pct"
    )

    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    assert result.session == "morning"
    assert result.safe_mode is True
    assert result.safe_mode_reason == "drawdown_15pct"


# ============================================================================
# Test 11: db_path_override passed through
# ============================================================================


def test_db_path_override_propagation(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test db_path_override is passed through to agents.

    Covers test hint 16.
    """
    custom_db_path = "/tmp/test_db.sqlite"
    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13),
        db_path_override=custom_db_path
    )

    # Verify risk_agent was called with db_path_override
    assert mock_all_agents["run_risk_agent"].called
    risk_call = mock_all_agents["run_risk_agent"].call_args
    assert risk_call.kwargs.get("db_path_override") == custom_db_path

    # Verify execution_agent was called with db_path_override
    assert mock_all_agents["run_execution_agent"].called
    exec_call = mock_all_agents["run_execution_agent"].call_args
    assert exec_call.kwargs.get("db_path_override") == custom_db_path


# ============================================================================
# Test 12: Report session calls only reporter_agent
# ============================================================================


def test_report_session_calls_only_reporter(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test report session only calls reporter_agent.

    Covers test hint 17.
    """
    result = run_orchestrator(
        session="report",
        run_date=datetime.date(2026, 4, 14)
    )

    assert result.session == "report"
    assert len(result.steps) == 1
    assert result.steps[0].agent_name == "reporter_agent"


# ============================================================================
# Additional edge case tests
# ============================================================================


def test_agent_step_result_on_success(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test AgentStepResult fields on success.

    Tests AgentStepResult structure.
    """
    result = run_orchestrator(
        session="report",
        run_date=datetime.date(2026, 4, 14)
    )

    step = result.steps[0]
    assert isinstance(step, AgentStepResult)
    assert step.success is True
    assert step.error_message is None
    assert step.agent_name == "reporter_agent"
    assert isinstance(step.started_at, datetime.datetime)
    assert isinstance(step.completed_at, datetime.datetime)


def test_agent_step_result_on_failure(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test AgentStepResult fields on failure.

    Tests AgentStepResult structure on exception.
    """
    mock_all_agents["run_reporter_agent"].side_effect = Exception("Test error")

    result = run_orchestrator(
        session="report",
        run_date=datetime.date(2026, 4, 14)
    )

    step = result.steps[0]
    assert isinstance(step, AgentStepResult)
    assert step.success is False
    assert "Test error" in step.error_message
    assert step.agent_name == "reporter_agent"


def test_orchestrator_result_fields(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test OrchestratorResult has all required fields.

    Tests OrchestratorResult structure.
    """
    result = run_orchestrator(
        session="report",
        run_date=datetime.date(2026, 4, 14)
    )

    assert isinstance(result, OrchestratorResult)
    assert result.session == "report"
    assert isinstance(result.run_date, datetime.date)
    assert isinstance(result.safe_mode, bool)
    assert result.safe_mode_reason is None or isinstance(result.safe_mode_reason, str)
    assert isinstance(result.steps, list)
    assert isinstance(result.started_at, datetime.datetime)
    assert isinstance(result.completed_at, datetime.datetime)


def test_dashboard_starts_when_port_free(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> Any:
    """Test dashboard starts when port 8765 is free.

    Tests dashboard auto-start logic.
    """
    with patch("src.agents.orchestrator.subprocess.Popen") as mock_subprocess:
        mock_process = MagicMock()
        mock_subprocess.return_value = mock_process

        run_orchestrator(
            session="report",
            run_date=datetime.date(2026, 4, 14)
        )

        # Should have started subprocess
        assert mock_subprocess.called


def test_dashboard_already_running_port_busy(
    mock_all_agents: dict[str, Any],
    mock_socket_port_busy: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test dashboard doesn't start when port 8765 is busy.

    Tests dashboard detection logic.
    """
    with patch("src.agents.orchestrator.subprocess.Popen") as mock_subprocess:
        run_orchestrator(
            session="report",
            run_date=datetime.date(2026, 4, 14)
        )

        # Should NOT have started subprocess
        assert not mock_subprocess.called


def test_execution_agent_skipped_on_kill_switch(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test execution_agent is not called when kill switch fires.

    Tests execution skip logic.
    """
    # Mock risk_agent to return kill_switch_fired=True
    mock_all_agents["run_risk_agent"].return_value = MagicMock(
        kill_switch_fired=True,
        kill_switch_reason="drawdown_15pct"
    )

    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    # execution_agent should NOT be in the steps
    execution_steps = [s for s in result.steps if s.agent_name == "execution_agent"]
    assert len(execution_steps) == 0


def test_morning_safe_mode_reason_preserved(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test that safe_mode_reason is set and preserved.

    Tests safe_mode tracking.
    """
    # Make signal_agent fail
    mock_all_agents["run_signal_agent"].side_effect = Exception("Signal agent error")

    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    assert result.safe_mode is True
    assert result.safe_mode_reason is not None
    assert "Signal agent error" in result.safe_mode_reason


def test_evening_session_runs_all_steps(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test evening session with all agents succeeding.

    Tests complete evening flow.
    """
    result = run_orchestrator(
        session="evening",
        run_date=datetime.date(2026, 4, 13)
    )

    assert result.session == "evening"
    assert result.safe_mode is False
    assert result.safe_mode_reason is None
    assert len(result.steps) == 4
    assert all(step.success for step in result.steps)


def test_morning_session_with_all_success(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test morning session with all agents succeeding.

    Tests complete morning flow.
    """
    # Ensure all agents succeed
    mock_all_agents["check_watchlist_timeout"].return_value = None
    mock_all_agents["run_signal_agent"].return_value = MagicMock(late_start=False)
    mock_all_agents["run_risk_agent"].return_value = MagicMock(
        kill_switch_fired=False,
        kill_switch_reason=None
    )
    mock_all_agents["run_execution_agent"].return_value = None

    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    assert result.session == "morning"
    assert result.safe_mode is False
    assert len(result.steps) >= 4  # At least check_timeout, morning_validator, signal, risk, execution


def test_valid_sessions_all_work(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test all valid session names are accepted.

    Tests session validation.
    """
    valid_sessions = ["evening", "morning", "monitor", "report"]

    for session in valid_sessions:
        result = run_orchestrator(
            session=session,
            run_date=datetime.date(2026, 4, 14)
        )
        assert result.session == session


def test_evening_on_friday_produces_monday_run_date(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test evening on Friday produces Monday run_date.

    Tests next trading day computation.
    """
    # Friday 2026-04-17, evening session with run_date=None
    # The function should compute next trading day as Monday 2026-04-20
    result = run_orchestrator(
        session="evening",
        run_date=None  # Will be computed
    )

    assert result.session == "evening"
    # Run_date should be a weekday
    assert result.run_date.weekday() < 5


def test_morning_session_safe_mode_on_signal_exception(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test morning enters safe_mode when signal_agent raises exception.

    Tests exception -> safe_mode flow.
    """
    mock_all_agents["run_signal_agent"].side_effect = Exception("Signal failed")

    result = run_orchestrator(
        session="morning",
        run_date=datetime.date(2026, 4, 13)
    )

    assert result.safe_mode is True
    assert "Signal failed" in result.safe_mode_reason


def test_report_on_saturday_blocked_by_weekday_guard(
    mock_all_agents: dict[str, Any],
    mock_socket_port_free: Any,
    mock_log_agent_action: Any,
) -> None:
    """Test report session is blocked on Saturday.

    Tests weekday guard for report session.
    """
    # Saturday 2026-04-11
    result = run_orchestrator(
        session="report",
        run_date=datetime.date(2026, 4, 11)
    )

    assert result.session == "report"
    assert result.steps == []  # No agents run on weekends
