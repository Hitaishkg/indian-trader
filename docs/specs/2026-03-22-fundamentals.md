# Spec: src/data/fundamentals.py

**Date:** 2026-03-22
**Phase:** 1 -- Foundation
**Build order position:** 5 of 9 (depends on: `src/data/validator.py`, `src/config/settings.py`, `src/data/fetcher.py`, `src/data/cleaner.py` already built)
**Author:** Architect Agent

---

## 1. Purpose

`src/data/fundamentals.py` is the fundamental data acquisition layer for the Indian Trader pipeline. It scrapes ROE, debt-to-equity, quarterly EPS, and P/E ratio data from Screener.in for NSE-listed stocks, caches results as JSON files in `data/cache/`, and returns a normalised DataFrame that is a superset of the `src/data/validator.py` Section 5.2 contract.

This module is consumed by:
- `src/data/validator.py` -- receives the fundamentals DataFrame for data quality validation (ROE plausibility, D/E coverage checks)
- `src/strategy/quality_filter.py` (Phase 2) -- applies hard filters (ROE > 15%, D/E < 1.0, EPS positive 4 quarters, price > 50, volume > 20 crore)
- `main.py` (Phase 1, step 9) -- calls `fetch_fundamentals` as part of the dry-run pipeline
- The Screener Agent (Phase 4) -- calls `fetch_fundamentals` during the evening session

This module has zero knowledge of strategy thresholds, trading logic, or technical indicators. It only fetches, caches, and returns fundamental data.

---

## 2. File and Directory Structure

The Coder Agent must create the module file. The cache directory already exists from the fetcher module.

```
src/
  data/
    __init__.py          (already exists -- do not modify)
    fundamentals.py      (this module)
data/
  cache/                 (already exists, gitignored)
    RELIANCE_fundamentals.json
    TCS_fundamentals.json
    ...
```

Cache files are named `{SYMBOL}_fundamentals.json` (one JSON file per stock symbol). The `data/cache/` directory is already present in `.gitignore`. The Coder Agent must still call `os.makedirs(CACHE_DIR, exist_ok=True)` before any write -- never assume the directory exists at runtime.

---

## 3. Dependencies

### Already in pyproject.toml
- `pandas>=2.2.0`
- `yfinance>=0.2.40`
- `numpy>=1.26.0`
- `python-dotenv>=1.0.0`

### Must be added to pyproject.toml

The Coder Agent must add the following two dependencies to `pyproject.toml`:

```toml
"requests>=2.31.0",
"beautifulsoup4>=4.12.0",
```

`lxml` is NOT required -- use the default `html.parser` from the standard library. It is slower than `lxml` but has zero dependency overhead and is sufficient for scraping one page per stock with 2-5 second delays.

### Standard library modules used

`datetime`, `json`, `logging`, `math`, `os`, `random`, `time`, `dataclasses`, `zoneinfo`.

---

## 4. Output DataFrame Contract

The output DataFrame must be a strict superset of the `src/data/validator.py` Section 5.2 contract. The validator requires columns `symbol`, `roe`, `debt_to_equity`. This module returns those plus additional columns needed by `quality_filter.py` and for audit purposes.

### Required columns (exact names, case-sensitive)

| Column | dtype | Nullable | Description |
|---|---|---|---|
| `symbol` | `str` (object) | NO | NSE ticker symbol, uppercase, e.g. `"RELIANCE"` |
| `roe` | `float64` | YES | Return on Equity as decimal (e.g. `0.18` = 18%). NaN if unavailable |
| `debt_to_equity` | `float64` | YES | Debt-to-equity ratio as decimal (e.g. `0.5`). NaN if unavailable |
| `eps_positive_4q` | `bool` | NO | True if EPS was positive in all of the last 4 consecutive quarters |
| `pe_ratio` | `float64` | YES | Price-to-Earnings ratio. NaN if unavailable |
| `data_source` | `str` (object) | NO | One of: `"screener"`, `"yfinance_fallback"`, `"failed"` |
| `data_quality` | `str` (object) | NO | One of: `"clean"`, `"degraded"`, `"stale_data"`, `"fundamentals_stale"`, `"failed"` |
| `cache_age_days` | `float64` | YES | Age of the cached data in days at time of fetch. NaN if freshly fetched |
| `fetched_at_ist` | `str` (object) | NO | IST timestamp (ISO 8601) when data was fetched or served from cache |

The DataFrame must have a default integer index (RangeIndex).

### Data quality values explained

