"""Monitor Agent — position monitoring during market hours for the Indian Trader pipeline.

Runs every 5 minutes during market hours (09:15-15:30 IST). Checks all open
positions against stop-loss and take-profit levels via PaperTrader.check_gtts(),
tightens stop-losses when the regime filter or LLM sentiment warrants it, and
runs GTT reconciliation every 30 minutes.

At 15:35 IST, checks if Nifty 50 dropped >3% and triggers an emergency rescreen.

Kill switch detection is informational only — does not halt monitoring of existing
positions, since halting new trades is the risk_agent's responsibility.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.agents.risk_agent import (
    CONSECUTIVE_LOSSES_KILL_SWITCH,
    DRAWDOWN_KILL_SWITCH_PCT,
    KILL_SWITCH_MIN_TRADES,
    SHARPE_KILL_SWITCH,
    WIN_RATE_KILL_SWITCH_PCT,
)
from src.agents.screener_agent import ScreenerAgentError, run_screener_agent
from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_ohlcv, fetch_sector_indices
from src.execution.paper_trader import PaperTrader
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "monitor_agent"
STARTING_CAPITAL: float = 10_000.0
PRICE_FETCH_LOOKBACK_DAYS: int = 5
NIFTY_EMERGENCY_DROP_PCT: float = 3.0
STOP_LOSS_ATR_NORMAL: float = 2.0
STOP_LOSS_ATR_TIGHT: float = 1.0
LLM_NEGATIVE_CONFIDENCE_THRESHOLD: float = 0.8
NEGATIVE_SENTIMENT: str = "Negative"
TIGHTEN_REGIMES: frozenset[str] = frozenset({"BELOW_200DMA", "BELOW_200DMA_10DAYS"})
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

MARKET_OPEN_HOUR: int = 9
MARKET_OPEN_MINUTE: int = 15
MARKET_CLOSE_HOUR: int = 15
MARKET_CLOSE_MINUTE: int = 30
GTT_RECONCILIATION_INTERVAL_MINUTES: int = 30
EMERGENCY_RESCREEN_HOUR: int = 15
EMERGENCY_RESCREEN_MINUTE: int = 35
NIFTY_DROP_THRESHOLD_PCT: float = 3.0
STOP_LOSS_ATR_MULTIPLIER_TIGHT: float = 1.0
LLM_TIGHTEN_CONFIDENCE_THRESHOLD: float = 0.8
LLM_TIGHTEN_SENTIMENT: str = "Negative"

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class MonitorAgentError(Exception):
    """Raised when the Monitor Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase of the agent failed. Valid values:
            'db_read', 'paper_trader_init', 'price_fetch',
            'gtt_check', 'gtt_reconciliation', 'stop_tighten',
            'emergency_rescreen'.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with message and phase identifier."""
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorResult:
    """Output of run_monitor_agent().

    Attributes:
        positions_checked: Number of open positions evaluated.
        exits_triggered: List of dicts from PaperTrader.check_gtts() — each has
            symbol, exit_price, exit_reason, trade_id.
        stops_tightened: Number of positions whose stop-loss was tightened
            (regime or LLM).
        gtt_reconciliation_ran: True if the 30-minute reconciliation ran this tick.
        kill_switch_detected: True if any kill switch condition was detected
            during this run. No new changes made; existing monitoring continues.
        emergency_rescreen_triggered: True if Nifty dropped > 3% and
            screener_agent was re-invoked.
        completed_at: IST datetime when this run finished.
    """

    positions_checked: int
    exits_triggered: list[dict[str, object]]
    stops_tightened: int
    gtt_reconciliation_ran: bool
    kill_switch_detected: bool
    emergency_rescreen_triggered: bool
    completed_at: datetime.datetime


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_db_path(db_path_override: str | None) -> str:
    """Resolve the SQLite database path from override or settings.

    Args:
        db_path_override: Explicit path, or None to derive from settings.

    Returns:
        Absolute path string to the SQLite file.
    """
    if db_path_override is not None:
        return db_path_override
    raw_url: str = settings.database_url
    if raw_url.startswith("sqlite:///"):
        return raw_url[len("sqlite:///"):]
    if raw_url.startswith("sqlite://"):
        return raw_url[len("sqlite://"):]
    return raw_url


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL pragmas.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        Configured sqlite3.Connection with row_factory set.

    Raises:
        sqlite3.Error: On connection or pragma failure.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _is_market_hours(current_time: datetime.datetime) -> bool:
    """Return True when current_time falls within 09:15–15:30 IST on a weekday.

    Args:
        current_time: IST-aware datetime.

    Returns:
        True if within market hours, False otherwise.
    """
    if current_time.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_dt = current_time.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0
    )
    close_dt = current_time.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )
    return open_dt <= current_time <= close_dt


def _fetch_atr(
    conn: sqlite3.Connection, symbol: str, run_date: datetime.date
) -> float:
    """Fetch ATR for a symbol from the signals table.

    Primary: today's signal with signal_type='BUY'.
    Fallback: most recent signal for this symbol (any date).
    If neither found: returns 0.0.

    Args:
        conn: Open SQLite connection.
        symbol: NSE ticker symbol.
        run_date: The run date to try first.

    Returns:
        ATR float, or 0.0 if unavailable.
    """
    try:
        row = conn.execute(
            """
            SELECT atr FROM signals
            WHERE symbol = ? AND run_date = ? AND signal_type = 'BUY'
            ORDER BY id DESC LIMIT 1
            """,
            (symbol, run_date.isoformat()),
        ).fetchone()
        if row is not None:
            return float(row["atr"])

        row = conn.execute(
            """
            SELECT atr FROM signals
            WHERE symbol = ?
            ORDER BY run_date DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is not None:
            return float(row["atr"])
    except sqlite3.Error:
        pass

    return 0.0


