"""Data Collector Agent — refreshes fundamentals_history for the full Nifty 200 universe.

Runs as the first step of the evening session. Calls fetch_historical_fundamentals()
which handles per-symbol staleness (skips symbols fetched within 45 days). Runs
sanity checks on coverage and ROE plausibility after the fetch. Sends alert if
coverage drops below 80% but does not halt the pipeline — only raises
DataCollectorError on total failure (all symbols fail or DB inaccessible).
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_nifty200_symbols
from src.data.fundamentals import FundamentalsError, fetch_historical_fundamentals
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert

_IST = ZoneInfo("Asia/Kolkata")
AGENT_NAME = "data_collector_agent"
COVERAGE_ALERT_THRESHOLD = 0.80
ROE_MIN = -0.50
ROE_MAX = 2.00


class DataCollectorError(Exception):
    """Raised when the data collector fails entirely (all symbols fail or DB inaccessible)."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase
        self.message = message


@dataclass
class DataCollectorResult:
    """Summary of a data collection run."""

    symbols_attempted: int
    symbols_fresh_skipped: int
    symbols_fetched: int
    symbols_failed: int
    coverage_pct: float
    sanity_passed: bool
    run_date: datetime.date


def run_data_collector_agent(run_date: datetime.date | None = None) -> DataCollectorResult:
    """Refresh fundamentals_history for the full Nifty 200 universe.

    Fetches the current Nifty 200 symbol list, calls fetch_historical_fundamentals()
    (which handles per-symbol staleness — skips symbols with all-fresh data within
    45 days), then runs coverage and ROE plausibility sanity checks. Sends an alert
    if coverage drops below 80% but does not halt the pipeline on partial failure.

    Args:
        run_date: The trading date for this run. Defaults to today in IST.

    Returns:
        DataCollectorResult with counts, coverage, and sanity check outcome.

    Raises:
        DataCollectorError: If the Nifty 200 symbol list cannot be fetched, or if
                            the fundamentals DB write fails entirely.
    """
    if run_date is None:
        run_date = datetime.datetime.now(_IST).date()

    db_path = settings.database_url.replace("sqlite:///", "")

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"starting: run_date={run_date}",
        level="INFO",
    )

    # --- Step 1: Get symbol universe ---
    try:
        symbols = fetch_nifty200_symbols()
    except FetchError as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"symbol_fetch_failed: {exc}",
            level="ERROR",
        )
        raise DataCollectorError(
            phase="symbol_fetch",
            message=f"Failed to fetch Nifty 200 symbol list: {exc}",
        ) from exc

    symbols_attempted = len(symbols)
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"symbols_loaded: {symbols_attempted} Nifty 200 symbols",
        level="INFO",
    )

    # --- Step 2: Snapshot how many symbols already have fresh data pre-fetch ---
    symbols_fresh_before = _count_fresh_symbols(db_path)

    # --- Step 3: Run the fundamentals refresh ---
    # fetch_historical_fundamentals() handles staleness per-symbol internally.
    # Raises FundamentalsError only on DB-level failure, not on individual symbol failures.
    try:
        fetch_historical_fundamentals(symbols)
    except (FundamentalsError, ValueError) as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"fundamentals_fetch_failed: {exc}",
            level="ERROR",
        )
        raise DataCollectorError(
            phase="fundamentals_fetch",
            message=f"fundamentals_history write failed: {exc}",
        ) from exc

    # --- Step 4: Measure outcome ---
    symbols_fresh_after = _count_fresh_symbols(db_path)
    symbols_fresh_skipped = min(symbols_fresh_before, symbols_fresh_after)
    symbols_fetched = max(0, symbols_fresh_after - symbols_fresh_before)
    symbols_failed = max(0, symbols_attempted - symbols_fresh_after)
    coverage_pct = symbols_fresh_after / symbols_attempted if symbols_attempted > 0 else 0.0

    # --- Step 5: Sanity checks ---
    coverage_ok = coverage_pct >= COVERAGE_ALERT_THRESHOLD
    roe_ok = _check_roe_plausibility(db_path)
    sanity_passed = coverage_ok and roe_ok

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"sanity_check: coverage={coverage_pct:.1%} "
            f"({'OK' if coverage_ok else 'LOW'}), "
            f"roe_plausibility={'OK' if roe_ok else 'FAIL'}"
        ),
        level="INFO" if sanity_passed else "WARNING",
        data_quality_score=coverage_pct,
    )

    if not sanity_passed:
        _emit_sanity_alert(
            coverage_ok=coverage_ok,
            roe_ok=roe_ok,
            coverage_pct=coverage_pct,
            symbols_attempted=symbols_attempted,
        )

    result = DataCollectorResult(
        symbols_attempted=symbols_attempted,
        symbols_fresh_skipped=symbols_fresh_skipped,
        symbols_fetched=symbols_fetched,
        symbols_failed=symbols_failed,
        coverage_pct=coverage_pct,
        sanity_passed=sanity_passed,
        run_date=run_date,
    )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"completed: attempted={symbols_attempted}, "
            f"fresh_skipped={symbols_fresh_skipped}, "
            f"fetched={symbols_fetched}, "
            f"failed={symbols_failed}, "
            f"coverage={coverage_pct:.1%}, "
            f"sanity={'passed' if sanity_passed else 'FAILED'}"
        ),
        level="INFO",
        data_quality_score=coverage_pct,
    )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_fresh_symbols(db_path: str) -> int:
    """Count distinct symbols in fundamentals_history with at least one row within 45 days.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        Count of symbols with fresh data. Returns 0 on any DB error.
    """
    cutoff = datetime.datetime.now(_IST) - datetime.timedelta(days=45)
    cutoff_str = cutoff.isoformat()

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT symbol)
                FROM fundamentals_history
                WHERE fetched_at_ist >= ?
                """,
                (cutoff_str,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _check_roe_plausibility(db_path: str) -> bool:
    """Check that ROE values written in the last 24 hours fall within [-0.50, 2.00].

    Only checks recent rows so we validate what was just written, not the full
    historical archive (which may have extreme values from early years).

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        True if all checked ROE values are plausible or if the table is unreadable.
        Returns False (and logs) if any ROE value is outside [-0.50, 2.00].
    """
    cutoff = datetime.datetime.now(_IST) - datetime.timedelta(hours=24)
    cutoff_str = cutoff.isoformat()

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            rows = conn.execute(
                """
                SELECT symbol, roe
                FROM fundamentals_history
                WHERE fetched_at_ist >= ?
                  AND roe IS NOT NULL
                  AND data_quality != 'missing'
                """,
                (cutoff_str,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return True  # DB unreadable — don't block pipeline on uncertain data

    for symbol, roe in rows:
        try:
            roe_float = float(roe)
        except (TypeError, ValueError):
            continue
        if not (ROE_MIN <= roe_float <= ROE_MAX):
            log_agent_action(
                agent_name=AGENT_NAME,
                action=(
                    f"roe_plausibility_fail: {symbol} roe={roe_float:.4f} "
                    f"outside [{ROE_MIN}, {ROE_MAX}]"
                ),
                symbol=symbol,
                level="WARNING",
            )
            return False

    return True


def _emit_sanity_alert(
    coverage_ok: bool,
    roe_ok: bool,
    coverage_pct: float,
    symbols_attempted: int,
) -> None:
    """Build and send the sanity failure alert. Never raises.

    Args:
        coverage_ok: Whether coverage threshold was met.
        roe_ok: Whether ROE plausibility check passed.
        coverage_pct: Actual coverage fraction (0.0-1.0).
        symbols_attempted: Total symbols in the Nifty 200 universe.
    """
    lines: list[str] = []
    if not coverage_ok:
        lines.append(
            f"Coverage low: {coverage_pct:.1%} of {symbols_attempted} symbols have "
            f"fresh fundamentals (threshold: {COVERAGE_ALERT_THRESHOLD:.0%}). "
            "Screener quality filter will run on incomplete data."
        )
    if not roe_ok:
        lines.append(
            f"ROE plausibility check failed: one or more symbols have ROE outside "
            f"[{ROE_MIN:.0%}, {ROE_MAX:.0%}] — possible Screener.in data corruption. "
            "Inspect fundamentals_history table."
        )
    _safe_send_alert(
        subject="[IndianTrader] Data Collector: fundamentals sanity check FAILED",
        message="\n\n".join(lines),
    )


def _safe_send_alert(subject: str, message: str) -> None:
    """Send alert, swallowing exceptions so the pipeline never crashes over notifications.

    Args:
        subject: Alert email/notification subject.
        message: Alert body text.
    """
    try:
        send_alert(subject=subject, message=message)
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"send_alert_failed: {exc}",
            level="ERROR",
        )