| Value | Meaning | Set when |
|---|---|---|
| `"clean"` | Data from Screener.in, cross-validation passed or skipped (yfinance P/E unavailable) | Normal Screener.in fetch, P/E deviation <= 20% or yfinance P/E is None |
| `"degraded"` | Data from yfinance fallback after Screener.in 3-strike failure | 3 consecutive Screener.in failures triggered yfinance fallback |
| `"stale_data"` | P/E cross-validation failed between Screener.in and yfinance | P/E deviation > 20% between sources |
| `"fundamentals_stale"` | Cache is older than 45 days | Cache file mtime is > 45 days old and `force_refresh=False` |
| `"failed"` | Both Screener.in (3-strike) and yfinance fallback failed entirely | Complete failure to obtain any data |

### Relationship to downstream modules

- `validator.py` reads: `symbol`, `roe`, `debt_to_equity` (its minimum contract). Extra columns are silently ignored.
- `quality_filter.py` reads: all columns. Uses `roe`, `debt_to_equity`, `eps_positive_4q` for hard filters. Uses `data_quality` to reject `"fundamentals_stale"` and `"stale_data"` entries (per data.md rules). Uses `pe_ratio` for cross-validation logging.

---

## 5. Screener.in Scraping

### URL pattern

```
https://www.screener.in/company/{SYMBOL}/consolidated/
```

The symbol is the NSE ticker in uppercase (e.g. `RELIANCE`, `TCS`, `HDFCBANK`). No `.NS` suffix. Use the `/consolidated/` path to get consolidated financials (preferred for multi-entity companies). If the consolidated page returns a 404 or redirect, fall back to the standalone URL:

```
https://www.screener.in/company/{SYMBOL}/
```

This fallback counts as 1 of the 3 strikes only if it also fails.

### Request configuration

```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
timeout = 15  # seconds
```

### Delay between requests

```python
time.sleep(random.uniform(2.0, 5.0))
```

Called BEFORE each Screener.in request (not after). The first symbol in the batch also gets a delay to avoid hammering the server immediately on function entry.

### Fields to extract

The Coder Agent must parse the HTML using BeautifulSoup (`html.parser`) and extract:

**1. ROE (Return on Equity)**
- Location: the "Ratios" section or the key financial ratios list on the company page
- Look for text containing "Return on equity" or "ROE" in a `<li>`, `<td>`, or `<span>` element
- The value is typically displayed as a percentage string like `"18.5%"` or `"18.5"`
- Parse: strip `%`, strip commas, convert to float, divide by 100 to get decimal form
- Example: `"18.5%"` becomes `0.185`

**2. Debt-to-Equity (D/E)**
- Location: same ratios section
- Look for text containing "Debt to equity" or "Debt / Equity"
- Parse: strip commas, convert to float
- Already in decimal form on Screener.in (e.g. `0.45` means 0.45x D/E)

**3. Quarterly EPS**
- Location: the "Quarterly Results" table on the company page
- Look for the row labelled "EPS" or "EPS in Rs" in the quarterly results table
- Extract the last 4 quarterly EPS values (most recent 4 columns)
- `eps_positive_4q = True` if ALL 4 values are strictly > 0
- If fewer than 4 quarters of EPS data are available: set `eps_positive_4q = False` (conservative -- insufficient data treated as failure)

**4. P/E Ratio (Stock P/E)**
- Location: the ratios section
- Look for text containing "Stock P/E" or "Price to Earning"
- Parse: strip commas, convert to float
- This is the trailing P/E ratio

### Handling missing fields

If any individual field cannot be found on the page (HTML structure changed, element not present), set that field to `NaN` (for numeric fields) or `False` (for `eps_positive_4q`). Do NOT fail the entire stock -- partial data is acceptable. Log at WARNING: `"Could not extract {field_name} from Screener.in for {symbol}"`.

### HTTP error handling

- HTTP 200: parse normally
- HTTP 404: treat as a failure toward the 3-strike counter. Log at WARNING.
- HTTP 429 (rate limited): treat as a failure toward the 3-strike counter. Log at WARNING: `"Screener.in rate limited for {symbol}"`.
- HTTP 403 (forbidden): treat as a failure toward the 3-strike counter. Log at WARNING.
- Any other non-200 status: treat as a failure toward the 3-strike counter. Log at WARNING with status code.
- Network timeout or connection error: treat as a failure toward the 3-strike counter. Log at WARNING.

---

## 6. Cache Design

### File format

JSON, one file per symbol:

```
data/cache/{SYMBOL}_fundamentals.json
```

