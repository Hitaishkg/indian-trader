# Spec: src/strategy/quality_filter.py

**Date**: 2026-03-24
**Author**: Architect Agent (Opus)
**Phase**: 2 — Strategy Core (step 2 of 6)
**Status**: Awaiting approval

---

## 1. Purpose

Implements the five hard quality filters that every Nifty 50 stock must pass before
entering the momentum ranking pipeline. This is the first gate in the three-step
stock selection process (quality filter -> momentum rank -> regime filter). Stocks
that fail any single hard filter are eliminated. A soft tiebreaker score (52-week
high proximity) is computed but never used to eliminate stocks.

---

## 2. Dependencies

| Module | Import | What is used |
|--------|--------|-------------|
| `src/data/fundamentals.py` | `fetch_fundamentals` | Input DataFrame: one row per symbol with columns `symbol`, `roe`, `debt_to_equity`, `eps_positive_4q`, `pe_ratio`, `data_source`, `data_quality`, `cache_age_days`, `fetched_at_ist` |
| `src/data/fetcher.py` | `fetch_ohlcv` | Input DataFrame: columns `symbol`, `date`, `open`, `high`, `low`, `close`, `volume` |
| `src/data/cleaner.py` | `clean_ohlcv` | Optional — caller may pre-clean OHLCV before passing in |
| `src/utils/logger.py` | `log_agent_action` | Structured logging to `agent_logs` table |
| `src/config/settings.py` | `settings` | For `log_level` only |

This module does NOT import fetcher or fundamentals directly. It receives their
output DataFrames as arguments. The caller (main.py, screener_agent.py, or
backtest/runner.py) is responsible for fetching and cleaning data before calling
`apply_quality_filter()`.

---

## 3. Public API

### 3.1 `apply_quality_filter(fundamentals_df, ohlcv_df, lookback_days=252) -> tuple[pd.DataFrame, FilterReport]`

```python
def apply_quality_filter(
    fundamentals_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    lookback_days: int = 252,
) -> tuple[pd.DataFrame, FilterReport]:
    """Apply all five hard quality filters to the stock universe.

    Filters:
    1. ROE > 15% (0.15 as decimal)
    2. Debt-to-equity < 1.0
    3. EPS positive for last 4 consecutive quarters (eps_positive_4q == True)
    4. Average daily traded value > 20,00,00,000 INR (20 crore)
    5. Latest close price > 50 INR

    Also computes the soft tiebreaker: percentage distance from 52-week high.

    Args:
        fundamentals_df: Output of fetch_fundamentals(). One row per symbol.
            Required columns: symbol, roe, debt_to_equity, eps_positive_4q.
        ohlcv_df: Output of fetch_ohlcv() or clean_ohlcv(). Multi-symbol OHLCV.
            Required columns: symbol, date, close, volume.
        lookback_days: Number of trading days to use for volume averaging and
            52-week high calculation. Default 252 (one trading year).

    Returns:
        Tuple of (filtered_df, report).
        filtered_df: DataFrame with one row per symbol that passed ALL hard
            filters. Empty DataFrame (same schema, zero rows) if fewer than 3
            stocks pass. Columns: symbol, roe, debt_to_equity, avg_daily_value,
            latest_price, high_52w, pct_from_52w_high, within_30pct_of_52w_high,
            passed_hard_filters.
        report: FilterReport frozen dataclass with filtering metadata.

    Raises:
        ValueError: If fundamentals_df or ohlcv_df is empty.
        ValueError: If required columns are missing from either DataFrame.
    """
```

### 3.2 `class FilterReport`

```python
@dataclass(frozen=True)
class FilterReport:
    """Summary of quality filtering results.

    Attributes:
        universe_size: Total number of symbols evaluated.
        passed_count: Number of symbols that passed all 5 hard filters.
        failed_count: Number of symbols that failed at least one hard filter.
        thin_universe: True if passed_count < 3 (minimum universe rule).
        filter_failure_counts: Dict mapping filter name to number of stocks
            that failed it. Keys: "roe", "debt_to_equity", "eps", "volume",
            "price". A stock may appear in multiple failure counts.
        filtered_at_ist: ISO 8601 IST timestamp when filtering was performed.
    """
    universe_size: int
    passed_count: int
    failed_count: int
    thin_universe: bool
    filter_failure_counts: dict[str, int]
    filtered_at_ist: str
```

