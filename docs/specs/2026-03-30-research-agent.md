# Spec: Research Agent

**Date**: 2026-03-30
**Module**: `src/agents/research_agent.py`
**Phase**: 3, Step 1
**Status**: Awaiting approval

---

## 1. Module Purpose

The Research Agent runs every evening at 22:40 IST as part of the trading pipeline's evening session. It takes the top 5 ranked candidates from the `screener_results` table, fetches recent news for each via Brave Search API, detects earnings events, and synthesises sentiment using Google Gemini 2.5 Flash. Results are written to the `research_reports` table with the `completed_at` column set last to prevent race conditions with the downstream Watchlist Builder Agent. The module is a pure Python function -- it does not use the Python Agent SDK or Claude directly; it calls Brave Search via HTTP and Gemini via the `google-genai` SDK.

---

## 2. Public API

```python
import datetime
from dataclasses import dataclass


class ResearchAgentError(Exception):
    """Raised when the Research Agent encounters a fatal error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed: 'db_read', 'brave_search', 'gemini', 'db_write'.
    """

    def __init__(self, message: str, phase: str) -> None: ...


@dataclass(frozen=True)
class StockResearch:
    """Research result for a single stock."""

    symbol: str
    sentiment: str  # Exactly one of: "Positive", "Negative", "Neutral", "Mixed"
    confidence: float  # 0.0 to 1.0
    source_urls: list[str]
    earnings_transcript_unavailable: bool
    completed_at: datetime.datetime  # IST timezone-aware


@dataclass(frozen=True)
class ResearchAgentResult:
    """Full output of run_research_agent()."""

    run_date: datetime.date
    stocks_researched: int
    results: list[StockResearch]
    skipped_symbols: list[str]  # symbols skipped due to errors
    completed_at: datetime.datetime  # IST timezone-aware, when full run finished


def run_research_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> ResearchAgentResult:
    """Run the research agent for the given date.

    Fetches screener_results for run_date (defaults to today IST),
    runs Brave Search + Gemini synthesis for each of top 5 symbols,
    writes results to research_reports table with completed_at set last.

    Args:
        run_date: Date to run for. Defaults to today in IST.
        symbols: Override -- use these symbols instead of reading screener_results.
                 Used in testing. If provided, screener_results table is not read.

    Returns:
        ResearchAgentResult with per-stock results and run metadata.

    Raises:
        ResearchAgentError: If DB write fails (phase='db_write'),
                            or if DB read fails (phase='db_read').
                            Brave/Gemini failures are handled gracefully
                            per-stock and do not raise.
    """
    ...
```

---

## 3. Input Contract

### From `screener_results` table (when `symbols` parameter is None)

The module reads rows where:
- `screened_at` date portion matches `run_date` (compare `screened_at LIKE '{run_date}%'`)
- `quality_passed = 1`
- `rank IS NOT NULL`
- Ordered by `rank ASC`, limited to top 5

Required columns read: `symbol`, `rank`.

If no rows match, the function returns `ResearchAgentResult` with `stocks_researched=0` and empty `results` list. This is not an error.

### From `symbols` parameter (testing override)

When `symbols` is provided and non-empty, each string must be a valid NSE ticker symbol. No DB read from `screener_results` occurs. The symbols list is used directly (up to first 5 entries; additional entries silently ignored).

### Environment dependencies

- `settings.brave_api_key`: Must be non-None. If None, raise `ResearchAgentError(message="BRAVE_API_KEY not configured", phase="brave_search")`.
- `settings.gemini_api_key`: Always non-None (required field in settings.py).
- `settings.database_url`: Used to resolve SQLite path for DB reads/writes.

---

## 4. Output Contract

### Return value: `ResearchAgentResult`

| Field | Type | Guarantee |
|-------|------|-----------|
| `run_date` | `datetime.date` | Always the effective run date (IST today or provided) |
| `stocks_researched` | `int` | Count of stocks with successful research (completed_at written) |
| `results` | `list[StockResearch]` | One entry per successfully researched stock; may be empty |
| `skipped_symbols` | `list[str]` | Symbols where Gemini failed and no fallback was possible; completed_at NOT written for these |
| `completed_at` | `datetime.datetime` | IST timezone-aware timestamp when the full run finished |

### `StockResearch` fields

