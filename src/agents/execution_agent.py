"""Execution Agent — human checkpoint gateway for approved trade signals.

Runs at 09:05 IST every trading morning. Reads APPROVED rows from
risk_approvals, sends a human confirmation request via Telegram + Gmail,
polls a checkpoint file for up to 8 minutes, validates current market prices
against approved prices, and places CNC orders via PaperTrader.

All orders are written to the orders table BEFORE placement (enforced by
PaperTrader.place_order). If no human confirmation arrives within the timeout
window, the agent enters safe mode and places zero trades.

Phase 4 scope: PaperTrader only — no Shoonya broker API.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_ohlcv
from src.execution.paper_trader import PaperTrader
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_checkpoint, send_info

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "execution_agent"
CHECKPOINT_FILE_PREFIX: str = "/tmp/indian-trader-checkpoint-"
CHECKPOINT_POLL_INTERVAL_SECS: int = 15
CHECKPOINT_TIMEOUT_SECS: int = 480  # 8 minutes
DEVIATION_RECALC_THRESHOLD: float = 0.005   # 0.5%
DEVIATION_SKIP_THRESHOLD: float = 0.015     # 1.5%
STOP_LOSS_ATR_MULTIPLIER: float = 2.0
STOP_LOSS_PCT_CAP: float = 0.03
TAKE_PROFIT_RATIO: float = 2.0
MAX_POSITION_PCT: float = 0.40
MAX_TRADE_AMOUNT: float = 10_000.0
STARTING_CAPITAL: float = 10_000.0
PRICE_FETCH_LOOKBACK_DAYS: int = 5
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

_DDL_EXECUTION_CHECKPOINTS: str = """
CREATE TABLE IF NOT EXISTS execution_checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED', 'TIMEOUT')),
    symbols     TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    resolved_at TEXT,
    UNIQUE(run_date)
);
"""


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class ExecutionAgentError(Exception):
    """Raised when the Execution Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase of the agent failed.
            Valid values: 'db_read', 'db_write', 'checkpoint', 'paper_trader_init'.
    """

    def __init__(self, message: str, phase: str) -> None:
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRecord:
    """One trade that was placed or skipped by the execution agent.

    Attributes:
        symbol: NSE ticker symbol.
        run_date: Date this execution was for.
        quantity: Shares ordered. 0 if skipped.
        entry_price: Actual entry price used (may differ from risk_approvals if recalculated).
        stop_loss: Stop-loss price used.
        take_profit: Take-profit price used.
        order_id: PaperTrader order table row ID. -1 if not placed.
        status: One of 'PLACED', 'SKIPPED_SLIPPAGE', 'SKIPPED_RECALC_ZERO',
            'SKIPPED_PRICE_FETCH_FAILED', 'SKIPPED_ORDER_ERROR'.
        deviation_pct: Price deviation from risk_approvals entry_price_approx.
            0.0 if no deviation check was performed.
        recalculated: True if position was recalculated due to >0.5% deviation.
        placed_at: IST timestamp. None if not placed.
    """

    symbol: str
    run_date: datetime.date
    quantity: int
    entry_price: float
    stop_loss: float
    take_profit: float
    order_id: int
    status: str
    deviation_pct: float
    recalculated: bool
    placed_at: datetime.datetime | None


@dataclass(frozen=True)
class ExecutionResult:
    """Full output of run_execution_agent().

    Attributes:
        run_date: Date the execution ran for.
        human_confirmed: True if human wrote the confirmation string to the checkpoint file.
        safe_mode: True if no trades placed due to timeout or other safe mode trigger.
        safe_mode_reason: 'timeout_no_confirmation', 'no_approved_trades', or None.
        orders_placed: List of OrderRecord with status='PLACED'.
        orders_skipped: List of OrderRecord with non-PLACED status.
        completed_at: IST timestamp when agent finished.
    """

    run_date: datetime.date
    human_confirmed: bool
    safe_mode: bool
    safe_mode_reason: str | None
    orders_placed: list[OrderRecord]
    orders_skipped: list[OrderRecord]
    completed_at: datetime.datetime


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ist_now() -> datetime.datetime:
    return datetime.datetime.now(tz=IST)


def _resolve_db_path(db_path_override: str | None) -> str:
    if db_path_override is not None:
        return db_path_override
    url = settings.database_url
    if url.startswith("sqlite:///"):
        remainder = url[len("sqlite:///"):]
    else:
        remainder = url
    if os.path.isabs(remainder):
        return remainder
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(project_root, remainder)


def _open_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _setup_table(db_path: str) -> None:
    try:
        conn = _open_connection(db_path)
        conn.execute(_DDL_EXECUTION_CHECKPOINTS)
        conn.close()
    except sqlite3.Error as exc:
        raise ExecutionAgentError(
            message=f"Failed to create execution_checkpoints table: {exc}",
            phase="db_write",
        ) from exc


def _checkpoint_file_path(run_date: datetime.date) -> str:
    return f"{CHECKPOINT_FILE_PREFIX}{run_date.isoformat()}.txt"


def _build_checkpoint_message(
    run_date: datetime.date,
    approved_rows: list[sqlite3.Row],
    watchlist_by_symbol: dict[str, sqlite3.Row],
) -> str:
    """Build the human-readable checkpoint notification message.

    Args:
        run_date: Execution date.
        approved_rows: APPROVED rows from risk_approvals.
        watchlist_by_symbol: Watchlist rows keyed by symbol, for context.

    Returns:
        Formatted checkpoint message string.
    """
    lines: list[str] = [
        f"EXECUTION CHECKPOINT - {run_date}",
        "",
        f"{len(approved_rows)} trades approved for execution:",
        "",
    ]
    for i, row in enumerate(approved_rows, start=1):
        symbol = row["symbol"]
        qty = row["quantity"]
        entry = float(row["entry_price_approx"])
        sl = float(row["stop_loss"])
        tp = float(row["take_profit"])
        wl = watchlist_by_symbol.get(symbol)
        rank = int(wl["rank"]) if wl else "N/A"
        sentiment = wl["sentiment"] if wl else "N/A"
        confidence = float(wl["confidence"]) if wl else 0.0
        score = int(wl["scorecard_score"]) if wl else "N/A"

        lines.append(
            f"{i}. {symbol}: BUY {qty} shares @ ~Rs.{entry:.0f}"
        )
        lines.append(
            f"   SL: Rs.{sl:.0f} | TP: Rs.{tp:.0f}"
        )
        if wl:
            lines.append(
                f"   Rank: {rank} | Sentiment: {sentiment} ({confidence:.0%}) | Score: {score}"
            )
        lines.append("")

    lines.append(
        f"To confirm: echo {run_date} > {_checkpoint_file_path(run_date)}"
    )
    lines.append("Deadline: 09:13 IST (8 minutes from now)")
    return "\n".join(lines)


def _write_checkpoint_pending(
    db_path: str,
    run_date: datetime.date,
    symbols: list[str],
    message: str,
) -> None:
    created_at = _ist_now().isoformat()
    try:
        conn = _open_connection(db_path)
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT OR REPLACE INTO execution_checkpoints
                (run_date, status, symbols, message, created_at, resolved_at)
            VALUES (?, 'PENDING', ?, ?, ?, NULL)
            """,
            (run_date.isoformat(), json.dumps(symbols), message, created_at),
        )
        conn.execute("COMMIT")
        conn.close()
    except sqlite3.Error as exc:
        raise ExecutionAgentError(
            message=f"Failed to write PENDING checkpoint: {exc}",
            phase="db_write",
        ) from exc


