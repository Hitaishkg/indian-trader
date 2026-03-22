# Spec: src/data/validator.py

**Date:** 2026-03-22
**Phase:** 1 — Foundation
**Build order position:** 1 of 9 (gating dependency — nothing else in Phase 1 may be built until this module passes all tests)
**Author:** Architect Agent

---

## 1. Purpose

`src/data/validator.py` is the data quality gate for the entire trading system. It runs on real, live NSE data (not mocks) before any strategy logic executes. Its job is to detect data corruption, coverage gaps, and time-series holes that would silently corrupt momentum calculations, quality filter decisions, and position sizing.

Every trade decision logged to `agent_logs` must carry a `data_quality_score`. This module produces those scores. If the universe-wide score drops below 0.6, it raises `DataQualityError` and the pipeline halts — no trading proceeds on bad data.

This module has zero knowledge of strategy logic. It only validates data shape and content.

---

## 2. File and Directory Structure

The following paths are created or referenced by this module. The Coder Agent must create all missing directories.

```
src/
  __init__.py                  (empty)
  data/
    __init__.py                (empty)
    validator.py               (this module)
data/
  trading.db                   (SQLite — created on first run if absent)
docs/
  specs/
    2026-03-22-validator.md    (this file)
```

No other files are created by this module.

---

## 3. Dependencies to Add to pyproject.toml

Add all of the following to the `[project] dependencies` list in `pyproject.toml`. The Coder Agent must not add any dependency not listed here.

```toml
dependencies = [
    "pandas>=2.2.0",
    "yfinance>=0.2.40",
    "jugaad-data>=0.26",
    "numpy>=1.26.0",
]
```

Notes:
- `zoneinfo` is part of the Python 3.12 standard library — no separate install needed.
- `sqlite3` is part of the Python 3.12 standard library — no separate install needed.
- `dataclasses` is part of the Python 3.12 standard library — no separate install needed.
- Do not pin exact versions. Use `>=` lower bounds only.

---

## 4. SQLite Schema — agent_logs Table

This table is shared across all agents. The validator creates it if it does not exist; it never drops or alters it if it already exists.

```sql
CREATE TABLE IF NOT EXISTS agent_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name      TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    symbol          TEXT,
    detail          TEXT,
    data_quality_score REAL,
    timestamp_ist   TEXT    NOT NULL
);
```

Column definitions:

| Column | Type | Nullable | Description |
|---|---|---|---|
| id | INTEGER | NO | Auto-incrementing primary key |
| agent_name | TEXT | NO | Always `"validator"` for this module |
| event_type | TEXT | NO | One of: `roe_check`, `de_coverage_check`, `ohlcv_gap_check`, `stock_score`, `universe_score`, `data_coverage_low`, `data_quality_error` |
| symbol | TEXT | YES | NSE ticker symbol (e.g. `"RELIANCE"`). NULL for universe-level events |
| detail | TEXT | YES | Human-readable description of the check result. JSON-serialised dict for structured data |
| data_quality_score | REAL | YES | Float 0.0–1.0. NULL for events that do not produce a score |
| timestamp_ist | TEXT | NO | ISO 8601 datetime string in IST (Asia/Kolkata), e.g. `"2026-03-22T22:05:31+05:30"` |

