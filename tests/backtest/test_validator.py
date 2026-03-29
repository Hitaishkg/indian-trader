"""Tests for src/backtest/validator.py -- backtest gate checker."""

import datetime
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest

from src.backtest.runner import BacktestResult
from src.backtest.validator import (
    AGENT_NAME,
    GateResult,
    MAX_DRAWDOWN_THRESHOLD,
    MIN_TRADES_THRESHOLD,
    PROFIT_FACTOR_THRESHOLD,
    SHARPE_THRESHOLD,
    WIN_RATE_THRESHOLD,
    ValidationResult,
    validate_backtest,
)


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------


@pytest.fixture
def passing_result() -> BacktestResult:
    """A BacktestResult where all 5 gates pass."""
    return BacktestResult(
        start_date=datetime.date(2010, 1, 4),
        end_date=datetime.date(2023, 12, 29),
        total_return_pct=85.0,
        annualized_return_pct=5.2,
        sharpe_ratio=1.5,
        max_drawdown_pct=10.0,
        win_rate_pct=55.0,
        total_trades=120,
        profit_factor=2.0,
        regime_changes=8,
        regime_blocked_weeks=3,
        raw_stats={},
        gates_passed=False,
    )


# -----------------------------------------------------------------------
# Test 1: All gates pass
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_all_gates_pass(mock_log, passing_result):
    """Test case 1: All gates pass. Verify all_gates_passed=True and gates_passed set to True."""
    result = validate_backtest(passing_result)

    assert result.all_gates_passed is True
    assert result.validated_result.gates_passed is True
    assert len(result.gate_results) == 5
    assert all(g.passed for g in result.gate_results)

    # Verify log was called
    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args[1]
    assert call_kwargs["agent_name"] == AGENT_NAME
    assert call_kwargs["action"] == "validate_backtest"
    assert call_kwargs["level"] == "INFO"
    assert "PASSED" in call_kwargs["result"]


# -----------------------------------------------------------------------
# Test 2: Sharpe fails (0.8)
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_sharpe_fails(mock_log, passing_result):
    """Test case 2: Sharpe ratio fails (0.8 < 1.0). Others pass."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=0.8,  # FAIL
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False
    assert result.validated_result.gates_passed is False

    # Find sharpe gate result
    sharpe_gate = [g for g in result.gate_results if g.gate_name == "sharpe_ratio"][0]
    assert sharpe_gate.passed is False
    assert sharpe_gate.actual_value == 0.8

    # Other 4 gates should pass
    other_gates = [g for g in result.gate_results if g.gate_name != "sharpe_ratio"]
    assert all(g.passed for g in other_gates)

    # Log level should be WARNING on fail
    call_kwargs = mock_log.call_args[1]
    assert call_kwargs["level"] == "WARNING"
    assert "FAILED" in call_kwargs["result"]


# -----------------------------------------------------------------------
# Test 3: Drawdown fails (16.0%)
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_drawdown_fails(mock_log, passing_result):
    """Test case 3: Max drawdown fails (16.0% >= 15.0%). Others pass."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=16.0,  # FAIL
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False
    assert result.validated_result.gates_passed is False

    # Find drawdown gate result
    dd_gate = [g for g in result.gate_results if g.gate_name == "max_drawdown"][0]
    assert dd_gate.passed is False
    assert dd_gate.actual_value == 16.0


# -----------------------------------------------------------------------
# Test 4: Win rate fails (35.0%)
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_win_rate_fails(mock_log, passing_result):
    """Test case 4: Win rate fails (35.0% <= 40.0%). Others pass."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=35.0,  # FAIL
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False

    # Find win_rate gate result
    wr_gate = [g for g in result.gate_results if g.gate_name == "win_rate"][0]
    assert wr_gate.passed is False
    assert wr_gate.actual_value == 35.0


# -----------------------------------------------------------------------
# Test 5: Trade count fails (50 trades)
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_trade_count_fails(mock_log, passing_result):
    """Test case 5: Total trades fails (50 < 100). Others pass."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=50,  # FAIL
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False

    # Find total_trades gate result
    tt_gate = [g for g in result.gate_results if g.gate_name == "total_trades"][0]
    assert tt_gate.passed is False
    assert tt_gate.actual_value == 50


# -----------------------------------------------------------------------
# Test 6: Profit factor fails (1.1)
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_profit_factor_fails(mock_log, passing_result):
    """Test case 6: Profit factor fails (1.1 < 1.3). Others pass."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=1.1,  # FAIL
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False

    # Find profit_factor gate result
    pf_gate = [g for g in result.gate_results if g.gate_name == "profit_factor"][0]
    assert pf_gate.passed is False
    assert pf_gate.actual_value == 1.1


# -----------------------------------------------------------------------
# Test 7: Boundary values -- exactly on threshold (fail or pass)
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_boundary_sharpe_exactly_1_0_fails(mock_log, passing_result):
    """Test case 7a: sharpe_ratio=1.0 fails (must be strictly >1.0)."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=1.0,  # FAIL (not > 1.0)
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    sharpe_gate = [g for g in result.gate_results if g.gate_name == "sharpe_ratio"][0]
    assert sharpe_gate.passed is False


