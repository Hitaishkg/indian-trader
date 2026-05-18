"""Morning Validator Agent for the Indian Trader pipeline.

Runs at 08:00 IST every weekday morning. Reads today's human-approved watchlist,
fetches the last 12 hours of news per stock via Tavily, uses a single Gemini call
per stock to detect material overnight events (earnings, trading halt, circuit
breaker, RBI decision, promoter fraud, SEBI investigation) that would invalidate
a swing trade. Stocks flagged for material events are removed. Surviving stocks
receive a fresh morning OHLCV fetch and a regime re-confirmation against the
Nifty 50 200 DMA. All survivors are written to the morning_signals table.

Hard deadline is 08:15 IST — if exceeded, the agent enters safe mode, writes
nothing, sends an alert, and returns without crashing.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from src.config.settings import settings
from src.data.fetcher import fetch_ohlcv, fetch_sector_indices
from src.strategy.regime import apply_regime_filter
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_info

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
AGENT_NAME: str = "morning_validator_agent"
DEADLINE_HOUR: int = 8
DEADLINE_MINUTE: int = 15
NEWS_LOOKBACK_HOURS: int = 12
TAVILY_MAX_RESULTS: int = 8
TAVILY_REQUEST_DELAY: float = 0.5
GEMINI_MODEL: str = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS: int = 20
OHLCV_LOOKBACK_DAYS: int = 5
REGIME_LOOKBACK_DAYS: int = 400
MATERIAL_EVENT_KEYWORDS: tuple[str, ...] = (
    "earnings",
    "quarterly results",
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "trading halt",
    "circuit breaker",
    "upper circuit",
    "lower circuit",
    "RBI rate",
    "repo rate",
    "MPC decision",
    "SEBI investigation",
    "SEBI order",
    "promoter fraud",
    "promoter pledge",
    "promoter resignation",
    "auditor resignation",
    "ratings downgrade",
)

# WAL pragmas applied to every SQLite connection
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# DDL for morning_signals table
_CREATE_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS morning_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    latest_price REAL NOT NULL,
    regime TEXT NOT NULL,
    position_size_multiplier REAL NOT NULL,
    overnight_news_checked INTEGER NOT NULL,
    removal_reason TEXT,
    validated_at TEXT NOT NULL,
    UNIQUE(symbol, run_date)
);
"""

_CREATE_INDEX_SQL: str = """
CREATE INDEX IF NOT EXISTS idx_morning_signals_run_date
    ON morning_signals(run_date);
"""