The validator applies these SQLite pragmas at every connection open, before any read or write:

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA cache_size=-64000;
PRAGMA synchronous=NORMAL;
```

---

## 5. DataFrame Input Contract

The validator accepts a standard internal DataFrame format. Both yfinance and jugaad-data outputs must be normalised into this format by the caller before passing to the validator. The validator does NOT perform source-specific parsing — it only validates the normalised format.

### 5.1 OHLCV DataFrame

One row per trading day per symbol. Used for the OHLCV gap check.

Required columns (exact names, case-sensitive):

| Column | dtype | Description |
|---|---|---|
| `symbol` | `str` | NSE ticker symbol, uppercase, e.g. `"RELIANCE"` |
| `date` | `datetime64[ns, Asia/Kolkata]` | Trading date, timezone-aware, time component is midnight IST |
| `open` | `float64` | Open price in INR |
| `high` | `float64` | High price in INR |
| `low` | `float64` | Low price in INR |
| `close` | `float64` | Close price in INR |
| `volume` | `float64` | Volume as float (may come as float from some sources — do not cast to int inside validator) |

Validation rejects the DataFrame (raises `ValueError` before any checks run) if any required column is missing, or if `date` is not timezone-aware.

### 5.2 Fundamentals DataFrame

One row per symbol. Used for ROE check and D/E coverage check.

Required columns (exact names, case-sensitive):

| Column | dtype | Nullable | Description |
|---|---|---|---|
| `symbol` | `str` | NO | NSE ticker symbol, uppercase |
| `roe` | `float64` | YES | Return on Equity as a decimal (e.g. `0.18` = 18%). NULL if unavailable |
| `debt_to_equity` | `float64` | YES | Debt-to-equity ratio as a decimal (e.g. `0.5`). NULL if unavailable |

No other columns are required. Extra columns are silently ignored.

### 5.3 yfinance vs jugaad-data Normalisation Notes

These notes are for the Coder Agent who will later build `src/data/fetcher.py`. They are recorded here so the DataFrame contract is unambiguous.

**yfinance** returns OHLCV with a DatetimeIndex (no `symbol` column when fetching a single ticker). The fetcher must:
- Reset the index to get a `date` column
- Add a `symbol` column
- Localise the index to `Asia/Kolkata` if it is timezone-naive

**jugaad-data** returns columns named `CH_SYMBOL`, `CH_TIMESTAMP`, `CH_OPENING_PRICE`, `CH_TRADE_HIGH_PRICE`, `CH_TRADE_LOW_PRICE`, `CH_CLOSING_PRICE`, `CH_TOT_TRADED_QTY`. The fetcher must rename these to the standard column names above.

The validator itself never handles either of these raw formats. If a non-standard DataFrame is passed, the validator raises `ValueError` immediately.

---

## 6. Public API

### 6.1 DataQualityReport Dataclass

```python
@dataclass
class DataQualityReport:
    """
    Result of a full data quality validation run.

    Attributes:
        per_stock_scores: Mapping from NSE symbol to data_quality_score (0.0–1.0).
        universe_quality_score: Weighted aggregate score across all checked stocks (0.0–1.0).
        failed_roe_symbols: List of symbols where ROE was outside [-0.50, 2.00].
        roe_missing_symbols: List of symbols where ROE was NULL/NaN.
        de_coverage_ratio: Fraction of universe with non-null debt_to_equity (0.0–1.0).
        de_coverage_low: True if de_coverage_ratio < 0.80.
        gap_violations: Mapping from symbol to list of (gap_start_date, gap_length_days) tuples
                        for every OHLCV gap longer than 5 consecutive trading days.
        checked_at_ist: ISO 8601 timestamp (IST) when this report was generated.
        universe_size: Total number of symbols passed into the validator.
    """
    per_stock_scores: dict[str, float]
    universe_quality_score: float
    failed_roe_symbols: list[str]
    roe_missing_symbols: list[str]
    de_coverage_ratio: float
    de_coverage_low: bool
    gap_violations: dict[str, list[tuple[str, int]]]
    checked_at_ist: str
    universe_size: int
```

All fields are required at construction — no defaults. The dataclass is frozen (`frozen=True`) to prevent mutation after construction.

The `gap_violations` dict maps symbol to a list of tuples. Each tuple is `(gap_start_date, gap_length_days)` where `gap_start_date` is an ISO 8601 date string (`"YYYY-MM-DD"`) and `gap_length_days` is an int.

### 6.2 DataQualityError Exception

```python
class DataQualityError(Exception):
    """
    Raised when universe_quality_score drops below 0.6.

    Attributes:
        universe_quality_score: The score that triggered this error.
        report: The full DataQualityReport for inspection by the caller.
    """
    def __init__(self, universe_quality_score: float, report: DataQualityReport) -> None: ...
    universe_quality_score: float
    report: DataQualityReport
