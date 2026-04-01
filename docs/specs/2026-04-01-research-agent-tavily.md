# Spec: Research Agent -- Brave Search to Tavily Migration

**Date**: 2026-04-01
**Module**: `src/agents/research_agent.py` (modification, not new module)
**Phase**: 3 (in progress)
**Status**: Awaiting approval

---

## 1. Module Purpose

This is a migration spec, not a new module spec. The Research Agent already exists and is fully built. This spec describes the exact changes needed to replace the Brave Search HTTP integration with the Tavily Python SDK for news fetching. Everything else in the module -- Gemini synthesis, DB schema, completed_at race-condition guard, earnings branch logic, StockResearch/ResearchAgentResult dataclasses, error handling patterns, and the public API -- remains identical. The motivation is to move from raw HTTP calls against the Brave News API to the higher-level `tavily-python` SDK, which provides structured results with ISO date strings instead of ambiguous age strings.

---

## 2. Public API

**No changes.** The public API is identical to the original spec (docs/specs/2026-03-30-research-agent.md, Section 2). The function signature, dataclasses, and exception class remain exactly as they are. The only user-visible difference is that `ResearchAgentError` phase values may include `'tavily_search'` instead of `'brave_search'` when the API key is missing.

```python
def run_research_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> ResearchAgentResult:
    """Unchanged signature and docstring, except internal implementation
    now uses Tavily instead of Brave for news fetching."""
    ...
```

The docstring should be updated to say "Tavily Search" where it currently says "Brave Search". No other public interface changes.

---

## 3. Input Contract

**No changes.** Same as original spec Section 3, except:

- `settings.brave_api_key` is no longer checked. Instead, `settings.tavily_api_key` must be non-None. If None, raise `ResearchAgentError(message="TAVILY_API_KEY not configured", phase="tavily_search")`.
- `settings.gemini_api_key` and `settings.database_url` remain unchanged.

---

## 4. Output Contract

**No changes.** Same as original spec Section 4. DB schema, write order, NaN/None behaviour -- all identical.

---

## 5. Implementation Details

### 5.1 Settings changes (src/config/settings.py)

Add a new phase-gated field to the `Settings` dataclass:

```python
tavily_api_key: str | None  # phase-gated: None until Phase 3
```

Add `"tavily_api_key"` to `_SECRET_FIELDS`.

In `load_settings()`, add alongside the other phase-gated variables:

```python
tavily_api_key = _phase_gated("TAVILY_API_KEY")
```

Pass it to the `Settings(...)` constructor.

The `brave_api_key` field and its loading code remain in settings.py. It is not removed -- the user may still have `BRAVE_API_KEY` in their .env and removing the field would break settings loading. The field simply becomes unused by research_agent.py.

### 5.2 Import changes (research_agent.py)

**Remove:**
```python
import requests
```

**Add:**
```python
from tavily import TavilyClient
```

The `import requests` line can be removed because no other code in research_agent.py uses `requests`. The `requests` package itself stays in pyproject.toml because other modules (fundamentals.py scraper) use it.

### 5.3 Constants changes

**Remove these constants:**
```python
BRAVE_NEWS_ENDPOINT: str = "https://api.search.brave.com/res/v1/news/search"
BRAVE_REQUEST_DELAY: float = 1.1
BRAVE_TIMEOUT: int = 10
BRAVE_RESULTS_COUNT: int = 10
```

**Add these constants:**
```python
TAVILY_REQUEST_DELAY: float = 0.5   # courtesy delay between Tavily calls (seconds)
TAVILY_MAX_RESULTS: int = 10        # results per query
```

All other constants (GEMINI_*, VALID_SENTIMENTS, FALLBACK_*, EARNINGS_*, TRANSCRIPT_MIN_CHARS, MAX_SYMBOLS, SYMBOL_TO_COMPANY, _GEMINI_SYSTEM_PROMPT, _WAL_PRAGMAS, _CREATE_TABLE_SQL, _CREATE_INDEX_SQL) remain unchanged.

### 5.4 API key check at run start

Replace:
```python
if not settings.brave_api_key:
    ...
    raise ResearchAgentError(
        message="BRAVE_API_KEY not configured",
        phase="brave_search",
    )
```

With:
```python
if not settings.tavily_api_key:
    log_agent_action(
        agent_name=AGENT_NAME,
        action="TAVILY_API_KEY not configured",
        level="ERROR",
        result="error",
    )
    raise ResearchAgentError(
        message="TAVILY_API_KEY not configured",
        phase="tavily_search",
    )
```

