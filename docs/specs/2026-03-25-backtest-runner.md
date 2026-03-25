# Spec: src/backtest/runner.py -- Backtesting Runner

**Date**: 2026-03-25
**Author**: Architect Agent (Opus)
**Status**: Awaiting approval
**Phase**: 2 -- Strategy Core (step 5 of 6)

---

## Purpose

Wraps the `backtesting` library (pip package `backtesting.py`) to replay the
full three-step stock selection strategy (quality filter, 12-1 momentum rank,
regime filter) plus technical entry signals over 2010--2023 NSE historical data.
Returns a `BacktestResult` frozen dataclass with all metrics needed by
`backtest/validator.py` (Phase 2 step 6) to check the five backtest gates.

---

## Dependencies

| Dependency | Used for |
|------------|----------|
| `backtesting` (backtesting.py) | `Strategy` base class, `Backtest` runner, stats output |
| `src/strategy/quality_filter.py` | `apply_quality_filter()` -- five hard filters |
| `src/strategy/momentum.py` | `compute_momentum()` -- 12-1 momentum ranking |
| `src/strategy/regime.py` | `apply_regime_filter()`, `compute_200dma()`, `count_consecutive_days_below_200dma()` |
| `src/indicators/technical.py` | `add_indicators()`, `compute_atr_series()` -- RSI, MACD, ATR |
| `src/data/fetcher.py` | `CACHE_DIR` constant for CSV cache path resolution |
| `src/utils/logger.py` | `log_agent_action()` for structured logging |
| `pandas` | DataFrame manipulation |

No database writes. No broker calls. No network calls during backtest runs
(all data must be pre-loaded or cached). No LLM calls of any kind.

---

## Scope Boundaries

### Included in backtest
- Quality filter (all 5 hard filters) -- applied weekly
- 12-1 momentum ranking -- applied weekly
- Nifty 50 200 DMA regime filter -- applied daily
- Technical entry signals: RSI < 40, MACD crossover
- ATR-based stop-loss (2x ATR, capped at 3% of entry)
- Take-profit at 2x stop-loss distance (1:2 risk-reward minimum)
- Position sizing: 1% equity risk per trade, 40% max per position, max 2 open
- Regime tightening: stop-loss from 2x ATR to 1x ATR when below 200 DMA

### Excluded from backtest
- **LLM sentiment signals** -- Gemini/Groq/Ollama are NOT called. The backtest
  tests the mechanical strategy only. This is an explicit design decision:
  LLM signals cannot be backtested over historical data because the news
  corpus and model behaviour would not match real-time conditions.
- **Bollinger Band mean reversion context** -- computed by `add_indicators()`
  but not used as an entry or exit condition in the backtest. Included in the
  indicator DataFrame for completeness; ignored by the Strategy subclass.
- **Human checkpoint** -- always auto-approved in backtest.
- **GTT reconciliation** -- not applicable in simulation.

---

## Known Simplifications

### Single-symbol-at-a-time limitation

The `backtesting.py` library operates on one symbol's OHLCV feed at a time.
It does not natively support multi-asset portfolio simulation. The runner
handles the multi-symbol universe as follows:

1. Each week (Monday rebalance), the full pipeline runs: quality filter on
   `fundamentals_df`, momentum ranking on quality-filtered symbols using
   `ohlcv_df`, regime filter using `nifty_ohlcv_df`.
2. The top-ranked candidate from the weekly pipeline is selected.
3. A separate `backtesting.py` `Backtest` instance runs on that symbol's
   OHLCV for the period until the next rebalance (or until stop/take-profit).
4. Equity carries forward across all per-symbol backtest segments.
5. Only one symbol is active at any time in the simulation (not 2 simultaneous
   positions). This underestimates real system capacity but is conservative.

This simplification means:
- The backtest tests the selection pipeline's quality, not portfolio-level
  diversification benefits.
- Trade count will be lower than real trading (one at a time vs two at a time).
- If the strategy passes backtest gates under this conservative constraint, it
  will perform at least as well with two positions in live trading.

Document this in the module docstring and in the `BacktestResult.raw_stats`
under key `"simplifications"`.

### Fundamentals are static

Historical fundamentals (ROE, D/E, EPS) are not available on a quarterly
rolling basis from jugaad-data. The `fundamentals_df` input is treated as a
single snapshot applied uniformly across the entire backtest period. This means
the quality filter will pass/fail the same stocks throughout. The caller
(or integration test) should ideally provide multiple fundamentals snapshots
for different periods, but the runner accepts a single DataFrame for simplicity.

Document this in the module docstring.

---

## Constants