def _update_checkpoint_status(
    db_path: str,
    run_date: datetime.date,
    status: str,
) -> None:
    resolved_at = _ist_now().isoformat()
    try:
        conn = _open_connection(db_path)
        conn.execute("BEGIN")
        conn.execute(
            """
            UPDATE execution_checkpoints
            SET status = ?, resolved_at = ?
            WHERE run_date = ?
            """,
            (status, resolved_at, run_date.isoformat()),
        )
        conn.execute("COMMIT")
        conn.close()
    except sqlite3.Error as exc:
        raise ExecutionAgentError(
            message=f"Failed to update checkpoint to {status}: {exc}",
            phase="db_write",
        ) from exc


def _poll_checkpoint_file(run_date: datetime.date) -> bool:
    """Poll checkpoint file every CHECKPOINT_POLL_INTERVAL_SECS for CHECKPOINT_TIMEOUT_SECS.

    Returns True if the file content matches run_date.isoformat(). Returns False on timeout.
    File is deleted on both confirmation and timeout.

    Args:
        run_date: The date string expected in the checkpoint file.

    Returns:
        True if confirmed, False if timed out.
    """
    checkpoint_path = _checkpoint_file_path(run_date)
    deadline = time.monotonic() + CHECKPOINT_TIMEOUT_SECS

    while time.monotonic() < deadline:
        try:
            content = Path(checkpoint_path).read_text().strip()
            if content == run_date.isoformat():
                try:
                    Path(checkpoint_path).unlink()
                except OSError:
                    pass
                return True
        except OSError:
            pass  # File does not exist yet — keep polling
        time.sleep(CHECKPOINT_POLL_INTERVAL_SECS)

    # Timeout — clean up file if it exists
    try:
        Path(checkpoint_path).unlink()
    except OSError:
        pass
    return False


