"""Backtest gate checker for the Indian Trader strategy pipeline.

Evaluates a BacktestResult from src/backtest/runner.py against the 5 required
backtest gates defined in risk.md. Returns a ValidationResult containing
per-gate pass/fail breakdowns and an overall verdict.

If all 5 gates pass, produces a new BacktestResult with gates_passed=True via
dataclasses.replace(). Never mutates the original frozen dataclass.

Contains zero business logic beyond gate checking -- no strategy logic,
no data fetching, no database writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.backtest.runner import BacktestResult
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "backtest_validator"

SHARPE_THRESHOLD: float = 1.0
MAX_DRAWDOWN_THRESHOLD: float = 15.0
WIN_RATE_THRESHOLD: float = 40.0
MIN_TRADES_THRESHOLD: int = 100
PROFIT_FACTOR_THRESHOLD: float = 1.3


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Result for a single backtest gate evaluation.

    Attributes:
        gate_name: Machine-readable gate identifier, e.g. "sharpe_ratio".
        threshold: Human-readable threshold description, e.g. "> 1.0".
        actual_value: The actual value extracted from BacktestResult.
        passed: True if the gate condition is satisfied.
    """

    gate_name: str
    threshold: str
    actual_value: float | int
    passed: bool


@dataclass(frozen=True)
class ValidationResult:
    """Complete output of validate_backtest().

    Attributes:
        all_gates_passed: True only if every gate in gate_results passed.
        gate_results: Exactly 5 GateResult entries, one per gate, in the
            order: sharpe_ratio, max_drawdown, win_rate, total_trades,
            profit_factor.
        validated_result: If all_gates_passed is True, a copy of the input
            BacktestResult with gates_passed=True. If False, the original
            BacktestResult unchanged (gates_passed remains False).
    """

    all_gates_passed: bool
    gate_results: tuple[GateResult, ...]
    validated_result: BacktestResult


# ---------------------------------------------------------------------------
# Private helper: per-gate comparison logic
# ---------------------------------------------------------------------------

def _check_gate(gate_name: str, actual: float | int, threshold: float | int) -> bool:
    """Apply the correct comparison operator for the given gate.

    Args:
        gate_name: Machine-readable gate identifier.
        actual: The measured value from BacktestResult.
        threshold: The constant threshold to compare against.

    Returns:
        True if the gate condition is satisfied, False otherwise.
    """
    if gate_name == "max_drawdown":
        return actual < threshold
    if gate_name == "total_trades":
        return actual >= threshold
    # sharpe_ratio, win_rate, profit_factor all use strict greater-than
    return actual > threshold


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_backtest(result: BacktestResult) -> ValidationResult:
    """Evaluate a BacktestResult against all 5 required backtest gates.

    Gates (all must pass for overall pass):
        1. sharpe_ratio > 1.0
        2. max_drawdown_pct < 15.0
        3. win_rate_pct > 40.0
        4. total_trades >= 100
        5. profit_factor > 1.3

    If all 5 gates pass:
        - all_gates_passed = True
        - validated_result = dataclasses.replace(result, gates_passed=True)

    If any gate fails:
        - all_gates_passed = False
        - validated_result = result (unchanged, gates_passed stays False)

    Logs one entry via log_agent_action() on completion with the overall
    verdict and per-gate summary.

    Args:
        result: BacktestResult from run_backtest(). gates_passed should be
            False on input but this is not enforced.

    Returns:
        ValidationResult with per-gate breakdown and final verdict.

    Raises:
        ValueError: If result.total_trades < 0 (indicates corrupt input).
    """
    if result.total_trades < 0:
        raise ValueError(
            f"total_trades cannot be negative: {result.total_trades}"
        )

    # Define the 5 gates in fixed order: (gate_name, threshold_str, field_name, threshold_value)
    _gate_definitions: tuple[tuple[str, str, str, float | int], ...] = (
        ("sharpe_ratio", "> 1.0", "sharpe_ratio", SHARPE_THRESHOLD),
        ("max_drawdown", "< 15.0", "max_drawdown_pct", MAX_DRAWDOWN_THRESHOLD),
        ("win_rate", "> 40.0", "win_rate_pct", WIN_RATE_THRESHOLD),
        ("total_trades", ">= 100", "total_trades", MIN_TRADES_THRESHOLD),
        ("profit_factor", "> 1.3", "profit_factor", PROFIT_FACTOR_THRESHOLD),
    )

    gate_results: list[GateResult] = []
    for gate_name, threshold_str, field_name, threshold_value in _gate_definitions:
        actual: float | int = getattr(result, field_name)
        passed: bool = _check_gate(gate_name, actual, threshold_value)
        gate_results.append(
            GateResult(
                gate_name=gate_name,
                threshold=threshold_str,
                actual_value=actual,
                passed=passed,
            )
        )

    all_passed: bool = all(g.passed for g in gate_results)

    if all_passed:
        validated_result: BacktestResult = replace(result, gates_passed=True)
    else:
        validated_result = result

    log_agent_action(
        agent_name=AGENT_NAME,
        action="validate_backtest",
        level="INFO" if all_passed else "WARNING",
        result=(
            f"{'PASSED' if all_passed else 'FAILED'}: "
            f"sharpe={result.sharpe_ratio:.2f}, "
            f"drawdown={result.max_drawdown_pct:.1f}%, "
            f"win_rate={result.win_rate_pct:.1f}%, "
            f"trades={result.total_trades}, "
            f"pf={result.profit_factor:.2f}"
        ),
    )

    return ValidationResult(
        all_gates_passed=all_passed,
        gate_results=tuple(gate_results),
        validated_result=validated_result,
    )