| Field | Type | Guarantee |
|-------|------|-----------|
| `sentiment` | `str` | Exactly one of: `"Positive"`, `"Negative"`, `"Neutral"`, `"Mixed"` |
| `confidence` | `float` | Between 0.0 and 1.0 inclusive. Clamped if Gemini returns out-of-range. |
| `source_urls` | `list[str]` | May be empty list if Brave returned no results. Never None. |
| `earnings_transcript_unavailable` | `bool` | True only when earnings detected AND transcript query returned <200 chars |
| `completed_at` | `datetime.datetime` | IST timezone-aware. Set only after all fields are populated. |

### Database writes: `research_reports` table

One row per stock. The `completed_at` column is written via a separate UPDATE after the initial INSERT, ensuring it is the last field populated.

Write order per stock:
1. INSERT row with `completed_at = NULL`
2. Perform all Brave Search + Gemini work
3. UPDATE the row to set all result fields + `completed_at` = current IST timestamp

If Gemini fails for a stock (after retry), the row remains with `completed_at = NULL`. The Watchlist Builder will not read it.

### NaN / None behaviour

- `confidence` is never NaN. If Gemini returns a non-numeric value, fallback to 0.3.
- `source_urls` is never None. Empty list `[]` if no URLs found.
- `sentiment` is never None or empty. Defaults to `"Neutral"` on any parse failure.

---

## 5. Implementation Details

### 5.1 Database path resolution

Extract SQLite file path from `settings.database_url` by stripping the `sqlite:///` prefix. If the path is relative, resolve it relative to the project root (same logic as `logger.py`).

### 5.2 Table creation

On first call, execute `CREATE TABLE IF NOT EXISTS research_reports (...)` with the schema defined in Section 5.8. Also create the index. This is idempotent.

### 5.3 Reading screener results

```sql
SELECT symbol, rank
FROM screener_results
WHERE screened_at LIKE ? || '%'
  AND quality_passed = 1
  AND rank IS NOT NULL
ORDER BY rank ASC
LIMIT 5
```

Parameter: `run_date.isoformat()` (e.g. `"2026-03-30"`)

### 5.4 Brave Search news fetching

For each stock, make 3 HTTP GET requests to `https://api.search.brave.com/res/v1/news/search`:

**Headers:**
- `X-Subscription-Token`: `settings.brave_api_key`
- `Accept`: `application/json`
- `Accept-Encoding`: `gzip`

**Query 1:** `q="{symbol} stock news India"`, `count=10`, `freshness=pd`
**Query 2:** `q="{symbol} NSE earnings quarterly results"`, `count=10`, `freshness=pw`
**Query 3:** `q="{company_name} business outlook"`, `count=10`, `freshness=pw`

For `company_name`, use the `SYMBOL_TO_COMPANY` dict (Section 6). For symbols not in the dict, use `"{symbol} company"`.

**Rate limiting:** Sleep 1.1 seconds between every Brave request (across all stocks, not just within one stock). Use `time.sleep(1.1)`.

**Response parsing:** Extract `results` array from JSON. Each result has `title`, `description`, `url`, `age`. Collect all results from all 3 queries into a single list per stock. Deduplicate by URL.

**Error handling per request:** If HTTP status is not 200, log a warning via `log_agent_action()` and continue. That query returns zero results. If all 3 queries fail, the stock proceeds to Gemini with an empty articles list (Gemini will return Neutral/0.3).

**Timeout:** 10 seconds per request via `requests.get(..., timeout=10)`.

### 5.5 Earnings detection

After collecting all Brave results for a stock, scan the combined results for earnings keywords in `title` or `description`:

Keywords: `["Q1", "Q2", "Q3", "Q4", "quarterly", "results", "earnings", "profit"]`

Match rule: case-insensitive word boundary match is not required -- simple `keyword.lower() in (title + " " + description).lower()` suffices.

Age check: the `age` field from Brave is a human-readable string like "2 days ago". Parse it to check if the article is within 5 days. Parsing rules:
- Contains "hour" or "minute" -> within 5 days (True)
- Contains "day" -> extract the integer, check <= 5
- Contains "week" -> extract integer, 1 week = 7 days, check <= 5 (so only "0 weeks" would pass, which does not occur -- effectively any "week" result is >5 days)
- Otherwise -> assume >5 days (False)