Examples:
- `data/cache/RELIANCE_fundamentals.json`
- `data/cache/HDFCBANK_fundamentals.json`

### Cache schema

The JSON file contains a serialised `_FundamentalsCache` dataclass (see Section 11). Example:

```json
{
    "symbol": "RELIANCE",
    "roe": 0.185,
    "debt_to_equity": 0.45,
    "eps_positive_4q": true,
    "pe_ratio": 28.5,
    "data_source": "screener",
    "data_quality": "clean",
    "cached_at_ist": "2026-03-22T22:15:30+05:30"
}
```

Fields with `NaN` values must be serialised as `null` in JSON (Python's `json.dumps` handles `float('nan')` poorly -- the Coder Agent must convert NaN to None before serialisation).

### Cache expiry

Fundamentals change quarterly. Cache expiry is 45 days (not 24 hours like OHLCV).

```python
CACHE_EXPIRY_SECONDS: int = 45 * 86400  # 45 days in seconds
```

### Cache freshness check

```python
cache_is_fresh = (time.time() - os.path.getmtime(cache_path)) < CACHE_EXPIRY_SECONDS
```

### Cache hit behaviour

If a cache file exists and is fresh (< 45 days old):
1. Read the JSON file and deserialise into `_FundamentalsCache`
2. Compute `cache_age_days = (time.time() - os.path.getmtime(cache_path)) / 86400`
3. Return cached data with the computed `cache_age_days`
4. Set `data_quality` from the cached value (preserve original quality assessment)
5. Log at INFO: `"Cache hit for {symbol} fundamentals ({cache_age_days:.1f} days old)"`

### Cache miss behaviour

If no cache file exists or cache is expired:
1. Fetch from Screener.in (or yfinance fallback)
2. Write the result to the JSON cache file
3. Set `cache_age_days = 0.0` for freshly fetched data
4. Log at INFO: `"Cache miss for {symbol} fundamentals, fetching from {source}"`

### Stale cache handling (> 45 days)

When a cache file is older than 45 days AND `force_refresh=False`:
- Attempt a fresh fetch from Screener.in (then yfinance fallback if needed)
- If fresh fetch succeeds: use the fresh data, write new cache, set quality from fresh fetch
- If fresh fetch fails entirely (both sources): return the stale cached data with `data_quality = "fundamentals_stale"`
- Log at WARNING: `"Fundamentals cache expired for {symbol} ({age:.1f} days old)"`

This ensures the system degrades gracefully: stale data is returned (so the pipeline does not crash) but flagged so `quality_filter.py` can reject it.

### force_refresh behaviour

When `force_refresh=True`:
- Ignore all cache files for all symbols
- Fetch fresh data from Screener.in (then yfinance fallback)
- Write new cache files
- `cache_age_days = 0.0` for all symbols

---

## 7. 3-Strike Fallback Rule

Per data.md, strictly implemented:

### Strike counting

- Track consecutive Screener.in failures per symbol within a single call to `fetch_fundamentals`
- A "failure" is any of: non-200 HTTP response, network exception, HTML parsing failure where zero usable fields could be extracted
- Partial success (some fields extracted, others NaN) does NOT count as a failure
- The strike counter resets to 0 on any successful Screener.in fetch for that symbol

### Implementation

For each symbol, use a simple integer counter initialised to 0. On each Screener.in attempt:

```
attempt 1 fails -> strike_count = 1, retry
attempt 2 fails -> strike_count = 2, retry
attempt 3 fails -> strike_count = 3, trigger yfinance fallback
```

There are no delays between retry attempts beyond the standard 2-5 second delay already applied before each request.

### On fallback trigger

When strike_count reaches 3:
1. Log at WARNING: `"Screener.in failed 3 consecutive times for {symbol}, falling back to yfinance"`
2. Log the event type as `screener_fallback` (for future agent_logs integration)
3. Fetch from yfinance (see Section 8)
4. Set `data_source = "yfinance_fallback"` and `data_quality = "degraded"` on the result

### Strike counter scope

The strike counter is local to each `fetch_fundamentals` call. It does NOT persist across calls. Each invocation of `fetch_fundamentals` starts with a clean slate for every symbol.

---

## 8. yfinance Fallback

When Screener.in fails 3 times for a symbol, fetch from yfinance as the fallback source.

### What to fetch

```python
info = yf.Ticker(f"{symbol}.NS").info
```

Extract from the `info` dict:

| yfinance key | Maps to | Normalisation |
|---|---|---|
| `returnOnEquity` | `roe` | Already a decimal in yfinance (e.g. `0.185`). Use as-is. |
| `debtToEquity` | `debt_to_equity` | **Requires normalisation.** yfinance often returns this in percentage form (e.g. `45.0` meaning 0.45x). Apply heuristic: if `value > 10`, divide by 100. Rationale: a D/E ratio above 10x is extremely rare for Nifty 50 stocks; values that high almost certainly represent percentage form. |
| `trailingEps` | used for `eps_positive_4q` | **Limitation:** yfinance only provides trailing (annual) EPS, not per-quarter EPS. Set `eps_positive_4q = True` if `trailingEps > 0`, `False` otherwise. Document this as an approximation -- it cannot detect a single negative quarter within a positive trailing year. |
| `trailingPE` | `pe_ratio` | Use as-is. Already a ratio. |

### Handling missing keys

If any key is missing from the `info` dict or the value is `None`:
- Set the corresponding output field to `NaN`
- For `eps_positive_4q`: set to `False` if `trailingEps` is missing
- Do NOT raise -- partial data is acceptable from yfinance fallback

### yfinance failure

If `yf.Ticker().info` itself fails (network error, empty dict, exception):
- This symbol has now failed both sources
- Set all numeric fields to `NaN`, `eps_positive_4q = False`, `data_source = "failed"`, `data_quality = "failed"`
- Log at ERROR: `"Both Screener.in and yfinance failed for {symbol}"`
- Include the row in the output DataFrame (do not silently drop it)

---

## 9. Cross-Validation Rule

Per data.md, after a successful Screener.in fetch, cross-check P/E ratio against yfinance.

### When to run

- Only after a successful Screener.in fetch where `pe_ratio` is not NaN
- NOT run for yfinance fallback data (would be comparing yfinance to itself)
- NOT run for failed fetches

### Implementation

```python
yf_pe = yf.Ticker(f"{symbol}.NS").info.get("trailingPE")
```

If `yf_pe` is `None` or `NaN`:
- Skip cross-validation for this symbol
- Log at INFO: `"Cross-validation skipped for {symbol}: yfinance P/E unavailable"`
- Keep `data_quality = "clean"`

If `yf_pe` is available:
```python
deviation = abs(screener_pe - yf_pe) / max(screener_pe, yf_pe)
if deviation > 0.20:
    data_quality = "stale_data"
```

### On stale_data flag

- Set `data_quality = "stale_data"` on the returned row
- Log at WARNING: `"P/E cross-validation failed for {symbol}: Screener={screener_pe:.1f}, yfinance={yf_pe:.1f} -- flagged as stale_data"`
- Do NOT change `data_source` -- it remains `"screener"` because the data came from Screener.in
- Do NOT remove the row from the DataFrame -- return it with the flag so `quality_filter.py` can decide

### Cross-validation delay

The yfinance call for cross-validation does NOT require a 2-5 second delay (that delay is for Screener.in rate-limiting only). However, yfinance calls are lightweight and do not need throttling.

---

## 10. Public API

### 10.1 `fetch_fundamentals`

```python
def fetch_fundamentals(
    symbols: list[str],
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Fetch fundamental data for NSE stocks from Screener.in with yfinance fallback.

    Scrapes ROE, D/E, quarterly EPS, and P/E from Screener.in for each symbol.
    Caches results as JSON in data/cache/ with 45-day expiry. Falls back to
    yfinance after 3 consecutive Screener.in failures per symbol. Cross-validates
    P/E between sources when both are available.

    Args:
        symbols: List of NSE ticker symbols without .NS suffix.
                 E.g. ["RELIANCE", "TCS", "INFY"].
        force_refresh: If True, bypass cache for all symbols and fetch fresh data.

    Returns:
        pd.DataFrame with one row per symbol. Columns: symbol, roe, debt_to_equity,
        eps_positive_4q, pe_ratio, data_source, data_quality, cache_age_days,
        fetched_at_ist. Sorted by symbol ascending.
        Symbols that completely fail both sources are included with NaN values
        and data_source="failed".

    Raises:
        ValueError: If symbols list is empty.
    """
```

Processing order:
1. Validate `symbols` is non-empty (raise `ValueError` if empty)
2. Create `CACHE_DIR` if it does not exist
3. For each symbol sequentially:
   a. Check cache (unless `force_refresh=True`)
   b. On cache miss or expired: fetch from Screener.in (with 3-strike retry)
   c. On Screener.in 3-strike failure: fall back to yfinance
   d. On successful Screener.in fetch: run cross-validation
   e. Write to cache on any successful fetch
   f. On total failure: include row with NaN values
4. Concatenate all rows into a single DataFrame
5. Sort by `symbol` ascending
6. Reset index

This function NEVER raises on individual symbol failures. It always returns a DataFrame with one row per input symbol.

### 10.2 `get_cache_age_days`

```python
def get_cache_age_days(symbol: str) -> float | None:
    """Return the age of cached fundamentals data for a symbol in days.

    Args:
        symbol: NSE ticker symbol without .NS suffix.

    Returns:
        Age in days as float, or None if no cache file exists for this symbol.
    """
```

Implementation:
```python
cache_path = _cache_path(symbol)
if not os.path.exists(cache_path):
    return None
return (time.time() - os.path.getmtime(cache_path)) / 86400
```

---

## 11. Internal Dataclass

### `_FundamentalsCache`

```python
@dataclass(frozen=True)
class _FundamentalsCache:
    """Internal cache schema for fundamentals JSON files.

    All fields map directly to the output DataFrame columns plus
    cached_at_ist for cache provenance tracking. This dataclass is
    serialised to JSON on cache write and deserialised on cache read.
    """

    symbol: str
    roe: float | None          # None when NaN (JSON null)
    debt_to_equity: float | None
    eps_positive_4q: bool
    pe_ratio: float | None
    data_source: str           # "screener" | "yfinance_fallback" | "failed"
    data_quality: str          # "clean" | "degraded" | "stale_data" | "fundamentals_stale" | "failed"
    cached_at_ist: str         # ISO 8601 IST timestamp
```

### Serialisation

To JSON:
```python
# Convert NaN/None to None for JSON compatibility
cache_dict = {
    "symbol": cache.symbol,
    "roe": None if cache.roe is None or math.isnan(cache.roe) else cache.roe,
    "debt_to_equity": None if cache.debt_to_equity is None or math.isnan(cache.debt_to_equity) else cache.debt_to_equity,
    "eps_positive_4q": cache.eps_positive_4q,
    "pe_ratio": None if cache.pe_ratio is None or math.isnan(cache.pe_ratio) else cache.pe_ratio,
    "data_source": cache.data_source,
    "data_quality": cache.data_quality,
    "cached_at_ist": cache.cached_at_ist,
}
json.dumps(cache_dict, indent=2)
```

From JSON:
```python
data = json.loads(file_content)
# Convert JSON null back to None (which becomes NaN in DataFrame)
cache = _FundamentalsCache(
    symbol=data["symbol"],
    roe=data.get("roe"),  # None from JSON null
    debt_to_equity=data.get("debt_to_equity"),
    eps_positive_4q=data.get("eps_positive_4q", False),
    pe_ratio=data.get("pe_ratio"),
    data_source=data["data_source"],
    data_quality=data["data_quality"],
    cached_at_ist=data["cached_at_ist"],
)
```

---

## 12. Logging

Use Python's standard `logging` module. Same pattern as `fetcher.py` and `cleaner.py`.

### Logger setup

```python
import logging
from src.config.settings import settings

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, settings.log_level))

if not logging.root.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.DEBUG)
    logging.root.addHandler(_handler)
```

### Log messages by event

| Event | Level | Message format |
|---|---|---|
| Cache hit | INFO | `"Cache hit for {symbol} fundamentals ({cache_age_days:.1f} days old)"` |
| Cache miss | INFO | `"Cache miss for {symbol} fundamentals, fetching from {source}"` |
| Cache expired | WARNING | `"Fundamentals cache expired for {symbol} ({age:.1f} days old)"` |
| Screener.in fetch success | INFO | `"Fetched fundamentals from Screener.in for {symbol}"` |
| Screener.in fetch failure (per attempt) | WARNING | `"Screener.in failed for {symbol} (strike {n}/3): {error}"` |
| 3-strike fallback triggered | WARNING | `"Screener.in failed 3 consecutive times for {symbol}, falling back to yfinance"` |
| yfinance fallback used | WARNING | `"Using yfinance fallback for {symbol} fundamentals"` |
| yfinance fallback success | INFO | `"Fetched fundamentals from yfinance for {symbol}"` |
| Cross-validation passed | DEBUG | `"P/E cross-validation passed for {symbol}: Screener={s_pe:.1f}, yfinance={y_pe:.1f}"` |
| Cross-validation failed | WARNING | `"P/E cross-validation failed for {symbol}: Screener={screener_pe:.1f}, yfinance={yf_pe:.1f} -- flagged as stale_data"` |
| Cross-validation skipped | INFO | `"Cross-validation skipped for {symbol}: yfinance P/E unavailable"` |
| Field extraction failure | WARNING | `"Could not extract {field_name} from Screener.in for {symbol}"` |
| Complete failure (both sources) | ERROR | `"Both Screener.in and yfinance failed for {symbol}"` |
| Corrupt cache file | WARNING | `"Corrupt cache file for {symbol}, deleting and refetching"` |
| D/E normalisation applied | DEBUG | `"Normalised yfinance D/E for {symbol}: {raw} -> {normalised}"` |

---

## 13. Settings Integration

Import the settings singleton at module level:

```python
from src.config.settings import settings
```

Usage within this module:

| Settings field | Usage |
|---|---|
| `settings.log_level` | Set the module logger's level via `getattr(logging, settings.log_level)` |

The `settings.database_url` field is NOT used by this module. The cache directory is derived from `__file__` (same pattern as `fetcher.py`). This module does not write to or read from SQLite.

---

## 14. Error Handling

No bare `except` clauses anywhere in the module. Every `except` block catches specific exception types.

| Situation | Exceptions to catch | Action |
|---|---|---|
| Screener.in HTTP error | `requests.exceptions.RequestException`, `requests.exceptions.HTTPError`, `requests.exceptions.Timeout` | Increment strike counter, log WARNING, retry or fallback |
| Screener.in response with non-200 status | (check `response.status_code != 200` explicitly) | Increment strike counter, log WARNING |
| HTML parsing -- element not found | `AttributeError`, `ValueError` | Set field to NaN, log WARNING for specific field |
| HTML parsing -- value conversion failure | `ValueError` | Set field to NaN, log WARNING |
| yfinance info dict failure | `requests.exceptions.RequestException`, `KeyError`, `ValueError` | Set fields to NaN, log ERROR |
| yfinance missing key | `KeyError` | Set that field to NaN, do not raise |
| JSON cache read failure | `json.JSONDecodeError`, `OSError` | Delete corrupt file, log WARNING, treat as cache miss |
| JSON cache write failure | `OSError` | Log WARNING, do not raise -- data is still returned even if cache write fails |
| Empty symbols list | (check `len(symbols) == 0`) | Raise `ValueError("symbols list must not be empty")` |

---

## 15. Module-Level Constants

Define at module level. Domain constants, not configurable via environment variables.

```python
CACHE_DIR: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "cache",
)

CACHE_EXPIRY_SECONDS: int = 45 * 86400  # 45 days

SCREENER_BASE_URL: str = "https://www.screener.in/company"

SCREENER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SCREENER_TIMEOUT: int = 15  # seconds

MAX_STRIKES: int = 3  # 3-strike fallback threshold

PE_CROSS_VALIDATION_THRESHOLD: float = 0.20  # 20% deviation triggers stale_data flag

DE_NORMALISATION_THRESHOLD: float = 10.0  # yfinance D/E values above this are divided by 100

AGENT_NAME: str = "fundamentals"  # for future agent_logs integration
```

---

## 16. Internal Helper Functions

These are not public API but are documented for the Coder Agent's implementation clarity. All prefixed with `_`.

### `_cache_path`

```python
def _cache_path(symbol: str) -> str:
    """Return the absolute file path for a symbol's fundamentals cache JSON.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        Absolute path string, e.g. "/path/to/data/cache/RELIANCE_fundamentals.json".
    """
```

### `_read_cache`

```python
def _read_cache(symbol: str) -> _FundamentalsCache | None:
    """Read cached fundamentals for a symbol.

    Returns None if cache file does not exist, is expired (> 45 days),
    or is corrupt (invalid JSON). Corrupt files are deleted.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        _FundamentalsCache if cache is fresh and valid, None otherwise.
    """
```

Note on stale cache: this function returns `None` for expired cache (triggering a fresh fetch). If the fresh fetch also fails, the caller must separately read the stale cache file to return `fundamentals_stale` data. See `_read_stale_cache` below.

### `_read_stale_cache`

```python
def _read_stale_cache(symbol: str) -> _FundamentalsCache | None:
    """Read cached fundamentals regardless of expiry, for graceful degradation.

    Used when a fresh fetch fails and we need to return stale data with
    data_quality="fundamentals_stale" rather than crashing the pipeline.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        _FundamentalsCache with data_quality overridden to "fundamentals_stale",
        or None if no cache file exists at all.
    """
```

### `_write_cache`

```python
def _write_cache(cache: _FundamentalsCache) -> None:
    """Write fundamentals data to the JSON cache file.

    Creates the cache directory if it does not exist. Handles NaN-to-None
    conversion for JSON serialisation.

    Args:
        cache: The fundamentals data to cache.
    """
```

### `_scrape_screener`

```python
def _scrape_screener(symbol: str) -> _FundamentalsCache | None:
    """Scrape fundamental data from Screener.in for a single symbol.

    Makes one HTTP request (consolidated URL, then standalone fallback).
    Parses the HTML response for ROE, D/E, quarterly EPS, and P/E.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        _FundamentalsCache with data_source="screener", or None on failure.
        Partial data (some fields NaN) is returned as success, not failure.
    """
```

### `_fetch_yfinance_fundamentals`

```python
def _fetch_yfinance_fundamentals(symbol: str) -> _FundamentalsCache | None:
    """Fetch fundamental data from yfinance for a single symbol.

    Used as fallback when Screener.in fails 3 times.

    Args:
        symbol: NSE ticker symbol (without .NS suffix).

    Returns:
        _FundamentalsCache with data_source="yfinance_fallback" and
        data_quality="degraded", or None on failure.
    """
```

### `_cross_validate_pe`

```python
def _cross_validate_pe(symbol: str, screener_pe: float) -> str:
    """Cross-validate P/E ratio between Screener.in and yfinance.

    Args:
        symbol: NSE ticker symbol.
        screener_pe: P/E ratio from Screener.in.

    Returns:
        data_quality string: "clean" if validation passes or is skipped,
        "stale_data" if deviation > 20%.
    """
```

### `_cache_to_row`

```python
def _cache_to_row(cache: _FundamentalsCache, cache_age_days: float) -> dict[str, object]:
    """Convert a _FundamentalsCache to a dict suitable for DataFrame row construction.

    Args:
        cache: The fundamentals cache entry.
        cache_age_days: Age of cache in days (0.0 for fresh fetches).

    Returns:
        Dict with all output DataFrame column names as keys.
    """
```

### `_now_ist`

```python
def _now_ist() -> str:
    """Return current IST time as ISO 8601 string with timezone offset.

    Returns:
        String like "2026-03-22T22:15:30+05:30".
    """
```

---

## 17. Acceptance Criteria

The Tester Agent must verify all of the following before marking this module complete. Each criterion must map to at least one test.

### Cache behaviour

1. **Cache hit returns cached data without network call.** Mock `requests.get` and `yf.Ticker`. Write a valid JSON cache file with mtime < 45 days. Call `fetch_fundamentals`. Verify no network call was made. Verify returned DataFrame matches cached data.

2. **Cache older than 45 days triggers fresh fetch.** Write a cache file and set its mtime to 46 days ago (using `os.utime`). Mock Screener.in to return valid data. Call `fetch_fundamentals`. Verify Screener.in was called.

3. **Stale cache returned with fundamentals_stale when fresh fetch fails.** Write a cache file with mtime 46 days ago. Mock both Screener.in and yfinance to fail. Call `fetch_fundamentals`. Verify returned row has `data_quality = "fundamentals_stale"` and contains the stale cached values.

4. **force_refresh=True bypasses cache.** Write a fresh cache file. Call `fetch_fundamentals(force_refresh=True)`. Verify Screener.in was called.

5. **Corrupt JSON cache is deleted and refetched.** Write invalid JSON to the cache path. Call `fetch_fundamentals`. Verify the corrupt file was deleted and Screener.in was called.

6. **Cache directory is created if it does not exist.** Delete `data/cache/` directory. Mock Screener.in. Call `fetch_fundamentals`. Verify the directory was created.

7. **NaN values are serialised as null in JSON cache.** Fetch fundamentals for a symbol where ROE is NaN. Read the written cache file. Verify the `roe` field is `null` in JSON (not `NaN` string).

### 3-strike fallback

8. **3-strike fallback: Screener.in fails 3 times, yfinance is called.** Mock Screener.in to return HTTP 500 on all attempts. Mock yfinance to return valid data. Call `fetch_fundamentals`. Verify yfinance was called. Verify `data_source = "yfinance_fallback"`.

9. **yfinance fallback sets data_quality="degraded".** Same setup as criterion 8. Verify `data_quality = "degraded"` in the returned row.

10. **Strike counter resets per call.** Call `fetch_fundamentals` twice for the same symbol. Mock Screener.in to fail on the first call (all 3 strikes) and succeed on the second call. Verify the second call uses Screener.in data (not yfinance fallback).

11. **Partial Screener.in success does not count as a strike.** Mock Screener.in to return a page where ROE is extractable but D/E is missing. Verify `data_source = "screener"` (not fallback). Verify `debt_to_equity` is NaN.

### Cross-validation

12. **Cross-validation: P/E deviation > 20% sets data_quality="stale_data".** Mock Screener.in to return P/E = 30.0. Mock yfinance `trailingPE` = 20.0 (deviation = 33%). Verify `data_quality = "stale_data"`.

13. **Cross-validation: P/E deviation <= 20% keeps data_quality="clean".** Mock Screener.in P/E = 28.0 and yfinance P/E = 25.0 (deviation = 10.7%). Verify `data_quality = "clean"`.

14. **Cross-validation skipped when yfinance P/E unavailable.** Mock yfinance to return `info` dict without `trailingPE` key. Verify `data_quality = "clean"`. Verify INFO log about skipped cross-validation.

15. **Cross-validation not run for yfinance fallback data.** After 3-strike fallback to yfinance, verify no second yfinance call is made for cross-validation.

### Output contract

16. **Output DataFrame has all required columns.** Verify columns are exactly `["symbol", "roe", "debt_to_equity", "eps_positive_4q", "pe_ratio", "data_source", "data_quality", "cache_age_days", "fetched_at_ist"]`.

17. **roe column dtype is float64.** Verify `df["roe"].dtype == np.float64`.

18. **eps_positive_4q column dtype is bool.** Verify `df["eps_positive_4q"].dtype == bool`.

19. **Output DataFrame passes validator.py fundamentals contract.** Import `validator._validate_fundamentals_df` and pass the fetcher output to it. Verify no `ValueError` is raised.

20. **Output is sorted by symbol ascending.** Fetch for `["TCS", "RELIANCE", "INFY"]`. Verify output order is `["INFY", "RELIANCE", "TCS"]`.

### Complete failure

21. **Complete failure row has data_source="failed" and data_quality="failed".** Mock both Screener.in and yfinance to fail entirely. Verify the row is present with NaN numerics and `data_source = "failed"`.

22. **Complete failure does not raise -- row is included in output.** Same setup. Verify `fetch_fundamentals` returns a DataFrame (does not raise), and the failed symbol is present.

### yfinance normalisation

23. **yfinance D/E normalised: value > 10 divided by 100.** Mock yfinance `debtToEquity = 45.0`. Verify returned `debt_to_equity = 0.45`.

24. **yfinance D/E not normalised: value <= 10 used as-is.** Mock yfinance `debtToEquity = 0.8`. Verify returned `debt_to_equity = 0.8`.

### Request behaviour

25. **Delay between Screener.in requests.** Mock `time.sleep`. Call `fetch_fundamentals` for 2 symbols. Verify `time.sleep` was called at least twice with values between 2.0 and 5.0.

### get_cache_age_days

26. **Returns None when no cache exists.** Verify `get_cache_age_days("NONEXISTENT") is None`.

27. **Returns positive float when cache exists.** Write a cache file. Verify return value is >= 0.0.

### Error handling

28. **ValueError raised when symbols list is empty.** Call `fetch_fundamentals([])`. Verify `ValueError`.

29. **No bare except clauses in the module.** Grep the source file for bare `except:` -- verify zero matches.

### Code quality

30. **mypy passes with `--ignore-missing-imports`.** Run mypy on the file.

31. **ruff check passes.** Run ruff on the file.

---

## 18. Explicit Non-Goals

This module does NOT:

- Fetch OHLCV price data. That is `src/data/fetcher.py`.
- Calculate technical indicators (RSI, MACD, ATR). That is `src/indicators/technical.py` (Phase 2).
- Apply quality filter thresholds (ROE > 15%, D/E < 1.0). Those live in `src/strategy/quality_filter.py` (Phase 2). This module only fetches the raw values.
- Write to SQLite or any database. All caching is JSON-only in `data/cache/`.
- Send notifications via Telegram or Gmail.
- Validate data quality. It produces DataFrames; `src/data/validator.py` validates them.
- Clean or repair data. It returns what the sources provide.
- Handle authentication tokens or broker API credentials.
- Fetch data for non-NSE stocks.
- Scrape Screener.in in parallel or asynchronously. All requests are sequential with delays.
- Persist the strike counter across function calls.
- Create any custom exception class. All errors are handled via standard Python exceptions and the return DataFrame's `data_quality` column.
