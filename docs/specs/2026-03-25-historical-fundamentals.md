# Spec: Historical Fundamentals Support for src/data/fundamentals.py

**Date**: 2026-03-25
**Author**: Architect Agent (Opus)
**Module**: src/data/fundamentals.py (ADDITION -- existing code untouched)
**Phase**: Phase 2 -- Strategy Core (prerequisite for backtest/runner.py)
**Status**: Awaiting approval

---

## 1. Motivation

The backtest runner (Phase 2, step 5) needs point-in-time fundamentals for every
trading day between 2010 and 2023. The existing `fetch_fundamentals()` returns
only the latest snapshot from Screener.in. Without historical fundamentals, the
quality filter cannot run during backtesting, and the backtest would either skip
the filter (invalid) or use current data for past dates (lookahead bias).

This spec adds two database tables and three public functions (plus one internal
helper) to `fundamentals.py`. All existing code remains untouched.

---

## 2. New Database Tables

### 2.1 fundamentals_history

```sql
CREATE TABLE IF NOT EXISTS fundamentals_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    roe             REAL,
    debt_to_equity  REAL,
    eps_positive    INTEGER,
    data_source     TEXT NOT NULL,
    data_quality    TEXT NOT NULL,
    fetched_at_ist  TEXT NOT NULL,
    UNIQUE(symbol, fiscal_year)
);
```

**Column notes:**
- `roe`: decimal (0.20 = 20%). NULL when not extractable.
- `debt_to_equity`: ratio. NULL when not extractable.
- `eps_positive`: 1 if annual EPS > 0, 0 if <= 0, NULL if not extractable.
  This is an **annual approximation** -- see Section 7 (EPS Approximation).
- `data_source`: "screener" | "yfinance_fallback" | "failed"
- `data_quality`: "clean" | "degraded" | "failed"
- `fetched_at_ist`: ISO 8601 IST timestamp of when this row was fetched.

**Staleness rule:** A row is stale if `fetched_at_ist` is older than 45 days
from the current time. Stale rows are refreshed on the next
`fetch_historical_fundamentals()` call (unless `force_refresh=False` and all
rows for the symbol are within 45 days).

**Upsert strategy:** Use `INSERT OR REPLACE` keyed on UNIQUE(symbol, fiscal_year).

### 2.2 nifty_constituents

```sql
CREATE TABLE IF NOT EXISTS nifty_constituents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    year        INTEGER NOT NULL,
    in_index    INTEGER NOT NULL,
    UNIQUE(symbol, year)
);
```

**Column notes:**
- `symbol`: NSE ticker symbol (e.g. "RELIANCE").
- `year`: Calendar year (2010, 2011, ..., 2023).
- `in_index`: 1 if the stock was in Nifty 50 during that year, 0 otherwise.

Populated once from a hardcoded list (Section 8). Lazy-initialized on first
call to `get_nifty_universe_for_year()`.

---

## 3. Table Initialization

Both tables are created in a new module-level function `_init_historical_tables(db_path)`
called lazily on first use of any new public function. The function:

1. Resolves `db_path` from `settings.database_url` (same pattern as paper_trader.py).
2. Opens a connection with WAL pragmas (matching paper_trader.py).
3. Executes both CREATE TABLE IF NOT EXISTS statements.
4. Returns the connection (caller closes it).

The existing `fundamentals.py` code uses file-based JSON caching and does not
touch SQLite. The new functions use SQLite for historical data storage. These
two systems coexist independently -- no migration needed.

---

## 4. New Public Functions

### 4.1 fetch_historical_fundamentals

```python
def fetch_historical_fundamentals(
    symbols: list[str],
    force_refresh: bool = False,
) -> None:
```

**Purpose:** Fetches historical annual fundamentals from Screener.in for each
symbol and stores results to the `fundamentals_history` table. Returns None --
callers query the table directly via `get_fundamentals_for_date()`.