def _fetch_regime(conn: sqlite3.Connection) -> str | None:
    """Fetch the most recent regime from screener_results.

    Args:
        conn: Open SQLite connection.

    Returns:
        Regime string (e.g. 'ABOVE_200DMA'), or None if table empty/missing.
    """
    try:
        row = conn.execute(
            """
            SELECT regime FROM screener_results
            WHERE run_date = (SELECT MAX(run_date) FROM screener_results)
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            return str(row["regime"])
    except sqlite3.Error:
        pass
    return None


def _fetch_llm_sentiment(
    conn: sqlite3.Connection, symbol: str
) -> tuple[str | None, float]:
    """Fetch latest research sentiment for a symbol.

    Args:
        conn: Open SQLite connection.
        symbol: NSE ticker symbol.

    Returns:
        Tuple of (sentiment string or None, confidence float).
    """
    try:
        row = conn.execute(
            """
            SELECT sentiment, confidence FROM research_reports
            WHERE symbol = ? AND completed_at IS NOT NULL
            ORDER BY completed_at DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is not None:
            return str(row["sentiment"]), float(row["confidence"])
    except sqlite3.Error:
        pass
    return None, 0.0


def _fetch_trades(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Fetch all completed trades ordered by closed_at.

    Args:
        conn: Open SQLite connection.

    Returns:
        List of dicts with keys: pnl, closed_at.
    """
    try:
        rows = conn.execute(
            "SELECT pnl, closed_at FROM trades ORDER BY closed_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _compute_peak_equity(trades: list[dict[str, object]]) -> float:
    """Compute peak portfolio equity from completed trades.

    Args:
        trades: List of trade dicts with 'pnl' key.

    Returns:
        Peak equity float.
    """
    peak = STARTING_CAPITAL
    running = STARTING_CAPITAL
    for trade in trades:
        running += float(trade["pnl"])  # type: ignore[arg-type]
        if running > peak:
            peak = running
    return peak


def _check_kill_switches(
    trades: list[dict[str, object]], portfolio_equity: float
) -> tuple[bool, str | None]:
    """Evaluate all kill switch conditions.

    Args:
        trades: All completed trades from the trades table.
        portfolio_equity: Current total equity (STARTING_CAPITAL + total_pnl).

    Returns:
        Tuple of (kill_switch_detected, reason_string_or_None).
    """
    # Drawdown check (fires regardless of trade count)
    peak_equity = _compute_peak_equity(trades)
    if peak_equity > 0:
        drawdown_pct = (peak_equity - portfolio_equity) / peak_equity * 100.0
        if drawdown_pct > DRAWDOWN_KILL_SWITCH_PCT:
            return True, f"drawdown_15pct ({drawdown_pct:.1f}%)"

    # Consecutive losses (fires regardless of min trades)
    if len(trades) >= CONSECUTIVE_LOSSES_KILL_SWITCH:
        last_n = trades[-CONSECUTIVE_LOSSES_KILL_SWITCH:]
        if all(float(t["pnl"]) <= 0 for t in last_n):  # type: ignore[arg-type]
            return True, f"consecutive_losses_{CONSECUTIVE_LOSSES_KILL_SWITCH}"

    # Win rate (only after KILL_SWITCH_MIN_TRADES completed trades)
    if len(trades) >= KILL_SWITCH_MIN_TRADES:
        wins = sum(1 for t in trades if float(t["pnl"]) > 0)  # type: ignore[arg-type]
        win_rate = wins / len(trades) * 100.0
        if win_rate < WIN_RATE_KILL_SWITCH_PCT:
            return True, f"win_rate_below_40pct ({win_rate:.1f}%)"

    return False, None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_monitor_agent(
    run_date: datetime.date | None = None,
    current_time: datetime.datetime | None = None,
    db_path_override: str | None = None,
) -> MonitorResult:
    """Run one tick of the monitor loop.

    Called by the orchestrator every 5 minutes during 09:15-15:30 IST.
    The orchestrator handles scheduling; this function runs once per call
    with no internal sleep loops.

    Args:
        run_date: Date to run for. Defaults to today in IST.
        current_time: IST-aware datetime of this tick. Defaults to now.
            Used to decide whether GTT reconciliation runs (minute % 30 == 0)
            and whether to check for emergency rescreen (hour == 15, minute == 35).
        db_path_override: Absolute path to SQLite DB. When None, derived from
            settings.database_url. Used in tests.

    Returns:
        MonitorResult summarising this tick's activity.

    Raises:
        MonitorAgentError: On fatal DB or PaperTrader failures.
    """
    # --- Step 1: Resolve run_date, current_time, db_path ---
    now_ist = datetime.datetime.now(IST)
    if current_time is None:
        current_time = now_ist
    if run_date is None:
        run_date = current_time.date()

    db_path = _resolve_db_path(db_path_override)

    log_agent_action(
        agent_name=AGENT_NAME,
        action="monitor_tick_started",
        level="INFO",
        result=current_time.isoformat(),
    )

    # --- Step 2: Market hours gate ---
    if not _is_market_hours(current_time):
        completed_at = datetime.datetime.now(IST)
        log_agent_action(
            agent_name=AGENT_NAME,
            action="monitor_tick_complete",
            level="INFO",
            result="outside_market_hours",
        )
        return MonitorResult(
            positions_checked=0,
            exits_triggered=[],
            stops_tightened=0,
            gtt_reconciliation_ran=False,
            kill_switch_detected=False,
            emergency_rescreen_triggered=False,
            completed_at=completed_at,
        )

    # --- Instantiate PaperTrader ---
    try:
        pt = PaperTrader(db_path)
    except (ValueError, sqlite3.Error) as exc:
        raise MonitorAgentError(
            f"PaperTrader init failed: {exc}", phase="paper_trader_init"
        ) from exc

    # --- Open DB connection for direct queries ---
    try:
        conn = _open_db(db_path)
    except sqlite3.Error as exc:
        raise MonitorAgentError(
            f"DB connection failed: {exc}", phase="db_read"
        ) from exc

    exits_triggered: list[dict[str, object]] = []
    stops_tightened: int = 0
    gtt_reconciliation_ran: bool = False
    kill_switch_detected: bool = False
    emergency_rescreen_triggered: bool = False
    positions_checked_count: int = 0

    try:
        # --- Step 2: Get open positions ---
        try:
            positions = pt.get_positions()
        except sqlite3.Error as exc:
            raise MonitorAgentError(
                f"Failed to read positions: {exc}", phase="db_read"
            ) from exc

        positions_checked_count = len(positions)

        if not positions:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="no_open_positions",
                level="INFO",
                result="ok",
            )
        else:
            # --- Fetch current prices ---
            current_prices: dict[str, float] = {}
            for pos in positions:
                symbol: str = str(pos["symbol"])
                start_date = run_date - datetime.timedelta(days=PRICE_FETCH_LOOKBACK_DAYS)
                try:
                    df = fetch_ohlcv(
                        [symbol],
                        start_date=start_date,
                        end_date=run_date,
                        cache_expiry_hours=0,
                    )
                    if not df.empty:
                        sym_df = df[df["symbol"] == symbol] if "symbol" in df.columns else df
                        if not sym_df.empty:
                            current_prices[symbol] = float(sym_df["close"].iloc[-1])
                        else:
                            current_prices[symbol] = float(pos["current_price"])
                    else:
                        current_prices[symbol] = float(pos["current_price"])
                except FetchError:
                    log_agent_action(
                        agent_name=AGENT_NAME,
                        action="price_fetch_failed",
                        level="WARNING",
                        symbol=symbol,
                        result="using_stale_price",
                    )
                    current_prices[symbol] = float(pos["current_price"])

            # --- Step 3: Check GTTs ---
            exits_triggered = pt.check_gtts(current_prices)
            for exit_info in exits_triggered:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="gtt_exit_triggered",
                    level="INFO",
                    symbol=str(exit_info["symbol"]),
                    result=f"exit_reason={exit_info['exit_reason']} exit_price={exit_info['exit_price']}",
                )

            # --- Step 4: Stop-loss tightening ---
            # Re-read positions (some may have been closed)
            try:
                positions = pt.get_positions()
            except sqlite3.Error as exc:
                raise MonitorAgentError(
                    f"Failed to re-read positions after GTT check: {exc}",
                    phase="db_read",
                ) from exc

            if positions:
                try:
                    regime = _fetch_regime(conn)
                except sqlite3.Error:
                    regime = None

                tighten_stops = regime in TIGHTEN_REGIMES if regime else False

                for pos in positions:
                    sym = str(pos["symbol"])
                    entry_price = float(pos["entry_price"])
                    current_stop = float(pos["stop_loss"])

                    try:
                        atr = _fetch_atr(conn, sym, run_date)
                    except sqlite3.Error:
                        atr = 0.0

                    if atr == 0.0:
                        log_agent_action(
                            agent_name=AGENT_NAME,
                            action="atr_unavailable_skip_tighten",
                            level="WARNING",
                            symbol=sym,
                            result="skipped",
                        )
                        continue

                    tightened_this_symbol = False

                    # Regime tightening
                    if tighten_stops:
                        new_stop = entry_price - (atr * STOP_LOSS_ATR_TIGHT)
                        if new_stop > current_stop:
                            try:
                                pt.update_stop_loss(sym, new_stop)
                                stops_tightened += 1
                                tightened_this_symbol = True
                                current_stop = new_stop
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action="stop_tightened_regime",
                                    level="INFO",
                                    symbol=sym,
                                    result=f"old={pos['stop_loss']:.2f} new={new_stop:.2f}",
                                )
                            except ValueError as exc:
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action=f"stop_tighten_regime_failed: {exc}",
                                    level="WARNING",
                                    symbol=sym,
                                    result="error",
                                )
                        else:
                            log_agent_action(
                                agent_name=AGENT_NAME,
                                action="stop_not_tightened_already_tight",
                                level="DEBUG",
                                symbol=sym,
                                result=f"current_stop={current_stop:.2f} new_stop={new_stop:.2f}",
                            )

                    # LLM tightening
                    try:
                        sentiment, confidence = _fetch_llm_sentiment(conn, sym)
                    except sqlite3.Error:
                        sentiment, confidence = None, 0.0

                    if (
                        sentiment == NEGATIVE_SENTIMENT
                        and confidence > LLM_NEGATIVE_CONFIDENCE_THRESHOLD
                    ):
                        new_stop_llm = entry_price - (atr * STOP_LOSS_ATR_TIGHT)
                        if new_stop_llm > current_stop:
                            try:
                                pt.update_stop_loss(sym, new_stop_llm)
                                if not tightened_this_symbol:
                                    stops_tightened += 1
                                    tightened_this_symbol = True
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action="stop_tightened_llm",
                                    level="INFO",
                                    symbol=sym,
                                    result=(
                                        f"confidence={confidence:.2f} "
                                        f"old={current_stop:.2f} new={new_stop_llm:.2f}"
                                    ),
                                )
                            except ValueError as exc:
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action=f"stop_tighten_llm_failed: {exc}",
                                    level="WARNING",
                                    symbol=sym,
                                    result="error",
                                )
                        else:
                            if not tightened_this_symbol:
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action="stop_not_tightened_already_tight",
                                    level="DEBUG",
                                    symbol=sym,
                                    result=f"current_stop={current_stop:.2f} new_stop={new_stop_llm:.2f}",
                                )

        # --- Step 5: GTT reconciliation (every 30 minutes) ---
        if current_time.minute % GTT_RECONCILIATION_INTERVAL_MINUTES == 0:
            gtt_reconciliation_ran = True
            try:
                positions_for_recon = pt.get_positions()
            except sqlite3.Error as exc:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=f"gtt_reconciliation positions read failed: {exc}",
                    level="ERROR",
                    result="error",
                )
                positions_for_recon = []

            issues_found = 0
            for pos in positions_for_recon:
                sym = str(pos["symbol"])
                sl = float(pos["stop_loss"])
                tp = float(pos["take_profit"])
                entry = float(pos["entry_price"])

                is_valid = (
                    sl > 0
                    and tp > 0
                    and sl < entry
                    and tp > entry
                )

                if not is_valid:
                    issues_found += 1
                    log_agent_action(
                        agent_name=AGENT_NAME,
                        action="gtt_missing_or_invalid",
                        level="ERROR",
                        symbol=sym,
                        result=f"stop_loss={sl} take_profit={tp} entry={entry}",
                    )
                    send_alert(
                        subject=f"GTT Reconciliation: {sym} invalid GTT",
                        message=(
                            f"GTT reconciliation: {sym} has invalid "
                            f"stop_loss={sl} or take_profit={tp}. "
                            "Manual check required."
                        ),
                    )

                    # Attempt repair using ATR
                    try:
                        repair_atr = _fetch_atr(conn, sym, run_date)
                    except sqlite3.Error:
                        repair_atr = 0.0

                    if repair_atr > 0.0:
                        # Check whether regime is tightened
                        try:
                            repair_regime = _fetch_regime(conn)
                        except sqlite3.Error:
                            repair_regime = None
                        atr_mult = (
                            STOP_LOSS_ATR_TIGHT
                            if repair_regime in TIGHTEN_REGIMES
                            else STOP_LOSS_ATR_NORMAL
                        )
                        repaired_sl = entry - (repair_atr * atr_mult)
                        repaired_tp = entry + (repair_atr * atr_mult * 2.0)

                        if repaired_sl > 0 and repaired_sl < entry:
                            try:
                                pt.update_stop_loss(sym, repaired_sl)
                            except ValueError as exc:
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action=f"gtt_reconciliation stop_loss repair failed: {exc}",
                                    level="ERROR",
                                    symbol=sym,
                                    result="error",
                                )

                        if repaired_tp > entry:
                            try:
                                conn.execute(
                                    "UPDATE positions SET take_profit = ?, updated_at = ? WHERE symbol = ?",
                                    (
                                        repaired_tp,
                                        datetime.datetime.now(IST).isoformat(timespec="seconds"),
                                        sym,
                                    ),
                                )
                                conn.commit()
                            except sqlite3.Error as exc:
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action=f"gtt_reconciliation take_profit repair failed: {exc}",
                                    level="ERROR",
                                    symbol=sym,
                                    result="error",
                                )
                    else:
                        log_agent_action(
                            agent_name=AGENT_NAME,
                            action="gtt_reconciliation repair skipped: no ATR available",
                            level="WARNING",
                            symbol=sym,
                            result="alert_only",
                        )

            log_agent_action(
                agent_name=AGENT_NAME,
                action="gtt_reconciliation_complete",
                level="INFO",
                result=f"positions_checked={len(positions_for_recon)} issues_found={issues_found}",
            )

        # --- Step 6: Kill switch check ---
        trades = _fetch_trades(conn)
        try:
            pnl_summary = pt.get_pnl()
            portfolio_equity = STARTING_CAPITAL + pnl_summary["total_pnl"]
        except sqlite3.Error:
            portfolio_equity = STARTING_CAPITAL

        kill_switch_detected, kill_switch_reason = _check_kill_switches(
            trades, portfolio_equity
        )

        if kill_switch_detected:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="kill_switch_detected_monitor",
                level="CRITICAL",
                result=kill_switch_reason,
            )
            send_alert(
                subject="Monitor: Kill Switch Detected",
                message=(
                    f"Monitor: kill switch detected — {kill_switch_reason}. "
                    "No new trades will be opened. "
                    "Existing positions continue to be monitored."
                ),
            )

        # --- Step 7: Emergency rescreen check (15:35 IST only) ---
        if (
            current_time.hour == EMERGENCY_RESCREEN_HOUR
            and current_time.minute == EMERGENCY_RESCREEN_MINUTE
        ):
            try:
                nifty_df = fetch_sector_indices(
                    start_date=run_date - datetime.timedelta(days=10),
                    end_date=run_date,
                    cache_expiry_hours=0,
                )

                nifty_rows = None
                if not nifty_df.empty:
                    # Attempt to filter for Nifty 50 index
                    if "symbol" in nifty_df.columns:
                        nifty_mask = nifty_df["symbol"].str.contains(
                            "NIFTY50|^NSEI|Nifty 50", case=False, na=False
                        )
                        nifty_rows = nifty_df[nifty_mask]
                        if nifty_rows.empty:
                            nifty_rows = nifty_df
                    else:
                        nifty_rows = nifty_df

                if nifty_rows is not None and len(nifty_rows) >= 2:
                    sorted_rows = nifty_rows.sort_values("date") if "date" in nifty_rows.columns else nifty_rows
                    latest_close = float(sorted_rows["close"].iloc[-1])
                    prev_close = float(sorted_rows["close"].iloc[-2])

                    if prev_close > 0:
                        drop_pct = (prev_close - latest_close) / prev_close * 100.0
                        if drop_pct > NIFTY_EMERGENCY_DROP_PCT:
                            log_agent_action(
                                agent_name=AGENT_NAME,
                                action="emergency_rescreen_triggered",
                                level="WARNING",
                                result=f"nifty_drop={drop_pct:.1f}%",
                            )
                            try:
                                run_screener_agent(run_date=run_date)
                                emergency_rescreen_triggered = True
                                send_alert(
                                    subject="Emergency Rescreen: Nifty Drop",
                                    message=(
                                        f"Nifty 50 dropped {drop_pct:.1f}% today. "
                                        "Emergency rescreen completed."
                                    ),
                                )
                            except ScreenerAgentError as exc:
                                log_agent_action(
                                    agent_name=AGENT_NAME,
                                    action="emergency_rescreen_failed",
                                    level="ERROR",
                                    result=str(exc),
                                )
                                send_alert(
                                    subject="Emergency Rescreen Failed",
                                    message=f"Emergency rescreen failed: {exc}",
                                )

            except FetchError as exc:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="emergency_rescreen_nifty_fetch_failed",
                    level="ERROR",
                    result=str(exc),
                )

    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    completed_at = datetime.datetime.now(IST)

    log_agent_action(
        agent_name=AGENT_NAME,
        action="monitor_tick_complete",
        level="INFO",
        result=(
            f"positions_checked={positions_checked_count} "
            f"exits={len(exits_triggered)} "
            f"stops_tightened={stops_tightened} "
            f"kill_switch={kill_switch_detected}"
        ),
    )

    return MonitorResult(
        positions_checked=positions_checked_count,
        exits_triggered=exits_triggered,
        stops_tightened=stops_tightened,
        gtt_reconciliation_ran=gtt_reconciliation_ran,
        kill_switch_detected=kill_switch_detected,
        emergency_rescreen_triggered=emergency_rescreen_triggered,
        completed_at=completed_at,
    )