@patch("src.backtest.validator.log_agent_action")
def test_boundary_drawdown_exactly_15_0_fails(mock_log, passing_result):
    """Test case 7b: max_drawdown_pct=15.0 fails (must be strictly <15.0)."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=15.0,  # FAIL (not < 15.0)
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    dd_gate = [g for g in result.gate_results if g.gate_name == "max_drawdown"][0]
    assert dd_gate.passed is False


@patch("src.backtest.validator.log_agent_action")
def test_boundary_win_rate_exactly_40_0_fails(mock_log, passing_result):
    """Test case 7c: win_rate_pct=40.0 fails (must be strictly >40.0)."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=40.0,  # FAIL (not > 40.0)
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    wr_gate = [g for g in result.gate_results if g.gate_name == "win_rate"][0]
    assert wr_gate.passed is False


@patch("src.backtest.validator.log_agent_action")
def test_boundary_total_trades_exactly_100_passes(mock_log, passing_result):
    """Test case 7d: total_trades=100 passes (condition is >=100, not >100)."""
    boundary_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=100,  # PASS (exactly at boundary, >= applies)
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(boundary_result)

    tt_gate = [g for g in result.gate_results if g.gate_name == "total_trades"][0]
    assert tt_gate.passed is True


@patch("src.backtest.validator.log_agent_action")
def test_boundary_profit_factor_exactly_1_3_fails(mock_log, passing_result):
    """Test case 7e: profit_factor=1.3 fails (must be strictly >1.3)."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=1.3,  # FAIL (not > 1.3)
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    pf_gate = [g for g in result.gate_results if g.gate_name == "profit_factor"][0]
    assert pf_gate.passed is False


# -----------------------------------------------------------------------
# Test 8: profit_factor=inf passes
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_profit_factor_inf_passes(mock_log, passing_result):
    """Test case 8: profit_factor=inf passes (inf > 1.3 is True)."""
    result_with_inf = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=float("inf"),  # PASS
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(result_with_inf)

    assert result.all_gates_passed is True
    pf_gate = [g for g in result.gate_results if g.gate_name == "profit_factor"][0]
    assert pf_gate.passed is True
    assert pf_gate.actual_value == float("inf")


# -----------------------------------------------------------------------
# Test 9: profit_factor=0.0 fails
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_profit_factor_0_0_fails(mock_log, passing_result):
    """Test case 9: profit_factor=0.0 fails (0.0 < 1.3)."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=0.0,  # FAIL
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False
    pf_gate = [g for g in result.gate_results if g.gate_name == "profit_factor"][0]
    assert pf_gate.passed is False
    assert pf_gate.actual_value == 0.0


# -----------------------------------------------------------------------
# Test 10: Frozen dataclass immutability
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_frozen_dataclass_immutability(mock_log, passing_result):
    """Test case 10: Frozen dataclass prevents mutation after creation."""
    result = validate_backtest(passing_result)

    # validated_result should be a frozen dataclass
    with pytest.raises(FrozenInstanceError):
        result.validated_result.gates_passed = False


# -----------------------------------------------------------------------
# Test 11: Negative total_trades raises ValueError
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_negative_total_trades_raises_value_error(mock_log, passing_result):
    """Test case 11: Negative total_trades raises ValueError with 'negative' in message."""
    bad_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=-1,  # Invalid
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    with pytest.raises(ValueError) as exc_info:
        validate_backtest(bad_result)

    assert "negative" in str(exc_info.value).lower()


# -----------------------------------------------------------------------
# Test 12: Gate results ordering
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_gate_results_ordering(mock_log, passing_result):
    """Test case 12: gate_results has exactly 5 elements in correct order."""
    result = validate_backtest(passing_result)

    assert len(result.gate_results) == 5

    # Verify order: sharpe_ratio, max_drawdown, win_rate, total_trades, profit_factor
    expected_names = ["sharpe_ratio", "max_drawdown", "win_rate", "total_trades", "profit_factor"]
    actual_names = [g.gate_name for g in result.gate_results]
    assert actual_names == expected_names


