# Spec: src/backtest/validator.py -- Backtest Gate Checker

**Date**: 2026-03-29
**Author**: Architect Agent
**Status**: Awaiting approval

---

## 1. Module Purpose

Pure computation module that evaluates a `BacktestResult` (from `src/backtest/runner.py`) against the 5 required backtest gates defined in `risk.md`. Returns a `ValidationResult` containing per-gate pass/fail breakdowns and an overall verdict. If all 5 gates pass, produces a new `BacktestResult` with `gates_passed=True` via `dataclasses.replace()`. Never mutates the original frozen dataclass. Contains zero business logic beyond gate checking -- no strategy logic, no data fetching, no database writes.

---

## 2. Public API

```python
from dataclasses import dataclass, replace
from src.backtest.runner import BacktestResult

AGENT_NAME: str = "backtest_validator"

# Gate threshold constants
SHARPE_THRESHOLD: float = 1.0
MAX_DRAWDOWN_THRESHOLD: float = 15.0
WIN_RATE_THRESHOLD: float = 40.0
MIN_TRADES_THRESHOLD: int = 100
PROFIT_FACTOR_THRESHOLD: float = 1.3


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
```

---

## 3. Input Contract

| Field | Source | Type | Precondition |
|-------|--------|------|-------------|
| `result` | `BacktestResult` from `run_backtest()` | frozen dataclass | Must be a valid `BacktestResult` instance |
| `result.sharpe_ratio` | float | Annualized Sharpe ratio | Can be negative, zero, or positive |
| `result.max_drawdown_pct` | float | Positive percentage points | e.g. 14.2 means 14.2% drawdown |
| `result.win_rate_pct` | float | Percentage points | e.g. 55.0 means 55% |
| `result.total_trades` | int | Count of round-trip trades | Must be >= 0; raise ValueError if < 0 |
| `result.profit_factor` | float | Gross profit / gross loss | Can be 0.0, positive, or float('inf') |
| `result.gates_passed` | bool | Should be False on entry | Not enforced -- validator works regardless |

NaN behaviour: If any field is `float('nan')`, the comparison operators (`>`, `<`, `>=`) return False in Python. This means NaN values will naturally cause that gate to fail. No special NaN handling needed.

---

## 4. Output Contract

### ValidationResult guarantees

- `all_gates_passed` is `True` if and only if all 5 `GateResult.passed` values are `True`.
- `gate_results` is always a tuple of exactly 5 `GateResult` entries, in fixed order: `sharpe_ratio`, `max_drawdown`, `win_rate`, `total_trades`, `profit_factor`.
- `validated_result.gates_passed` is `True` only when `all_gates_passed` is `True`.
- `validated_result` is the original `result` object (same identity) when any gate fails.
- `validated_result` is a new object (via `dataclasses.replace`) when all gates pass.
- All dataclasses are frozen -- no mutation possible after creation.

### GateResult field values

| gate_name | threshold (str) | actual_value source | passed condition |
|-----------|----------------|---------------------|-----------------|
| `"sharpe_ratio"` | `"> 1.0"` | `result.sharpe_ratio` | `result.sharpe_ratio > 1.0` |
| `"max_drawdown"` | `"< 15.0"` | `result.max_drawdown_pct` | `result.max_drawdown_pct < 15.0` |
| `"win_rate"` | `"> 40.0"` | `result.win_rate_pct` | `result.win_rate_pct > 40.0` |
| `"total_trades"` | `">= 100"` | `result.total_trades` | `result.total_trades >= 100` |
| `"profit_factor"` | `"> 1.3"` | `result.profit_factor` | `result.profit_factor > 1.3` |

---

## 5. Implementation Details

### Algorithm

1. Validate input: if `result.total_trades < 0`, raise `ValueError("total_trades cannot be negative: {result.total_trades}")`.
2. Evaluate each of the 5 gates, constructing a `GateResult` for each.
3. Determine `all_passed = all(g.passed for g in gate_results)`.
4. If `all_passed`, create `validated_result = dataclasses.replace(result, gates_passed=True)`.
5. If not `all_passed`, set `validated_result = result` (no copy, original object).
6. Construct and return `ValidationResult(all_gates_passed=all_passed, gate_results=tuple(gate_results), validated_result=validated_result)`.
7. Before returning, call `log_agent_action()` with the overall result.