```python
BACKTEST_START: str = "2010-01-01"
BACKTEST_END: str = "2023-12-31"
INITIAL_CASH: float = 10000.0
COMMISSION: float = 0.001          # 0.1% per side
RISK_PER_TRADE: float = 0.01       # 1% of equity
MAX_POSITION_PCT: float = 0.40     # 40% of equity max per position
MAX_OPEN_POSITIONS: int = 2        # max simultaneous (constrained to 1 by simplification)
WEEKLY_REBALANCE_DAY: int = 0      # Monday = 0 in Python weekday()
ATR_STOP_MULTIPLIER: float = 2.0   # 2x ATR for stop-loss
STOP_LOSS_HARD_CAP: float = 0.03   # 3% of entry price hard cap
MIN_RISK_REWARD: float = 2.0       # 1:2 minimum risk-reward ratio
RSI_ENTRY_THRESHOLD: float = 40.0  # RSI < 40 for entry signal
AGENT_NAME: str = "backtest_runner"
```

---

## Public API

### `class BacktestResult` (frozen dataclass)

```python
@dataclass(frozen=True)
class BacktestResult:
    """Frozen dataclass holding all backtest output metrics.

    Attributes:
        sharpe_ratio: Annualised Sharpe ratio from backtesting.py stats.
        max_drawdown: Maximum drawdown as positive fraction (0.12 = 12%).
        win_rate: Fraction of winning trades (0.55 = 55%).
        total_trades: Total number of completed round-trip trades.
        profit_factor: Gross profit / gross loss. Inf if no losses.
            0.0 if no trades.
        total_return: Total return as fraction (0.50 = 50%).
        annual_return: Annualised return as fraction.
        start_date: ISO 8601 date string of first bar.
        end_date: ISO 8601 date string of last bar.
        gates_passed: True if all 5 backtest gates met. Default False.
            Populated by backtest/validator.py, not by this module.
        raw_stats: Full backtesting.py stats dict for debugging.
            Includes key "simplifications" documenting known limitations.
    """
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    total_trades: int
    profit_factor: float
    total_return: float
    annual_return: float
    start_date: str
    end_date: str
    gates_passed: bool = False
    raw_stats: dict[str, object] = field(default_factory=dict)
```

### `run_backtest()`

```python
def run_backtest(
    ohlcv_df: pd.DataFrame,
    fundamentals_df: pd.DataFrame,
    nifty_ohlcv_df: pd.DataFrame,
    initial_cash: float = INITIAL_CASH,
    commission: float = COMMISSION,
) -> BacktestResult:
    """Run the full strategy backtest over historical data.

    Orchestrates the weekly rebalance loop:
    1. Each Monday (or first trading day of the week), runs quality filter,
       momentum ranking, and regime filter to select the top candidate.
    2. Computes technical indicators on the candidate's OHLCV.
    3. Runs backtesting.py Backtest on the candidate symbol's OHLCV with
       IndianTraderStrategy until next rebalance or position exit.
    4. Carries equity forward to the next segment.
    5. Aggregates all per-segment results into a single BacktestResult.

    Args:
        ohlcv_df: Full multi-symbol OHLCV for 2010--2023. Required columns:
            symbol, date, open, high, low, close, volume. Date must be
            timezone-aware (Asia/Kolkata).
        fundamentals_df: Pre-loaded fundamentals for all symbols. Required
            columns: symbol, roe, debt_to_equity, eps_positive_4q,
            data_quality. Single snapshot applied uniformly.
        nifty_ohlcv_df: Nifty 50 index OHLCV. Required columns: date, close.
            Must have >= 200 rows. No symbol column.
        initial_cash: Starting capital in INR. Default 10000.0.
        commission: Broker commission per trade as fraction. Default 0.001.

    Returns:
        BacktestResult frozen dataclass with all metrics.

    Raises:
        ValueError: If ohlcv_df has fewer than 252 rows for any symbol
            (insufficient for momentum calculation).
        ValueError: If nifty_ohlcv_df has fewer than 200 rows.
        ValueError: If fundamentals_df is empty.
        ValueError: If ohlcv_df is empty.
    """
```

**Implementation outline:**

