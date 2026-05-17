"""Research Agent for the Indian Trader evening pipeline.

Runs every evening at 22:40 IST. Reads the top candidates from screener_results,
and for each stock runs a true LangChain ReAct agent loop: Gemini decides which
Tavily searches to run, whether to fetch earnings transcripts, and when it has
enough evidence to conclude. Results are written to the research_reports table
with the completed_at column set last to prevent race conditions.

This replaces the previous fixed 3-query pipeline with a genuine agent that
can adaptively search for more data when initial results are ambiguous.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from src.config.settings import settings
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
AGENT_NAME: str = "research_agent"

GEMINI_MODEL: str = "gemini-2.5-flash"
MAX_AGENT_ITERATIONS: int = 6   # max tool-calling rounds per stock
MAX_SYMBOLS: int = 10
TAVILY_REQUEST_DELAY: float = 0.5
TAVILY_MAX_RESULTS: int = 8
EARNINGS_AGE_LIMIT_DAYS: int = 5
EARNINGS_KEYWORDS: list[str] = ["Q1", "Q2", "Q3", "Q4", "quarterly", "results", "earnings", "profit"]
TRANSCRIPT_MIN_CHARS: int = 200

TAVILY_INCLUDE_DOMAINS: list[str] = [
    "economictimes.indiatimes.com",
    "moneycontrol.com",
    "business-standard.com",
    "livemint.com",
    "financialexpress.com",
    "reuters.com",
    "bloomberg.com",
]

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

_SYSTEM_PROMPT = (
    "You are a financial news analyst specialising in Indian equities. "
    "Your job is to research a given NSE-listed stock and determine its news sentiment. "
    "\n\nProcess:\n"
    "1. Start with a broad news search for the stock.\n"
    "2. If results are ambiguous or contradictory, search again with a more specific query.\n"
    "3. If you see earnings-related news from the last 5 days, fetch the earnings transcript.\n"
    "4. Once you have sufficient evidence (at least 3 independent sources), stop searching.\n"
    "5. Return your final verdict.\n"
    "\nRules:\n"
    "- Be conservative: default to Neutral when uncertain.\n"
    "- Mixed means genuinely contradictory signals from multiple sources.\n"
    "- Never infer sentiment from the company name alone — base it on actual news.\n"
    "- Confidence reflects how clearly the sources agree, not your certainty in the company."
)

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

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

_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------


class SentimentResult(BaseModel):
    """Structured final output from the research agent."""

    sentiment: Literal["Positive", "Negative", "Neutral", "Mixed"] = Field(
        description="Overall news sentiment for the stock."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence score 0.0-1.0. How clearly do sources agree?",
    )
    source_urls: list[str] = Field(
        default_factory=list,
        description="URLs of the actual articles used in the analysis.",
    )
    earnings_transcript_used: bool = Field(
        default=False,
        description="True if an earnings transcript was fetched and used.",
    )


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


class ResearchAgentError(Exception):
    """Raised when the Research Agent encounters a fatal error."""

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with message and phase identifier."""
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


@dataclass(frozen=True)
class StockResearch:
    """Research result for a single stock."""

    symbol: str
    sentiment: str
    confidence: float
    source_urls: list[str]
    earnings_transcript_unavailable: bool
    completed_at: datetime.datetime


@dataclass(frozen=True)
class ResearchAgentResult:
    """Full output of run_research_agent()."""

    run_date: datetime.date
    stocks_researched: int
    results: list[StockResearch]
    skipped_symbols: list[str]
    completed_at: datetime.datetime


# ---------------------------------------------------------------------------
# LangChain tool factory (closure over tavily_client + symbol context)
# ---------------------------------------------------------------------------


