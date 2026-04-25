"""Research Agent for the Indian Trader evening pipeline.

Runs every evening at 22:40 IST. Reads the top 5 screener candidates,
fetches recent news for each via Tavily Search, detects earnings events,
and synthesises sentiment using Google Gemini 2.5 Flash. Results are
written to the research_reports table with the completed_at column set
last to prevent race conditions with the downstream Watchlist Builder.

This module is a plain Python function. It does NOT use the Python Agent
SDK or Claude API.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types as genai_types
from tavily import TavilyClient

from src.config.settings import settings
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Timezone constant
# ---------------------------------------------------------------------------

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
AGENT_NAME: str = "research_agent"

# Tavily Search
TAVILY_REQUEST_DELAY: float = 0.5  # courtesy delay between Tavily calls (seconds)
TAVILY_MAX_RESULTS: int = 10  # results per query

# Quality Indian financial news sources — narrows Tavily results away from
# stock-data pages (Yahoo Finance quote pages, NSE data pages) and toward
# genuine editorial news. Used as include_domains on all news queries.
TAVILY_INCLUDE_DOMAINS: list[str] = [
    "economictimes.indiatimes.com",
    "moneycontrol.com",
    "business-standard.com",
    "livemint.com",
    "financialexpress.com",
    "reuters.com",
    "bloomberg.com",
]

# Gemini
GEMINI_MODEL: str = "gemini-2.5-flash"
GEMINI_QUOTA_RETRY_DELAY: int = 60  # seconds to wait on 429

# Sentiment
VALID_SENTIMENTS: frozenset[str] = frozenset(
    {"Positive", "Negative", "Neutral", "Mixed"}
)
FALLBACK_SENTIMENT: str = "Neutral"
FALLBACK_CONFIDENCE: float = 0.3

# Earnings detection
EARNINGS_KEYWORDS: list[str] = [
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "quarterly",
    "results",
    "earnings",
    "profit",
]
EARNINGS_AGE_LIMIT_DAYS: int = 5
TRANSCRIPT_MIN_CHARS: int = 200

# Max symbols to process per run
MAX_SYMBOLS: int = 5

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

# Gemini system instruction
_GEMINI_SYSTEM_PROMPT: str = (
    "You are a financial news analyst specialising in Indian equities. "
    "You will be given recent news articles about an NSE-listed stock. "
    "Analyse the articles and provide: 1) Overall sentiment (exactly one of: "
    "Positive, Negative, Neutral, Mixed), 2) Confidence score (float 0.0 to 1.0), "
    "3) Source URLs list. Be conservative -- default to Neutral when uncertain. "
    "Mixed means genuinely contradictory signals. Never guess sentiment from the "
    "company name alone."
)

# WAL pragmas applied to every connection
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# DDL for the research_reports table
_CREATE_TABLE_SQL: str = """
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
"""

_CREATE_INDEX_SQL: str = """
CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date
    ON research_reports(symbol, run_date);