### 3.3 Module-level constants

```python
ROE_THRESHOLD: float = 0.15                    # 15% as decimal
DEBT_TO_EQUITY_THRESHOLD: float = 1.0
VOLUME_THRESHOLD_INR: float = 20_00_00_000.0   # 20 crore INR
PRICE_THRESHOLD_INR: float = 50.0
HIGH_52W_PROXIMITY_PCT: float = 0.30           # 30% — soft tiebreaker only
DEFAULT_LOOKBACK_DAYS: int = 252
MIN_UNIVERSE_SIZE: int = 3
AGENT_NAME: str = "quality_filter"
```

---

## 4. Inputs — Column Contracts

### 4.1 fundamentals_df (from `fetch_fundamentals()`)

| Column | Type | Notes |
|--------|------|-------|
| symbol | str | NSE ticker, e.g. "RELIANCE" |
| roe | float64 | Decimal form (0.20 = 20%). NaN if unavailable. |
| debt_to_equity | float64 | Ratio. NaN if unavailable. |
| eps_positive_4q | bool | True if last 4 quarters all had positive EPS |
| data_source | str | "screener", "yfinance_fallback", "failed" |
| data_quality | str | "clean", "degraded", "stale_data", "fundamentals_stale", "failed" |

**Important**: Rows with `data_quality == "fundamentals_stale"` or
`data_quality == "failed"` must be treated as failing ALL hard filters. The
rules state: "Do not trade on fundamentals that are 45+ days old" and stale/failed
data is unreliable. Log the reason as `fundamentals_stale` or `fundamentals_failed`.

### 4.2 ohlcv_df (from `fetch_ohlcv()` / `clean_ohlcv()`)

| Column | Type | Notes |
|--------|------|-------|
| symbol | str | NSE ticker |
| date | datetime64[ns, Asia/Kolkata] | Trading date |
| close | float64 | Closing price in INR |
| volume | float64 | Volume (number of shares traded) |

Only the `close` and `volume` columns are needed from OHLCV. `open`, `high`,
`low` are not used by this module.

---

## 5. Output DataFrame Schema

One row per symbol that passed ALL five hard filters. If `thin_universe` is
True (fewer than 3 passed), return an empty DataFrame with the same column
schema and zero rows.

| Column | Type | Derivation |
|--------|------|-----------|
| symbol | str | From fundamentals_df |
| roe | float64 | From fundamentals_df, raw value |
| debt_to_equity | float64 | From fundamentals_df, raw value |
| avg_daily_value | float64 | mean(close * volume) over lookback window, per symbol |
| latest_price | float64 | Most recent close in OHLCV for that symbol |
| high_52w | float64 | max(close) over last `lookback_days` trading days |
| pct_from_52w_high | float64 | (high_52w - latest_price) / high_52w; range [0.0, 1.0] |
| within_30pct_of_52w_high | bool | True if pct_from_52w_high <= 0.30 |
| passed_hard_filters | bool | Always True (failed rows excluded from output) |

Sorted by symbol ascending. RangeIndex.

---

## 6. Algorithm — Step by Step

### 6.1 Input validation

1. Raise `ValueError` if `fundamentals_df` is empty or missing required columns
   (`symbol`, `roe`, `debt_to_equity`, `eps_positive_4q`).
2. Raise `ValueError` if `ohlcv_df` is empty or missing required columns
   (`symbol`, `date`, `close`, `volume`).

### 6.2 Pre-compute OHLCV metrics per symbol

For each unique symbol in `fundamentals_df`:

1. Extract that symbol's OHLCV rows from `ohlcv_df`.
2. Sort by date descending. Take the most recent `lookback_days` rows.
3. Compute `avg_daily_value = mean(close * volume)` over those rows.
4. Compute `latest_price = close` of the most recent row.
5. Compute `high_52w = max(close)` over those rows.
6. Compute `pct_from_52w_high = (high_52w - latest_price) / high_52w`.
7. If `high_52w == 0` (should never happen with real data), set
   `pct_from_52w_high = 1.0` to avoid division by zero.
8. Compute `within_30pct_of_52w_high = (pct_from_52w_high <= 0.30)`.

If a symbol from `fundamentals_df` has NO rows in `ohlcv_df`:
- Set `avg_daily_value = 0.0`, `latest_price = 0.0`, `high_52w = 0.0`,
  `pct_from_52w_high = 1.0`, `within_30pct_of_52w_high = False`.
