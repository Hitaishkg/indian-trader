"""Signal Agent for the Indian Trader morning pipeline.

Runs every morning at 08:20 IST. Reads the top-ranked screener candidates,
fetches fresh morning OHLCV, computes technical indicators (RSI, MACD,
Bollinger Bands, ATR), reads research sentiment from the previous evening's
research run, applies the combined decision rule, and sends a Groq LLM
confidence check as an advisory filter. Results — both BUY and HOLD signals
— are written to the signals table for full audit trail.

Hard deadline: must complete by 08:50 IST. If run starts after 08:50 IST,
the agent logs late_start and returns an empty result. This triggers safe
mode in the orchestrator.

This module is a plain Python function. It does NOT use the Python Agent
SDK or Claude API.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types as genai_types

from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_ohlcv
from src.indicators.technical import add_indicators
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Timezone constant
# ---------------------------------------------------------------------------

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "signal_agent"

# Hard deadline
DEADLINE_HOUR: int = 8
DEADLINE_MINUTE: int = 50

# Technical thresholds
RSI_BUY_THRESHOLD: float = 40.0        # RSI < this → BUY technical signal
OHLCV_LOOKBACK_DAYS: int = 60          # calendar days of OHLCV to fetch

# Groq
GROQ_MODEL: str = "llama-3.3-70b-versatile"
GROQ_API_ENDPOINT: str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT_SECONDS: int = 15
GROQ_CONFIDENCE_THRESHOLD: float = 0.6  # below this → downgrade BUY to HOLD

# Gemini fallback
GEMINI_MODEL: str = "gemini-2.5-flash"

# Sentinel for LLM unavailable
LLM_UNAVAILABLE_SENTINEL: float = -1.0

# Max symbols to process per run
MAX_SYMBOLS: int = 5

# Valid values
VALID_SIGNAL_TYPES: frozenset[str] = frozenset({"BUY", "HOLD"})
VALID_BOLLINGER_POSITIONS: frozenset[str] = frozenset({"ABOVE", "MIDDLE", "BELOW"})
VALID_MACD_SIGNALS: frozenset[str] = frozenset({"BUY", "HOLD"})

# WAL pragmas (applied to every SQLite connection)
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# Gemini system instruction for signal validation
_SIGNAL_SYSTEM_PROMPT: str = (
    "You are a trading signal validator for Indian equities. Given an evening "
    "research thesis and morning technical indicators, assess whether the thesis "
    "still holds. Reply ONLY with JSON: "
    '{"confidence": 0.0-1.0, "reasoning": "one sentence"}. '
    "Be conservative — default confidence is 0.5 when uncertain."
)

# DDL for the signals table
_CREATE_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    rsi REAL NOT NULL,
    macd_signal TEXT NOT NULL,
    bollinger_position TEXT NOT NULL,
    atr REAL NOT NULL,
    groq_confidence REAL NOT NULL,
    signal_type TEXT NOT NULL,
    skip_reason TEXT,
    signalled_at TEXT NOT NULL
);
"""

_CREATE_INDEX_SQL: str = """
CREATE INDEX IF NOT EXISTS idx_signals_symbol_date
    ON signals(symbol, run_date);
"""

# Project root for DB path resolution (two levels up from this file)
_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