If at least one result has an earnings keyword AND is within 5 days -> earnings event detected.

### 5.6 Earnings transcript branch

When earnings detected, make one additional Brave request:
- Query: `"{symbol} earnings call transcript analyst"`
- `count=10`, `freshness=pw`
- Same headers and rate limiting as above

Aggregate the `description` fields of the results. If total character count > 200, treat this as transcript content and use it for Gemini synthesis INSTEAD OF the standard news articles. If <= 200 chars, set `earnings_transcript_unavailable = True` and fall back to the standard news articles collected in step 5.4.

### 5.7 Gemini synthesis

**SDK setup:**
```python
from google import genai
from google.genai import types as genai_types

client = genai.Client(api_key=settings.gemini_api_key)
```

The model object should be created once per `run_research_agent()` call and reused for all stocks.

**System instruction** (passed via `GenerateContentConfig(system_instruction=...)`):

```
You are a financial news analyst specialising in Indian equities. You will be given recent news articles about an NSE-listed stock. Analyse the articles and provide: 1) Overall sentiment (exactly one of: Positive, Negative, Neutral, Mixed), 2) Confidence score (float 0.0 to 1.0), 3) Source URLs list. Be conservative -- default to Neutral when uncertain. Mixed means genuinely contradictory signals. Never guess sentiment from the company name alone.
```

**User prompt per stock:**

```
Stock: {symbol}
News articles (last 48 hours):
{formatted_articles}

Return JSON only, no markdown formatting:
{"sentiment": "Positive|Negative|Neutral|Mixed", "confidence": 0.0-1.0, "source_urls": ["url1", "url2"]}
```

Where `formatted_articles` is each article formatted as:
```
Title: {title}
Source: {url}
Summary: {description}
---
```

If no articles available, `formatted_articles` = "No recent news articles found."

**Response parsing:**
1. Strip any markdown code fences (```json ... ```) from the response text
2. Parse as JSON
3. Validate `sentiment` is one of the 4 allowed values
4. Validate `confidence` is a float between 0.0 and 1.0
5. Validate `source_urls` is a list of strings

**Retry on invalid JSON:**
If JSON parsing fails, send one retry prompt to the same model:
```
Your previous response was not valid JSON. Return ONLY a JSON object with these exact keys: sentiment, confidence, source_urls. No other text.
```
If the retry also fails, use the fallback sentinel values.

**Retry on quota error (HTTP 429 / ResourceExhausted):**
Sleep 60 seconds and retry once. If still fails, use fallback sentinel values.

**Fallback sentinel values:**
- `sentiment = "Neutral"`
- `confidence = 0.3`
- `source_urls = []` (empty list)

These fallback values are conservative -- they will not block trades (Neutral passes the combined decision rule) but the low confidence ensures the stock is ranked lower by the Watchlist Builder.

**Gemini total failure for a stock:**
If Gemini raises an exception that is not a quota error (e.g. API key invalid, network error) and the retry also fails, the stock is added to `skipped_symbols`. The DB row remains with `completed_at = NULL`. Log the error.

### 5.8 Database schema

```sql
CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_urls TEXT NOT NULL,
    earnings_transcript_unavailable INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    raw_response TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date
    ON research_reports(symbol, run_date);
```

### 5.9 DB write sequence per stock

1. **INSERT** with placeholder values:
   ```sql
   INSERT INTO research_reports
       (symbol, run_date, sentiment, confidence, source_urls,
        earnings_transcript_unavailable, completed_at, raw_response, created_at)
   VALUES (?, ?, 'Neutral', 0.0, '[]', 0, NULL, NULL, ?)
   ```
   Parameters: `(symbol, run_date_iso, created_at_iso)`
   Store the returned `lastrowid`.

2. **Perform Brave Search + Gemini work** (may take up to 2 minutes).

3. **UPDATE** with actual results:
   ```sql
   UPDATE research_reports
   SET sentiment = ?,
       confidence = ?,
       source_urls = ?,
       earnings_transcript_unavailable = ?,
       raw_response = ?,
       completed_at = ?
   WHERE id = ?
   ```

If the UPDATE fails, raise `ResearchAgentError(phase='db_write')`.

If Gemini fails completely (stock goes to `skipped_symbols`), do NOT update the row. The row remains with `completed_at = NULL`, `sentiment = 'Neutral'`, `confidence = 0.0`.