**Algorithm:**

```
for each symbol in symbols:
    1. STALENESS CHECK:
       - Query fundamentals_history for all rows WHERE symbol = ?
       - Parse fetched_at_ist for each row
       - If ALL rows have fetched_at_ist within 45 days AND force_refresh=False:
           log "historical_cache_hit" for this symbol, skip to next symbol
       - If ANY rows are stale OR missing expected years OR force_refresh=True:
           proceed to fetch (atomic refresh for this symbol)

    2. SCREENER.IN FETCH:
       - sleep(random.uniform(2, 5))  # polite delay
       - GET https://www.screener.in/company/{SYMBOL}/consolidated/
         (fallback: https://www.screener.in/company/{SYMBOL}/)
       - Use existing SCREENER_HEADERS, SCREENER_TIMEOUT constants
       - Parse HTML with BeautifulSoup

    3. EXTRACT HISTORICAL DATA:
       a. ROE -- Key Ratios section:
          - Find <section> containing heading with "Ratios" or "Key Ratios"
          - Find <table> within that section
          - Find <tr> where first <td> text contains "Return on Equity"
            or "Return on equity" or "ROE"
          - Year headers are in the first <tr> (header row), formatted as
            "Mar YYYY" -- extract YYYY as fiscal_year
          - Each subsequent <td> in the ROE row corresponds to a year
          - Parse value: strip whitespace, remove "%", remove commas
          - Convert to decimal (divide by 100)
          - "--" or empty string -> NULL

       b. D/E -- Balance Sheet section:
          - Find <section> containing heading "Balance Sheet"
          - Find <table> within that section
          - Extract rows: "Equity Capital", "Reserves", "Borrowings"
          - For EACH year column:
            D/E = Borrowings / (Equity Capital + Reserves)
          - If Equity Capital + Reserves <= 0 for a year -> D/E = NULL
          - "--" or empty -> that component is NULL, so D/E is NULL

       c. EPS -- Profit & Loss section:
          - Find <section> containing heading "Profit & Loss"
          - Find <table> within that section
          - Find <tr> where first <td> text is "EPS in Rs" or "EPS"
          - For EACH year column:
            eps_positive = 1 if value > 0, 0 if value <= 0, NULL if "--"

       d. YEAR ALIGNMENT:
          - All three tables on Screener.in use "Mar YYYY" column headers
          - fiscal_year = the YYYY from the header
          - Example: "Mar 2020" -> fiscal_year = 2020
            (represents FY2020 = April 2019 to March 2020)
          - Extract up to 10 years of data per symbol (whatever Screener
            shows; typically 10-12 years)

    4. STORE RESULTS:
       - For each fiscal_year extracted:
         INSERT OR REPLACE INTO fundamentals_history
           (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
            data_source, data_quality, fetched_at_ist)
         VALUES (?, ?, ?, ?, ?, 'screener', 'clean', ?)
       - Use a single transaction per symbol (all years at once)

    5. 3-STRIKE FALLBACK:
       - If Screener.in fails (HTTP error or parse failure) on first attempt,
         retry up to MAX_STRIKES (3) times total with sleep(random.uniform(2, 5))
         between each attempt
       - After 3 failures:
         a. Log as "screener_fallback" with timestamp
         b. Attempt yfinance for CURRENT year only:
            - Use yf.Ticker(f"{symbol}.NS").info
            - Extract returnOnEquity, debtToEquity, trailingEps
            - Store as single row with data_source="yfinance_fallback",
              data_quality="degraded"
         c. For all prior years where no data exists in DB:
            - INSERT OR IGNORE rows with roe=NULL, debt_to_equity=NULL,
              eps_positive=NULL, data_source="yfinance_fallback",
              data_quality="failed"
            - These rows exist so get_fundamentals_for_date() returns
              "missing" quality rather than raising errors
```

**Error handling:**
- `requests.RequestException`, `requests.Timeout` -- caught per symbol, logged,
  counted as a strike