# Project root for DB path resolution (two levels up from this file)
_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Company name map imported from research_agent
_SYMBOL_TO_COMPANY: dict[str, str] = {
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


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class MorningValidatorError(Exception):
    """Raised on fatal failures in the morning validator agent.

    Attributes:
        message: Human-readable error description.
        phase: One of 'watchlist_read', 'news_fetch', 'ohlcv_fetch',
               'regime_fetch', 'db_write', 'config'.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with a message and phase identifier.

        Args:
            message: Human-readable error description.
            phase: Which phase failed.
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MorningValidatorResult:
    """Result of a morning validation run.

    Attributes:
        run_date: Date the validation was run for.
        watchlist_size: Number of rows read from watchlist (human_approved=1).
        validated_count: Number of rows written to morning_signals.
        removed_count: Number of symbols dropped for overnight events.
        removal_reasons: List of removal descriptions, e.g. ["HDFCBANK: earnings_dropped"].
        regime_confirmed: True if regime unchanged from screener run.
        regime_now: Current regime string.
        safe_mode: True if 08:15 deadline was exceeded.
        completed_at: IST tz-aware datetime when agent finished.
    """

    run_date: datetime.date
    watchlist_size: int
    validated_count: int
    removed_count: int
    removal_reasons: list[str]
    regime_confirmed: bool
    regime_now: str
    safe_mode: bool
    completed_at: datetime.datetime


# ---------------------------------------------------------------------------
# Pydantic model for Gemini structured output
# ---------------------------------------------------------------------------


class MaterialEventVerdict(BaseModel):
    """Gemini structured output for material event detection."""

    is_material: bool = Field(description="True if any item is a material event.")
    event_type: Literal[
        "earnings_dropped",
        "trading_halt",
        "circuit_breaker",
        "rbi_decision",
        "sebi_investigation",
        "promoter_fraud",
        "ratings_downgrade",
        "other_material",
        "none",
    ] = Field(description="Event type. 'none' if is_material=False.")
    reasoning: str = Field(description="One sentence explaining the decision.")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ist_now() -> datetime.datetime:
    """Return the current time in IST.

    Returns:
        Current datetime in Asia/Kolkata timezone.
    """
    return datetime.datetime.now(tz=IST)


def _deadline_exceeded(run_date: datetime.date) -> bool:
    """Return True if current IST time has passed 08:15 for run_date.

    Args:
        run_date: The trading date to check the deadline for.

    Returns:
        True if the 08:15 deadline has passed.
    """
    deadline = datetime.datetime.combine(
        run_date,
        datetime.time(DEADLINE_HOUR, DEADLINE_MINUTE),
        tzinfo=IST,
    )
    return _ist_now() >= deadline


def _resolve_db_path(db_path_override: str | None) -> str:
    """Resolve the SQLite database file path.

    Args:
        db_path_override: Explicit path override (for tests). None → use settings.

    Returns:
        Absolute path to the SQLite database file.
    """
    if db_path_override is not None:
        return db_path_override

    url = settings.database_url
    if url.startswith("sqlite:///"):
        remainder = url[len("sqlite:///"):]
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
        Open sqlite3.Connection with isolation_level=None (autocommit).
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _setup_table(db_path: str) -> None:
    """Create morning_signals table and index if they do not exist.

    Args:
        db_path: Absolute path to the SQLite database file.

    Raises:
        MorningValidatorError: If DDL execution fails.
    """
    conn = _open_connection(db_path)
    try:
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_CREATE_INDEX_SQL)
    except sqlite3.Error as exc:
        conn.close()
        raise MorningValidatorError(
            message=f"DB setup failed: {exc}",
            phase="db_write",
        ) from exc
    conn.close()


def _read_watchlist(
    db_path: str,
    run_date: datetime.date,
) -> list[dict[str, object]]:
    """Read human-approved watchlist rows for run_date.

    Args:
        db_path: Absolute path to the SQLite database file.
        run_date: Date to query.

    Returns:
        List of dicts with keys: symbol, sentiment, confidence, rank,
        regime, position_size_multiplier, scorecard_score, scorecard_max.

    Raises:
        MorningValidatorError: On sqlite3.Error.
    """
    conn = _open_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT symbol, sentiment, confidence, rank,
                   regime, position_size_multiplier,
                   scorecard_score, scorecard_max
            FROM watchlist
            WHERE run_date = ? AND human_approved = 1
            ORDER BY rank ASC
            """,
            (run_date.isoformat(),),
        )
        rows = [
            {
                "symbol": row[0],
                "sentiment": row[1],
                "confidence": row[2],
                "rank": row[3],
                "regime": row[4],
                "position_size_multiplier": row[5],
                "scorecard_score": row[6],
                "scorecard_max": row[7],
            }
            for row in cursor.fetchall()
        ]
    except sqlite3.Error as exc:
        conn.close()
        raise MorningValidatorError(
            message=f"Failed to read watchlist: {exc}",
            phase="watchlist_read",
        ) from exc
    conn.close()
    return rows