```

### 6.3 validate_data (main public function)

```python
def validate_data(
    ohlcv_df: pd.DataFrame,
    fundamentals_df: pd.DataFrame,
    db_path: str,
    trading_calendar: list[datetime.date] | None = None,
) -> DataQualityReport:
    """
    Run all three data quality checks on the provided DataFrames and return a report.

    Validates ROE plausibility, debt-to-equity coverage, and OHLCV continuity
    for every symbol present in fundamentals_df. Logs every check result to the
    agent_logs table in the SQLite database at db_path. Raises DataQualityError
    if universe_quality_score < 0.6.

    Args:
        ohlcv_df: Normalised OHLCV DataFrame conforming to the contract in Section 5.1.
                  Must contain data for all symbols in fundamentals_df. Symbols present
                  in fundamentals_df but absent from ohlcv_df receive a gap_score of 0.0.
        fundamentals_df: Normalised fundamentals DataFrame conforming to Section 5.2.
                         Defines the universe. Every symbol in this DataFrame is checked.
        db_path: Absolute path to the SQLite database file. Created if it does not exist.
        trading_calendar: Optional explicit list of trading dates (date objects, IST) to use
                          when computing OHLCV gaps. When None, gaps are detected by comparing
                          the actual date sequence against pandas business-day frequency
                          (BDay), which approximates NSE trading days. The caller should
                          pass an actual NSE calendar when available for correctness.

    Returns:
        DataQualityReport populated with results from all checks.

    Raises:
        ValueError: If ohlcv_df or fundamentals_df are missing required columns,
                    or if ohlcv_df dates are not timezone-aware.
        DataQualityError: If universe_quality_score < 0.6 after all checks complete.
        sqlite3.OperationalError: If the database cannot be opened or written to.
    """
```

### 6.4 check_roe (internal, called by validate_data)

Not part of the public API. Documented here so the Coder Agent implements it correctly.

```python
def _check_roe(
    symbol: str,
    roe: float | None,
) -> tuple[bool, str]:
    """
    Validate a single stock's ROE value.

    Returns (passed: bool, detail: str).
    passed is True if roe is not None/NaN AND -0.50 <= roe <= 2.00.
    detail is a human-readable string explaining the result.
    """
```

### 6.5 check_ohlcv_gaps (internal, called by validate_data)

```python
def _check_ohlcv_gaps(
    symbol: str,
    dates: pd.Series,
    trading_calendar: list[datetime.date] | None,
) -> list[tuple[str, int]]:
    """
    Detect gaps longer than 5 consecutive trading days in a sorted date series.

    Args:
        symbol: NSE ticker symbol (used only for logging context).
        dates: Sorted Series of datetime64 values for this symbol's OHLCV rows.
        trading_calendar: See validate_data docstring.

    Returns:
        List of (gap_start_date_str, gap_length_days) tuples.
        Returns empty list if no gaps exceed 5 consecutive trading days.
    """
```

### 6.6 compute_stock_score (internal, called by validate_data)

```python
def _compute_stock_score(
    roe_passed: bool,
    roe_missing: bool,
    gap_violations: list[tuple[str, int]],
) -> float:
    """
    Compute data_quality_score for a single stock.

    See Section 7 for the exact scoring formula.

    Returns a float in [0.0, 1.0].
    """
```

---

## 7. Scoring Logic

### 7.1 Per-Stock data_quality_score

Each stock receives a score from 0.0 to 1.0 built from three sub-components. Each sub-component is binary (1.0 or 0.0) except for the gap penalty which is graduated.

| Sub-component | Weight | Pass condition | Score if fail |
|---|---|---|---|
| ROE plausibility | 0.40 | ROE is not null AND -0.50 <= roe <= 2.00 | 0.0 |
| ROE present | 0.10 | ROE is not null/NaN | 0.0 |
| OHLCV gap | 0.50 | Zero gaps longer than 5 consecutive trading days | Graduated — see below |

ROE sub-components combine: if ROE is present and in range, both the 0.40 and 0.10 components score 1.0 (total 0.50 from ROE). If ROE is present but out of range, only the 0.10 component scores 1.0 (total 0.10 from ROE). If ROE is null, both score 0.0 (total 0.0 from ROE).

OHLCV gap sub-component scoring:
- 0 gaps: 1.0 (full 0.50 weight)
- 1 gap: 0.5 (half weight = 0.25 contribution)
- 2 or more gaps: 0.0 (zero contribution)

These weights multiply into the final score:

```
data_quality_score = (roe_plausibility_score * 0.40)
                   + (roe_present_score * 0.10)
                   + (gap_score * 0.50)
```

Result is clamped to [0.0, 1.0] after calculation (floating point safety).

### 7.2 D/E Coverage Check

The D/E check is universe-level, not per-stock. It does not contribute to individual `data_quality_score`.

```
de_coverage_ratio = count(symbols where debt_to_equity IS NOT NULL AND IS NOT NaN)
                    / count(total symbols in fundamentals_df)