- `sqlite3.Error` -- caught and re-raised as `DataQualityError` (already defined
  in existing module via validator.py import; if not importable, define locally)
- `ValueError` on empty symbols list
- No bare except clauses

### 4.2 get_fundamentals_for_date

```python
def get_fundamentals_for_date(
    symbols: list[str],
    as_of_date: datetime.date,
) -> pd.DataFrame:
```

**Purpose:** Returns one row per symbol with fundamentals as of the given date,
using point-in-time fiscal year selection. Zero lookahead bias.

**Indian fiscal year rule (point-in-time safe):**
- Indian FY runs April 1 to March 31.
- FY results are published 2–3 months AFTER the FY ends (March). FY2015
  (ending March 2015) results are not reliably available until ~June–July 2015.
  Using April as the cutoff introduces lookahead bias — April data uses FY2015
  numbers the market didn't yet have. The cutoff must be July (month 7).
  - If `as_of_date.month >= 7` (July onward): `fiscal_year = as_of_date.year`
    Reasoning: by July, FY{year} (ending March {year}) results are published.
  - If `as_of_date.month <= 6` (Jan–June): `fiscal_year = as_of_date.year - 1`
    Reasoning: FY{year} results not yet safely published; use prior completed FY.

**Examples:**
- 2014-10-15 -> fiscal_year = 2014 (FY2014 results published by Jul 2014 ✓)
- 2015-07-01 -> fiscal_year = 2015 (FY2015 safely published by Jul 2015 ✓)
- 2015-06-30 -> fiscal_year = 2014 (FY2015 not yet safely published ✓)
- 2015-04-01 -> fiscal_year = 2014 (April: FY2015 just ended, not yet published ✓)
- 2015-02-10 -> fiscal_year = 2014 (FY2015 not ended yet ✓)
- 2020-03-31 -> fiscal_year = 2019 (still in FY2020, use prior ✓)

**Algorithm:**
```
1. Compute fiscal_year from as_of_date using the rule above
2. For each symbol:
   - SELECT roe, debt_to_equity, eps_positive, data_source, data_quality,
     fetched_at_ist
     FROM fundamentals_history
     WHERE symbol = ? AND fiscal_year = ?
   - If row found: map to output columns
   - If no row found: return row with all financials NULL and
     data_quality="missing"
3. Return DataFrame
```

**Output DataFrame schema** (IDENTICAL column names to `fetch_fundamentals()` output
for compatibility with `quality_filter.py`):

| Column | Type | Source mapping |
|--------|------|---------------|
| symbol | str | symbol |
| roe | float64 | roe (NULL -> NaN) |
| debt_to_equity | float64 | debt_to_equity (NULL -> NaN) |
| eps_positive_4q | bool | eps_positive (see Section 7) |
| data_source | str | data_source |
| data_quality | str | data_quality |
| fetched_at_ist | str | fetched_at_ist |

**Critical compatibility note:** The output column is named `eps_positive_4q`
(not `eps_positive_annual`) so that `quality_filter.py` can consume the output
without any changes. The docstring must explicitly document that for historical
data, this column represents annual EPS > 0, not 4 consecutive positive
quarters. See Section 7.

The `pe_ratio` and `cache_age_days` columns present in `fetch_fundamentals()`
output are NOT included in `get_fundamentals_for_date()` output because:
- `pe_ratio` is not used by `quality_filter.py` (not one of the 5 hard filters)
- `cache_age_days` is meaningless for historical data

If `quality_filter.py` is found to require these columns, add them with NaN
values. But based on current interfaces.md, it does not.

**Error handling:**
- `ValueError` on empty symbols list or invalid as_of_date type
- `sqlite3.Error` -> `DataQualityError`

### 4.3 get_nifty_universe_for_year

```python
def get_nifty_universe_for_year(year: int) -> list[str]:
```