### Key implementation notes

- Use `dataclasses.replace()` from the standard library. Do not import `copy`.
- The 5 gate evaluations are defined in a list/tuple structure (not 5 separate if-statements) for clarity and to guarantee the fixed ordering.
- `float('inf') > 1.3` evaluates to `True` in Python. No special-casing needed.
- `float('nan') > 1.3` evaluates to `False` in Python. No special-casing needed.
- Strict inequality for all thresholds except `total_trades` which uses `>=`. This means `sharpe_ratio == 1.0` fails (it must be strictly greater than 1.0).

### Suggested internal structure

Define the 5 gate definitions as a tuple of tuples to avoid repetition:

```python
_GATE_DEFINITIONS: tuple[tuple[str, str, str, float | int], ...] = (
    ("sharpe_ratio", "> 1.0", "sharpe_ratio", SHARPE_THRESHOLD),
    ("max_drawdown", "< 15.0", "max_drawdown_pct", MAX_DRAWDOWN_THRESHOLD),
    ("win_rate", "> 40.0", "win_rate_pct", WIN_RATE_THRESHOLD),
    ("total_trades", ">= 100", "total_trades", MIN_TRADES_THRESHOLD),
    ("profit_factor", "> 1.3", "profit_factor", PROFIT_FACTOR_THRESHOLD),
)
```

Each gate needs its own comparison operator. Use a small mapping or inline logic:
- `sharpe_ratio`: `actual > threshold`
- `max_drawdown`: `actual < threshold`
- `win_rate`: `actual > threshold`
- `total_trades`: `actual >= threshold`
- `profit_factor`: `actual > threshold`

Do not use `operator` module lambdas stored in tuples -- keep it simple with explicit comparisons in a helper function or a dict mapping gate_name to a callable.

---

## 6. Constants

| Constant | Value | Explanation |
|----------|-------|-------------|
| `AGENT_NAME` | `"backtest_validator"` | Used in log_agent_action() calls |
| `SHARPE_THRESHOLD` | `1.0` | Sharpe ratio must be strictly greater than 1.0 |
| `MAX_DRAWDOWN_THRESHOLD` | `15.0` | Max drawdown must be strictly less than 15.0% |
| `WIN_RATE_THRESHOLD` | `40.0` | Win rate must be strictly greater than 40.0% |
| `MIN_TRADES_THRESHOLD` | `100` | Total trades must be >= 100 (not strictly greater) |
| `PROFIT_FACTOR_THRESHOLD` | `1.3` | Profit factor must be strictly greater than 1.3 |

---

## 7. Logging

One call to `log_agent_action()` when validation completes:

```python
log_agent_action(
    agent_name=AGENT_NAME,
    action="validate_backtest",
    level="INFO" if all_passed else "WARNING",
    result=f"{'PASSED' if all_passed else 'FAILED'}: "
           f"sharpe={result.sharpe_ratio:.2f}, "
           f"drawdown={result.max_drawdown_pct:.1f}%, "
           f"win_rate={result.win_rate_pct:.1f}%, "
           f"trades={result.total_trades}, "
           f"pf={result.profit_factor:.2f}"
)
```

Notes:
- Level is `"INFO"` on pass, `"WARNING"` on fail.
- `profit_factor` of `float('inf')` will format as `"inf"` which is acceptable.
- No symbol parameter (this is a portfolio-level check, not per-stock).

---

## 8. Error Handling

| Condition | Action |
|-----------|--------|
| `result.total_trades < 0` | Raise `ValueError(f"total_trades cannot be negative: {result.total_trades}")` |
| `result` is not a `BacktestResult` | Do not type-check at runtime. Rely on type hints. |
| `result.profit_factor` is `float('inf')` | Natural Python comparison handles this (`inf > 1.3` is `True`). No special handling. |
| `result.profit_factor` is `float('nan')` | Natural Python comparison handles this (`nan > 1.3` is `False`). Gate fails. No special handling. |