"""

# Project root for DB path resolution (two levels up from this file)
_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


class ResearchAgentError(Exception):
    """Raised when the Research Agent encounters a fatal error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed: 'db_read', 'tavily_search', 'gemini', 'db_write'.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with a message and phase identifier.

        Args:
            message: Human-readable error description.
            phase: One of 'db_read', 'tavily_search', 'gemini', 'db_write'.
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_research_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> ResearchAgentResult:
    """Run the research agent for the given date.

    Fetches screener_results for run_date (defaults to today IST),
    runs Tavily Search + Gemini synthesis for each of top 5 symbols,
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
                            Tavily/Gemini failures are handled gracefully
                            per-stock and do not raise.
    """
    if run_date is None:
        run_date = datetime.datetime.now(tz=IST).date()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"research_run_started for {run_date}",
        level="INFO",
    )

    # Check Tavily API key before doing anything else
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

    # Resolve DB path and initialise table
    db_path = _resolve_db_path()
    conn = _open_connection(db_path)
    try:
        _ensure_table(conn)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        raise ResearchAgentError(
            message=f"Failed to create research_reports table: {exc}",
            phase="db_write",
        ) from exc

    # Determine which symbols to research
    if symbols is not None:
        target_symbols: list[str] = list(symbols[:MAX_SYMBOLS])
    else:
        try:
            target_symbols = _read_screener_results(conn, run_date)
        except sqlite3.Error as exc:
            conn.close()
            raise ResearchAgentError(
                message=f"Failed to read screener_results: {exc}",
                phase="db_read",
            ) from exc

    if not target_symbols:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"no screener_results for {run_date}",
            level="INFO",
            result="empty",
        )
        conn.close()
        return ResearchAgentResult(
            run_date=run_date,
            stocks_researched=0,
            results=[],
            skipped_symbols=[],
            completed_at=datetime.datetime.now(tz=IST),
        )

    # Initialise Gemini client (created once, reused for all stocks)
    gemini_client = genai.Client(api_key=settings.gemini_api_key)

    # Initialise Tavily client (created once, reused for all stocks)
    tavily_client = TavilyClient(api_key=settings.tavily_api_key)

    results: list[StockResearch] = []
    skipped_symbols: list[str] = []

    for symbol in target_symbols:
        stock_result = _research_one_stock(
            symbol=symbol,
            run_date=run_date,
            conn=conn,
            gemini_client=gemini_client,
            tavily_client=tavily_client,
        )
        if stock_result is None:
            skipped_symbols.append(symbol)
        else:
            results.append(stock_result)

    conn.close()

    run_completed_at = datetime.datetime.now(tz=IST)
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"research_run_completed: {len(results)} stocks, {len(skipped_symbols)} skipped",
        level="INFO",
        result="ok",
    )

    return ResearchAgentResult(
        run_date=run_date,
        stocks_researched=len(results),
        results=results,
        skipped_symbols=skipped_symbols,
        completed_at=run_completed_at,
    )


# ---------------------------------------------------------------------------
# Private helpers — DB
# ---------------------------------------------------------------------------


def _resolve_db_path() -> str:
    """Resolve the SQLite database file path from settings.

    Returns:
        Absolute path to the SQLite database file.
    """
    url = settings.database_url
    if url.startswith("sqlite:///"):
        remainder = url[len("sqlite:///") :]
    else:
        remainder = url

    if os.path.isabs(remainder):
        return remainder
    return os.path.join(_PROJECT_ROOT, remainder)