### 5.5 Tavily client creation

After the API key check and before the stock processing loop, create a single TavilyClient instance:

```python
tavily_client = TavilyClient(api_key=settings.tavily_api_key)
```

This replaces the pattern of building HTTP headers for each request. The client is reused for all stocks within a single run, same as the Gemini client.

Pass `tavily_client` to `_research_one_stock()` instead of relying on the global settings for the API key.

### 5.6 Replace _brave_headers(), _brave_search(), _fetch_news_articles()

**Delete entirely:**
- `_brave_headers() -> dict[str, str]`
- `_brave_search(query, symbol, *, freshness, sleep_before) -> list[dict]`

**Replace `_fetch_news_articles()` with `_fetch_tavily_news()`:**

```python
def _fetch_tavily_news(
    symbol: str,
    tavily_client: TavilyClient,
) -> list[dict]:
    """Fetch and deduplicate news articles for a symbol via 3 Tavily queries.

    Queries:
      1. "{symbol} stock news India" -- topic="news", time_range="week"
      2. "{symbol} NSE quarterly results earnings" -- topic="finance", time_range="week"
      3. "{company_name} business outlook" -- topic="news", time_range="week"

    Args:
        symbol: NSE ticker symbol.
        tavily_client: Reusable TavilyClient instance.

    Returns:
        Deduplicated list of article dicts (keyed by 'url').
        Each dict has keys: title, url, content, score, published_date.
    """
```

Implementation:

```python
company_name = SYMBOL_TO_COMPANY.get(symbol, f"{symbol} company")

queries = [
    (f"{symbol} stock news India", "news"),
    (f"{symbol} NSE quarterly results earnings", "finance"),
    (f"{company_name} business outlook", "news"),
]

all_articles: list[dict] = []

for query, topic in queries:
    time.sleep(TAVILY_REQUEST_DELAY)
    try:
        results = tavily_client.search(
            query=query,
            topic=topic,
            time_range="week",
            max_results=TAVILY_MAX_RESULTS,
            include_answer=False,
            search_depth="basic",
        )
        articles = results.get("results", [])
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"tavily_search query={query!r} results={len(articles)}",
            level="DEBUG",
            symbol=symbol,
            result="ok",
        )
        all_articles.extend(articles)
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"tavily_search failed: {exc}",
            level="WARNING",
            symbol=symbol,
            result="error",
        )
        # Continue with empty results for this query

# Deduplicate by URL
seen_urls: set[str] = set()
unique_articles: list[dict] = []
for article in all_articles:
    url = article.get("url", "")
    if url and url not in seen_urls:
        seen_urls.add(url)
        unique_articles.append(article)

return unique_articles
```

Key differences from Brave:
- `time.sleep(TAVILY_REQUEST_DELAY)` before each call (0.5s vs 1.1s)
- Single SDK method call instead of raw HTTP
- Catch generic `Exception` (Tavily SDK does not document specific exception types)
- Query 1 freshness was "pd" (past day) in Brave; now "week" for all three queries because Tavily's `time_range` only supports "day", "week", "month", "year" and "week" is the closest equivalent to ensure sufficient coverage
- Query 2 text slightly changed: `"{symbol} NSE quarterly results earnings"` (was `"{symbol} NSE earnings quarterly results"` -- reordered for Tavily relevance)

### 5.7 Replace _parse_age_to_days(), _is_within_days(), _detect_earnings()

**Delete entirely:**
- `_parse_age_to_days(age_str: str) -> float | None`
- `_is_within_days(age_str: str, max_days: int) -> bool`

**Replace `_detect_earnings()` with a version that uses `published_date`:**

```python
def _detect_earnings(articles: list[dict]) -> bool:
    """Scan articles for earnings keywords in recent articles (within 5 days).

    Uses published_date (ISO date string from Tavily) instead of age strings.
    Articles with missing or unparseable published_date are treated as not recent.

    Args:
        articles: List of article dicts from Tavily, each with 'title',
                  'content', and 'published_date' fields.

    Returns:
        True if at least one article within EARNINGS_AGE_LIMIT_DAYS days
        contains an earnings keyword.
    """
    today = datetime.date.today()

    for article in articles:
        title = article.get("title", "") or ""
        content = article.get("content", "") or ""
        published_date_str = article.get("published_date", "") or ""

        # Parse published_date
        if not published_date_str:
            continue

        try:
            pub_date = datetime.datetime.strptime(
                published_date_str[:10], "%Y-%m-%d"
            ).date()
        except (ValueError, TypeError):
            continue

        age_days = (today - pub_date).days
        if age_days > EARNINGS_AGE_LIMIT_DAYS:
            continue

        combined_text = (title + " " + content).lower()
        for keyword in EARNINGS_KEYWORDS:
            if keyword.lower() in combined_text:
                return True

    return False
```