class SignalAgentError(Exception):
    """Raised when the Signal Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed: 'db_read', 'ohlcv_fetch', 'db_write'.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with a message and phase identifier.

        Args:
            message: Human-readable error description.
            phase: One of 'db_read', 'ohlcv_fetch', 'db_write'.
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


@dataclass(frozen=True)
class StockSignal:
    """Signal result for a single stock."""

    symbol: str
    rsi: float
    macd_signal: str          # "BUY" or "HOLD"
    bollinger_position: str   # "ABOVE", "MIDDLE", or "BELOW"
    atr: float
    groq_confidence: float    # 0.0 to 1.0; -1.0 sentinel when LLM unavailable
    signal_type: str          # "BUY" or "HOLD"
    skip_reason: str | None   # populated when signal_type="HOLD", None on BUY
    signalled_at: datetime.datetime  # IST timezone-aware


@dataclass(frozen=True)
class SignalAgentResult:
    """Full output of run_signal_agent()."""

    run_date: datetime.date
    symbols_processed: int
    buy_signals: list[StockSignal]
    hold_signals: list[StockSignal]
    late_start: bool           # True if run started after 08:50 IST
    completed_at: datetime.datetime  # IST timezone-aware


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_signal_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> SignalAgentResult:
    """Run the signal agent for the given date.

    Reads top screener candidates, computes technical indicators on fresh
    OHLCV, applies the combined decision rule, runs Groq advisory check,
    and writes all results to the signals table.

    Args:
        run_date: Date to run for. Defaults to today in IST.
        symbols: Override — use these symbols instead of reading screener_results.
                 Used in testing. If provided, screener_results table is not read.

    Returns:
        SignalAgentResult with per-stock signals and run metadata.
        Returns result with late_start=True and empty signal lists if run
        starts after 08:50 IST.

    Raises:
        SignalAgentError: If DB read fails (phase='db_read'),
                         if OHLCV fetch fails for all symbols (phase='ohlcv_fetch'),
                         or if DB write fails (phase='db_write').
                         Groq/Gemini LLM failures are handled gracefully
                         and do not raise.
    """
    # Hard deadline check — must be first, before any DB reads
    now_ist = datetime.datetime.now(tz=IST)
    deadline = now_ist.replace(
        hour=DEADLINE_HOUR, minute=DEADLINE_MINUTE, second=0, microsecond=0
    )
    if now_ist > deadline:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"late_start: current time {now_ist.strftime('%H:%M:%S')} "
                f"exceeds 08:50 deadline"
            ),
            level="WARNING",
            result="skipped",
        )
        return SignalAgentResult(
            run_date=run_date or now_ist.date(),
            symbols_processed=0,
            buy_signals=[],
            hold_signals=[],
            late_start=True,
            completed_at=now_ist,
        )

    if run_date is None:
        run_date = now_ist.date()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"signal_run_started for {run_date}",
        level="INFO",
    )

    # Check Groq API key upfront
    groq_api_key_available = bool(settings.groq_api_key)
    if not groq_api_key_available:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="groq_api_key_missing: skipping LLM check for all symbols",
            level="WARNING",
            result="fallback",
        )

    # Resolve DB path and initialise table
    db_path = _resolve_db_path()
    conn = _open_connection(db_path)
    try:
        _ensure_table(conn)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        raise SignalAgentError(
            message=f"Failed to create signals table: {exc}",
            phase="db_write",
        ) from exc

    # Determine which symbols to process
    if symbols is not None:
        target_symbols: list[str] = list(symbols[:MAX_SYMBOLS])
    else:
        try:
            target_symbols = _read_screener_results(conn, run_date)
        except sqlite3.Error as exc:
            conn.close()
            raise SignalAgentError(
                message=f"Failed to read screener_results: {exc}",
                phase="db_read",
            ) from exc

    if not target_symbols:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"no_screener_results for {run_date}",
            level="INFO",
            result="empty",
        )
        conn.close()
        return SignalAgentResult(
            run_date=run_date,
            symbols_processed=0,
            buy_signals=[],
            hold_signals=[],
            late_start=False,
            completed_at=datetime.datetime.now(tz=IST),
        )

    # Read research sentiment for each symbol
    research_by_symbol: dict[str, tuple[str, float]] = {}
    try:
        for sym in target_symbols:
            sentiment, confidence = _read_research_report(conn, sym)
            research_by_symbol[sym] = (sentiment, confidence)
    except sqlite3.Error as exc:
        conn.close()
        raise SignalAgentError(
            message=f"Failed to read research_reports: {exc}",
            phase="db_read",
        ) from exc

    # Fetch OHLCV for all symbols
    start_date = run_date - datetime.timedelta(days=OHLCV_LOOKBACK_DAYS)
    failed_ohlcv_symbols: set[str] = set()

    try:
        ohlcv_df = fetch_ohlcv(
            symbols=target_symbols,
            start_date=start_date,
            end_date=run_date,
        )
    except (FetchError, ValueError) as exc:
        # If fetch fails entirely, raise
        conn.close()
        raise SignalAgentError(
            message=f"OHLCV fetch failed for all symbols: {exc}",
            phase="ohlcv_fetch",
        ) from exc

    # Determine which symbols actually have data in the returned DataFrame
    if ohlcv_df.empty:
        conn.close()
        raise SignalAgentError(
            message="fetch_ohlcv returned empty DataFrame for all symbols",
            phase="ohlcv_fetch",
        )

    symbols_with_data: set[str] = set()
    if "symbol" in ohlcv_df.columns:
        symbols_with_data = set(ohlcv_df["symbol"].unique())

    for sym in target_symbols:
        if sym not in symbols_with_data:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="ohlcv_fetch_failed",
                level="WARNING",
                symbol=sym,
                result="error",
            )
            failed_ohlcv_symbols.add(sym)

    # Compute indicators on the full DataFrame
    indicators_df = None
    if symbols_with_data:
        try:
            indicators_df = add_indicators(ohlcv_df)
        except ValueError:
            # If add_indicators fails on the full DataFrame, mark all as failed
            for sym in symbols_with_data:
                failed_ohlcv_symbols.add(sym)

    # Create Gemini client once (reused if fallback needed)
    gemini_client: genai.Client | None = None
    if settings.gemini_api_key:
        gemini_client = genai.Client(api_key=settings.gemini_api_key)

    # Process each symbol
    all_signals: list[StockSignal] = []

    for sym in target_symbols:
        signal = _process_symbol(
            symbol=sym,
            run_date=run_date,
            research_by_symbol=research_by_symbol,
            failed_ohlcv_symbols=failed_ohlcv_symbols,
            indicators_df=indicators_df,
            groq_api_key_available=groq_api_key_available,
            gemini_client=gemini_client,
        )
        all_signals.append(signal)

    # Write all signals to DB after all symbols are processed
    try:
        _write_signals(conn, all_signals, run_date)
        conn.commit()
    except sqlite3.Error as exc:
        conn.close()
        raise SignalAgentError(
            message=f"Failed to write signals to DB: {exc}",
            phase="db_write",
        ) from exc

    conn.close()

    buy_signals = [s for s in all_signals if s.signal_type == "BUY"]
    hold_signals = [s for s in all_signals if s.signal_type == "HOLD"]

    if not buy_signals:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="no_signals_today",
            level="INFO",
            result="ok",
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"signal_run_completed: {len(buy_signals)} BUY, {len(hold_signals)} HOLD",
        level="INFO",
        result="ok",
    )

    return SignalAgentResult(
        run_date=run_date,
        symbols_processed=len(all_signals),
        buy_signals=buy_signals,
        hold_signals=hold_signals,
        late_start=False,
        completed_at=datetime.datetime.now(tz=IST),
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
        An open sqlite3.Connection.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create signals table and index if they do not exist.

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
        WHERE screened_at LIKE ? || '%'
          AND quality_passed = 1
          AND rank IS NOT NULL
        ORDER BY rank ASC
        LIMIT 5
    """
    cursor = conn.execute(sql, (run_date.isoformat(),))
    rows = cursor.fetchall()
    return [row[0] for row in rows]


