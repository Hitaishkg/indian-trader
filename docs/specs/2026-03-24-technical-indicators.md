# Spec: src/indicators/technical.py — Technical Indicator Calculations

**Date**: 2026-03-24
**Phase**: 2 — Strategy Core (step 1 of 7)
**Author**: Architect Agent (Opus)
**Status**: Awaiting approval

---

## 1. Module Purpose

Pure calculation module that computes RSI, MACD, Bollinger Bands, and ATR on
cleaned OHLCV data using pandas-ta. Receives the output of `clean_ohlcv()` and
returns the same DataFrame with indicator columns appended. Contains zero
strategy logic — no buy/sell signals, no thresholds, no filtering. Each
indicator is computed per symbol independently so that values never bleed across
symbol boundaries. This module replaces the temporary `compute_atr()` function
in `main.py` (which uses `ewm(span=14, adjust=False)`) with a proper
Wilder-smoothed ATR via pandas-ta.

---

## 2. Public API

### 2.1. `add_indicators`

```python
def add_indicators(
    df: pd.DataFrame,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_length: int = 20,
    bb_std: float = 2.0,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Compute technical indicators per symbol and append columns to the DataFrame.

    Processes each symbol group independently to prevent cross-symbol data
    bleeding. Symbols with fewer than MINIMUM_LOOKBACK rows receive NaN for
    all indicator columns rather than partial or incorrect values.

    Args:
        df: Cleaned OHLCV DataFrame from clean_ohlcv(). Must contain columns:
            symbol, date, open, high, low, close, volume. The date column must
            be timezone-aware (Asia/Kolkata) and the DataFrame must be sorted
            by (symbol, date) ascending.
        rsi_period: RSI lookback period. Default 14.
        macd_fast: MACD fast EMA period. Default 12.
        macd_slow: MACD slow EMA period. Default 26.
        macd_signal: MACD signal line period. Default 9.
        bb_length: Bollinger Bands lookback length. Default 20.
        bb_std: Bollinger Bands standard deviation multiplier. Default 2.0.
        atr_period: ATR lookback period. Default 14.

    Returns:
        A new DataFrame with the same rows and original columns, plus 8 new
        indicator columns: rsi, macd, macd_signal, macd_hist, bb_upper,
        bb_mid, bb_lower, atr. Rows where indicators cannot be computed
        (insufficient lookback) contain NaN in those columns.

    Raises:
        ValueError: If required columns are missing or date is timezone-naive.
        ValueError: If the DataFrame is empty.
    """
```

### 2.2. `compute_atr_series`

```python
def compute_atr_series(
    df: pd.DataFrame,
    period: int = 14,
) -> pd.Series:
    """Compute ATR series for a single symbol's OHLCV data using Wilder smoothing.

    Exposed as a standalone function because other modules (risk_agent,
    execution_agent, main.py) need ATR for a single symbol without computing
    all indicators. Uses pandas-ta internally.

    Args:
        df: OHLCV DataFrame for ONE symbol. Must contain columns: high, low,
            close. Must be sorted by date ascending.
        period: ATR lookback period. Default 14.

    Returns:
        pd.Series of ATR values aligned to the input index. Leading rows
        within the lookback window contain NaN.

    Raises:
        ValueError: If required columns (high, low, close) are missing.
    """
```

---

## 3. Input Contract

The input DataFrame is the first element of the tuple returned by
`clean_ohlcv()` from `src/data/cleaner.py`.

### Required columns

| Column | dtype | Description |
|--------|-------|-------------|
| symbol | object (str) | NSE ticker symbol (e.g. "RELIANCE", "HDFCBANK") |
| date | datetime64[ns, Asia/Kolkata] | Timezone-aware trading date |
| open | float64 | Opening price in INR |
| high | float64 | High price in INR |
| low | float64 | Low price in INR |
| close | float64 | Closing price in INR |
| volume | float64 | Volume traded |

### Preconditions

- DataFrame is non-empty.
- Date column is timezone-aware with tz=Asia/Kolkata.
- Within each symbol group, rows are sorted by date ascending (this is
  guaranteed by `clean_ohlcv()` and `fetch_ohlcv()`).
- Original columns must not be modified or dropped.

### Validation on entry

The module must validate at function entry:
1. All 7 required columns are present — raise `ValueError` if any missing.
2. Date column is timezone-aware — raise `ValueError` if naive.
3. DataFrame is non-empty — raise `ValueError` if `len(df) == 0`.