- Log as `ohlcv_missing` for that symbol.

### 6.3 Apply hard filters per symbol

For each symbol, evaluate ALL five filters (do not short-circuit). Track
which filters each symbol failed for the FilterReport.

| # | Filter | Condition to PASS | Failure key |
|---|--------|-------------------|-------------|
| 1 | ROE | `roe > 0.15` and `roe` is not NaN | `"roe"` |
| 2 | Debt-to-equity | `debt_to_equity < 1.0` and `debt_to_equity` is not NaN | `"debt_to_equity"` |
| 3 | EPS | `eps_positive_4q == True` | `"eps"` |
| 4 | Volume | `avg_daily_value > 20_00_00_000.0` | `"volume"` |
| 5 | Price | `latest_price > 50.0` | `"price"` |

Additional failure conditions (evaluated BEFORE the five filters):
- If `data_quality` is `"fundamentals_stale"` -> fail all filters, log reason
  `"fundamentals_stale"`, failure key `"fundamentals_stale"`.
- If `data_quality` is `"failed"` -> fail all filters, log reason
  `"fundamentals_failed"`, failure key `"fundamentals_failed"`.
- If `roe` is NaN -> fails ROE filter. Log as `"roe_data_missing"`.
- If `debt_to_equity` is NaN -> fails D/E filter. Log as `"de_data_missing"`.
- If `eps_positive_4q` column is missing for a row or NaN -> fails EPS filter.
  Log as `"eps_data_missing"`.

A symbol passes ONLY if it passes ALL five hard filters AND does not have
stale/failed fundamentals.

### 6.4 Apply minimum universe rule

If `passed_count < 3`:
- Set `thin_universe = True` in FilterReport.
- Log via `log_agent_action()`: agent_name="quality_filter",
  action="thin_universe: only {N} stocks passed, minimum is 3, returning empty",
  level="WARNING", result="thin_universe".
- Return an empty DataFrame (with correct column schema) and the FilterReport.

### 6.5 Build output

- Construct output DataFrame from symbols that passed all hard filters.
- Include the computed OHLCV metrics columns.
- Sort by symbol ascending, reset index.

### 6.6 Logging

For every symbol evaluated, log via `log_agent_action()`:
- **Passed**: agent_name="quality_filter", action="passed all hard filters",
  symbol=symbol, result="passed"
- **Failed**: agent_name="quality_filter",
  action="failed filters: {comma-separated list of failure keys}",
  symbol=symbol, result="failed"

After all symbols processed, log summary:
- agent_name="quality_filter",
  action="filter complete: {passed_count}/{universe_size} passed, thin_universe={thin_universe}",
  result="ok" or "thin_universe"

---

## 7. Error Handling

| Scenario | Behaviour |
|----------|----------|
| `fundamentals_df` is empty | Raise `ValueError("fundamentals_df must not be empty")` |
| `ohlcv_df` is empty | Raise `ValueError("ohlcv_df must not be empty")` |
| Required column missing from fundamentals_df | Raise `ValueError("fundamentals_df missing required columns: {list}")` |
| Required column missing from ohlcv_df | Raise `ValueError("ohlcv_df missing required columns: {list}")` |
| Symbol in fundamentals has no OHLCV data | Fails volume and price filters, logged as `ohlcv_missing` |
| ROE is NaN | Fails ROE filter, logged as `roe_data_missing` |
| D/E is NaN | Fails D/E filter, logged as `de_data_missing` |
| eps_positive_4q is NaN or missing | Fails EPS filter, logged as `eps_data_missing` |
| data_quality is "fundamentals_stale" | Fails all filters, logged as `fundamentals_stale` |
| data_quality is "failed" | Fails all filters, logged as `fundamentals_failed` |
| Fewer than 3 stocks pass | Return empty DataFrame + FilterReport(thin_universe=True) |
| log_agent_action() fails internally | Never raises (logger design) — no handling needed |

No bare `except` clauses. Only `ValueError` is raised by this module.

---

## 8. Database Interactions

This module does NOT read from or write to any database table directly. All
database interaction happens through `log_agent_action()`, which writes to the
`agent_logs` table.

| Table | Read/Write | Via |
|-------|-----------|-----|
| agent_logs | Write | `log_agent_action()` from `src/utils/logger.py` |

---

## 9. File Structure