def _read_research_report(
    conn: sqlite3.Connection,
    symbol: str,
) -> tuple[str, float]:
    """Read the most recent completed research report for a symbol.

    Falls back to Neutral/0.3 defaults if no completed research exists.

    Args:
        conn: An open sqlite3.Connection.
        symbol: NSE ticker symbol.

    Returns:
        Tuple of (sentiment, confidence). Defaults to ("Neutral", 0.3)
        when no completed research report exists.

    Raises:
        sqlite3.Error: On any DB error.
    """
    sql = """
        SELECT sentiment, confidence
        FROM research_reports
        WHERE symbol = ?
          AND completed_at IS NOT NULL
        ORDER BY completed_at DESC
        LIMIT 1
    """
    cursor = conn.execute(sql, (symbol,))
    row = cursor.fetchone()
    if row is None:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="research_missing_for_symbol: using neutral defaults",
            level="WARNING",
            symbol=symbol,
            result="fallback",
        )
        return "Neutral", 0.3
    return str(row[0]), float(row[1])


def _write_signals(
    conn: sqlite3.Connection,
    signals: list[StockSignal],
    run_date: datetime.date,
) -> None:
    """Write all signals to the signals table.

    All rows are written after ALL symbols are processed. If any INSERT
    fails, raises sqlite3.Error so the caller can raise SignalAgentError.

    Args:
        conn: An open sqlite3.Connection.
        signals: List of StockSignal objects to write.
        run_date: The run date for all rows.

    Raises:
        sqlite3.Error: On any DB error.
    """
    sql = """
        INSERT INTO signals
            (symbol, run_date, rsi, macd_signal, bollinger_position,
             atr, groq_confidence, signal_type, skip_reason, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    for signal in signals:
        conn.execute(
            sql,
            (
                signal.symbol,
                run_date.isoformat(),
                signal.rsi,
                signal.macd_signal,
                signal.bollinger_position,
                signal.atr,
                signal.groq_confidence,
                signal.signal_type,
                signal.skip_reason,
                signal.signalled_at.isoformat(timespec="seconds"),
            ),
        )
        if signal.signal_type == "BUY":
            log_agent_action(
                agent_name=AGENT_NAME,
                action="buy_signal written",
                level="INFO",
                symbol=signal.symbol,
                result="ok",
            )
        else:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"hold_signal written reason={signal.skip_reason}",
                level="INFO",
                symbol=signal.symbol,
                result="ok",
            )


# ---------------------------------------------------------------------------
# Private helpers — indicator extraction
# ---------------------------------------------------------------------------


def _extract_latest_indicators(
    indicators_df: object,
    symbol: str,
    run_date: datetime.date,
) -> tuple[float, float, float, float, float, float] | None:
    """Extract the most recent indicator row for a symbol.

    Args:
        indicators_df: DataFrame returned by add_indicators().
        symbol: NSE ticker symbol to filter.
        run_date: Upper bound for the date filter (latest date <= run_date).

    Returns:
        Tuple of (rsi, macd_hist, close, bb_upper, bb_lower, atr) for the
        most recent row, or None if no data available for the symbol.
    """
    import pandas as pd  # noqa: PLC0415

    df = indicators_df  # type: ignore[assignment]

    # Filter to this symbol
    if "symbol" in df.columns:
        sym_df = df[df["symbol"] == symbol].copy()
    else:
        sym_df = df.copy()

    if sym_df.empty:
        return None

    # Filter to rows with date <= run_date
    if "date" in sym_df.columns:
        sym_df["_date_parsed"] = pd.to_datetime(sym_df["date"]).dt.date
        sym_df = sym_df[sym_df["_date_parsed"] <= run_date]

    if sym_df.empty:
        return None

    # Get the latest row
    if "date" in sym_df.columns:
        sym_df = sym_df.sort_values("date")
    latest = sym_df.iloc[-1]

    # Extract values — these may be NaN if indicators couldn't be computed
    rsi = float(latest.get("rsi", float("nan")))
    macd_hist = float(latest.get("macd_hist", float("nan")))
    close = float(latest.get("close", float("nan")))
    bb_upper = float(latest.get("bb_upper", float("nan")))
    bb_lower = float(latest.get("bb_lower", float("nan")))
    atr = float(latest.get("atr", float("nan")))

    import math  # noqa: PLC0415

    if any(math.isnan(v) for v in [rsi, macd_hist, close, bb_upper, bb_lower, atr]):
        return None

    return rsi, macd_hist, close, bb_upper, bb_lower, atr


def _compute_bollinger_position(
    close: float,
    bb_upper: float,
    bb_lower: float,
) -> str:
    """Compute the Bollinger Band position for a close price.

    Args:
        close: Current closing price.
        bb_upper: Upper Bollinger Band value.
        bb_lower: Lower Bollinger Band value.

    Returns:
        "BELOW" if close < bb_lower, "ABOVE" if close > bb_upper,
        "MIDDLE" otherwise.
    """
    if close < bb_lower:
        return "BELOW"
    if close > bb_upper:
        return "ABOVE"
    return "MIDDLE"


# ---------------------------------------------------------------------------
# Private helpers — per-symbol processing
# ---------------------------------------------------------------------------


def _process_symbol(
    symbol: str,
    run_date: datetime.date,
    research_by_symbol: dict[str, tuple[str, float]],
    failed_ohlcv_symbols: set[str],
    indicators_df: object | None,
    groq_api_key_available: bool,
    gemini_client: object | None,
) -> StockSignal:
    """Process a single symbol through the full decision pipeline.

    Args:
        symbol: NSE ticker symbol.
        run_date: The run date.
        research_by_symbol: Dict mapping symbol to (sentiment, confidence).
        failed_ohlcv_symbols: Set of symbols for which OHLCV fetch failed.
        indicators_df: Full indicators DataFrame, or None if unavailable.
        groq_api_key_available: Whether the Groq API key is configured.
        gemini_client: Gemini client instance, or None if not configured.

    Returns:
        StockSignal with the final decision for this symbol.
    """
    signalled_at = datetime.datetime.now(tz=IST)
    sentiment, research_confidence = research_by_symbol.get(
        symbol, ("Neutral", 0.3)
    )

    # OHLCV or indicator failure cases
    if symbol in failed_ohlcv_symbols:
        return StockSignal(
            symbol=symbol,
            rsi=0.0,
            macd_signal="HOLD",
            bollinger_position="MIDDLE",
            atr=0.0,
            groq_confidence=LLM_UNAVAILABLE_SENTINEL,
            signal_type="HOLD",
            skip_reason="ohlcv_fetch_failed",
            signalled_at=signalled_at,
        )

    if indicators_df is None:
        return StockSignal(
            symbol=symbol,
            rsi=0.0,
            macd_signal="HOLD",
            bollinger_position="MIDDLE",
            atr=0.0,
            groq_confidence=LLM_UNAVAILABLE_SENTINEL,
            signal_type="HOLD",
            skip_reason="insufficient_indicator_data",
            signalled_at=signalled_at,
        )

    # Extract latest indicators for this symbol
    extracted = _extract_latest_indicators(indicators_df, symbol, run_date)
    if extracted is None:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="insufficient_indicator_data",
            level="WARNING",
            symbol=symbol,
            result="skipped",
        )
        return StockSignal(
            symbol=symbol,
            rsi=0.0,
            macd_signal="HOLD",
            bollinger_position="MIDDLE",
            atr=0.0,
            groq_confidence=LLM_UNAVAILABLE_SENTINEL,
            signal_type="HOLD",
            skip_reason="insufficient_indicator_data",
            signalled_at=signalled_at,
        )

    rsi, macd_hist, close, bb_upper, bb_lower, atr = extracted

    # Step 2: Compute Bollinger position (always, for audit trail)
    bollinger_position = _compute_bollinger_position(close, bb_upper, bb_lower)

    # Step 1: Technical BUY check
    macd_signal_str = "BUY" if macd_hist > 0 else "HOLD"
    technical_buy = (rsi < RSI_BUY_THRESHOLD) and (macd_hist > 0)

    if not technical_buy:
        return StockSignal(
            symbol=symbol,
            rsi=rsi,
            macd_signal=macd_signal_str,
            bollinger_position=bollinger_position,
            atr=atr,
            groq_confidence=LLM_UNAVAILABLE_SENTINEL,
            signal_type="HOLD",
            skip_reason="no_technical_buy_signal",
            signalled_at=signalled_at,
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"technical_buy_signal rsi={rsi:.1f} macd_hist={macd_hist:.4f}",
        level="DEBUG",
        symbol=symbol,
        result="ok",
    )

    # Step 3: Sentiment filter
    if sentiment == "Negative":
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"negative_sentiment_block confidence={research_confidence:.2f}",
            level="INFO",
            symbol=symbol,
            result="skipped",
        )
        return StockSignal(
            symbol=symbol,
            rsi=rsi,
            macd_signal=macd_signal_str,
            bollinger_position=bollinger_position,
            atr=atr,
            groq_confidence=LLM_UNAVAILABLE_SENTINEL,
            signal_type="HOLD",
            skip_reason="negative_sentiment",
            signalled_at=signalled_at,
        )

    # Step 4: Groq advisory check
    groq_confidence: float = LLM_UNAVAILABLE_SENTINEL
    final_signal_type = "BUY"
    skip_reason: str | None = None

    if groq_api_key_available:
        groq_result = _call_groq(
            symbol=symbol,
            sentiment=sentiment,
            research_confidence=research_confidence,
            rsi=rsi,
            macd_hist=macd_hist,
            bollinger_position=bollinger_position,
        )

        if groq_result is not None:
            groq_confidence = groq_result
        else:
            # Groq failed — try Gemini fallback
            if gemini_client is not None:
                gemini_result = _call_gemini_fallback(
                    symbol=symbol,
                    sentiment=sentiment,
                    research_confidence=research_confidence,
                    rsi=rsi,
                    macd_hist=macd_hist,
                    bollinger_position=bollinger_position,
                    gemini_client=gemini_client,
                )
                if gemini_result is not None:
                    groq_confidence = gemini_result
                else:
                    log_agent_action(
                        agent_name=AGENT_NAME,
                        action="llm_unavailable: keeping rule_based decision",
                        level="WARNING",
                        symbol=symbol,
                        result="fallback",
                    )
                    groq_confidence = LLM_UNAVAILABLE_SENTINEL
            else:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="llm_unavailable: keeping rule_based decision",
                    level="WARNING",
                    symbol=symbol,
                    result="fallback",
                )
                groq_confidence = LLM_UNAVAILABLE_SENTINEL

        # Only apply threshold check if we got a real confidence score
        if groq_confidence != LLM_UNAVAILABLE_SENTINEL:
            if groq_confidence < GROQ_CONFIDENCE_THRESHOLD:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=(
                        f"groq_low_confidence={groq_confidence:.2f}: "
                        f"downgrading BUY to HOLD"
                    ),
                    level="INFO",
                    symbol=symbol,
                    result="skipped",
                )
                final_signal_type = "HOLD"
                skip_reason = "groq_low_confidence"

    return StockSignal(
        symbol=symbol,
        rsi=rsi,
        macd_signal=macd_signal_str,
        bollinger_position=bollinger_position,
        atr=atr,
        groq_confidence=groq_confidence,
        signal_type=final_signal_type,
        skip_reason=skip_reason,
        signalled_at=signalled_at,
    )


# ---------------------------------------------------------------------------
# Private helpers — LLM calls
# ---------------------------------------------------------------------------


def _build_llm_prompt(
    sentiment: str,
    research_confidence: float,
    rsi: float,
    macd_hist: float,
    bollinger_position: str,
) -> str:
    """Build the LLM prompt for Groq and Gemini.

    Args:
        sentiment: Research sentiment string.
        research_confidence: Research confidence float.
        rsi: RSI value.
        macd_hist: MACD histogram value.
        bollinger_position: Bollinger band position string.

    Returns:
        Formatted prompt string.
    """
    bb_label = (
        "below"
        if bollinger_position == "BELOW"
        else "above"
        if bollinger_position == "ABOVE"
        else "middle"
    )
    macd_label = "bullish" if macd_hist > 0 else "bearish"
    return (
        f"Evening thesis: {sentiment} sentiment "
        f"(confidence: {research_confidence:.2f}).\n"
        f"Morning technicals: RSI={rsi:.1f}, MACD={macd_label}, "
        f"BB={bb_label} band.\n"
        'Does the thesis still hold? Reply JSON only: '
        '{"confidence": 0.0-1.0, "reasoning": "one sentence"}'
    )


def _parse_llm_confidence(text: str) -> float | None:
    """Parse a confidence float from an LLM JSON response.

    Strips markdown code fences if present, parses JSON, extracts
    the 'confidence' field, and clamps to [0.0, 1.0].

    Args:
        text: Raw LLM response text.

    Returns:
        Clamped confidence float, or None on any parse failure.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    confidence_raw = data.get("confidence")
    if confidence_raw is None:
        return None
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return None

    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, confidence))