def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL pragmas applied.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        An open sqlite3.Connection.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create research_reports table and index if they do not exist.

    Args:
        conn: An open sqlite3.Connection.
    """
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_INDEX_SQL)


def _read_screener_results(
    conn: sqlite3.Connection,
    run_date: datetime.date,
) -> list[str]:
    """Read top-5 quality-passed symbols from screener_results for run_date.

    Args:
        conn: An open sqlite3.Connection.
        run_date: The date to filter screener_results by.

    Returns:
        List of symbol strings, at most 5 entries, ordered by rank ASC.

    Raises:
        sqlite3.Error: On any DB error.
    """
    sql = """
        SELECT symbol, rank
        FROM screener_results
        WHERE run_date = ?
          AND quality_passed = 1
          AND rank IS NOT NULL
        ORDER BY rank ASC
        LIMIT 10
    """
    cursor = conn.execute(sql, (run_date.isoformat(),))
    rows = cursor.fetchall()
    return [row[0] for row in rows]


def _insert_placeholder_row(
    conn: sqlite3.Connection,
    symbol: str,
    run_date: datetime.date,
    created_at_iso: str,
) -> int:
    """Insert a placeholder row into research_reports with completed_at=NULL.

    Args:
        conn: An open sqlite3.Connection.
        symbol: NSE ticker symbol.
        run_date: The research run date.
        created_at_iso: ISO 8601 IST timestamp for the created_at column.

    Returns:
        The lastrowid of the inserted row.

    Raises:
        sqlite3.Error: On any DB error.
    """
    sql = """
        INSERT INTO research_reports
            (symbol, run_date, sentiment, confidence, source_urls,
             earnings_transcript_unavailable, completed_at, raw_response, created_at)
        VALUES (?, ?, 'Neutral', 0.0, '[]', 0, NULL, NULL, ?)
    """
    cursor = conn.execute(sql, (symbol, run_date.isoformat(), created_at_iso))
    conn.commit()
    row_id: int = cursor.lastrowid  # type: ignore[assignment]
    return row_id


def _update_row(
    conn: sqlite3.Connection,
    row_id: int,
    sentiment: str,
    confidence: float,
    source_urls: list[str],
    earnings_transcript_unavailable: bool,
    raw_response: str | None,
    completed_at_iso: str,
) -> None:
    """Update a research_reports row with final results and set completed_at last.

    Args:
        conn: An open sqlite3.Connection.
        row_id: The id of the row to update.
        sentiment: One of Positive/Negative/Neutral/Mixed.
        confidence: Float 0.0-1.0.
        source_urls: List of URL strings.
        earnings_transcript_unavailable: True if earnings detected but transcript not found.
        raw_response: Raw Gemini response text or None.
        completed_at_iso: ISO 8601 IST timestamp (set last to signal completion).

    Raises:
        ResearchAgentError: If the UPDATE affects 0 rows or fails.
    """
    source_urls_json = json.dumps(source_urls)
    sql = """
        UPDATE research_reports
        SET sentiment = ?,
            confidence = ?,
            source_urls = ?,
            earnings_transcript_unavailable = ?,
            raw_response = ?,
            completed_at = ?
        WHERE id = ?
    """
    try:
        cursor = conn.execute(
            sql,
            (
                sentiment,
                confidence,
                source_urls_json,
                1 if earnings_transcript_unavailable else 0,
                raw_response,
                completed_at_iso,
                row_id,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise ResearchAgentError(
                message=f"UPDATE affected 0 rows for id={row_id}",
                phase="db_write",
            )
    except sqlite3.Error as exc:
        raise ResearchAgentError(
            message=f"UPDATE research_reports failed: {exc}",
            phase="db_write",
        ) from exc


# ---------------------------------------------------------------------------
# Private helpers — Tavily Search
# ---------------------------------------------------------------------------


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
                include_domains=TAVILY_INCLUDE_DOMAINS,
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


# ---------------------------------------------------------------------------
# Private helpers — Gemini synthesis
# ---------------------------------------------------------------------------


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


def _parse_gemini_response(
    text: str,
) -> tuple[str, float, list[str]] | None:
    """Parse and validate a Gemini JSON response text.

    Args:
        text: Raw text from Gemini (may include markdown fences).

    Returns:
        Tuple of (sentiment, confidence, source_urls) if valid, or None on failure.
    """
    # Strip markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    sentiment = data.get("sentiment", "")
    if sentiment not in VALID_SENTIMENTS:
        return None

    confidence_raw = data.get("confidence", None)
    if confidence_raw is None:
        return None
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return None

    # Clamp confidence to [0.0, 1.0]
    confidence = max(0.0, min(1.0, confidence))

    source_urls_raw = data.get("source_urls", [])
    if not isinstance(source_urls_raw, list):
        source_urls: list[str] = []
    else:
        source_urls = [str(u) for u in source_urls_raw if u]

    return sentiment, confidence, source_urls


def _synthesise_with_gemini(
    symbol: str,
    articles: list[dict],
    gemini_client: genai.Client,
    raw_response_out: list[str | None],
) -> tuple[str, float, list[str]] | None:
    """Call Gemini to synthesise sentiment from news articles.

    Handles JSON parse failures (retry once) and quota errors (sleep 60s, retry once).
    On Gemini API exceptions that are not quota errors, returns None so the stock
    is added to skipped_symbols.

    Args:
        symbol: NSE ticker symbol for logging.
        articles: List of article dicts to synthesise.
        gemini_client: Reusable Gemini client instance.
        raw_response_out: Single-element list used as an output parameter.
                          raw_response_out[0] is set to the raw Gemini text.

    Returns:
        Tuple of (sentiment, confidence, source_urls) on success, or None on fatal failure.
        Fallback sentinel is returned (not None) when JSON retry fails or quota retry fails.
    """
    formatted = _format_articles_for_prompt(articles)
    user_prompt = (
        f"Stock: {symbol}\n"
        f"News articles (last 48 hours):\n{formatted}\n\n"
        "Return JSON only, no markdown formatting:\n"
        '{"sentiment": "Positive|Negative|Neutral|Mixed", "confidence": 0.0-1.0, '
        '"source_urls": ["url1", "url2"]}'
    )

    def _call_gemini(prompt: str) -> str | None:
        """Inner helper to call Gemini and return the response text."""
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_GEMINI_SYSTEM_PROMPT,
                ),
            )
            return response.text
        except Exception as exc:
            # Check if it is a quota/rate-limit error
            exc_str = str(exc).lower()
            if "429" in exc_str or "resourceexhausted" in exc_str or "quota" in exc_str:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="gemini_quota_429, sleeping 60s",
                    level="WARNING",
                    symbol=symbol,
                    result="retry",
                )
                time.sleep(GEMINI_QUOTA_RETRY_DELAY)
                # Retry once after sleeping
                try:
                    response2 = gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(
                            system_instruction=_GEMINI_SYSTEM_PROMPT,
                        ),
                    )
                    return response2.text
                except Exception as exc2:
                    log_agent_action(
                        agent_name=AGENT_NAME,
                        action=f"gemini_failed: {exc2}",
                        level="ERROR",
                        symbol=symbol,
                        result="error",
                    )
                    return None
            else:
                # Non-quota exception -- fatal for this stock
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=f"gemini_failed: {exc}",
                    level="ERROR",
                    symbol=symbol,
                    result="error",
                )
                return None

    # First attempt
    raw_text = _call_gemini(user_prompt)

    if raw_text is None:
        # Fatal Gemini failure -- stock goes to skipped_symbols
        raw_response_out[0] = None
        return None

    raw_response_out[0] = raw_text
    parsed = _parse_gemini_response(raw_text)

    if parsed is not None:
        sentiment, confidence, source_urls = parsed
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"gemini_synthesis sentiment={sentiment} confidence={confidence}",
            level="INFO",
            symbol=symbol,
            result="ok",
        )
        return sentiment, confidence, source_urls

    # First parse failed -- retry with explicit JSON instruction
    log_agent_action(
        agent_name=AGENT_NAME,
        action="gemini_invalid_json, retrying",
        level="WARNING",
        symbol=symbol,
        result="retry",
    )

    retry_prompt = (
        "Your previous response was not valid JSON. Return ONLY a JSON object "
        "with these exact keys: sentiment, confidence, source_urls. No other text."
    )
    raw_text2 = _call_gemini(retry_prompt)

    if raw_text2 is not None:
        raw_response_out[0] = raw_text2
        parsed2 = _parse_gemini_response(raw_text2)
        if parsed2 is not None:
            sentiment2, confidence2, source_urls2 = parsed2
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"gemini_synthesis sentiment={sentiment2} confidence={confidence2}",
                level="INFO",
                symbol=symbol,
                result="ok",
            )
            return sentiment2, confidence2, source_urls2

    # Both attempts failed -- use fallback sentinel
    log_agent_action(
        agent_name=AGENT_NAME,
        action="gemini_fallback_sentinel used",
        level="WARNING",
        symbol=symbol,
        result="fallback",
    )
    return FALLBACK_SENTIMENT, FALLBACK_CONFIDENCE, []


# ---------------------------------------------------------------------------
# Private helpers — per-stock orchestration
# ---------------------------------------------------------------------------


def _research_one_stock(
    symbol: str,
    run_date: datetime.date,
    conn: sqlite3.Connection,
    gemini_client: genai.Client,
    tavily_client: TavilyClient,
) -> StockResearch | None:
    """Run the full research pipeline for one stock.

    Inserts a placeholder DB row first, performs all Tavily + Gemini work,
    then updates the row with results and sets completed_at last.

    Args:
        symbol: NSE ticker symbol.
        run_date: The research run date.
        conn: An open sqlite3.Connection.
        gemini_client: Reusable Gemini client instance.
        tavily_client: Reusable TavilyClient instance.

    Returns:
        StockResearch on success, None if Gemini failed fatally (stock skipped).

    Raises:
        ResearchAgentError: On DB write failures.
    """
    created_at_iso = datetime.datetime.now(tz=IST).isoformat(timespec="seconds")

    # Step 1: INSERT placeholder row (completed_at = NULL)
    try:
        row_id = _insert_placeholder_row(conn, symbol, run_date, created_at_iso)
    except sqlite3.Error as exc:
        raise ResearchAgentError(
            message=f"INSERT research_reports failed for {symbol}: {exc}",
            phase="db_write",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"research_report row inserted id={row_id}",
        level="DEBUG",
        symbol=symbol,
        result="ok",
    )

    # Step 2: Fetch news articles (3 Tavily queries)
    articles = _fetch_tavily_news(symbol, tavily_client)

    # Step 3: Earnings detection
    earnings_detected = _detect_earnings(articles)
    earnings_transcript_unavailable = False
    synthesis_articles = articles  # default: use standard articles

    if earnings_detected:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="earnings_event_detected",
            level="INFO",
            symbol=symbol,
            result="ok",
        )
        # Step 3a: Attempt transcript fetch
        transcript_text = _fetch_transcript(symbol, tavily_client)
        if len(transcript_text) > TRANSCRIPT_MIN_CHARS:
            # Use transcript content as synthetic articles for Gemini
            transcript_article = {
                "title": f"{symbol} Earnings Call Transcript",
                "url": "",
                "content": transcript_text,
                "published_date": datetime.date.today().isoformat(),
            }
            synthesis_articles = [transcript_article]
        else:
            # Transcript unavailable -- fall back to standard articles
            earnings_transcript_unavailable = True
            log_agent_action(
                agent_name=AGENT_NAME,
                action="earnings_transcript_unavailable",
                level="WARNING",
                symbol=symbol,
                result="fallback",
            )

    # Step 4: Gemini synthesis
    raw_response_out: list[str | None] = [None]
    gemini_result = _synthesise_with_gemini(
        symbol=symbol,
        articles=synthesis_articles,
        gemini_client=gemini_client,
        raw_response_out=raw_response_out,
    )

    if gemini_result is None:
        # Fatal Gemini failure -- row stays with completed_at=NULL
        log_agent_action(
            agent_name=AGENT_NAME,
            action="stock_skipped: gemini_failed",
            level="WARNING",
            symbol=symbol,
            result="skipped",
        )
        return None

    sentiment, confidence, source_urls = gemini_result
    raw_response = raw_response_out[0]

    # Step 5: UPDATE row with final results (completed_at set here -- last field)
    completed_at_ist = datetime.datetime.now(tz=IST)
    completed_at_iso = completed_at_ist.isoformat(timespec="seconds")

    _update_row(
        conn=conn,
        row_id=row_id,
        sentiment=sentiment,
        confidence=confidence,
        source_urls=source_urls,
        earnings_transcript_unavailable=earnings_transcript_unavailable,
        raw_response=raw_response,
        completed_at_iso=completed_at_iso,
    )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"research_report completed id={row_id}",
        level="INFO",
        symbol=symbol,
        result="ok",
    )

    return StockResearch(
        symbol=symbol,
        sentiment=sentiment,
        confidence=confidence,
        source_urls=source_urls,
        earnings_transcript_unavailable=earnings_transcript_unavailable,
        completed_at=completed_at_ist,
    )