### 5.10 Processing order

Stocks are processed sequentially (not in parallel). This is deliberate:
- Brave free tier rate limit is 1 req/s -- parallelism would not help
- Sequential processing makes logging and error recovery straightforward
- Total time for 5 stocks is approximately 10 minutes, which is acceptable for the 22:40 IST evening window

---

## 6. Constants

```python
AGENT_NAME: str = "research_agent"

# Brave Search API
BRAVE_NEWS_ENDPOINT: str = "https://api.search.brave.com/res/v1/news/search"
BRAVE_REQUEST_DELAY: float = 1.1  # seconds between requests (free tier: 1 req/s)
BRAVE_TIMEOUT: int = 10  # seconds per HTTP request
BRAVE_RESULTS_COUNT: int = 10  # results per query

# Gemini
GEMINI_MODEL: str = "gemini-2.5-flash-preview-04-17"
GEMINI_QUOTA_RETRY_DELAY: int = 60  # seconds to wait on 429

# Sentiment
VALID_SENTIMENTS: frozenset[str] = frozenset({"Positive", "Negative", "Neutral", "Mixed"})
FALLBACK_SENTIMENT: str = "Neutral"
FALLBACK_CONFIDENCE: float = 0.3

# Earnings detection
EARNINGS_KEYWORDS: list[str] = [
    "Q1", "Q2", "Q3", "Q4", "quarterly", "results", "earnings", "profit"
]
EARNINGS_AGE_LIMIT_DAYS: int = 5
TRANSCRIPT_MIN_CHARS: int = 200

# Symbol-to-company mapping (top Nifty 50 stocks)
SYMBOL_TO_COMPANY: dict[str, str] = {
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "HDFCBANK": "HDFC Bank",
    "INFY": "Infosys",
    "ICICIBANK": "ICICI Bank",
    "HINDUNILVR": "Hindustan Unilever",
    "ITC": "ITC Limited",
    "SBIN": "State Bank of India",
    "BHARTIARTL": "Bharti Airtel",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "LT": "Larsen and Toubro",
    "AXISBANK": "Axis Bank",
    "ASIANPAINT": "Asian Paints",
    "MARUTI": "Maruti Suzuki",
    "HCLTECH": "HCL Technologies",
    "SUNPHARMA": "Sun Pharmaceutical",
    "TITAN": "Titan Company",
    "BAJFINANCE": "Bajaj Finance",
    "WIPRO": "Wipro",
    "ULTRACEMCO": "UltraTech Cement",
    "NESTLEIND": "Nestle India",
    "NTPC": "NTPC Limited",
    "POWERGRID": "Power Grid Corporation",
    "M&M": "Mahindra and Mahindra",
    "TATAMOTORS": "Tata Motors",
    "TATASTEEL": "Tata Steel",
    "ONGC": "Oil and Natural Gas Corporation",
    "JSWSTEEL": "JSW Steel",
    "ADANIENT": "Adani Enterprises",
    "ADANIPORTS": "Adani Ports",
    "TECHM": "Tech Mahindra",
    "BAJAJFINSV": "Bajaj Finserv",
    "INDUSINDBK": "IndusInd Bank",
    "COALINDIA": "Coal India",
    "BPCL": "Bharat Petroleum",
    "GRASIM": "Grasim Industries",
    "DIVISLAB": "Divi's Laboratories",
    "DRREDDY": "Dr Reddy's Laboratories",
    "BRITANNIA": "Britannia Industries",
    "CIPLA": "Cipla",
    "EICHERMOT": "Eicher Motors",
    "HEROMOTOCO": "Hero MotoCorp",
    "APOLLOHOSP": "Apollo Hospitals",
    "SBILIFE": "SBI Life Insurance",
    "HDFCLIFE": "HDFC Life Insurance",
    "TATACONSUM": "Tata Consumer Products",
    "HINDALCO": "Hindalco Industries",
    "BAJAJ-AUTO": "Bajaj Auto",
    "LTIM": "LTIMindtree",
    "SHRIRAMFIN": "Shriram Finance",
}

# Max symbols to process per run
MAX_SYMBOLS: int = 5
```

---

## 7. Logging

All logging via `log_agent_action()` from `src/utils/logger.py`.

