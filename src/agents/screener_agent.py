"""Screener Agent for the Indian Trader stock selection pipeline.

Runs the complete three-step stock selection pipeline:
  quality filter → momentum ranking → regime filter

Writes the top 5 candidates to the screener_results table. Scheduled every
Monday at 22:00 IST by the orchestrator. Can also be invoked standalone for
emergency rescreens (e.g., when the Monitor Agent detects a Nifty 50
single-day close-to-close drop > 3%).

Output feeds directly into:
  - src/agents/research_agent.py — reads screener_results to determine symbols
  - src/agents/signal_agent.py — reads screener_results for morning confirmation

This module is a plain Python function. It does NOT use the Python Agent SDK.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_nifty50_symbols, fetch_nifty200_symbols, fetch_ohlcv, fetch_sector_indices
from src.data.fundamentals import FundamentalsError, get_fundamentals_for_date, get_nifty_universe_for_year
from src.strategy.momentum import compute_momentum
from src.strategy.quality_filter import apply_quality_filter
from src.strategy.regime import apply_regime_filter
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_info

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "screener_agent"
OHLCV_LOOKBACK_DAYS: int = 400
MIN_UNIVERSE_SIZE: int = 3          # mirrors quality_filter.MIN_UNIVERSE_SIZE
MAX_TOP_N: int = 5
MOMENTUM_TIEBREAKER_PCT: float = 2.0  # documented here; enforcement is inside compute_momentum

# WAL pragmas (applied to every SQLite connection)
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# DDL for screener_results table
_CREATE_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS screener_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    momentum_score REAL NOT NULL,
    quality_passed INTEGER NOT NULL,
    regime TEXT NOT NULL,
    position_size_multiplier REAL NOT NULL,
    screened_at TEXT NOT NULL,
    UNIQUE(symbol, run_date)
);
"""