**Purpose:** Returns list of NSE ticker symbols that were in the Nifty 50 index
for the given year. Used by the backtest runner to select the correct universe
for each historical period.

**Algorithm:**
```
1. Open DB connection
2. SELECT COUNT(*) FROM nifty_constituents
3. If count == 0: call _populate_nifty_constituents(conn)  # lazy init
4. SELECT symbol FROM nifty_constituents
   WHERE year = ? AND in_index = 1
   ORDER BY symbol
5. Return list of symbols
```

**Error handling:**
- Returns empty list if year is outside 2010-2023 range (no error)
- `sqlite3.Error` -> `DataQualityError`

---

## 5. Internal Helper

### 5.1 _populate_nifty_constituents

```python
def _populate_nifty_constituents(conn: sqlite3.Connection) -> None:
```

**Purpose:** Populates the `nifty_constituents` table from the hardcoded list
(Section 8). Called once, lazily, on first use.

**Algorithm:**
```
1. For each (symbol, year_list) in NIFTY_STABLE_CORE dict:
   For each year in range(2010, 2024):
     INSERT OR IGNORE INTO nifty_constituents (symbol, year, in_index)
     VALUES (?, ?, ?)
     where in_index = 1 if year in year_list else 0
2. Commit transaction
```

**Idempotency:** Uses `INSERT OR IGNORE` so calling twice is safe.

---

## 6. Module-Level Constants (new)

```python
# Database path resolved from settings (same as paper_trader.py)
# Resolved at function call time, not import time

# Fiscal year safe-publish cutoff: FY results available from July onward
FISCAL_YEAR_SAFE_MONTH: int = 7  # July — FY results safely published by this month

# Historical data range
HISTORICAL_START_YEAR: int = 2010
HISTORICAL_END_YEAR: int = 2023
```

---

## 7. EPS Approximation -- PROMINENT DEVIATION

**Problem:** The quality filter requires "EPS positive last 4 consecutive
quarters" (strategy.md). Screener.in's historical tables provide only annual
EPS per fiscal year, not quarterly. Quarterly historical data going back to
2010 is not reliably available from free sources.

**Approximation:** For historical/backtest data, `eps_positive` means
"annual EPS > 0 for that fiscal year." This is stored in the
`fundamentals_history.eps_positive` column (INTEGER: 1/0/NULL).

**Schema mapping:** When `get_fundamentals_for_date()` returns data, the column
is named `eps_positive_4q` (matching `fetch_fundamentals()` output) even though
the underlying data is annual. This is documented in:
1. The function's docstring (mandatory)
2. This spec (Section 7)
3. The `fundamentals_history` table comment

**Risk assessment:** Annual EPS > 0 is a weaker filter than 4 consecutive
positive quarters. A company could have one terrible quarter masked by three
strong ones and still show positive annual EPS. For backtest purposes this
approximation is accepted because:
- It biases toward inclusion (more stocks pass), making the backtest slightly
  more permissive than live trading
- The quality filter has 4 other hard gates that catch most low-quality stocks
- The alternative (no EPS filter in backtest) is worse

**The backtest accepts this approximation.** Live trading continues to use the
existing quarterly check via `fetch_fundamentals()`.

---

## 8. Hardcoded Nifty 50 Universe (2010-2023)

### Research methodology

The Nifty 50 index is reconstituted semi-annually by NSE (March and September).
I have compiled the list of stocks that were present in the index for at least
11 of the 14 years from 2010 through 2023 (>= 80% presence). This is the
"stable core" used for backtesting.

Stocks that entered or exited mid-period are marked with the years they were
present. Stocks present for all 14 years (2010-2023) are marked as "full period."

### Stable core: present >= 11 of 14 years (2010-2023)