---

## 4. Output Contract

### New columns appended

| Column | dtype | Source indicator | NaN behaviour |
|--------|-------|-----------------|---------------|
| rsi | float64 | RSI(14) | NaN for first ~14 rows per symbol, and for all rows of symbols with < 26 total rows |
| macd | float64 | MACD line (12,26,9) | NaN for first ~33 rows per symbol, and for all rows of symbols with < 26 total rows |
| macd_signal | float64 | MACD signal line | Same as macd |
| macd_hist | float64 | MACD histogram | Same as macd |
| bb_upper | float64 | Bollinger upper band (20, 2.0) | NaN for first ~19 rows per symbol, and for all rows of symbols with < 26 total rows |
| bb_mid | float64 | Bollinger middle band (SMA 20) | Same as bb_upper |
| bb_lower | float64 | Bollinger lower band (20, 2.0) | NaN for first ~19 rows per symbol, and for all rows of symbols with < 26 total rows |
| atr | float64 | ATR(14, Wilder) | NaN for first ~14 rows per symbol, and for all rows of symbols with < 26 total rows |

### Guarantees

- Original columns (symbol, date, open, high, low, close, volume) are
  unchanged — identical values, identical dtypes.
- Row count is identical to input — no rows added or removed.
- Row order is identical to input.
- A new DataFrame is returned — the input is never mutated.
- The `macd_signal` column name collides with the MACD signal line column name
  from pandas-ta (`MACDs_12_26_9`). The module must rename it explicitly.
  Callers reference `df["macd_signal"]`, never the pandas-ta internal name.

---

## 5. Per-Symbol Isolation Requirement

All indicator calculations MUST be performed per symbol group using
`df.groupby("symbol")`. This prevents:

- ATR using the previous day's close of a different symbol.
- MACD EMA state carrying over across symbol boundaries.
- Bollinger Band SMA averaging prices from multiple stocks.

Implementation pattern:

```python
def _compute_for_symbol(group: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators for a single symbol group."""
    # ... pandas-ta calls ...
    return group_with_indicators

result = df.groupby("symbol", group_keys=False).apply(_compute_for_symbol)
```

The inner function receives only one symbol's rows and returns only that
symbol's rows with indicator columns added. `group_keys=False` prevents
pandas from adding a redundant group index.

---

## 6. Minimum Lookback Logic

### Constant

```python
MINIMUM_LOOKBACK: int = 26
```

This is the MACD slow period — the largest minimum data requirement among all
four indicators. Using a single threshold for all indicators (rather than
per-indicator thresholds) simplifies the logic and ensures consistency: either
a symbol has enough data for all indicators, or it gets NaN for all of them.

### Behaviour when a symbol has fewer than MINIMUM_LOOKBACK rows

Inside `_compute_for_symbol()`:

1. Check `len(group) < MINIMUM_LOOKBACK`.
2. If true: assign NaN to all 8 indicator columns for every row in the group.
3. Log a warning via `log_agent_action()` with `symbol=<symbol>`,
   `action="insufficient_lookback: {len(group)} rows < {MINIMUM_LOOKBACK}"`,
   `result="skipped"`.
4. Return the group with NaN indicator columns — do not raise, do not drop.

This ensures downstream modules always receive a complete DataFrame with a
predictable schema, regardless of data availability per symbol.

---

## 7. pandas-ta Usage — Exact Call Patterns

### 7.1. RSI

```python
import pandas_ta as ta

rsi_series = ta.rsi(close=group["close"], length=rsi_period)
# Returns: pd.Series named "RSI_14" (for period=14)
# Assign to group["rsi"]
```

### 7.2. MACD

```python
macd_df = ta.macd(close=group["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal)
# Returns: pd.DataFrame with columns:
#   "MACD_12_26_9"   -> assign to group["macd"]
#   "MACDh_12_26_9"  -> assign to group["macd_hist"]
#   "MACDs_12_26_9"  -> assign to group["macd_signal"]
```

### 7.3. Bollinger Bands

```python
bb_df = ta.bbands(close=group["close"], length=bb_length, std=bb_std)
# Returns: pd.DataFrame with columns:
#   "BBL_20_2.0"  -> assign to group["bb_lower"]
#   "BBM_20_2.0"  -> assign to group["bb_mid"]
#   "BBU_20_2.0"  -> assign to group["bb_upper"]
```