# Project root for DB path resolution (two levels up from this file)
_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class ScreenerAgentError(Exception):
    """Raised when the Screener Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with a message and phase identifier.

        Args:
            message: Human-readable error description.
            phase: One of 'db_write', 'ohlcv_fetch', 'fundamentals_fetch',
                   'quality_filter', 'momentum', 'regime'.
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenerResult:
    """Result for a single top-5 candidate.

    Attributes:
        symbol: NSE ticker symbol.
        rank: Momentum rank (1 = highest momentum).
        momentum_score: 12-1 momentum score.
        quality_passed: True if passed all 5 hard quality filters.
        regime: Market regime string: ABOVE_200DMA, BELOW_200DMA, or BELOW_200DMA_10DAYS.
        position_size_multiplier: 1.0 / 0.5 / 0.0 based on regime.
        screened_at: IST timezone-aware datetime when this result was computed.
        run_date: Date for which the screen was run.
    """

    symbol: str
    rank: int
    momentum_score: float
    quality_passed: bool
    regime: str
    position_size_multiplier: float
    screened_at: datetime.datetime
    run_date: datetime.date


@dataclass(frozen=True)
class ScreenerAgentResult:
    """Full output of run_screener_agent().

    Attributes:
        run_date: Date the screen was run for.
        symbols_screened: Total number of symbols in the Nifty universe (input size).
        symbols_passed_quality: Number that passed all 5 quality filters.
        top5: Top 5 candidates. Empty list when thin_universe or regime_blocked.
        thin_universe: True when < 3 stocks passed quality filter.
        regime_blocked: True when BELOW_200DMA_10DAYS.
        completed_at: IST timezone-aware datetime when agent completed.
    """

    run_date: datetime.date
    symbols_screened: int
    symbols_passed_quality: int
    top5: list[ScreenerResult]
    thin_universe: bool
    regime_blocked: bool
    completed_at: datetime.datetime


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ist_now() -> datetime.datetime:
    """Return the current time as an IST timezone-aware datetime.

    Returns:
        Current datetime in Asia/Kolkata timezone.
    """
    return datetime.datetime.now(ZoneInfo("Asia/Kolkata"))


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
        An open sqlite3.Connection with isolation_level=None (autocommit).
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _setup_table(db_path: str) -> None:
    """Create screener_results table if it does not exist, then close connection.

    Args:
        db_path: Absolute path to the SQLite database file.

    Raises:
        ScreenerAgentError: If the DDL execution fails.
    """
    conn = _open_connection(db_path)
    try:
        conn.execute(_CREATE_TABLE_SQL)
    except sqlite3.Error as exc:
        conn.close()
        raise ScreenerAgentError(
            message=f"DB setup failed: {exc}",
            phase="db_write",
        ) from exc
    conn.close()


def _write_results(
    db_path: str,
    results: list[ScreenerResult],
    run_date: datetime.date,
) -> None:
    """Write screener results to the database using INSERT OR REPLACE.

    Opens a fresh connection, begins a transaction, inserts all rows, commits,
    checkpoints WAL, then closes. Empty results list is valid (thin_universe).

    Args:
        db_path: Absolute path to the SQLite database file.
        results: List of ScreenerResult objects to write.
        run_date: The run date (used for thin_universe case with zero results).

    Raises:
        ScreenerAgentError: If any DB operation fails.
    """
    conn = _open_connection(db_path)
    try:
        conn.execute("BEGIN")
        for r in results:
            conn.execute(
                """
                INSERT OR REPLACE INTO screener_results
                    (symbol, run_date, rank, momentum_score, quality_passed,
                     regime, position_size_multiplier, screened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.symbol,
                    r.run_date.isoformat(),
                    r.rank,
                    r.momentum_score,
                    1 if r.quality_passed else 0,
                    r.regime,
                    r.position_size_multiplier,
                    r.screened_at.isoformat(timespec="seconds"),
                ),
            )
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        raise ScreenerAgentError(
            message=f"DB write failed: {exc}",
            phase="db_write",
        ) from exc
    conn.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_screener_agent(
    run_date: datetime.date | None = None,
) -> ScreenerAgentResult:
    """Run the full 3-step screener pipeline and write top 5 to screener_results.

    Steps:
      0. DB setup — create screener_results table if not exists, close immediately.
      1. Fetch Nifty 50 universe, OHLCV, and fundamentals.
      2. Quality filter — eliminates stocks failing any of 5 hard filters.
      3. Momentum ranking — 12-1 factor, top N candidates.
      4. Regime filter — Nifty 50 200 DMA check; adjusts position_size_multiplier.
      5. Write results to DB (even if regime_blocked or thin_universe).

    Args:
        run_date: Date to run for. Defaults to datetime.date.today() in IST.

    Returns:
        ScreenerAgentResult with pipeline summary and top5 candidates.

    Raises:
        ScreenerAgentError: On fatal errors in any pipeline phase.
    """
    # Resolve run_date in IST
    if run_date is None:
        run_date = _ist_now().date()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"screener_run_started: {run_date}",
        level="INFO",
    )

    # ------------------------------------------------------------------
    # Step 0 — DB setup (before any data fetch)
    # ------------------------------------------------------------------
    db_path = _resolve_db_path()
    _setup_table(db_path)

    # ------------------------------------------------------------------
    # Step 1 — Fetch symbol universe
    # ------------------------------------------------------------------
    if run_date.year <= 2023:
        # Historical backtest: use hardcoded Nifty 50 constituent data
        nifty_symbols = get_nifty_universe_for_year(run_date.year)
    else:
        # Live paper/real trading: dispatch by configured universe
        if settings.nifty_universe == "nifty200":
            nifty_symbols = fetch_nifty200_symbols()
        else:
            nifty_symbols = fetch_nifty50_symbols()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"universe_fetched: {len(nifty_symbols)} symbols ({settings.nifty_universe})",
        level="INFO",
    )

    # ------------------------------------------------------------------
    # Step 1 — Fetch OHLCV (stock + sector indices)
    # ------------------------------------------------------------------
    end_date = run_date
    start_date = run_date - datetime.timedelta(days=OHLCV_LOOKBACK_DAYS)

    try:
        ohlcv_df = fetch_ohlcv(
            symbols=nifty_symbols,
            start_date=start_date,
            end_date=end_date,
            cache_expiry_hours=0,
        )
    except (FetchError, Exception) as exc:
        raise ScreenerAgentError(
            message=f"OHLCV fetch failed: {exc}",
            phase="ohlcv_fetch",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"ohlcv_fetched: {len(ohlcv_df)} rows",
        level="INFO",
    )

    try:
        sector_df = fetch_sector_indices(
            start_date=start_date,
            end_date=end_date,
            cache_expiry_hours=0,
        )
    except Exception as exc:
        raise ScreenerAgentError(
            message=f"Sector indices fetch failed: {exc}",
            phase="ohlcv_fetch",
        ) from exc

    # ------------------------------------------------------------------
    # Step 1 — Fetch fundamentals
    # ------------------------------------------------------------------
    try:
        fundamentals_df = get_fundamentals_for_date(
            symbols=nifty_symbols,
            as_of_date=run_date,
        )
    except (FundamentalsError, ValueError) as exc:
        raise ScreenerAgentError(
            message=f"Fundamentals fetch failed: {exc}",
            phase="fundamentals_fetch",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"fundamentals_fetched: {len(fundamentals_df)} symbols",
        level="INFO",
    )

    # ------------------------------------------------------------------
    # Step 2 — Quality filter
    # ------------------------------------------------------------------
    try:
        quality_df, filter_report = apply_quality_filter(
            fundamentals_df=fundamentals_df,
            ohlcv_df=ohlcv_df,
        )
    except ValueError as exc:
        raise ScreenerAgentError(
            message=f"Quality filter failed: {exc}",
            phase="quality_filter",
        ) from exc

    passed_count = filter_report.passed_count
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"quality_filter_complete: {passed_count} passed of {filter_report.universe_size}",
        level="INFO",
    )

    if filter_report.thin_universe:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"thin_universe: only {passed_count} stocks passed quality filter",
            level="WARNING",
        )
        send_alert(
            subject="Screener: thin universe",
            message=(
                f"Screener: thin universe — only {passed_count} stocks passed "
                f"quality filter. No watchlist today."
            ),
        )
        # Write nothing to DB for thin_universe (no top5 rows to write)
        _write_results(db_path, [], run_date)
        return ScreenerAgentResult(
            run_date=run_date,
            symbols_screened=len(nifty_symbols),
            symbols_passed_quality=passed_count,
            top5=[],
            thin_universe=True,
            regime_blocked=False,
            completed_at=_ist_now(),
        )

    # ------------------------------------------------------------------
    # Step 3 — Momentum ranking
    # ------------------------------------------------------------------
    try:
        ranked_df, momentum_report = compute_momentum(
            quality_df=quality_df,
            ohlcv_df=ohlcv_df,
            top_n=MAX_TOP_N,
        )
    except ValueError as exc:
        raise ScreenerAgentError(
            message=f"Momentum computation failed: {exc}",
            phase="momentum",
        ) from exc

    if ranked_df.empty:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="thin_universe: 0 symbols had sufficient momentum history",
            level="WARNING",
        )
        send_alert(
            subject="Screener: thin universe",
            message=(
                "Screener: thin universe — 0 symbols had sufficient momentum history. "
                "No watchlist today."
            ),
        )
        _write_results(db_path, [], run_date)
        return ScreenerAgentResult(
            run_date=run_date,
            symbols_screened=len(nifty_symbols),
            symbols_passed_quality=passed_count,
            top5=[],
            thin_universe=True,
            regime_blocked=False,
            completed_at=_ist_now(),
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"momentum_scored: {len(ranked_df)} candidates ranked",
        level="INFO",
    )

    # ------------------------------------------------------------------
    # Step 4 — Regime filter
    # ------------------------------------------------------------------
    # Extract Nifty 50 data — keep symbol column, apply_regime_filter ignores it
    nifty_ohlcv_df = sector_df[sector_df["symbol"] == "NIFTY_50"].copy()

    try:
        filtered_df, regime_result = apply_regime_filter(
            ranked_df=ranked_df,
            nifty_ohlcv_df=nifty_ohlcv_df,
            open_positions=None,
        )
    except ValueError as exc:
        raise ScreenerAgentError(
            message=f"Regime filter failed: {exc}",
            phase="regime",
        ) from exc

    regime_str: str = regime_result.regime
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"regime_status: {regime_str}",
        level="INFO",
    )

    # ------------------------------------------------------------------
    # Step 5 — Build ScreenerResult objects
    # ------------------------------------------------------------------
    screened_at = _ist_now()
    top5: list[ScreenerResult] = []

    regime_blocked = regime_str == "BELOW_200DMA_10DAYS"

    if regime_blocked:
        # filtered_df is empty when BELOW_200DMA_10DAYS; build from ranked_df
        log_agent_action(
            agent_name=AGENT_NAME,
            action="regime_blocked: BELOW_200DMA_10DAYS, no new positions",
            level="WARNING",
        )
        send_alert(
            subject="Screener: regime blocked",
            message=(
                "Screener: BELOW_200DMA_10DAYS for 10+ consecutive days. "
                "No new positions today. Position size multiplier = 0."
            ),
        )
        for _, row in ranked_df.iterrows():
            top5.append(
                ScreenerResult(
                    symbol=str(row["symbol"]),
                    rank=int(row["rank"]),
                    momentum_score=float(row["momentum_score"]),
                    quality_passed=True,
                    regime="BELOW_200DMA_10DAYS",
                    position_size_multiplier=0.0,
                    screened_at=screened_at,
                    run_date=run_date,
                )
            )
    else:
        # filtered_df has position_size_multiplier column
        for _, row in filtered_df.iterrows():
            top5.append(
                ScreenerResult(
                    symbol=str(row["symbol"]),
                    rank=int(row["rank"]),
                    momentum_score=float(row["momentum_score"]),
                    quality_passed=True,
                    regime=regime_str,
                    position_size_multiplier=float(row["position_size_multiplier"]),
                    screened_at=screened_at,
                    run_date=run_date,
                )
            )

    top5_symbols = [r.symbol for r in top5]
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"top5_selected: {top5_symbols}",
        level="INFO",
    )

    # ------------------------------------------------------------------
    # Log completion before write phase (prevent SQLITE_BUSY_SNAPSHOT)
    # ------------------------------------------------------------------
    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"screener_run_completed: {passed_count} passed quality, "
            f"top5={top5_symbols}"
        ),
        level="INFO",
        result="ok",
    )

    # ------------------------------------------------------------------
    # Write phase (fresh connection, explicit BEGIN/COMMIT)
    # ------------------------------------------------------------------
    _write_results(db_path, top5, run_date)

    # ------------------------------------------------------------------
    # Notify on successful completion
    # ------------------------------------------------------------------
    send_info(
        message=(
            f"Screener complete: {passed_count} stocks passed quality filter. "
            f"Top 5: {top5_symbols}. Regime: {regime_str}."
        )
    )

    return ScreenerAgentResult(
        run_date=run_date,
        symbols_screened=len(nifty_symbols),
        symbols_passed_quality=passed_count,
        top5=top5,
        thin_universe=False,
        regime_blocked=regime_blocked,
        completed_at=_ist_now(),
    )