1. Validate all three input DataFrames (columns, minimum rows).
2. Sort `nifty_ohlcv_df` by date ascending.
3. Identify all unique Monday dates in `ohlcv_df` date range.
4. For each Monday (rebalance week):
   a. Slice `ohlcv_df` to include only data up to this Monday (look-back only,
      no future data leakage).
   b. Slice `nifty_ohlcv_df` up to this Monday.
   c. Run `apply_quality_filter(fundamentals_df, ohlcv_sliced)`.
   d. If `thin_universe` in FilterReport, skip this week. Log as
      `thin_universe_week_skipped`.
   e. Run `compute_momentum(quality_df, ohlcv_sliced)`.
   f. If ranked_df is empty, skip this week.
   g. Run `apply_regime_filter(ranked_df, nifty_sliced)`.
   h. If `position_size_multiplier == 0.0` (BELOW_200DMA_10DAYS), skip. Log as
      `regime_blocked_week_skipped`.
   i. Take rank-1 symbol from filtered_df.
   j. Slice that symbol's OHLCV from Monday to next Monday (or end of data).
   k. Compute indicators via `add_indicators()` on the symbol's full history
      (not just the week slice -- indicators need lookback).
   l. Instantiate `Backtest(data=symbol_week_ohlcv, strategy=IndianTraderStrategy,
      cash=current_equity, commission=commission)`.
   m. Run backtest. Extract stats.
   n. Update `current_equity` from stats.
   o. Accumulate trade results.
5. After all weeks processed, compute aggregate metrics.
6. Build and return `BacktestResult`.

**Aggregate metric computation:**

- `sharpe_ratio`: Annualised Sharpe from the full equity curve. If
  backtesting.py provides it per-segment, compute from daily returns across
  all segments. Use 252 trading days as annualisation factor.
  Formula: `mean(daily_returns) / std(daily_returns) * sqrt(252)`.
  If std is 0 (no trades or flat equity), sharpe_ratio = 0.0.
- `max_drawdown`: Maximum peak-to-trough decline across the full equity curve.
  Expressed as positive fraction (0.12 = 12%). Computed from the running
  equity series, not per-segment.
- `win_rate`: `winning_trades / total_trades`. 0.0 if total_trades == 0.
- `total_trades`: Sum of trades across all segments.
- `profit_factor`: `sum(profits from winning trades) / abs(sum(losses from
  losing trades))`. 0.0 if no trades. `float('inf')` if no losing trades
  but at least one winning trade.
- `total_return`: `(final_equity - initial_cash) / initial_cash`.
- `annual_return`: Annualised from total_return using the actual number of
  calendar years in the backtest period.
  Formula: `(1 + total_return) ** (365.25 / total_days) - 1`.
- `start_date`: ISO 8601 string of the first date in `ohlcv_df`.
- `end_date`: ISO 8601 string of the last date in `ohlcv_df`.

### `load_backtest_data()`

```python
def load_backtest_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Load OHLCV data from local CSV cache for given symbols and date range.

    Reads from the CSV cache directory used by src/data/fetcher.py. Each
    symbol has a separate CSV file named {symbol}.csv in data/cache/.

    Args:
        symbols: List of NSE ticker symbols to load.
        start_date: Start date as ISO 8601 string (e.g. "2010-01-01").
        end_date: End date as ISO 8601 string (e.g. "2023-12-31").

    Returns:
        Multi-symbol DataFrame with columns: symbol, date, open, high, low,
        close, volume. Date is timezone-aware (Asia/Kolkata). Sorted by
        (symbol, date) ascending.

    Raises:
        FileNotFoundError: If CSV cache file is missing for any symbol.
            Message includes the missing symbol name and expected path.
    """
```

**Implementation outline:**

1. For each symbol in `symbols`:
   a. Build path: `{CACHE_DIR}/{symbol}.csv`.
   b. If file does not exist, raise `FileNotFoundError` with clear message.
   c. Read CSV with `pd.read_csv()`.
   d. Parse date column, localise to IST.
   e. Filter to `[start_date, end_date]` inclusive.
   f. Normalise column names to lowercase: symbol, date, open, high, low,
      close, volume.
2. Concatenate all symbol DataFrames.
3. Sort by (symbol, date) ascending.
4. Return.

### `class IndianTraderStrategy(Strategy)` (internal)

```python
class IndianTraderStrategy(Strategy):
    """backtesting.py Strategy subclass implementing the Indian Trader
    mechanical entry/exit rules.

    This class is internal to the module. It is instantiated by run_backtest()
    for each weekly segment. It receives pre-computed indicator data and
    regime state via class-level parameters (backtesting.py convention).

    Class parameters (set before Backtest instantiation):
        regime_multiplier: float -- position size multiplier from regime filter
            (1.0 or 0.5). Set by run_backtest() before each segment.
        regime_tighten_stops: bool -- whether to use 1x ATR instead of 2x ATR
            for stop-loss. Set by run_backtest() before each segment.
        risk_per_trade: float -- fraction of equity to risk. Default 0.01.
        max_position_pct: float -- max fraction of equity per position. Default 0.40.

    backtesting.py calls init() once at setup, then next() on every bar.
    """
```