### 7.4. ATR (Wilder Smoothing)

pandas-ta's `ta.atr()` uses RMA (Wilder smoothing) by default when called
without a `mamode` parameter. However, to be explicit and match the project
requirement of Wilder smoothing (equivalent to `ewm(com=13, adjust=False)`
for period=14):

```python
atr_series = ta.atr(high=group["high"], low=group["low"], close=group["close"], length=atr_period)
# Returns: pd.Series named "ATRr_14" (for period=14)
# pandas-ta default mamode is "rma" which IS Wilder smoothing
# Wilder's smoothing with period N is equivalent to EMA with com=N-1, adjust=False
# For period=14: com=13, adjust=False — exactly what the project requires
# Assign to group["atr"]
```

**Verification note for Tester Agent**: compare `ta.atr()` output against
a manual Wilder calculation (`tr.ewm(com=13, adjust=False).mean()`) on a
known dataset. Values must match within floating-point tolerance (1e-10).

### 7.5. Handling pandas-ta returning None

pandas-ta functions return `None` (not an empty Series/DataFrame) when the
input has insufficient data. The code must check for `None` before assignment:

```python
rsi_series = ta.rsi(close=group["close"], length=rsi_period)
if rsi_series is not None:
    group["rsi"] = rsi_series
else:
    group["rsi"] = float("nan")
```

Apply this pattern to all four indicator calls.

---

## 8. Logging

Use `log_agent_action()` from `src/utils/logger.py`. Agent name: `"technical"`.

### Events to log

| When | agent_name | action | symbol | result | level |
|------|-----------|--------|--------|--------|-------|
| `add_indicators()` starts | technical | `"computing indicators for {n} symbols"` | None | None | INFO |
| Symbol has < MINIMUM_LOOKBACK rows | technical | `"insufficient_lookback: {rows} rows < {MINIMUM_LOOKBACK}"` | symbol | skipped | WARNING |
| `add_indicators()` completes | technical | `"indicators computed: {n_ok} symbols ok, {n_skipped} symbols skipped"` | None | ok | INFO |
| pandas-ta returns None unexpectedly | technical | `"pandas_ta returned None for {indicator_name}"` | symbol | error | WARNING |
| Validation error (missing columns, etc.) | technical | `"validation_failed: {reason}"` | None | error | ERROR |

---

## 9. Error Handling

### Exceptions raised by this module

| Exception | When | Message pattern |
|-----------|------|-----------------|
| `ValueError` | Required columns missing from input DataFrame | `"DataFrame missing required columns: {list}"` |
| `ValueError` | Date column is timezone-naive | `"date column must be timezone-aware (Asia/Kolkata)"` |
| `ValueError` | DataFrame is empty | `"DataFrame is empty, cannot compute indicators"` |
| `ValueError` | `compute_atr_series` input missing high/low/close | `"DataFrame missing required columns for ATR: {list}"` |

### Exceptions caught by this module

| Exception | Where | Action |
|-----------|-------|--------|
| `TypeError` | pandas-ta call returns unexpected type | Log warning, assign NaN to that indicator for the symbol, continue |
| `Exception` | Any pandas-ta call fails for a symbol | Log error via `log_agent_action`, assign NaN to all indicators for that symbol, continue processing remaining symbols — never crash the pipeline for one symbol's failure |

The module must NEVER use bare `except:` — always catch specific exceptions.

---

## 10. Module-Level Constants

```python
MINIMUM_LOOKBACK: int = 26
"""Minimum rows required per symbol to compute any indicator. Equals MACD slow
period — the largest minimum data requirement among all four indicators."""

RSI_PERIOD: int = 14
"""Default RSI lookback period."""

MACD_FAST: int = 12
"""Default MACD fast EMA period."""

MACD_SLOW: int = 26
"""Default MACD slow EMA period."""

MACD_SIGNAL_PERIOD: int = 9
"""Default MACD signal line smoothing period."""

BB_LENGTH: int = 20
"""Default Bollinger Bands lookback length."""

BB_STD: float = 2.0
"""Default Bollinger Bands standard deviation multiplier."""

ATR_PERIOD: int = 14
"""Default ATR lookback period (Wilder smoothing)."""

AGENT_NAME: str = "technical"
"""Agent name for log_agent_action() calls."""

_REQUIRED_COLUMNS: list[str] = ["symbol", "date", "open", "high", "low", "close", "volume"]
"""Columns that must exist in the input DataFrame."""

_ATR_REQUIRED_COLUMNS: list[str] = ["high", "low", "close"]
"""Columns required for standalone ATR computation."""
```