def _call_groq(
    symbol: str,
    sentiment: str,
    research_confidence: float,
    rsi: float,
    macd_hist: float,
    bollinger_position: str,
) -> float | None:
    """Call the Groq API and return confidence, or None on failure.

    Args:
        symbol: NSE ticker symbol (for logging).
        sentiment: Research sentiment string.
        research_confidence: Research confidence float.
        rsi: RSI value.
        macd_hist: MACD histogram value.
        bollinger_position: Bollinger band position string.

    Returns:
        Clamped confidence float on success, None on any failure.
    """
    prompt = _build_llm_prompt(
        sentiment=sentiment,
        research_confidence=research_confidence,
        rsi=rsi,
        macd_hist=macd_hist,
        bollinger_position=bollinger_position,
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 100,
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            GROQ_API_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=GROQ_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except requests.RequestException as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"groq_failed: {exc}, trying gemini fallback",
            level="WARNING",
            symbol=symbol,
            result="retry",
        )
        return None
    except (KeyError, json.JSONDecodeError) as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"groq_failed: {exc}, trying gemini fallback",
            level="WARNING",
            symbol=symbol,
            result="retry",
        )
        return None

    confidence = _parse_llm_confidence(content)
    if confidence is None:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="groq_failed: parse error, trying gemini fallback",
            level="WARNING",
            symbol=symbol,
            result="retry",
        )
        return None

    # Extract reasoning for logging (best-effort)
    reasoning = ""
    try:
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE
        )
        data = json.loads(cleaned)
        reasoning = str(data.get("reasoning", ""))
    except (json.JSONDecodeError, KeyError):
        pass

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"groq_confidence={confidence:.2f} reasoning={reasoning}",
        level="INFO",
        symbol=symbol,
        result="ok",
    )
    return confidence