def _fetch_current_price(symbol: str, run_date: datetime.date) -> float | None:
    """Fetch the most recent close price for a symbol.

    Args:
        symbol: NSE ticker symbol.
        run_date: Execution run date (used as end_date upper bound).

    Returns:
        Most recent close price as float, or None on failure.
    """
    start_date = run_date - datetime.timedelta(days=PRICE_FETCH_LOOKBACK_DAYS)
    try:
        df = fetch_ohlcv(
            symbols=[symbol],
            start_date=start_date,
            end_date=run_date,
            cache_expiry_hours=0,
        )
        if df.empty:
            return None
        sym_df = df[df["symbol"] == symbol] if "symbol" in df.columns else df
        if sym_df.empty:
            return None
        return float(sym_df["close"].iloc[-1])
    except FetchError:
        return None


def _fetch_atr_from_signals(
    db_path: str,
    run_date: datetime.date,
    symbol: str,
) -> float | None:
    """Read ATR for a symbol from the signals table.

    Args:
        db_path: Path to SQLite database.
        run_date: Date to query.
        symbol: NSE ticker symbol.

    Returns:
        ATR float or None if not found.
    """
    try:
        conn = _open_connection(db_path)
        row = conn.execute(
            "SELECT atr FROM signals WHERE run_date = ? AND symbol = ?",
            (run_date.isoformat(), symbol),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return float(row["atr"])
    except sqlite3.Error:
        return None


def _recalculate_position(
    current_price: float,
    risk_amount: float,
    portfolio_equity: float,
    atr: float,
) -> tuple[int, float, float]:
    """Recalculate position sizing at current price.

    Args:
        current_price: Current market price in INR.
        risk_amount: Risk amount from risk_approvals.
        portfolio_equity: Current portfolio equity (used for 40% cap check).
        atr: ATR from signals table.

    Returns:
        Tuple of (quantity, stop_loss, take_profit). quantity=0 if cannot trade.
    """
    stop_distance = min(atr * STOP_LOSS_ATR_MULTIPLIER, current_price * STOP_LOSS_PCT_CAP)
    if stop_distance <= 0.0:
        return 0, 0.0, 0.0

    quantity = math.floor(risk_amount / stop_distance)
    if quantity < 1:
        return 0, 0.0, 0.0

    # 40% equity cap
    max_position_value = portfolio_equity * MAX_POSITION_PCT
    position_value = current_price * quantity
    if position_value > max_position_value:
        quantity = math.floor(max_position_value / current_price)
        if quantity < 1:
            return 0, 0.0, 0.0

    # MAX_TRADE_AMOUNT hard cap
    position_value = current_price * quantity
    if position_value > MAX_TRADE_AMOUNT:
        quantity = math.floor(MAX_TRADE_AMOUNT / current_price)
        if quantity < 1:
            return 0, 0.0, 0.0

    stop_loss = current_price - stop_distance
    take_profit = current_price + stop_distance * TAKE_PROFIT_RATIO

    if stop_loss <= 0.0:
        return 0, 0.0, 0.0

    return quantity, stop_loss, take_profit


def _process_symbol(
    row: sqlite3.Row,
    run_date: datetime.date,
    db_path: str,
    pt: PaperTrader,
    portfolio_equity: float,
) -> OrderRecord:
    """Process a single approved symbol: price check, deviation, placement.

    Args:
        row: Row from risk_approvals with symbol, quantity, entry_price_approx,
            stop_loss, take_profit, risk_amount.
        run_date: Execution date.
        db_path: Path to SQLite database.
        pt: Initialised PaperTrader instance.
        portfolio_equity: Current portfolio equity for cap checks.

    Returns:
        OrderRecord with placement outcome.
    """
    symbol: str = row["symbol"]
    approved_entry: float = float(row["entry_price_approx"])
    approved_qty: int = int(row["quantity"])
    approved_sl: float = float(row["stop_loss"])
    approved_tp: float = float(row["take_profit"])
    risk_amount: float = float(row["risk_amount"])

    # Step 1 — fetch current price
    current_price = _fetch_current_price(symbol, run_date)
    if current_price is None:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"price_fetch_failed: {symbol}",
            level="WARNING",
            symbol=symbol,
            result="error",
        )
        return OrderRecord(
            symbol=symbol,
            run_date=run_date,
            quantity=0,
            entry_price=approved_entry,
            stop_loss=approved_sl,
            take_profit=approved_tp,
            order_id=-1,
            status="SKIPPED_PRICE_FETCH_FAILED",
            deviation_pct=0.0,
            recalculated=False,
            placed_at=None,
        )

    # Step 2 — compute deviation
    deviation_pct = abs(current_price - approved_entry) / approved_entry if approved_entry > 0.0 else 0.0

    # Step 3 — deviation > 1.5% → skip
    if deviation_pct > DEVIATION_SKIP_THRESHOLD:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"price_slippage_exceeded: {symbol} deviation={deviation_pct:.2%}",
            level="WARNING",
            symbol=symbol,
            result="skipped",
        )
        return OrderRecord(
            symbol=symbol,
            run_date=run_date,
            quantity=0,
            entry_price=current_price,
            stop_loss=approved_sl,
            take_profit=approved_tp,
            order_id=-1,
            status="SKIPPED_SLIPPAGE",
            deviation_pct=deviation_pct,
            recalculated=False,
            placed_at=None,
        )

    # Step 4 — deviation > 0.5% → recalculate
    final_qty = approved_qty
    final_sl = approved_sl
    final_tp = approved_tp
    final_price = current_price
    recalculated = False

    if deviation_pct > DEVIATION_RECALC_THRESHOLD:
        atr = _fetch_atr_from_signals(db_path, run_date, symbol)
        if atr is None:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"price_fetch_failed: {symbol} — ATR not found for recalculation",
                level="WARNING",
                symbol=symbol,
                result="error",
            )
            return OrderRecord(
                symbol=symbol,
                run_date=run_date,
                quantity=0,
                entry_price=current_price,
                stop_loss=approved_sl,
                take_profit=approved_tp,
                order_id=-1,
                status="SKIPPED_PRICE_FETCH_FAILED",
                deviation_pct=deviation_pct,
                recalculated=False,
                placed_at=None,
            )

        new_qty, new_sl, new_tp = _recalculate_position(
            current_price, risk_amount, portfolio_equity, atr
        )
        if new_qty == 0:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"recalculated_quantity_zero: {symbol}",
                level="WARNING",
                symbol=symbol,
                result="skipped",
            )
            return OrderRecord(
                symbol=symbol,
                run_date=run_date,
                quantity=0,
                entry_price=current_price,
                stop_loss=approved_sl,
                take_profit=approved_tp,
                order_id=-1,
                status="SKIPPED_RECALC_ZERO",
                deviation_pct=deviation_pct,
                recalculated=True,
                placed_at=None,
            )

        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"recalculated: {symbol} new_qty={new_qty} deviation={deviation_pct:.2%}",
            level="INFO",
            symbol=symbol,
            result="ok",
        )
        final_qty = new_qty
        final_sl = new_sl
        final_tp = new_tp
        recalculated = True

    # Step 5 — place order via PaperTrader
    try:
        order_id = pt.place_order(
            symbol=symbol,
            side="BUY",
            quantity=final_qty,
            entry_price=final_price,
            stop_loss=final_sl,
            take_profit=final_tp,
        )
    except (ValueError, sqlite3.Error) as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"order_placement_failed: {symbol} {exc}",
            level="ERROR",
            symbol=symbol,
            result="error",
        )
        return OrderRecord(
            symbol=symbol,
            run_date=run_date,
            quantity=final_qty,
            entry_price=final_price,
            stop_loss=final_sl,
            take_profit=final_tp,
            order_id=-1,
            status="SKIPPED_ORDER_ERROR",
            deviation_pct=deviation_pct,
            recalculated=recalculated,
            placed_at=None,
        )

    placed_at = _ist_now()
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"order_placed: {symbol} qty={final_qty} @ {final_price:.2f}",
        level="INFO",
        symbol=symbol,
        result="ok",
    )

    return OrderRecord(
        symbol=symbol,
        run_date=run_date,
        quantity=final_qty,
        entry_price=final_price,
        stop_loss=final_sl,
        take_profit=final_tp,
        order_id=order_id,
        status="PLACED",
        deviation_pct=deviation_pct,
        recalculated=recalculated,
        placed_at=placed_at,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_execution_agent(
    run_date: datetime.date | None = None,
    db_path_override: str | None = None,
) -> ExecutionResult:
    """Execute approved trades after human confirmation.

    Reads APPROVED risk_approvals for run_date, sends human checkpoint
    notification, waits for confirmation via file flag, validates current
    prices, and places CNC orders via PaperTrader.

    Args:
        run_date: Date to execute for. Defaults to today IST.
        db_path_override: Absolute SQLite path. None = derived from settings.

    Returns:
        ExecutionResult with all placement outcomes.

    Raises:
        ExecutionAgentError: On DB read/write failure (phase='db_read' or 'db_write'),
            or PaperTrader init failure (phase='paper_trader_init').
            Price fetch and order placement failures are handled per-symbol
            (logged and skipped), not raised.
    """
    if run_date is None:
        run_date = _ist_now().date()

    db_path = _resolve_db_path(db_path_override)

    # Log run started OUTSIDE any transaction
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"execution_agent_run_started: {run_date}",
        level="INFO",
        result="ok",
    )

    # Setup tables
    _setup_table(db_path)

    # READ: fetch APPROVED rows from risk_approvals
    approved_rows: list[sqlite3.Row] = []
    try:
        conn = _open_connection(db_path)
        approved_rows = conn.execute(
            """
            SELECT symbol, run_date, quantity, entry_price_approx, stop_loss,
                   take_profit, position_size_multiplier, risk_amount
            FROM risk_approvals
            WHERE run_date = ? AND approval_status = 'APPROVED'
            ORDER BY symbol ASC
            """,
            (run_date.isoformat(),),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        raise ExecutionAgentError(
            message=f"Failed to read risk_approvals: {exc}",
            phase="db_read",
        ) from exc

    # No approved trades → safe mode
    if not approved_rows:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="safe_mode: no_approved_trades",
            level="WARNING",
            result="skipped",
        )
        return ExecutionResult(
            run_date=run_date,
            human_confirmed=False,
            safe_mode=True,
            safe_mode_reason="no_approved_trades",
            orders_placed=[],
            orders_skipped=[],
            completed_at=_ist_now(),
        )

    # READ: watchlist context for checkpoint message
    symbols_list = [str(row["symbol"]) for row in approved_rows]
    watchlist_by_symbol: dict[str, sqlite3.Row] = {}
    try:
        conn = _open_connection(db_path)
        placeholders = ",".join("?" * len(symbols_list))
        wl_rows = conn.execute(
            f"""
            SELECT symbol, rank, sentiment, confidence, scorecard_score
            FROM watchlist
            WHERE run_date = ? AND symbol IN ({placeholders})
            """,
            [run_date.isoformat()] + symbols_list,
        ).fetchall()
        conn.close()
        for wl_row in wl_rows:
            watchlist_by_symbol[wl_row["symbol"]] = wl_row
    except sqlite3.Error as exc:
        raise ExecutionAgentError(
            message=f"Failed to read watchlist: {exc}",
            phase="db_read",
        ) from exc

    # Build and send checkpoint message
    checkpoint_msg = _build_checkpoint_message(run_date, approved_rows, watchlist_by_symbol)
    send_checkpoint(
        subject=f"EXECUTION CHECKPOINT - {run_date} [{len(approved_rows)} trades]",
        message=checkpoint_msg,
    )
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"checkpoint_sent: {len(symbols_list)} symbols",
        level="INFO",
        result="ok",
    )

    # Write PENDING checkpoint record
    _write_checkpoint_pending(db_path, run_date, symbols_list, checkpoint_msg)

    # Poll for confirmation
    confirmed = _poll_checkpoint_file(run_date)

    if not confirmed:
        _update_checkpoint_status(db_path, run_date, "TIMEOUT")
        send_alert(
            subject=f"EXECUTION TIMEOUT - {run_date} — safe mode activated",
            message=(
                f"No human confirmation received within {CHECKPOINT_TIMEOUT_SECS // 60} minutes.\n"
                f"Safe mode activated. No trades placed for {run_date}."
            ),
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action="checkpoint_timeout: safe_mode_activated",
            level="WARNING",
            result="timeout",
        )
        return ExecutionResult(
            run_date=run_date,
            human_confirmed=False,
            safe_mode=True,
            safe_mode_reason="timeout_no_confirmation",
            orders_placed=[],
            orders_skipped=[],
            completed_at=_ist_now(),
        )

    # Confirmed
    _update_checkpoint_status(db_path, run_date, "CONFIRMED")
    log_agent_action(
        agent_name=AGENT_NAME,
        action="checkpoint_confirmed",
        level="INFO",
        result="ok",
    )

    # Init PaperTrader
    try:
        pt = PaperTrader(db_path)
        pnl_data = pt.get_pnl()
        portfolio_equity = STARTING_CAPITAL + pnl_data["total_pnl"]
    except (ValueError, sqlite3.Error) as exc:
        raise ExecutionAgentError(
            message=f"PaperTrader initialisation failed: {exc}",
            phase="paper_trader_init",
        ) from exc

    # Process each approved symbol
    orders_placed: list[OrderRecord] = []
    orders_skipped: list[OrderRecord] = []

    for row in approved_rows:
        record = _process_symbol(row, run_date, db_path, pt, portfolio_equity)
        if record.status == "PLACED":
            orders_placed.append(record)
        else:
            orders_skipped.append(record)

    # Summary log and notification
    total = len(approved_rows)
    placed = len(orders_placed)
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"execution_agent_complete: {placed}/{total} placed",
        level="INFO",
        result="ok",
    )

    skipped_summary = ""
    if orders_skipped:
        skipped_lines = [
            f"  {r.symbol}: {r.status}" for r in orders_skipped
        ]
        skipped_summary = "\nSkipped:\n" + "\n".join(skipped_lines)

    placed_lines = [
        f"  {r.symbol}: {r.quantity} shares @ Rs.{r.entry_price:.2f} | "
        f"SL: Rs.{r.stop_loss:.2f} | TP: Rs.{r.take_profit:.2f}"
        for r in orders_placed
    ]
    send_info(
        message=(
            f"Execution complete {run_date}: {placed}/{total} orders placed.\n"
            + ("\n".join(placed_lines) if placed_lines else "")
            + skipped_summary
        )
    )

    return ExecutionResult(
        run_date=run_date,
        human_confirmed=True,
        safe_mode=False,
        safe_mode_reason=None,
        orders_placed=orders_placed,
        orders_skipped=orders_skipped,
        completed_at=_ist_now(),
    )