Key differences:
- Uses `published_date` (ISO date string) instead of Brave's `age` field
- Parses only the first 10 characters (`published_date[:10]`) to handle both `"2026-03-30"` and `"2026-03-30T14:22:00"` formats
- Uses `content` field instead of `description` field (Tavily returns `content`, not `description`)
- Missing or unparseable `published_date` causes the article to be skipped (safe default -- not treated as recent)

### 5.8 Replace _fetch_transcript()

```python
def _fetch_transcript(
    symbol: str,
    tavily_client: TavilyClient,
) -> str:
    """Fetch earnings transcript content via a 4th Tavily query.

    Args:
        symbol: NSE ticker symbol.
        tavily_client: Reusable TavilyClient instance.

    Returns:
        Concatenated content text from transcript search results.
    """
    time.sleep(TAVILY_REQUEST_DELAY)
    query = f"{symbol} earnings call transcript analyst"
    try:
        results = tavily_client.search(
            query=query,
            topic="finance",
            time_range="week",
            max_results=TAVILY_MAX_RESULTS,
            include_answer=False,
            search_depth="basic",
        )
        articles = results.get("results", [])
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"tavily_transcript_search failed: {exc}",
            level="WARNING",
            symbol=symbol,
            result="error",
        )
        return ""

    contents = [article.get("content", "") or "" for article in articles]
    return " ".join(contents)
```

Changes: accepts `tavily_client` parameter, uses `content` instead of `description`, uses `topic="finance"`.

### 5.9 Update _format_articles_for_prompt()

The article dict keys change from Brave's format to Tavily's format:

```python
def _format_articles_for_prompt(articles: list[dict]) -> str:
    """Format a list of article dicts into a string for the Gemini prompt.

    Args:
        articles: List of article dicts with 'title', 'url', 'content' keys.

    Returns:
        Formatted multi-line string, or 'No recent news articles found.' if empty.
    """
    if not articles:
        return "No recent news articles found."

    lines: list[str] = []
    for article in articles:
        title = article.get("title", "") or ""
        url = article.get("url", "") or ""
        content = article.get("content", "") or ""
        lines.append(f"Title: {title}\nSource: {url}\nSummary: {content}\n---")

    return "\n".join(lines)
```

Only change: `description` key becomes `content` key.

### 5.10 Update _research_one_stock() signature

Add `tavily_client` parameter:

```python
def _research_one_stock(
    symbol: str,
    run_date: datetime.date,
    conn: sqlite3.Connection,
    gemini_client: genai.Client,
    tavily_client: TavilyClient,
) -> StockResearch | None:
```

Internal calls change:
- `_fetch_news_articles(symbol)` becomes `_fetch_tavily_news(symbol, tavily_client)`
- `_fetch_transcript(symbol)` becomes `_fetch_transcript(symbol, tavily_client)`

All other logic in `_research_one_stock()` remains identical.

### 5.11 Update run_research_agent() internals

In the stock processing loop, pass `tavily_client` to `_research_one_stock()`:

```python
stock_result = _research_one_stock(
    symbol=symbol,
    run_date=run_date,
    conn=conn,
    gemini_client=gemini_client,
    tavily_client=tavily_client,
)
```

### 5.12 Source URL extraction

Tavily results use the `url` key, same as Brave. No change needed in how URLs flow through to Gemini or to `source_urls` in the DB. The Gemini prompt already receives URLs via `_format_articles_for_prompt()`.

### 5.13 Module docstring update

Update the module docstring to say "Tavily Search" instead of "Brave Search API".

---

## 6. Constants

**Full constant list after migration** (changed items marked):