No bare except clauses. No try/except needed in this module -- it is pure computation with no I/O except the log call. If `log_agent_action()` fails, let the exception propagate (it indicates a database problem that should halt the pipeline).

---

## 9. Out of Scope

- This module does NOT run backtests. That is `runner.py`.
- This module does NOT fetch data or read from any database table.
- This module does NOT write to any database table (logging goes through `log_agent_action()` which handles DB writes).
- This module does NOT generate reports or save results to files. Report generation is the caller's responsibility.
- This module does NOT apply any strategy logic (quality filter, momentum, regime).
- This module does NOT modify gate thresholds at runtime. All thresholds are module-level constants.
- This module does NOT validate the `raw_stats` dict contents.
- This module does NOT check whether `gates_passed` was already True on input (idempotent -- calling twice is safe).

---

## 10. Test Hints

Minimum 10 test cases for `tests/backtest/test_validator.py`:

1. **All gates pass**: Construct a BacktestResult where all 5 metrics exceed thresholds. Assert `all_gates_passed=True`, `validated_result.gates_passed=True`, all 5 `GateResult.passed=True`.

2. **Sharpe fails (0.8)**: sharpe_ratio=0.8, all others passing. Assert `all_gates_passed=False`, `validated_result.gates_passed=False`, sharpe gate `passed=False`, other 4 gates `passed=True`.

3. **Drawdown fails (16.0%)**: max_drawdown_pct=16.0, all others passing. Assert `all_gates_passed=False`, drawdown gate `passed=False`.

4. **Win rate fails (35.0%)**: win_rate_pct=35.0, all others passing. Assert `all_gates_passed=False`, win_rate gate `passed=False`.

5. **Trade count fails (50 trades)**: total_trades=50, all others passing. Assert `all_gates_passed=False`, total_trades gate `passed=False`.

6. **Profit factor fails (1.1)**: profit_factor=1.1, all others passing. Assert `all_gates_passed=False`, profit_factor gate `passed=False`.

7. **Boundary values -- exactly on threshold**: sharpe_ratio=1.0 (fail, condition is `>`), max_drawdown_pct=15.0 (fail, condition is `<`), win_rate_pct=40.0 (fail, condition is `>`), total_trades=100 (pass, condition is `>=`), profit_factor=1.3 (fail, condition is `>`). Test each boundary individually.

8. **profit_factor=float('inf') passes**: Set profit_factor to `float('inf')`, all others passing. Assert profit_factor gate `passed=True` and `all_gates_passed=True`.

9. **profit_factor=0.0 fails**: Set profit_factor to 0.0, all others passing. Assert profit_factor gate `passed=False`.

10. **Frozen dataclass immutability**: After getting a passing ValidationResult, attempt to set `validated_result.gates_passed = False`. Assert `FrozenInstanceError` (or `dataclasses.FrozenInstanceError`) is raised.

11. **Negative total_trades raises ValueError**: Construct a BacktestResult with total_trades=-1. Assert `ValueError` is raised with message containing "negative".

12. **Gate results ordering**: Verify `gate_results` tuple has exactly 5 elements and names are in order: `sharpe_ratio`, `max_drawdown`, `win_rate`, `total_trades`, `profit_factor`.

13. **Multiple gates fail simultaneously**: Set sharpe_ratio=0.5 and win_rate_pct=30.0, others passing. Assert exactly 2 gates show `passed=False`, `all_gates_passed=False`.

14. **Original result not mutated on pass**: After a passing validation, verify the original `result` object still has `gates_passed=False` (the original is not mutated; a new copy was made).

---

## 11. File Locations

| File | Action |
|------|--------|
| `src/backtest/validator.py` | **New file** -- the module being specified |
| `src/backtest/__init__.py` | Already exists -- no changes needed |
| `tests/backtest/test_validator.py` | **New file** -- test module |
| `tests/backtest/__init__.py` | Already exists -- no changes needed |

---

## 12. pyproject.toml

No new dependencies required. This module uses only:
- `dataclasses` (stdlib)
- `src.backtest.runner.BacktestResult` (existing)
- `src.utils.logger.log_agent_action` (existing)