# -----------------------------------------------------------------------
# Test 13: Multiple gates fail simultaneously
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_multiple_gates_fail_simultaneously(mock_log, passing_result):
    """Test case 13: When multiple gates fail (e.g., sharpe and win_rate), all are marked failed."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=0.5,  # FAIL
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=30.0,  # FAIL
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    assert result.all_gates_passed is False

    failed_gates = [g for g in result.gate_results if not g.passed]
    assert len(failed_gates) == 2

    failed_names = {g.gate_name for g in failed_gates}
    assert "sharpe_ratio" in failed_names
    assert "win_rate" in failed_names

    # The other 3 should pass
    passing_gates = [g for g in result.gate_results if g.passed]
    assert len(passing_gates) == 3


# -----------------------------------------------------------------------
# Test 14: Original result not mutated on pass
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_original_result_not_mutated_on_pass(mock_log, passing_result):
    """Test case 14: Original result object still has gates_passed=False after validation passes."""
    original_gates_passed = passing_result.gates_passed
    assert original_gates_passed is False

    result = validate_backtest(passing_result)

    # The original result should not have been mutated
    assert passing_result.gates_passed is False
    assert passing_result.gates_passed == original_gates_passed

    # The validated_result should be a different object with gates_passed=True
    assert result.validated_result is not passing_result
    assert result.validated_result.gates_passed is True


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------


@patch("src.backtest.validator.log_agent_action")
def test_all_fields_are_nan(mock_log, passing_result):
    """Edge case: NaN values in float fields cause gates to fail (NaN comparisons return False)."""
    nan_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=float("nan"),
        max_drawdown_pct=float("nan"),
        win_rate_pct=float("nan"),
        total_trades=passing_result.total_trades,
        profit_factor=float("nan"),
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(nan_result)

    # All float-based gates should fail (NaN comparisons are False)
    assert result.all_gates_passed is False

    sharpe_gate = [g for g in result.gate_results if g.gate_name == "sharpe_ratio"][0]
    dd_gate = [g for g in result.gate_results if g.gate_name == "max_drawdown"][0]
    wr_gate = [g for g in result.gate_results if g.gate_name == "win_rate"][0]
    pf_gate = [g for g in result.gate_results if g.gate_name == "profit_factor"][0]

    # NaN > 1.0 is False
    assert sharpe_gate.passed is False
    # NaN < 15.0 is False
    assert dd_gate.passed is False
    # NaN > 40.0 is False
    assert wr_gate.passed is False
    # NaN > 1.3 is False
    assert pf_gate.passed is False


@patch("src.backtest.validator.log_agent_action")
def test_negative_sharpe_ratio(mock_log, passing_result):
    """Edge case: Negative sharpe ratio should fail."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=-0.5,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=passing_result.total_trades,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    sharpe_gate = [g for g in result.gate_results if g.gate_name == "sharpe_ratio"][0]
    assert sharpe_gate.passed is False


@patch("src.backtest.validator.log_agent_action")
def test_gate_result_immutability(mock_log, passing_result):
    """Edge case: GateResult frozen dataclasses are immutable."""
    result = validate_backtest(passing_result)

    gate = result.gate_results[0]

    with pytest.raises(FrozenInstanceError):
        gate.passed = False


@patch("src.backtest.validator.log_agent_action")
def test_validation_result_is_frozen(mock_log, passing_result):
    """Edge case: ValidationResult is a frozen dataclass."""
    result = validate_backtest(passing_result)

    with pytest.raises(FrozenInstanceError):
        result.all_gates_passed = False


@patch("src.backtest.validator.log_agent_action")
def test_gate_result_fields_accessible(mock_log, passing_result):
    """Edge case: All GateResult fields are accessible and correct type."""
    result = validate_backtest(passing_result)

    gate = result.gate_results[0]

    # Verify fields exist and are correct types
    assert isinstance(gate.gate_name, str)
    assert isinstance(gate.threshold, str)
    assert isinstance(gate.actual_value, (float, int))
    assert isinstance(gate.passed, bool)

    # Verify threshold field contains the human-readable description
    assert gate.threshold in ["> 1.0", "< 15.0", "> 40.0", ">= 100", "> 1.3"]


@patch("src.backtest.validator.log_agent_action")
def test_validation_result_gate_results_is_tuple(mock_log, passing_result):
    """Edge case: gate_results is a tuple (immutable), not a list."""
    result = validate_backtest(passing_result)

    assert isinstance(result.gate_results, tuple)

    # Attempt to mutate should fail
    with pytest.raises((TypeError, AttributeError)):
        result.gate_results[0] = None


@patch("src.backtest.validator.log_agent_action")
def test_zero_total_trades_passes(mock_log, passing_result):
    """Edge case: zero trades is still < 100, so total_trades gate fails."""
    failing_result = BacktestResult(
        start_date=passing_result.start_date,
        end_date=passing_result.end_date,
        total_return_pct=passing_result.total_return_pct,
        annualized_return_pct=passing_result.annualized_return_pct,
        sharpe_ratio=passing_result.sharpe_ratio,
        max_drawdown_pct=passing_result.max_drawdown_pct,
        win_rate_pct=passing_result.win_rate_pct,
        total_trades=0,
        profit_factor=passing_result.profit_factor,
        regime_changes=passing_result.regime_changes,
        regime_blocked_weeks=passing_result.regime_blocked_weeks,
        raw_stats={},
        gates_passed=False,
    )

    result = validate_backtest(failing_result)

    tt_gate = [g for g in result.gate_results if g.gate_name == "total_trades"][0]
    assert tt_gate.passed is False