| When | agent_name | action | level | symbol | result |
|------|------------|--------|-------|--------|--------|
| Run starts | `research_agent` | `"research_run_started for {run_date}"` | `INFO` | None | None |
| No screener results | `research_agent` | `"no screener_results for {run_date}"` | `INFO` | None | `"empty"` |
| Brave request success | `research_agent` | `"brave_search query={q} results={n}"` | `DEBUG` | `{symbol}` | `"ok"` |
| Brave request failure | `research_agent` | `"brave_search failed: {status_code}"` | `WARNING` | `{symbol}` | `"error"` |
| Earnings detected | `research_agent` | `"earnings_event_detected"` | `INFO` | `{symbol}` | `"ok"` |
| Transcript unavailable | `research_agent` | `"earnings_transcript_unavailable"` | `WARNING` | `{symbol}` | `"fallback"` |
| Gemini success | `research_agent` | `"gemini_synthesis sentiment={s} confidence={c}"` | `INFO` | `{symbol}` | `"ok"` |
| Gemini invalid JSON | `research_agent` | `"gemini_invalid_json, retrying"` | `WARNING` | `{symbol}` | `"retry"` |
| Gemini quota error | `research_agent` | `"gemini_quota_429, sleeping 60s"` | `WARNING` | `{symbol}` | `"retry"` |
| Gemini total failure | `research_agent` | `"gemini_failed: {error}"` | `ERROR` | `{symbol}` | `"error"` |
| Gemini fallback used | `research_agent` | `"gemini_fallback_sentinel used"` | `WARNING` | `{symbol}` | `"fallback"` |
| DB row inserted | `research_agent` | `"research_report row inserted id={id}"` | `DEBUG` | `{symbol}` | `"ok"` |
| DB completed_at written | `research_agent` | `"research_report completed id={id}"` | `INFO` | `{symbol}` | `"ok"` |
| Stock skipped | `research_agent` | `"stock_skipped: {reason}"` | `WARNING` | `{symbol}` | `"skipped"` |
| Run completed | `research_agent` | `"research_run_completed: {n} stocks, {s} skipped"` | `INFO` | None | `"ok"` |
| BRAVE_API_KEY missing | `research_agent` | `"BRAVE_API_KEY not configured"` | `ERROR` | None | `"error"` |

---

## 8. Error Handling

| Error condition | Exception | Behaviour |
|----------------|-----------|-----------|
| `settings.brave_api_key` is None | `ResearchAgentError(phase='brave_search')` | Raised immediately. Cannot proceed without Brave. |
| DB read failure (screener_results) | `ResearchAgentError(phase='db_read')` | Raised. Cannot determine which stocks to research. |
| DB write failure (INSERT or UPDATE) | `ResearchAgentError(phase='db_write')` | Raised. Data integrity compromised. |
| Brave HTTP error (non-200) per request | No exception raised | Log warning, that query returns 0 results, continue. |
| Brave network timeout | No exception raised | Caught `requests.RequestException`, log warning, continue. |
| Gemini invalid JSON (first attempt) | No exception raised | Retry once with explicit JSON instruction. |
| Gemini invalid JSON (second attempt) | No exception raised | Use fallback sentinel, log warning. |
| Gemini quota error (429) | No exception raised | Sleep 60s, retry once. If still fails, use fallback sentinel. |
| Gemini other API error | No exception raised | Stock added to `skipped_symbols`, row stays with `completed_at=NULL`. |
| Gemini confidence out of range | No exception raised | Clamped to `[0.0, 1.0]`. |
| Gemini sentiment not in valid set | No exception raised | Treated same as invalid JSON -- retry then fallback. |
| No `src/agents/__init__.py` | N/A | Coder must create this file. |

Never use bare `except:`. Always catch specific exceptions:
- `requests.RequestException` for Brave HTTP errors
- `json.JSONDecodeError` for JSON parsing
- `sqlite3.Error` for DB operations
- `google.genai` exceptions: catch `Exception` from `client.models.generate_content()` but log the specific type

---

## 9. Out of Scope