def _make_tools(tavily_client: TavilyClient, symbol: str) -> list[Any]:
    """Create LangChain tools bound to this Tavily client and stock symbol."""

    company_name = SYMBOL_TO_COMPANY.get(symbol, f"{symbol} company")

    @tool
    def search_news(query: str) -> str:
        """Search for recent news articles about the stock.

        Args:
            query: The search query. Be specific — e.g. 'HEROMOTOCO quarterly results India'.

        Returns:
            JSON string with a list of articles (title, url, content, published_date).
        """
        time.sleep(TAVILY_REQUEST_DELAY)
        try:
            results = tavily_client.search(
                query=query,
                topic="news",
                time_range="week",
                max_results=TAVILY_MAX_RESULTS,
                include_answer=False,
                search_depth="basic",
                include_domains=TAVILY_INCLUDE_DOMAINS,
            )
            articles = results.get("results", [])
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"tool:search_news query={query!r} results={len(articles)}",
                level="DEBUG",
                symbol=symbol,
                result="ok",
            )
            return json.dumps([
                {
                    "title": a.get("title", ""),
                    "url": a.get("url", ""),
                    "content": (a.get("content", "") or "")[:500],
                    "published_date": a.get("published_date", ""),
                }
                for a in articles
            ])
        except Exception as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"tool:search_news failed: {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )
            return json.dumps({"error": str(exc)})

    @tool
    def fetch_earnings_transcript(ticker: str) -> str:
        """Fetch earnings call transcript or analyst commentary for the stock.

        Use this when you detect that earnings were reported in the last 5 days.

        Args:
            ticker: The NSE ticker symbol, e.g. 'HEROMOTOCO'.

        Returns:
            Text content of the transcript, or an error message.
        """
        time.sleep(TAVILY_REQUEST_DELAY)
        query = f"{ticker} {company_name} earnings call transcript analyst Q4 results"
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
            content = " ".join(a.get("content", "") or "" for a in articles)
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"tool:fetch_earnings_transcript chars={len(content)}",
                level="DEBUG",
                symbol=symbol,
                result="ok" if len(content) > TRANSCRIPT_MIN_CHARS else "short",
            )
            if len(content) < TRANSCRIPT_MIN_CHARS:
                return f"Transcript not found or too short ({len(content)} chars)."
            return content[:3000]
        except Exception as exc:
            return f"Error fetching transcript: {exc}"

    return [search_news, fetch_earnings_transcript]


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------


def _run_agent_for_stock(
    symbol: str,
    llm: ChatGoogleGenerativeAI,
    tavily_client: TavilyClient,
) -> SentimentResult | None:
    """Run the LangChain ReAct agent loop for one stock.

    The LLM decides which tools to call and when it has enough evidence.
    Returns structured SentimentResult, or None on fatal LLM failure.

    Args:
        symbol: NSE ticker symbol.
        llm: Shared ChatGoogleGenerativeAI instance.
        tavily_client: Shared Tavily client.

    Returns:
        SentimentResult on success, None on failure.
    """
    company_name = SYMBOL_TO_COMPANY.get(symbol, f"{symbol} company")
    tools = _make_tools(tavily_client, symbol)
    tool_map = {t.name: t for t in tools}

    llm_with_tools = llm.bind_tools(tools)
    structured_llm = llm.with_structured_output(SentimentResult)

    messages: list[Any] = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Research the NSE-listed stock: {symbol} ({company_name}).\n"
                f"Today's date: {datetime.date.today().isoformat()}.\n"
                f"Use the search_news tool to find recent news (last 7 days). "
                f"If you find earnings news from the last 5 days, also call "
                f"fetch_earnings_transcript. Stop when you have enough evidence "
                f"to give a confident verdict."
            )
        ),
    ]

    try:
        for iteration in range(MAX_AGENT_ITERATIONS):
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            if not response.tool_calls:
                # LLM decided it has enough — exit the loop
                break

            for tc in response.tool_calls:
                tool_fn = tool_map.get(tc["name"])
                if tool_fn is None:
                    tool_output = f"Unknown tool: {tc['name']}"
                else:
                    tool_output = tool_fn.invoke(tc["args"])
                messages.append(
                    ToolMessage(content=str(tool_output), tool_call_id=tc["id"])
                )

            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"agent_iteration={iteration + 1} tools_called={len(response.tool_calls)}",
                level="DEBUG",
                symbol=symbol,
                result="ok",
            )

        # Get structured final answer
        final_prompt = (
            "Based on all the news you have gathered, provide your final "
            "sentiment analysis for the stock."
        )
        messages.append(HumanMessage(content=final_prompt))
        result: SentimentResult = structured_llm.invoke(messages)
        return result

    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"agent_failed: {exc}",
            level="ERROR",
            symbol=symbol,
            result="error",
        )
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _resolve_db_path() -> str:
    """Resolve the SQLite database file path from settings."""
    url = settings.database_url
    if url.startswith("sqlite:///"):
        remainder = url[len("sqlite:///"):]
    else:
        remainder = url
    if os.path.isabs(remainder):
        return remainder
    return os.path.join(_PROJECT_ROOT, remainder)


def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL pragmas applied."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create research_reports table and index if they do not exist."""
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_INDEX_SQL)