```
src/strategy/__init__.py          # empty, makes strategy a package
src/strategy/quality_filter.py    # this module
tests/strategy/__init__.py        # empty
tests/strategy/test_quality_filter.py
```

---

## 10. Integration Points

### Upstream callers
- `main.py` — Phase 1 dry-run pipeline (already built, will be updated to call this)
- `src/agents/screener_agent.py` — Phase 3/4 Screener Agent (not yet built)
- `src/backtest/runner.py` — Phase 2 backtesting (not yet built)

### Downstream consumers
- `src/strategy/momentum.py` — receives the filtered DataFrame and computes 12-1
  momentum rank on it. Uses `within_30pct_of_52w_high` as the tiebreaker when
  two stocks score within 2% of each other.

### Data flow
```
fetch_fundamentals() -> fundamentals_df \
                                         -> apply_quality_filter() -> filtered_df -> momentum.py
fetch_ohlcv() -> clean_ohlcv() -> ohlcv_df /
```

---

## 11. Test Hints (minimum 12)

### Happy path
1. **All 5 filters pass**: 5 stocks all passing -> output has 5 rows, all `passed_hard_filters=True`.
2. **Exactly 3 pass (minimum universe met)**: 3 of 5 pass -> output has 3 rows, `thin_universe=False`.
3. **52-week high proximity computed correctly**: stock at 900, 52w high at 1000 -> `pct_from_52w_high=0.10`, `within_30pct_of_52w_high=True`.
4. **Soft tiebreaker flag**: stock 40% below 52w high -> `within_30pct_of_52w_high=False`.

### Filter failures (one per filter)
5. **ROE too low**: stock with ROE=0.10 -> excluded, failure_counts["roe"]=1.
6. **D/E too high**: stock with D/E=1.5 -> excluded, failure_counts["debt_to_equity"]=1.
7. **EPS negative**: stock with `eps_positive_4q=False` -> excluded, failure_counts["eps"]=1.
8. **Volume too low**: stock with avg_daily_value=15 crore -> excluded, failure_counts["volume"]=1.
9. **Price too low**: stock with latest_price=30 -> excluded, failure_counts["price"]=1.

### Edge cases
10. **Thin universe**: only 2 of 50 pass -> output is empty DataFrame, `thin_universe=True`, log contains "thin_universe".
11. **Zero stocks pass**: none pass -> output is empty, `thin_universe=True`.
12. **NaN ROE**: stock with NaN ROE -> fails ROE filter, logged as `roe_data_missing`.
13. **NaN D/E**: stock with NaN D/E -> fails D/E filter, logged as `de_data_missing`.
14. **Missing OHLCV**: symbol in fundamentals but not in ohlcv_df -> fails volume and price, logged as `ohlcv_missing`.
15. **Stale fundamentals**: `data_quality="fundamentals_stale"` -> fails all filters, logged as `fundamentals_stale`.
16. **Failed fundamentals**: `data_quality="failed"` -> fails all filters, logged as `fundamentals_failed`.

### Input validation
17. **Empty fundamentals_df**: raises `ValueError`.
18. **Empty ohlcv_df**: raises `ValueError`.
19. **Missing required column in fundamentals_df**: raises `ValueError` naming the missing column(s).
20. **Missing required column in ohlcv_df**: raises `ValueError` naming the missing column(s).

### FilterReport
21. **FilterReport is frozen**: attempting to mutate any attribute raises `FrozenInstanceError`.
22. **filter_failure_counts sums correctly**: stock failing ROE and volume -> appears in both counts.
23. **filtered_at_ist is valid IST timestamp**: parseable as ISO 8601 with +05:30 offset.

### Volume calculation
24. **Lookback window respected**: with lookback_days=20 and 252 rows of data, only the most recent 20 rows are used for avg_daily_value and high_52w.
25. **Volume threshold boundary**: stock with avg_daily_value=20_00_00_000.0 exactly -> fails (threshold is strictly greater than).

---

## 12. Non-Goals

- This module does NOT compute momentum rank (that is `momentum.py`).
- This module does NOT check regime filter (that is `regime.py`).
- This module does NOT fetch data — it receives DataFrames as arguments.
- This module does NOT write to any database table other than `agent_logs` via logger.
- This module does NOT send notifications.
- The 52-week high proximity is computed but NEVER used to eliminate stocks.
  It is a soft flag passed downstream for tiebreaking in `momentum.py` only.