**`init()` method:**

- Compute indicator overlays using `self.I()` wrapper (backtesting.py
  requirement for plotting and correct data alignment):
  - RSI (14-period)
  - MACD line, MACD signal line, MACD histogram
  - ATR (14-period)
- Store as instance attributes for use in `next()`.

**`next()` method (called on every bar):**

1. Skip if position already open (`self.position` is truthy).
2. Read current RSI value. If RSI >= `RSI_ENTRY_THRESHOLD` (40), skip.
3. Read MACD crossover: MACD line crosses above MACD signal line on this bar.
   Specifically: `macd[-1] > macd_signal[-1] and macd[-2] <= macd_signal[-2]`.
   If no crossover, skip.
4. Both RSI < 40 AND MACD crossover must be true to generate a BUY signal.
5. Calculate stop-loss:
   a. `atr_stop = current_close - (ATR_STOP_MULTIPLIER * current_atr)`.
   b. `pct_stop = current_close * (1 - STOP_LOSS_HARD_CAP)`.
   c. `stop_loss = max(atr_stop, pct_stop)` -- the higher of the two
      (tighter stop). The 3% cap means if ATR-based stop would be wider
      than 3%, use 3% instead.
   d. If `regime_tighten_stops` is True: use 1x ATR instead of 2x ATR.
      `atr_stop = current_close - (1.0 * current_atr)`.
6. Calculate take-profit:
   `take_profit = current_close + (MIN_RISK_REWARD * (current_close - stop_loss))`.
7. Calculate position size:
   a. `risk_amount = equity * risk_per_trade * regime_multiplier`.
   b. `stop_distance = current_close - stop_loss`.
   c. If `stop_distance <= 0`, skip (defensive).
   d. `raw_shares = risk_amount / stop_distance`.
   e. `max_shares = (equity * max_position_pct) / current_close`.
   f. `shares = int(min(raw_shares, max_shares))` -- round DOWN.
   g. If `shares < 1`, skip (cannot afford even 1 share).
8. Place order: `self.buy(size=shares, sl=stop_loss, tp=take_profit)`.

**Exit logic:**

backtesting.py handles stop-loss and take-profit exits automatically when
`sl=` and `tp=` are passed to `self.buy()`. No manual exit logic needed in
`next()` for these cases.

---

## Error Handling

| Error | Source | Behaviour |
|-------|--------|-----------|
| `FileNotFoundError` | `load_backtest_data()` | Raised with symbol name + expected path. Not caught internally. |
| `ValueError("fundamentals_df is empty")` | `run_backtest()` | Raised immediately. |
| `ValueError("ohlcv_df is empty")` | `run_backtest()` | Raised immediately. |
| `ValueError("nifty_ohlcv_df has < 200 rows")` | `run_backtest()` | Raised immediately. |
| `ValueError` from `apply_quality_filter` | Weekly loop | Logged and week skipped. Should not happen if inputs are valid. |
| `ValueError` from `compute_momentum` | Weekly loop | Logged and week skipped. Can happen if all symbols have < 252 rows early in the period. |
| `ValueError` from `apply_regime_filter` | Weekly loop | Logged and week skipped. Should not happen if nifty has >= 200 rows. |
| Exceptions from `backtesting.py` | Per-segment run | Propagated. Not swallowed. |

All errors logged via `log_agent_action()` with `agent_name="backtest_runner"`.

---

## Logging

Every significant event is logged via `log_agent_action()`:

- `backtest_started` -- with date range, initial cash, number of symbols
- `weekly_rebalance` -- selected symbol, rank, regime, multiplier
- `thin_universe_week_skipped` -- week date, passed count
- `regime_blocked_week_skipped` -- week date, regime state, consecutive days below
- `no_candidates_week_skipped` -- week date, reason
- `segment_completed` -- symbol, trades in segment, equity after segment
- `backtest_completed` -- total trades, sharpe, max drawdown, win rate, profit factor

---

## Data Flow

```
Caller provides:
  ohlcv_df (multi-symbol, 2010-2023)
  fundamentals_df (single snapshot)
  nifty_ohlcv_df (Nifty 50 index, date+close)
      |
      v
run_backtest()
      |
      +-- For each Monday rebalance week:
      |     |
      |     +-- apply_quality_filter(fundamentals_df, ohlcv_sliced)
      |     +-- compute_momentum(quality_df, ohlcv_sliced)
      |     +-- apply_regime_filter(ranked_df, nifty_sliced)
      |     +-- add_indicators(symbol_ohlcv)
      |     +-- Backtest(data, IndianTraderStrategy, cash, commission).run()
      |     +-- Update equity carry-forward
      |
      v
BacktestResult (frozen dataclass)
      |
      v
backtest/validator.py checks 5 gates (Phase 2 step 6)
```