def _read_screener_results(conn: sqlite3.Connection, run_date: datetime.date) -> list[str]:
    """Read top-N quality-passed symbols from screener_results for run_date."""
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
    return [row[0] for row in cursor.fetchall()]


def _insert_placeholder_row(
    conn: sqlite3.Connection,
    symbol: str,
    run_date: datetime.date,
    created_at_iso: str,
) -> int:
    """Insert placeholder row with completed_at=NULL."""
    sql = """
        INSERT INTO research_reports
            (symbol, run_date, sentiment, confidence, source_urls,
             earnings_transcript_unavailable, completed_at, raw_response, created_at)
        VALUES (?, ?, 'Neutral', 0.0, '[]', 0, NULL, NULL, ?)
    """
    cursor = conn.execute(sql, (symbol, run_date.isoformat(), created_at_iso))
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def _update_row(
    conn: sqlite3.Connection,
    row_id: int,
    result: SentimentResult,
    raw_response: str,
    completed_at_iso: str,
) -> None:
    """Update a research_reports row with final results, set completed_at last."""
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
    conn.execute(
        sql,
        (
            result.sentiment,
            result.confidence,
            json.dumps(result.source_urls),
            0,  # transcript unavailability is now inside the agent's reasoning
            raw_response,
            completed_at_iso,
            row_id,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_research_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> ResearchAgentResult:
    """Run the LangChain research agent for the given date.

    For each screener candidate, a true ReAct agent loop runs: Gemini
    decides which Tavily searches to perform, requests more data if
    ambiguous, and produces a structured SentimentResult when done.

    Args:
        run_date: Date to run for. Defaults to today in IST.
        symbols: Override list of symbols (used in testing).

    Returns:
        ResearchAgentResult with per-stock results and run metadata.

    Raises:
        ResearchAgentError: On DB read/write failures or missing API key.
    """
    if run_date is None:
        run_date = datetime.datetime.now(tz=IST).date()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"research_run_started for {run_date}",
        level="INFO",
    )

    if not settings.tavily_api_key:
        raise ResearchAgentError("TAVILY_API_KEY not configured", phase="tavily_search")
    if not settings.gemini_api_key:
        raise ResearchAgentError("GEMINI_API_KEY not configured", phase="gemini")

    db_path = _resolve_db_path()
    conn = _open_connection(db_path)
    try:
        _ensure_table(conn)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        raise ResearchAgentError(f"Failed to create table: {exc}", phase="db_write") from exc

    if symbols is not None:
        target_symbols: list[str] = list(symbols[:MAX_SYMBOLS])
    else:
        try:
            target_symbols = _read_screener_results(conn, run_date)
        except sqlite3.Error as exc:
            conn.close()
            raise ResearchAgentError(f"Failed to read screener_results: {exc}", phase="db_read") from exc

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

    # Initialise shared clients
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=settings.gemini_api_key,
        temperature=0.1,
    )
    tavily_client = TavilyClient(api_key=settings.tavily_api_key)

    results: list[StockResearch] = []
    skipped_symbols: list[str] = []

    for symbol in target_symbols:
        created_at_iso = datetime.datetime.now(tz=IST).isoformat(timespec="seconds")

        try:
            row_id = _insert_placeholder_row(conn, symbol, run_date, created_at_iso)
        except sqlite3.Error as exc:
            raise ResearchAgentError(
                f"INSERT failed for {symbol}: {exc}", phase="db_write"
            ) from exc

        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"agent_started for {symbol}",
            level="INFO",
            symbol=symbol,
        )

        agent_result = _run_agent_for_stock(symbol, llm, tavily_client)

        if agent_result is None:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="stock_skipped: agent_failed",
                level="WARNING",
                symbol=symbol,
                result="skipped",
            )
            skipped_symbols.append(symbol)
            continue

        completed_at_ist = datetime.datetime.now(tz=IST)
        completed_at_iso = completed_at_ist.isoformat(timespec="seconds")
        raw_response = agent_result.model_dump_json()

        _update_row(conn, row_id, agent_result, raw_response, completed_at_iso)

        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"agent_completed sentiment={agent_result.sentiment} "
                f"confidence={agent_result.confidence:.2f}"
            ),
            level="INFO",
            symbol=symbol,
            result="ok",
        )

        results.append(
            StockResearch(
                symbol=symbol,
                sentiment=agent_result.sentiment,
                confidence=agent_result.confidence,
                source_urls=agent_result.source_urls,
                earnings_transcript_unavailable=False,
                completed_at=completed_at_ist,
            )
        )

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