def _call_gemini_fallback(
    symbol: str,
    sentiment: str,
    research_confidence: float,
    rsi: float,
    macd_hist: float,
    bollinger_position: str,
    gemini_client: object,
) -> float | None:
    """Call Gemini as fallback when Groq fails; return confidence or None.

    Args:
        symbol: NSE ticker symbol (for logging).
        sentiment: Research sentiment string.
        research_confidence: Research confidence float.
        rsi: RSI value.
        macd_hist: MACD histogram value.
        bollinger_position: Bollinger band position string.
        gemini_client: Initialised Gemini client instance.

    Returns:
        Clamped confidence float on success, None on any failure.
    """
    prompt = _build_llm_prompt(
        sentiment=sentiment,
        research_confidence=research_confidence,
        rsi=rsi,
        macd_hist=macd_hist,
        bollinger_position=bollinger_position,
    )

    client: genai.Client = gemini_client  # type: ignore[assignment]

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_SIGNAL_SYSTEM_PROMPT
            ),
        )
        raw_text = response.text
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"gemini_fallback_failed: {exc}",
            level="WARNING",
            symbol=symbol,
            result="fallback",
        )
        return None

    if raw_text is None:
        return None

    confidence = _parse_llm_confidence(raw_text)
    if confidence is None:
        return None

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"gemini_fallback_confidence={confidence:.2f}",
        level="INFO",
        symbol=symbol,
        result="ok",
    )
    return confidence