```

If `de_coverage_ratio < 0.80`:
- Set `DataQualityReport.de_coverage_low = True`
- Log one row to `agent_logs` with `event_type = "data_coverage_low"`, `symbol = NULL`,
  `detail = '{"de_coverage_ratio": <value>, "missing_count": <n>, "total_count": <n>}'`
- Subtract 0.10 from every per-stock score before aggregation into universe_quality_score.
  Per-stock scores are clamped to 0.0 minimum after this deduction.
  Individual `per_stock_scores` in the report reflect the deducted values.

### 7.3 universe_quality_score

Simple arithmetic mean of all per-stock scores after the D/E deduction (if any):

```
universe_quality_score = sum(per_stock_scores.values()) / len(per_stock_scores)
```

If `fundamentals_df` is empty (zero symbols), `universe_quality_score = 0.0` and `DataQualityError` is raised immediately.

If `universe_quality_score < 0.6`, raise `DataQualityError` AFTER the full report is constructed and logged. The caller receives the report via the exception's `.report` attribute.

---

## 8. Logging Behaviour

Every check result is written to `agent_logs` immediately when the check completes — not batched at the end. This ensures partial results are visible if the process crashes mid-run.

Rows written per validation run:

| Event | event_type | symbol | data_quality_score | When |
|---|---|---|---|---|
| Per-stock ROE check | `roe_check` | ticker | NULL | After each stock |
| D/E coverage result | `de_coverage_check` | NULL | NULL | After scanning all fundamentals |
| `data_coverage_low` flag | `data_coverage_low` | NULL | NULL | Only if coverage < 0.80 |
| Per-stock OHLCV gap check | `ohlcv_gap_check` | ticker | NULL | After each stock |
| Per-stock final score | `stock_score` | ticker | 0.0–1.0 | After computing per-stock score |
| Universe final score | `universe_score` | NULL | 0.0–1.0 | After all stocks scored |
| DataQualityError raised | `data_quality_error` | NULL | 0.0–1.0 | Only if score < 0.6 |

The `detail` column for `roe_check` rows must be a JSON string: `'{"roe": <value_or_null>, "passed": true/false, "reason": "<string>"}'`

The `detail` column for `ohlcv_gap_check` rows must be a JSON string: `'{"gap_count": <n>, "gaps": [["YYYY-MM-DD", <days>], ...]}'`

The `detail` column for `stock_score` rows must be a JSON string: `'{"roe_plausibility_score": <f>, "roe_present_score": <f>, "gap_score": <f>, "de_deduction": <f>}'`

All `timestamp_ist` values use this format: `datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")`

---

## 9. Gap Detection Algorithm

The OHLCV gap check operates on a per-symbol basis. The algorithm is:

1. Filter `ohlcv_df` to the current symbol. Sort by `date` ascending.
2. Extract the sequence of dates as `datetime.date` objects.
3. If the sequence has fewer than 2 rows, return empty list (cannot detect gaps).
4. If `trading_calendar` is provided: iterate through consecutive date pairs. For each pair `(d1, d2)`, count how many calendar dates in `trading_calendar` fall strictly between `d1` and `d2`. If this count >= 5, record a gap: `gap_start = d1 + timedelta(days=1)`, `gap_length = count`.
5. If `trading_calendar` is None: use `pandas.bdate_range(start=d1, end=d2, inclusive="neither")` to approximate trading days between `d1` and `d2`. If `len(bdate_range) >= 5`, record a gap as above.
6. Return the list of `(gap_start_date_str, gap_length_days)` tuples. `gap_start_date_str` is `gap_start.isoformat()` (YYYY-MM-DD).

Rationale for threshold of 5: NSE has scheduled holidays that can produce gaps of up to 4 consecutive non-trading days in a single week. A gap of 5+ business days is abnormal and indicates missing data rather than a holiday cluster.

---

## 10. Error Handling Rules

Strict error handling — no bare except clauses. Every exception caught must be a specific type.

| Situation | Exception to catch | Action |
|---|---|---|
| Required column missing from DataFrame | `KeyError` | Re-raise as `ValueError` with descriptive message listing missing columns |
| SQLite write failure | `sqlite3.OperationalError`, `sqlite3.DatabaseError` | Log to stderr, re-raise |
| JSON serialisation failure in logging | `TypeError`, `ValueError` | Log the raw string fallback to detail column, do not crash |
| NaN comparison on float | Do not use `== float("nan")`. Use `pd.isna()` or `math.isnan()` | — |
| Empty OHLCV for a symbol present in fundamentals | Not an exception — assign `gap_score = 0.0`, log `gap_count = 0` with `detail` noting absence | — |

---

## 11. Timestamp Rules