- This module does NOT call the Python Agent SDK or Claude API. It is a plain Python function.
- This module does NOT implement the Watchlist Builder logic. It only writes to `research_reports`.
- This module does NOT send notifications (Telegram/Gmail). The orchestrator handles notifications.
- This module does NOT create or manage the `screener_results` table. It reads from it only.
- Prompt caching uses `cache_control` with `type="ephemeral"` on the system instruction via `genai_types.GenerateContentConfig`. The `client` instance is created once and reused for all 5 stock calls within a run.
- This module does NOT handle concurrent/parallel execution. Stocks are processed sequentially.
- This module does NOT validate that Brave Search URLs are genuinely independent sources (that is a manual spot-check during Phase 5).

---

## 10. Test Hints

The Tester Agent must cover at minimum these 10 scenarios:

1. **Valid sentiment values only**: Mock Gemini to return `"Bullish"` (invalid). Verify the module retries once, then falls back to `"Neutral"` / `0.3`. The DB row must have `sentiment="Neutral"`.

2. **completed_at is NULL on Gemini failure**: Mock Gemini to raise an exception on both attempts. Verify the DB row has `completed_at IS NULL`. Verify the symbol appears in `skipped_symbols`.

3. **Earnings detection triggers transcript branch**: Provide Brave results with title containing `"TCS Q3 results quarterly profit"` and age `"3 days ago"`. Verify the earnings transcript query is made (4th Brave call for that stock).

4. **Earnings transcript unavailable fallback**: Mock transcript query to return results with <200 total chars of description. Verify `earnings_transcript_unavailable=True` in both DB and `StockResearch` result. Verify standard news articles are used for Gemini synthesis.

5. **Rate limiting delay between Brave calls**: Mock `time.sleep` and verify it is called with `1.1` between every Brave request. For 1 stock with no earnings: exactly 2 sleeps (between queries 1-2 and 2-3). For 1 stock with earnings: 3 sleeps (queries 1-2, 2-3, 3-4).

6. **Gemini invalid JSON retry then fallback**: First `generate_content` returns `"I think positive"` (not JSON). Second returns `"still not json"`. Verify result uses `sentiment="Neutral"`, `confidence=0.3`, `source_urls=[]`.

7. **symbols override bypasses screener_results**: Pass `symbols=["TCS", "INFY"]`. Verify no SQL query to `screener_results`. Verify both symbols are researched.

8. **Empty screener results**: Create `screener_results` table with no rows for run_date. Verify `stocks_researched=0`, `results=[]`, no error raised.

9. **DB write order -- completed_at is last**: After INSERT and before UPDATE, verify the row has `completed_at IS NULL`. After UPDATE, verify `completed_at IS NOT NULL`. (Use a mock or spy on the DB connection to observe query order.)

10. **source_urls JSON round-trip**: Gemini returns `{"sentiment": "Positive", "confidence": 0.8, "source_urls": ["https://a.com", "https://b.com"]}`. Verify DB stores `'["https://a.com", "https://b.com"]'` (JSON string). Verify `StockResearch.source_urls` is `["https://a.com", "https://b.com"]` (Python list).

11. **Brave API key missing**: Set `settings.brave_api_key` to None. Verify `ResearchAgentError` is raised with `phase='brave_search'`.

12. **Gemini quota retry**: Mock first `generate_content` to raise a 429/ResourceExhausted error. Mock `time.sleep`. Verify sleep(60) is called. Mock second call to succeed. Verify the stock gets the correct result.

13. **Confidence clamping**: Mock Gemini to return `confidence: 1.5`. Verify the stored and returned confidence is `1.0`.

14. **MAX_SYMBOLS cap**: Pass `symbols=["A", "B", "C", "D", "E", "F", "G"]`. Verify only 5 are processed.

---

## 11. File Locations

| File | Action |
|------|--------|
| `src/agents/__init__.py` | Create (empty file, package marker) |
| `src/agents/research_agent.py` | Create (main module) |
| `tests/agents/__init__.py` | Create (empty file, package marker) |
| `tests/agents/test_research_agent.py` | Create (tests) |

---

## 12. pyproject.toml

Add one new dependency:

```toml
"google-genai>=1.0.0",
```

This is the new unified Google GenAI SDK (replaces the deprecated `google-generativeai`). Version `>=1.0.0` is required for the `google.genai.Client` interface and `GenerateContentConfig` used for the `gemini-2.5-flash-preview-04-17` model. Already installed at 1.69.0.

No other new dependencies required. `requests` is already present for Brave HTTP calls.