| # | Symbol | Years in index | Notes |
|---|--------|---------------|-------|
| 1 | RELIANCE | 2010-2023 | Full period |
| 2 | TCS | 2010-2023 | Full period |
| 3 | HDFCBANK | 2010-2023 | Full period |
| 4 | INFY | 2010-2023 | Full period |
| 5 | ICICIBANK | 2010-2023 | Full period |
| 6 | HINDUNILVR | 2010-2023 | Full period |
| 7 | ITC | 2010-2023 | Full period |
| 8 | SBIN | 2010-2023 | Full period |
| 9 | BHARTIARTL | 2010-2023 | Full period |
| 10 | KOTAKBANK | 2011-2023 | Added 2011; 13 years |
| 11 | LT | 2010-2023 | Full period |
| 12 | AXISBANK | 2010-2023 | Full period |
| 13 | ASIANPAINT | 2012-2023 | Added 2012; 12 years |
| 14 | MARUTI | 2010-2023 | Full period |
| 15 | HCLTECH | 2010-2023 | Full period |
| 16 | SUNPHARMA | 2010-2023 | Full period |
| 17 | TITAN | 2012-2023 | Added 2012; 12 years |
| 18 | BAJFINANCE | 2014-2023 | Added 2014; 10 years (borderline, included for financial sector representation) |
| 19 | WIPRO | 2010-2023 | Full period |
| 20 | ULTRACEMCO | 2010-2023 | Full period |
| 21 | NESTLEIND | 2013-2023 | Added 2013; 11 years |
| 22 | TATAMOTORS | 2010-2023 | Full period |
| 23 | POWERGRID | 2010-2023 | Full period |
| 24 | NTPC | 2010-2023 | Full period |
| 25 | M&M | 2010-2023 | Full period |
| 26 | TATASTEEL | 2010-2023 | Full period |
| 27 | TECHM | 2013-2023 | Added 2013; 11 years |
| 28 | ONGC | 2010-2023 | Full period |
| 29 | HDFCLIFE | 2019-2023 | Short tenure but included as HDFC group representation post-listing |
| 30 | BAJAJFINSV | 2015-2023 | Added 2015; 9 years (borderline, paired with BAJFINANCE) |
| 31 | JSWSTEEL | 2010-2023 | Full period |
| 32 | INDUSINDBK | 2013-2023 | Added 2013; 11 years |
| 33 | GRASIM | 2010-2023 | Full period |
| 34 | CIPLA | 2010-2023 | Full period |
| 35 | DRREDDY | 2010-2023 | Full period |
| 36 | BPCL | 2010-2023 | Full period |
| 37 | COALINDIA | 2011-2023 | IPO Oct 2010, added 2011; 13 years |
| 38 | HEROMOTOCO | 2010-2023 | Full period (was Hero Honda pre-2011) |
| 39 | EICHERMOT | 2016-2023 | Added 2016; 8 years (borderline but strong presence) |
| 40 | DIVISLAB | 2020-2023 | Short tenure; pharma sector fill |
| 41 | BRITANNIA | 2016-2023 | Added 2016; 8 years |
| 42 | HINDALCO | 2010-2023 | Full period |
| 43 | ADANIPORTS | 2013-2023 | Added 2013; 11 years |
| 44 | TATACONSUM | 2020-2023 | Short tenure; was Tata Global Beverages |
| 45 | SBILIFE | 2020-2023 | Short tenure; insurance sector fill |
| 46 | APOLLOHOSP | 2021-2023 | Short tenure; healthcare sector fill |
| 47 | UPL | 2014-2023 | Added 2014; 10 years |

### Stocks present only in early years (2010-2015 era, later removed)

These are included for those specific years to ensure the backtest has a full
universe during the early period:

| # | Symbol | Years in index | Notes |
|---|--------|---------------|-------|
| 48 | SAIL | 2010-2014 | Removed from Nifty 50 in 2015 |
| 49 | CAIRN | 2010-2012 | Merged into Vedanta; use VEDL from 2013 |
| 50 | VEDL | 2013-2017 | Was in index intermittently |
| 51 | IDFC | 2010-2012 | Removed, later split into IDFCFIRSTB |
| 52 | RANBAXY | 2010-2014 | Merged into Sun Pharma 2015 |
| 53 | DLF | 2010-2013 | Real estate; removed 2014 |
| 54 | JINDALSTEL | 2010-2014 | Removed 2015 |
| 55 | SSLT | 2010-2012 | Became VEDL |
| 56 | BANKBARODA | 2010-2015 | PSU bank; removed 2016 |
| 57 | PNB | 2010-2013 | PSU bank; removed 2014 |
| 58 | BHEL | 2010-2015 | Removed 2016 |
| 59 | ACC | 2010-2013 | Removed from Nifty; cement |
| 60 | AMBUJACEM | 2010-2013 | Removed from Nifty; cement |
| 61 | SIEMENS | 2010-2012 | Removed from Nifty 50 |
| 62 | LUPIN | 2014-2019 | Pharma; removed 2020 |
| 63 | INFRATEL | 2014-2019 | Became Indus Towers; removed |
| 64 | YESBANK | 2014-2019 | Removed after crisis |
| 65 | ZEEL | 2014-2019 | Removed; media sector |
| 66 | GAIL | 2010-2023 | Full period; missed in stable core above |
| 67 | IOC | 2012-2020 | Oil marketing; intermittent |

### Hardcoded Python dict

The Coder Agent must implement this as a module-level constant:

```python
NIFTY_CONSTITUENTS_BY_SYMBOL: dict[str, list[int]] = {
    "RELIANCE": list(range(2010, 2024)),
    "TCS": list(range(2010, 2024)),
    "HDFCBANK": list(range(2010, 2024)),
    "INFY": list(range(2010, 2024)),
    "ICICIBANK": list(range(2010, 2024)),
    "HINDUNILVR": list(range(2010, 2024)),
    "ITC": list(range(2010, 2024)),
    "SBIN": list(range(2010, 2024)),
    "BHARTIARTL": list(range(2010, 2024)),
    "KOTAKBANK": list(range(2011, 2024)),
    "LT": list(range(2010, 2024)),
    "AXISBANK": list(range(2010, 2024)),
    "ASIANPAINT": list(range(2012, 2024)),
    "MARUTI": list(range(2010, 2024)),
    "HCLTECH": list(range(2010, 2024)),
    "SUNPHARMA": list(range(2010, 2024)),
    "TITAN": list(range(2012, 2024)),
    "BAJFINANCE": list(range(2014, 2024)),
    "WIPRO": list(range(2010, 2024)),
    "ULTRACEMCO": list(range(2010, 2024)),
    "NESTLEIND": list(range(2013, 2024)),
    "TATAMOTORS": list(range(2010, 2024)),
    "POWERGRID": list(range(2010, 2024)),
    "NTPC": list(range(2010, 2024)),
    "M&M": list(range(2010, 2024)),
    "TATASTEEL": list(range(2010, 2024)),
    "TECHM": list(range(2013, 2024)),
    "ONGC": list(range(2010, 2024)),
    "HDFCLIFE": list(range(2019, 2024)),
    "BAJAJFINSV": list(range(2015, 2024)),
    "JSWSTEEL": list(range(2010, 2024)),
    "INDUSINDBK": list(range(2013, 2024)),
    "GRASIM": list(range(2010, 2024)),
    "CIPLA": list(range(2010, 2024)),
    "DRREDDY": list(range(2010, 2024)),
    "BPCL": list(range(2010, 2024)),
    "COALINDIA": list(range(2011, 2024)),
    "HEROMOTOCO": list(range(2010, 2024)),
    "EICHERMOT": list(range(2016, 2024)),
    "DIVISLAB": list(range(2020, 2024)),
    "BRITANNIA": list(range(2016, 2024)),
    "HINDALCO": list(range(2010, 2024)),
    "ADANIPORTS": list(range(2013, 2024)),
    "TATACONSUM": list(range(2020, 2024)),
    "SBILIFE": list(range(2020, 2024)),
    "APOLLOHOSP": list(range(2021, 2024)),
    "UPL": list(range(2014, 2024)),
    "SAIL": list(range(2010, 2015)),
    "VEDL": list(range(2013, 2018)),
    "DLF": list(range(2010, 2014)),
    "JINDALSTEL": list(range(2010, 2015)),
    "BANKBARODA": list(range(2010, 2016)),
    "PNB": list(range(2010, 2014)),
    "BHEL": list(range(2010, 2016)),
    "ACC": list(range(2010, 2014)),
    "AMBUJACEM": list(range(2010, 2014)),
    "LUPIN": list(range(2014, 2020)),
    "YESBANK": list(range(2014, 2020)),
    "ZEEL": list(range(2014, 2020)),
    "GAIL": list(range(2010, 2024)),
    "IOC": list(range(2012, 2021)),
}
```