---

## OHLCV DataFrame Format for backtesting.py

The `backtesting.py` library requires a specific DataFrame format:
- Index: DatetimeIndex (the date column)
- Columns: Open, High, Low, Close, Volume (capitalised)
- Single symbol only

The `_prepare_bt_dataframe()` private helper converts from the project's
normalised format (lowercase, symbol column, tz-aware) to backtesting.py
format:

```python
def _prepare_bt_dataframe(symbol_ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Convert single-symbol OHLCV to backtesting.py format.

    Args:
        symbol_ohlcv: Single-symbol DataFrame with columns:
            symbol, date, open, high, low, close, volume.

    Returns:
        DataFrame with DatetimeIndex and capitalised OHLC+V columns.
        Timezone information is stripped (backtesting.py uses naive dates).
    """
```

---

## File Structure

```
src/backtest/
    __init__.py    -- empty, exists for package import
    runner.py      -- this module
    validator.py   -- Phase 2 step 6 (not built yet)
```

Ensure `src/backtest/__init__.py` is created if it does not exist.

---

## Test Hints (minimum 12 tests)

Tests go in `tests/backtest/test_runner.py`. All tests use synthetic data
(small DataFrames with known values), not real market data.

1. `test_backtest_result_is_frozen` -- BacktestResult raises FrozenInstanceError
   on attribute mutation.
2. `test_backtest_result_has_all_fields` -- All 11 fields present and typed.
3. `test_run_backtest_returns_backtest_result` -- Return type is BacktestResult.
4. `test_sharpe_ratio_is_float` -- `isinstance(result.sharpe_ratio, float)`.
5. `test_max_drawdown_between_0_and_1` -- `0.0 <= result.max_drawdown <= 1.0`.
6. `test_win_rate_between_0_and_1` -- `0.0 <= result.win_rate <= 1.0`.
7. `test_total_trades_non_negative` -- `result.total_trades >= 0`.
8. `test_profit_factor_non_negative` -- `result.profit_factor >= 0.0`.
9. `test_total_return_is_float` -- `isinstance(result.total_return, float)`.
10. `test_load_backtest_data_file_not_found` -- `FileNotFoundError` raised when
    cache CSV missing for a symbol.
11. `test_run_backtest_empty_fundamentals_raises` -- `ValueError` on empty
    fundamentals_df.
12. `test_run_backtest_empty_ohlcv_raises` -- `ValueError` on empty ohlcv_df.
13. `test_run_backtest_insufficient_nifty_rows_raises` -- `ValueError` when
    nifty_ohlcv_df has < 200 rows.
14. `test_no_llm_calls_during_backtest` -- Mock Gemini/Groq/Ollama imports and
    assert they are never called.
15. `test_raw_stats_is_dict` -- `isinstance(result.raw_stats, dict)`.
16. `test_gates_passed_default_false` -- `result.gates_passed is False` (set by
    validator, not runner).
17. `test_load_backtest_data_returns_correct_columns` -- Verify output has all
    7 required columns.
18. `test_prepare_bt_dataframe_capitalised_columns` -- Output has Open, High,
    Low, Close, Volume columns with DatetimeIndex.

---

## Backtest Gate Thresholds (for reference -- enforced by validator.py)

| Gate | Threshold |
|------|-----------|
| Sharpe ratio | > 1.0 |
| Maximum drawdown | < 15% (0.15) |
| Win rate | > 40% (0.40) |
| Minimum trades | >= 100 |
| Profit factor | > 1.3 |

These are NOT enforced by runner.py. The runner computes the metrics;
`backtest/validator.py` checks the gates and sets `gates_passed`.

---

## Regime Filter Validation Period

Per phases.md: "Validate the regime filter specifically over 2013--2014
(extended sideways market) to confirm the 200 DMA filter does not cause
excessive whipsawing during flat periods."

This validation is done by the caller (or integration test), not by runner.py
itself. The caller can run `run_backtest()` with date-filtered OHLCV for
2013--2014 and inspect the number of regime changes in `raw_stats`. The runner
should include in `raw_stats` a key `"regime_changes"` (int) counting how many
times the regime switched during the backtest period, and a key
`"regime_blocked_weeks"` (int) counting weeks skipped due to
BELOW_200DMA_10DAYS.