```python
AGENT_NAME: str = "research_agent"

# Tavily Search  [CHANGED: was Brave Search API section]
TAVILY_REQUEST_DELAY: float = 0.5   # [NEW] courtesy delay between calls (seconds)
TAVILY_MAX_RESULTS: int = 10        # [NEW] results per query

# Gemini  [UNCHANGED]
GEMINI_MODEL: str = "gemini-2.5-flash-preview-04-17"
GEMINI_QUOTA_RETRY_DELAY: int = 60

# Sentiment  [UNCHANGED]
VALID_SENTIMENTS: frozenset[str] = frozenset({"Positive", "Negative", "Neutral", "Mixed"})
FALLBACK_SENTIMENT: str = "Neutral"
FALLBACK_CONFIDENCE: float = 0.3

# Earnings detection  [UNCHANGED]
EARNINGS_KEYWORDS: list[str] = [
    "Q1", "Q2", "Q3", "Q4", "quarterly", "results", "earnings", "profit"
]
EARNINGS_AGE_LIMIT_DAYS: int = 5
TRANSCRIPT_MIN_CHARS: int = 200

# Max symbols to process per run  [UNCHANGED]
MAX_SYMBOLS: int = 5

# Symbol-to-company mapping  [UNCHANGED]
SYMBOL_TO_COMPANY: dict[str, str] = { ... }  # same 50 entries
```

**Removed constants:** `BRAVE_NEWS_ENDPOINT`, `BRAVE_REQUEST_DELAY`, `BRAVE_TIMEOUT`, `BRAVE_RESULTS_COUNT`

---

## 7. Logging

Updated logging table (changes from Brave to Tavily marked):

| When | agent_name | action | level | symbol | result |
|------|------------|--------|-------|--------|--------|
| Run starts | `research_agent` | `"research_run_started for {run_date}"` | `INFO` | None | None |
| No screener results | `research_agent` | `"no screener_results for {run_date}"` | `INFO` | None | `"empty"` |
| Tavily request success | `research_agent` | `"tavily_search query={q} results={n}"` | `DEBUG` | `{symbol}` | `"ok"` | **[CHANGED]** |
| Tavily request failure | `research_agent` | `"tavily_search failed: {error}"` | `WARNING` | `{symbol}` | `"error"` | **[CHANGED]** |
| Tavily transcript failure | `research_agent` | `"tavily_transcript_search failed: {error}"` | `WARNING` | `{symbol}` | `"error"` | **[NEW]** |
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
| TAVILY_API_KEY missing | `research_agent` | `"TAVILY_API_KEY not configured"` | `ERROR` | None | `"error"` | **[CHANGED]** |

---

## 8. Error Handling

| Error condition | Exception | Behaviour |
|----------------|-----------|-----------|
| `settings.tavily_api_key` is None | `ResearchAgentError(phase='tavily_search')` | Raised immediately. **[CHANGED from brave_search]** |
| DB read failure (screener_results) | `ResearchAgentError(phase='db_read')` | Unchanged. |
| DB write failure (INSERT or UPDATE) | `ResearchAgentError(phase='db_write')` | Unchanged. |
| Tavily SDK exception per query | No exception raised | Catch `Exception`, log warning, that query returns 0 results, continue. **[CHANGED: was requests.RequestException]** |
| Gemini errors | No changes | All Gemini error handling identical to original spec. |
| `published_date` missing or unparseable | No exception raised | Article treated as not recent (safe default). **[NEW]** |

Never use bare `except:`. The Tavily SDK does not document specific exception types, so catch `Exception` (not bare `except`) for SDK calls and log the specific type/message. This is the same pattern already used for Gemini calls in the existing code.

---

## 9. Out of Scope

- This migration does NOT change the DB schema. The `research_reports` table is identical.
- This migration does NOT change the Gemini integration in any way.
- This migration does NOT change the dataclasses (StockResearch, ResearchAgentResult, ResearchAgentError).
- This migration does NOT change the completed_at two-step write pattern.
- This migration does NOT change the earnings branch logic, only the earnings detection mechanism (published_date instead of age string).
- This migration does NOT remove `BRAVE_API_KEY` from settings.py or from the user's .env file.
- This migration does NOT remove the `requests` package from pyproject.toml (other modules use it).
- This migration does NOT change `_synthesise_with_gemini()` or `_parse_gemini_response()`.
- This migration does NOT change `_resolve_db_path()`, `_open_connection()`, `_ensure_table()`, `_read_screener_results()`, `_insert_placeholder_row()`, or `_update_row()`.

---

## 10. Test Hints

All 10 scenarios below replace the original spec's test hints. Existing tests must be rewritten to mock Tavily instead of requests/Brave.

1. **Tavily client created with correct API key**: Mock `TavilyClient.__init__` and verify it receives `api_key=settings.tavily_api_key`. Run with `symbols=["TCS"]`.

2. **Three Tavily calls per stock with correct parameters**: Mock `TavilyClient.search`. For symbol "TCS", verify 3 calls with: query strings matching `"TCS stock news India"`, `"TCS NSE quarterly results earnings"`, `"Tata Consultancy Services business outlook"`; topic values `"news"`, `"finance"`, `"news"`; `time_range="week"` for all three; `max_results=10` for all three.