---

## 11. Test Hints — Key Scenarios for Tester Agent

### 11.1. Core functionality

1. **Happy path**: 50 rows per symbol, 3 symbols. Verify all 8 indicator
   columns present. Verify no NaN in the tail rows (where lookback is
   satisfied). Verify original columns unchanged.

2. **Per-symbol isolation**: 2 symbols with very different price ranges
   (e.g. Symbol A at 100 INR, Symbol B at 2000 INR). Verify that RSI/MACD/BB
   values for Symbol A are independent of Symbol B's prices — compute each
   symbol separately and compare against the grouped result.

3. **Row count preservation**: Output has exactly the same number of rows as
   input. No rows added, none removed.

4. **Input immutability**: Pass a DataFrame, call `add_indicators()`, verify
   the original DataFrame is unchanged (same columns, same values).

### 11.2. Minimum lookback

5. **Symbol with exactly 25 rows** (< MINIMUM_LOOKBACK): All 8 indicator
   columns must be NaN for every row of that symbol. Other symbols with
   sufficient data must still have valid indicators.

6. **Symbol with exactly 26 rows** (= MINIMUM_LOOKBACK): Indicators should
   be computed (not all NaN). Some leading rows will have NaN due to
   individual indicator lookback periods — this is expected.

7. **All symbols below minimum lookback**: All indicator columns are NaN
   everywhere. No crash.

### 11.3. ATR Wilder smoothing verification

8. **Wilder smoothing match**: For a known price series, compute ATR via
   `compute_atr_series()` and also via manual calculation
   (`tr.ewm(com=period-1, adjust=False).mean()`). Values must match within
   floating-point tolerance (atol=1e-10).

9. **compute_atr_series standalone**: Call on a single-symbol DataFrame.
   Verify the returned Series has the same index as the input. Verify NaN
   in leading rows, valid floats after lookback.

### 11.4. Error handling

10. **Missing columns**: Call `add_indicators()` with a DataFrame missing
    "close". Must raise `ValueError` with a message mentioning the missing
    column.

11. **Timezone-naive dates**: Must raise `ValueError`.

12. **Empty DataFrame**: Must raise `ValueError`.

13. **compute_atr_series missing columns**: Call with DataFrame missing "high".
    Must raise `ValueError`.

### 11.5. Edge cases

14. **Single symbol, single row**: All indicators NaN, no crash.

15. **Symbol with NaN prices in close column**: pandas-ta should handle
    gracefully (propagate NaN). Verify no crash.

16. **DataFrame with extra columns** (e.g. a "flag" column from cleaner):
    Extra columns must be preserved in output unchanged.

### 11.6. Logging verification

17. **Insufficient lookback logging**: When a symbol has < 26 rows, verify
    `log_agent_action` is called with `result="skipped"` and the symbol name.
    (Use mock/patch on `log_agent_action`.)

---

## 12. File Location and Imports

```
src/indicators/__init__.py   (empty, makes it a package)
src/indicators/technical.py  (this module)
tests/indicators/__init__.py (empty, makes it a package)
tests/indicators/test_technical.py (tests)
```

### Required imports

```python
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from src.utils.logger import log_agent_action
```

### pyproject.toml dependency

Ensure `pandas-ta` is listed in `[project.dependencies]`. The Coder Agent
must verify this and add it if missing:

```
"pandas-ta>=0.3.14b1",
```

---

## 13. Interaction with main.py

The temporary `compute_atr()` function in `main.py` (lines 32-57) must be
replaced by a call to `compute_atr_series()` from this module once it is
built. This replacement is NOT part of this spec — it will be handled in a
separate integration task. The Coder Agent must NOT modify `main.py` as part
of building this module.

---

## 14. What This Module Does NOT Do

- No buy/sell signal generation (that is `src/agents/signal_agent.py`)
- No quality filtering (that is `src/strategy/quality_filter.py`)
- No regime filtering (that is `src/strategy/regime.py`)
- No position sizing or risk calculation (that is `src/risk/`)
- No database writes (indicators are computed in memory, consumed by callers)
- No network calls (all computation is on in-memory DataFrames)