- All timestamps stored in `agent_logs.timestamp_ist` are in IST (Asia/Kolkata).
- Use `zoneinfo.ZoneInfo("Asia/Kolkata")` — available in Python 3.12 standard library.
- Never use `pytz`. Never use `datetime.utcnow()`. Always use `datetime.now(ZoneInfo("Asia/Kolkata"))`.
- The `checked_at_ist` field in `DataQualityReport` is the timestamp at the moment `validate_data` is called (captured at function entry, not at return).

---

## 12. Module-Level Constants

Define these at module level, not inside functions. They are not configurable via environment variables — they encode domain rules.

```python
ROE_MIN: float = -0.50          # Lower bound for plausible ROE
ROE_MAX: float = 2.00           # Upper bound for plausible ROE
DE_COVERAGE_THRESHOLD: float = 0.80   # Minimum fraction of universe with D/E data
UNIVERSE_QUALITY_THRESHOLD: float = 0.60  # Minimum acceptable universe_quality_score
MAX_OHLCV_GAP_DAYS: int = 5     # Maximum allowed consecutive missing trading days
AGENT_NAME: str = "validator"   # Written to agent_logs.agent_name
```

---

## 13. Explicit Non-Goals

This module does NOT:

- Fetch data from any external source (yfinance, jugaad-data, Screener.in, NSE). It only validates DataFrames handed to it.
- Apply quality filter rules (ROE > 15%, D/E < 1.0). Those thresholds live in `src/strategy/quality_filter.py`. The validator only checks for data corruption (ROE outside [-50%, 200%]) — a different concern.
- Cache any data to disk. No CSV, no parquet, no pickle files created.
- Send notifications via Telegram or Gmail.
- Have any knowledge of trading strategy, momentum, or signals.
- Create any tables other than `agent_logs`.
- Accept or handle authentication tokens, API keys, or environment variables. The caller is responsible for constructing the DataFrames and passing `db_path`.
- Validate that symbols are valid Nifty 50 constituents. It validates whatever symbols are passed.
- Backfill or repair gaps — detection only.

---

## 14. Complete Function Signature Reference

For the Coder Agent's reference — all public names exported by this module:

```python
# Exception
class DataQualityError(Exception): ...

# Dataclass
@dataclass(frozen=True)
class DataQualityReport: ...

# Public function
def validate_data(
    ohlcv_df: pd.DataFrame,
    fundamentals_df: pd.DataFrame,
    db_path: str,
    trading_calendar: list[datetime.date] | None = None,
) -> DataQualityReport: ...
```

Internal functions (prefixed with `_`) are implementation details and may be tested directly by the Tester Agent but are not part of the public contract consumed by other modules.

---

## 15. Acceptance Criteria

The Tester Agent must verify all of the following before marking this module complete:

1. `validate_data` returns a `DataQualityReport` when given valid DataFrames with no anomalies.
2. `DataQualityError` is raised when `universe_quality_score` falls below 0.60 — and the report is accessible via `exception.report`.
3. A stock with ROE = 2.50 (outside range) scores exactly 0.10 on the ROE sub-components.
4. A stock with ROE = None scores 0.0 on both ROE sub-components.
5. A stock with ROE = 0.18 (18%, inside range) scores 0.50 on ROE sub-components (0.40 + 0.10).
6. A stock with 1 OHLCV gap of 7 days scores 0.25 on the gap sub-component (0.5 * 0.50).
7. A stock with 2 OHLCV gaps scores 0.0 on the gap sub-component.
8. A gap of exactly 4 business days does NOT appear in `gap_violations`.
9. A gap of exactly 5 business days DOES appear in `gap_violations`.
10. `de_coverage_low = True` when fewer than 80% of symbols have a non-null `debt_to_equity`.
11. `de_coverage_low = True` causes all per-stock scores to be reduced by 0.10 (clamped at 0.0).
12. Every check result is logged to `agent_logs` with correct `event_type` and IST timestamp.
13. `timestamp_ist` values in `agent_logs` are timezone-aware IST strings (contain `+05:30`).
14. Passing a DataFrame with a missing required column raises `ValueError` (not `KeyError`).
15. Passing an OHLCV DataFrame with timezone-naive dates raises `ValueError`.
16. `DataQualityReport` is immutable — attempting to set a field after construction raises `FrozenInstanceError`.
17. Running `validate_data` twice on the same `db_path` results in two sets of rows in `agent_logs` — rows are never overwritten or deduplicated.
18. mypy passes with `--ignore-missing-imports` on this file with no errors.
19. ruff check passes with no errors on this file.