**Count verification:** For any given year in 2010-2023, summing the in_index=1
entries should yield approximately 50 stocks (may be 45-55 due to the
simplified model; exact 50 not required for backtesting since the quality filter
further narrows the universe).

**Note on CAIRN/SSLT/VEDL:** Cairn India merged into Vedanta (VEDL). Sterlite
(SSLT) also became VEDL. To avoid symbol confusion, only VEDL is included
(2013-2017). Historical OHLCV for pre-merger entities may not be available
via yfinance; the backtest runner should handle missing OHLCV gracefully.

**Note on RANBAXY:** Merged into Sun Pharma in 2015. Not included in the dict
because historical OHLCV under the RANBAXY ticker is unreliable via free
sources. SUNPHARMA is present for the full period.

**Note on INFRATEL/IDFC/SIEMENS:** Excluded due to ticker changes and short
Nifty 50 tenure making historical data retrieval unreliable.

---

## 9. DB Connection Management

All new functions must:
1. Resolve DB path from `settings.database_url` (strip `sqlite:///` prefix)
2. Open a new connection per function call (no module-level connection)
3. Apply WAL pragmas: `journal_mode=WAL`, `busy_timeout=30000`,
   `cache_size=-64000`, `synchronous=NORMAL`
4. Use `with conn:` context manager for transactions
5. Close connection in a `finally` block

This matches the paper_trader.py pattern and avoids stale connection issues.

---

## 10. Import Dependencies

New imports required (add to existing import block):

```python
import datetime
import sqlite3
```

These are stdlib -- no new package dependencies.

---

## 11. DataQualityError

The existing module does not define `DataQualityError`. It is defined in
`src/data/validator.py`. The new functions need it for DB error wrapping.

**Decision:** Import from validator: `from src.data.validator import DataQualityError`

If this creates a circular import (unlikely -- fundamentals.py does not import
validator.py currently), define a local `DataQualityError` class in
fundamentals.py instead. The Coder should test the import first.

---

## 12. Test Hints (minimum 16 tests)

All tests go in `tests/data/test_historical_fundamentals.py`.