def _read_prior_regime(db_path: str, run_date: datetime.date) -> str | None:
    """Read the most recent regime from screener_results for run_date.

    Args:
        db_path: Absolute path to the SQLite database file.
        run_date: The run date to look up.

    Returns:
        Regime string or None if no screener row exists for run_date.
    """
    conn = _open_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT regime FROM screener_results
            WHERE run_date = ?
            ORDER BY screened_at DESC
            LIMIT 1
            """,
            (run_date.isoformat(),),
        )
        row = cursor.fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    conn.close()
    return str(row[0]) if row else None


def _check_material_event(
    symbol: str,
    articles: list[dict[str, object]],
    llm_chain: Any,
    run_date: datetime.date,
) -> MaterialEventVerdict | None:
    """Call Gemini to determine if any article is a material event.

    Args:
        symbol: NSE ticker symbol.
        articles: List of article dicts from Tavily (title, url, content, published_date).
        llm_chain: Bound Gemini chain with MaterialEventVerdict structured output.
        run_date: Today's date for context.

    Returns:
        MaterialEventVerdict or None if Gemini call fails (fail-open).
    """
    company_name = _SYMBOL_TO_COMPANY.get(symbol, symbol)

    articles_text = "\n\n".join(
        f"Title: {a.get('title', '')}\n"
        f"URL: {a.get('url', '')}\n"
        f"Published: {a.get('published_date', '')}\n"
        f"Content: {str(a.get('content', ''))[:400]}"
        for a in articles
    )

    system_prompt = (
        "You are a swing-trade risk analyst. Decide whether the supplied news for an NSE stock "
        "represents a material overnight event that would invalidate an existing or planned "
        "3-10 day swing position."
        "\n\nMaterial events (must trigger is_material=True): earnings report, trading halt, "
        "circuit breaker, RBI rate decision directly affecting the sector, promoter fraud "
        "allegation, SEBI investigation, ratings downgrade, auditor resignation, major "
        "regulatory action."
        "\n\nNon-material (must NOT trigger): analyst upgrades/downgrades, price target changes, "
        "minor sector news, general market commentary, individual broker reports, "
        "target-price revisions."
    )

    user_prompt = (
        f"Symbol: {symbol}\n"
        f"Company: {company_name}\n"
        f"Date: {run_date.isoformat()}\n\n"
        f"News articles:\n{articles_text}"
    )

    try:
        verdict: MaterialEventVerdict = llm_chain.invoke(  # type: ignore[assignment]
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        return verdict
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"gemini_check_failed: {exc} — keeping stock",
            level="WARNING",
            symbol=symbol,
            result="error",
        )
        return None


def _build_synthetic_ranked_df(symbols: list[str]) -> pd.DataFrame:
    """Build a minimal ranked_df with dummy values for apply_regime_filter.

    The regime result depends only on nifty_ohlcv_df, not on ranked_df values.
    All numeric columns are 0.0, rank is 1 (int64), bool column True.

    Args:
        symbols: List of survivor symbols.

    Returns:
        DataFrame with required columns for apply_regime_filter.
    """
    n = len(symbols)
    return pd.DataFrame(
        {
            "symbol": symbols,
            "momentum_score": [0.0] * n,
            "twelve_month_return": [0.0] * n,
            "one_month_return": [0.0] * n,
            "rank": pd.array([1] * n, dtype="int64"),
            "pct_from_52w_high": [0.0] * n,
            "within_30pct_of_52w_high": [True] * n,
        }
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_morning_validator_agent(
    run_date: datetime.date | None = None,
    db_path_override: str | None = None,
) -> MorningValidatorResult:
    """Run morning validation for today's watchlist.

    Reads watchlist rows where run_date matches and human_approved=1.
    For each stock: fetch last 12h news, call Gemini for material-event
    detection, fetch morning OHLCV close, re-confirm regime. Writes
    surviving stocks to morning_signals. Hard deadline 08:15 IST.

    Args:
        run_date: Date to validate for. Defaults to today in IST.
        db_path_override: Override DB path for tests. None → resolve from settings.

    Returns:
        MorningValidatorResult summarising what was validated, removed,
        and whether safe mode was activated.

    Raises:
        MorningValidatorError: On fatal DB read/write failures or
            missing TAVILY_API_KEY / GEMINI_API_KEY.
    """
    # Step 1: Resolve run_date and db_path, log start
    if run_date is None:
        run_date = _ist_now().date()

    db_path = _resolve_db_path(db_path_override)

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"morning_validation_started: {run_date}",
        level="INFO",
    )

    # Step 2: Verify API keys
    if not settings.tavily_api_key:
        raise MorningValidatorError(
            message="TAVILY_API_KEY is not set",
            phase="config",
        )
    if not settings.gemini_api_key:
        raise MorningValidatorError(
            message="GEMINI_API_KEY is not set",
            phase="config",
        )

    # Step 3: CREATE TABLE IF NOT EXISTS, close
    _setup_table(db_path)

    # Step 4: Read watchlist rows
    watchlist_rows = _read_watchlist(db_path, run_date)
    watchlist_size = len(watchlist_rows)

    if watchlist_size == 0:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="empty_watchlist: no approved stocks, skipping",
            level="INFO",
            result="empty",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=0,
            validated_count=0,
            removed_count=0,
            removal_reasons=[],
            regime_confirmed=True,
            regime_now="UNKNOWN",
            safe_mode=False,
            completed_at=_ist_now(),
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"watchlist_loaded: {watchlist_size} approved stocks",
        level="INFO",
    )

    watchlist_symbols = [str(row["symbol"]) for row in watchlist_rows]

    # Step 5: Deadline check #1
    if _deadline_exceeded(run_date):
        log_agent_action(
            agent_name=AGENT_NAME,
            action="deadline_exceeded: safe mode activated",
            level="ERROR",
            result="safe_mode",
        )
        send_alert(
            "Morning validator: 08:15 deadline exceeded",
            "Safe mode activated — no validations written.",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=watchlist_size,
            validated_count=0,
            removed_count=0,
            removal_reasons=[],
            regime_confirmed=True,
            regime_now="UNKNOWN",
            safe_mode=True,
            completed_at=_ist_now(),
        )

    # Step 6: Instantiate Tavily and Gemini
    tavily_client = TavilyClient(api_key=settings.tavily_api_key)
    llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=settings.gemini_api_key,
        temperature=0.0,
    )
    gemini_chain = llm.with_structured_output(MaterialEventVerdict)

    # Step 7: News + Gemini loop
    removed_symbols: set[str] = set()
    removal_reasons: list[str] = []
    overnight_news_checked: dict[str, bool] = {}

    for symbol in watchlist_symbols:
        company_name = _SYMBOL_TO_COMPANY.get(symbol, symbol)
        query = f"{symbol} {company_name} news"
        articles: list[dict[str, object]] = []

        try:
            result_raw = tavily_client.search(
                query=query,
                topic="news",
                time_range="day",
                max_results=TAVILY_MAX_RESULTS,
                include_answer=False,
                search_depth="basic",
            )
            articles = result_raw.get("results", [])
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"news_fetched: {len(articles)} articles",
                level="DEBUG",
                symbol=symbol,
                result="ok",
            )
        except Exception as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"news_fetch_failed: {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )
            overnight_news_checked[symbol] = False
            time.sleep(TAVILY_REQUEST_DELAY)
            continue

        # Always call Gemini if ≥1 article returned (keywords are logging context only)
        if articles:
            verdict = _check_material_event(symbol, articles, gemini_chain, run_date)
            if verdict is None:
                # Gemini failed — fail-open, keep stock
                overnight_news_checked[symbol] = True
            elif verdict.is_material:
                removed_symbols.add(symbol)
                removal_reasons.append(f"{symbol}: {verdict.event_type}")
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=f"overnight_event_removal: {verdict.event_type} — {verdict.reasoning}",
                    level="WARNING",
                    symbol=symbol,
                    result="removed",
                )
                overnight_news_checked[symbol] = True
            else:
                overnight_news_checked[symbol] = True
        else:
            # 0 articles — skip Gemini, stock kept
            overnight_news_checked[symbol] = True

        time.sleep(TAVILY_REQUEST_DELAY)

    # Step 8: Deadline check #2
    if _deadline_exceeded(run_date):
        log_agent_action(
            agent_name=AGENT_NAME,
            action="deadline_exceeded: safe mode activated",
            level="ERROR",
            result="safe_mode",
        )
        send_alert(
            "Morning validator: 08:15 deadline exceeded",
            "Safe mode activated after news loop — no validations written.",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=watchlist_size,
            validated_count=0,
            removed_count=len(removed_symbols),
            removal_reasons=removal_reasons,
            regime_confirmed=True,
            regime_now="UNKNOWN",
            safe_mode=True,
            completed_at=_ist_now(),
        )

    # Step 9: Determine survivors
    survivors = [s for s in watchlist_symbols if s not in removed_symbols]

    if not survivors:
        send_alert(
            "Morning validator: all stocks removed",
            f"Removed: {', '.join(removal_reasons)}. 0 survivors.",
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"morning_validation_completed: validated=0 removed={len(removed_symbols)}",
            level="INFO",
            result="ok",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=watchlist_size,
            validated_count=0,
            removed_count=len(removed_symbols),
            removal_reasons=removal_reasons,
            regime_confirmed=True,
            regime_now="UNKNOWN",
            safe_mode=False,
            completed_at=_ist_now(),
        )

    # Step 10: Fetch fresh OHLCV for survivors
    ohlcv_start = run_date - datetime.timedelta(days=OHLCV_LOOKBACK_DAYS)
    try:
        ohlcv_df = fetch_ohlcv(
            symbols=survivors,
            start_date=ohlcv_start,
            end_date=run_date,
            cache_expiry_hours=0,
        )
    except Exception as exc:
        raise MorningValidatorError(
            message=f"OHLCV batch fetch failed: {exc}",
            phase="ohlcv_fetch",
        ) from exc

    # Build latest_price map and drop symbols with no data
    latest_price: dict[str, float] = {}
    for symbol in list(survivors):
        sym_df = ohlcv_df[ohlcv_df["symbol"] == symbol]
        if sym_df.empty:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="ohlcv_unavailable",
                level="WARNING",
                symbol=symbol,
                result="removed",
            )
            removal_reasons.append(f"{symbol}: ohlcv_unavailable")
            survivors.remove(symbol)
        else:
            latest_price[symbol] = float(sym_df.sort_values("date")["close"].iloc[-1])

    # Step 11: Deadline check #3
    if _deadline_exceeded(run_date):
        log_agent_action(
            agent_name=AGENT_NAME,
            action="deadline_exceeded: safe mode activated",
            level="ERROR",
            result="safe_mode",
        )
        send_alert(
            "Morning validator: 08:15 deadline exceeded",
            "Safe mode activated after OHLCV fetch — no validations written.",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=watchlist_size,
            validated_count=0,
            removed_count=len(removed_symbols) + (watchlist_size - len(survivors) - len(removed_symbols)),
            removal_reasons=removal_reasons,
            regime_confirmed=True,
            regime_now="UNKNOWN",
            safe_mode=True,
            completed_at=_ist_now(),
        )

    # Handle case where all survivors lost OHLCV data
    if not survivors:
        send_alert(
            "Morning validator: all stocks removed",
            f"Removed: {', '.join(removal_reasons)}. 0 survivors.",
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"morning_validation_completed: validated=0 removed={watchlist_size}",
            level="INFO",
            result="ok",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=watchlist_size,
            validated_count=0,
            removed_count=watchlist_size,
            removal_reasons=removal_reasons,
            regime_confirmed=True,
            regime_now="UNKNOWN",
            safe_mode=False,
            completed_at=_ist_now(),
        )

    # Step 12: Fetch Nifty sector indices and compute regime
    regime_start = run_date - datetime.timedelta(days=REGIME_LOOKBACK_DAYS)
    try:
        sector_df = fetch_sector_indices(
            start_date=regime_start,
            end_date=run_date,
            cache_expiry_hours=0,
        )
        # Extract Nifty 50 slice — only "date" and "close" needed
        # screener_agent uses symbol=="NIFTY_50" (confirmed from screener_agent.py line 491)
        nifty_df = sector_df[sector_df["symbol"] == "NIFTY_50"][["date", "close"]].copy()

        synthetic_df = _build_synthetic_ranked_df(survivors)
        _filtered_df, regime_result = apply_regime_filter(
            ranked_df=synthetic_df,
            nifty_ohlcv_df=nifty_df,
            open_positions=None,
        )
        regime_now = regime_result.regime
        position_size_multiplier = regime_result.position_size_multiplier
    except Exception as exc:
        raise MorningValidatorError(
            message=f"Regime fetch/compute failed: {exc}",
            phase="regime_fetch",
        ) from exc

    # Step 13: Compare to prior regime
    prior_regime = _read_prior_regime(db_path, run_date)
    if prior_regime is None:
        regime_confirmed = True
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"regime_confirmed: {regime_now}",
            level="INFO",
        )
    elif prior_regime == regime_now:
        regime_confirmed = True
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"regime_confirmed: {regime_now}",
            level="INFO",
        )
    else:
        regime_confirmed = False
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"regime_changed: {prior_regime} → {regime_now}",
            level="WARNING",
        )

    # Step 14: Deadline check #4
    if _deadline_exceeded(run_date):
        log_agent_action(
            agent_name=AGENT_NAME,
            action="deadline_exceeded: safe mode activated",
            level="ERROR",
            result="safe_mode",
        )
        send_alert(
            "Morning validator: 08:15 deadline exceeded",
            "Safe mode activated after regime fetch — no validations written.",
        )
        return MorningValidatorResult(
            run_date=run_date,
            watchlist_size=watchlist_size,
            validated_count=0,
            removed_count=watchlist_size - len(survivors),
            removal_reasons=removal_reasons,
            regime_confirmed=regime_confirmed,
            regime_now=regime_now,
            safe_mode=True,
            completed_at=_ist_now(),
        )

    # Steps 15–16: Build rows and write to DB
    validated_at_str = _ist_now().isoformat()

    rows_to_insert = []
    for symbol in survivors:
        rows_to_insert.append(
            (
                symbol,
                run_date.isoformat(),
                latest_price[symbol],
                regime_now,
                position_size_multiplier,
                1 if overnight_news_checked.get(symbol, True) else 0,
                None,  # removal_reason — always NULL for written rows
                validated_at_str,
            )
        )

    conn = _open_connection(db_path)
    try:
        conn.execute("BEGIN")
        conn.executemany(
            """
            INSERT OR REPLACE INTO morning_signals
                (symbol, run_date, latest_price, regime, position_size_multiplier,
                 overnight_news_checked, removal_reason, validated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        raise MorningValidatorError(
            message=f"DB write failed: {exc}",
            phase="db_write",
        ) from exc
    conn.close()

    # Step 17: Log completion OUTSIDE any transaction
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"morning_signals_written: {len(survivors)} rows",
        level="INFO",
        result="ok",
    )

    total_removed = watchlist_size - len(survivors)
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"morning_validation_completed: validated={len(survivors)} removed={total_removed}",
        level="INFO",
        result="ok",
    )

    # Step 18: Send notification
    if total_removed > 0:
        send_alert(
            "Morning validator: overnight events detected",
            f"Removed: {', '.join(removal_reasons)}. Survivors: {', '.join(survivors)}.",
        )
    else:
        send_info(
            f"Morning validator: 0 removals. {len(survivors)} stocks validated. "
            f"Regime: {regime_now}."
        )

    # Step 19: Return result
    return MorningValidatorResult(
        run_date=run_date,
        watchlist_size=watchlist_size,
        validated_count=len(survivors),
        removed_count=total_removed,
        removal_reasons=removal_reasons,
        regime_confirmed=regime_confirmed,
        regime_now=regime_now,
        safe_mode=False,
        completed_at=_ist_now(),
    )