3. **published_date parsed correctly -- not earnings trigger**: Provide Tavily results with `published_date="2026-03-28"`. Set test date (via mocking `datetime.date.today()`) to `2026-04-01`. Age = 4 days. Article has earnings keyword "quarterly". Verify `_detect_earnings()` returns True (4 <= 5).

4. **published_date edge case -- exactly EARNINGS_AGE_LIMIT_DAYS**: Provide `published_date="2026-03-27"` with today=`2026-04-01`. Age = 5 days. Has earnings keyword. Verify earnings detected (5 <= 5). This confirms inclusive boundary.

5. **published_date missing -- treated as not recent**: Provide articles with earnings keywords but `published_date=""` or `published_date=None`. Verify `_detect_earnings()` returns False (article skipped, no crash).

6. **Tavily raises exception -- empty articles, continues to Gemini**: Mock `TavilyClient.search` to raise `Exception("API error")`. Verify `_fetch_tavily_news()` returns empty list. Verify Gemini still called with empty articles. Verify result uses fallback sentinel (Neutral/0.3) or Gemini's analysis of "No recent news articles found."

7. **0.5s sleep between Tavily calls**: Mock `time.sleep`. For 1 stock with no earnings (3 queries), verify `time.sleep(0.5)` called 3 times. For 1 stock with earnings (4 queries), verify 4 calls.

8. **symbols override bypasses screener_results DB read**: Pass `symbols=["TCS", "INFY"]`. Verify no SQL query to `screener_results`. Verify both symbols are researched.

9. **completed_at=NULL when Gemini fails (stock in skipped_symbols)**: Mock Gemini to raise non-quota exception on both attempts. Verify the DB row has `completed_at IS NULL`. Verify the symbol appears in `skipped_symbols`.

10. **source_urls extracted from Tavily result url field**: Mock Tavily to return results with `url` keys. Mock Gemini to return those URLs in `source_urls`. Verify `StockResearch.source_urls` contains the expected URLs and DB stores them as JSON.

---

## 11. File Locations

| File | Action |
|------|--------|
| `src/config/settings.py` | Modify: add `tavily_api_key` field and loading |
| `src/agents/research_agent.py` | Modify: replace Brave with Tavily (as described above) |
| `tests/agents/test_research_agent.py` | Modify: rewrite all mocks from requests/Brave to TavilyClient |
| `tests/config/test_settings.py` | Modify: add test for TAVILY_API_KEY loading |

No new files created. No files deleted.

---

## 12. pyproject.toml

**No changes needed.** `tavily-python>=0.7.23` is already present. `requests>=2.31.0` stays (used by other modules).

---

## 13. Summary of All Deleted Code

For clarity, these are the exact functions/constants to remove:

**Functions deleted:**
- `_brave_headers() -> dict[str, str]`
- `_brave_search(query, symbol, *, freshness, sleep_before) -> list[dict]`
- `_fetch_news_articles(symbol) -> list[dict]`  (replaced by `_fetch_tavily_news()`)
- `_parse_age_to_days(age_str) -> float | None`
- `_is_within_days(age_str, max_days) -> bool`

**Constants deleted:**
- `BRAVE_NEWS_ENDPOINT`
- `BRAVE_REQUEST_DELAY`
- `BRAVE_TIMEOUT`
- `BRAVE_RESULTS_COUNT`

**Imports removed:**
- `import requests`

---

## 14. Summary of All Added Code

**New imports:**
- `from tavily import TavilyClient`

**New constants:**
- `TAVILY_REQUEST_DELAY: float = 0.5`
- `TAVILY_MAX_RESULTS: int = 10`

**New/replaced functions:**
- `_fetch_tavily_news(symbol, tavily_client) -> list[dict]` (replaces `_fetch_news_articles`)
- `_detect_earnings(articles) -> bool` (rewritten to use `published_date`)
- `_fetch_transcript(symbol, tavily_client) -> str` (updated signature + SDK call)
- `_format_articles_for_prompt(articles) -> str` (updated to use `content` key)

**Modified functions (signature change only):**
- `_research_one_stock(...)` -- adds `tavily_client` parameter
- `run_research_agent(...)` -- creates TavilyClient, passes to _research_one_stock

**Settings addition:**
- `Settings.tavily_api_key: str | None`
- `_SECRET_FIELDS` gains `"tavily_api_key"`
- `load_settings()` gains `tavily_api_key = _phase_gated("TAVILY_API_KEY")`