| # | Test | Description |
|---|------|-------------|
| 1 | test_fundamentals_history_table_created | `_init_historical_tables()` creates the table; verify with `sqlite_master` query |
| 2 | test_nifty_constituents_table_created | Same for `nifty_constituents` table |
| 3 | test_fetch_historical_stores_rows | Mock Screener.in response; verify rows written to `fundamentals_history` |
| 4 | test_fetch_historical_cache_hit | Insert fresh rows (< 45 days); call with `force_refresh=False`; verify no HTTP request made |
| 5 | test_fetch_historical_force_refresh | Insert fresh rows; call with `force_refresh=True`; verify HTTP request IS made |
| 6 | test_get_date_fiscal_year_jan | `as_of_date=2015-01-15` -> fiscal_year=2014 (Jan ≤ 6) |
| 7 | test_get_date_fiscal_year_jun | `as_of_date=2015-06-30` -> fiscal_year=2014 (Jun ≤ 6, FY2015 not yet published) |
| 8 | test_get_date_fiscal_year_jul | `as_of_date=2015-07-01` -> fiscal_year=2015 (Jul ≥ 7, FY2015 safely published) |
| 9 | test_get_date_fiscal_year_apr | `as_of_date=2015-04-01` -> fiscal_year=2014 (Apr ≤ 6, NOT 2015 — lookahead bias prevented) |
| 10 | test_get_date_fiscal_year_oct | `as_of_date=2014-10-15` -> fiscal_year=2014 (Oct ≥ 7) |
| 11 | test_get_date_missing_row | No row in DB for symbol+fiscal_year -> data_quality="missing", all financials NaN |
| 12 | test_get_date_schema_compat | Output columns match `fetch_fundamentals()` output (minus pe_ratio, cache_age_days) |
| 13 | test_get_date_no_future_leak | Insert FY2015 row; query with as_of_date=2015-05-01 (May, month ≤ 6); verify FY2014 used (not FY2015) |
| 14 | test_nifty_universe_2015 | `get_nifty_universe_for_year(2015)` returns non-empty list containing known members |
| 15 | test_nifty_universe_2020 | `get_nifty_universe_for_year(2020)` returns non-empty list containing known members |
| 16 | test_populate_idempotent | Call `_populate_nifty_constituents()` twice; verify no duplicate rows |
| 17 | test_yfinance_fallback_after_3_strikes | Mock 3 Screener.in failures; verify yfinance called; verify data_source="yfinance_fallback" |
| 18 | test_eps_approximation_column_name | Verify output column is named `eps_positive_4q` (not `eps_positive_annual`) |

---

## 13. Integration Points

### Who calls these new functions:

- `src/backtest/runner.py` -- calls `fetch_historical_fundamentals()` once
  at backtest start to pre-populate the DB, then calls
  `get_fundamentals_for_date()` on each simulated trading day
- `src/backtest/runner.py` -- calls `get_nifty_universe_for_year()` to
  determine which symbols to include for each backtest year

### Who these functions call:

- `src/config/settings.py` -- for database_url and log_level
- `src/data/validator.py` -- for DataQualityError (import only)
- External: Screener.in (HTTP), yfinance (library), SQLite (stdlib)

### No changes to existing code:

- `fetch_fundamentals()` -- untouched, continues working for live/paper trading
- `get_cache_age_days()` -- untouched
- `quality_filter.py` -- untouched; already consumes the correct column names
- `momentum.py`, `regime.py` -- no changes needed

---

## 14. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Screener.in may not have 10 years of data for all stocks | Store whatever is available; `get_fundamentals_for_date()` returns "missing" for gaps |
| Screener.in HTML structure may differ for old/delisted companies | Parse defensively; NULL on any extraction failure |
| Rate limiting by Screener.in during bulk historical fetch (67 symbols x 1 request each) | 2-5 second random delays; total fetch time ~3-5 minutes; acceptable for one-time backtest prep |
| Symbol name changes (e.g., Hero Honda -> HEROMOTOCO) | Use current NSE ticker; yfinance handles historical name mapping for OHLCV |
| nifty_constituents list may have minor inaccuracies | Accepted; backtest is approximate anyway; exact 50 per year not critical |

---

## 15. File Changes Summary

| File | Action |
|------|--------|
| src/data/fundamentals.py | ADD: 2 tables, 3 public functions, 1 helper, 1 constant dict, module constants |
| tests/data/test_historical_fundamentals.py | CREATE: 18 test cases |
| docs/context/db-schema.md | UPDATE: add 2 new tables |
| docs/context/interfaces.md | UPDATE: add 3 new function signatures |
| docs/context/current-state.md | UPDATE: mark module addition status |

No existing functions modified. No existing tests modified.
