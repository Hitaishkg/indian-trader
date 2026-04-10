"""Risk Agent — kill switch checks and position sizing for the Indian Trader pipeline.

Runs at 08:50 IST every trading morning. Reads human_approved=1 watchlist rows,
checks all four kill switches, sizes each approved symbol, and writes results to
the risk_approvals table. Does not place orders — the Execution Agent reads
risk_approvals to decide what to place.

Kill switch evaluation order (hardcoded, first trigger wins):
  1. drawdown_15pct
  2. consecutive_losses_5
  3. win_rate_below_40pct
  4. sharpe_below_0.8
"""

from __future__ import annotations

import datetime
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_ohlcv
from src.execution.paper_trader import PaperTrader
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_info

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "risk_agent"
STARTING_CAPITAL: float = 10_000.0
RISK_PCT: float = 0.01
STOP_LOSS_ATR_MULTIPLIER: float = 2.0
TAKE_PROFIT_RATIO: float = 2.0
MAX_POSITION_PCT: float = 0.40
MAX_OPEN_POSITIONS: int = 2
DRAWDOWN_KILL_SWITCH_PCT: float = 15.0
WIN_RATE_KILL_SWITCH_PCT: float = 40.0
CONSECUTIVE_LOSSES_KILL_SWITCH: int = 5
SHARPE_KILL_SWITCH: float = 0.8
KILL_SWITCH_MIN_TRADES: int = 20
STOP_LOSS_PCT_CAP: float = 0.03   # hard cap: stop never more than 3% below entry
PRICE_FETCH_LOOKBACK_DAYS: int = 5
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

_DDL_RISK_APPROVALS: str = """
CREATE TABLE IF NOT EXISTS risk_approvals (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                   TEXT    NOT NULL,
    run_date                 TEXT    NOT NULL,
    quantity                 INTEGER NOT NULL DEFAULT 0,
    entry_price_approx       REAL    NOT NULL DEFAULT 0.0,
    stop_loss                REAL    NOT NULL DEFAULT 0.0,
    take_profit              REAL    NOT NULL DEFAULT 0.0,
    position_size_multiplier REAL    NOT NULL DEFAULT 1.0,
    risk_amount              REAL    NOT NULL DEFAULT 0.0,
    approval_status          TEXT    NOT NULL CHECK (approval_status IN ('APPROVED', 'REJECTED')),
    rejection_reason         TEXT,
    approved_at              TEXT    NOT NULL,
    UNIQUE(symbol, run_date)
);
"""


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class RiskAgentError(Exception):
    """Raised when the Risk Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase of the agent failed.
            Valid values: 'db_read', 'db_write', 'paper_trader_init'.
    """

    def __init__(self, message: str, phase: str) -> None:
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskApproval:
    """One approved or rejected symbol with full sizing detail.

    Written to the risk_approvals table. One row per approved watchlist symbol.

    Attributes:
        symbol: NSE ticker symbol.
        run_date: Date this approval was computed for.
        quantity: Shares to buy. 0 if rejected.
        entry_price_approx: Most recent close price from yfinance (INR). 0.0 if unavailable.
        stop_loss: Stop-loss price in INR (entry_price_approx - stop_distance). 0.0 if unavailable.
        take_profit: Take-profit price in INR (entry_price_approx + stop_distance * 2). 0.0 if unavailable.
        position_size_multiplier: Regime multiplier applied (1.0 or 0.5).
        risk_amount: Actual risk in INR used for sizing (after multiplier).
        approval_status: "APPROVED" or "REJECTED".
        rejection_reason: None if APPROVED; one of the defined reason strings if REJECTED.
        approved_at: IST timestamp when this row was written.
    """

    symbol: str
    run_date: datetime.date
    quantity: int
    entry_price_approx: float
    stop_loss: float
    take_profit: float
    position_size_multiplier: float
    risk_amount: float
    approval_status: str
    rejection_reason: str | None
    approved_at: datetime.datetime


@dataclass(frozen=True)
class RiskAgentResult:
    """Full output of run_risk_agent().

    Attributes:
        run_date: Date the risk checks were run for.
        kill_switch_fired: True if any kill switch triggered.
        kill_switch_reason: Which kill switch fired. None if no kill switch.
        approved: RiskApproval objects with approval_status="APPROVED".
        rejected: RiskApproval objects with approval_status="REJECTED".
        portfolio_equity: Current portfolio equity in INR at time of run.
        peak_equity: Historical peak equity in INR (from trades table).
        current_drawdown_pct: (peak_equity - portfolio_equity) / peak_equity * 100.
        completed_at: IST timestamp when agent completed.
    """

    run_date: datetime.date
    kill_switch_fired: bool
    kill_switch_reason: str | None
    approved: list[RiskApproval]
    rejected: list[RiskApproval]
    portfolio_equity: float
    peak_equity: float
    current_drawdown_pct: float
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
        conn.execute(_DDL_RISK_APPROVALS)
        conn.close()
    except sqlite3.Error as exc:
        raise RiskAgentError(
            message=f"Failed to create risk_approvals table: {exc}",
            phase="db_write",
        ) from exc


def _compute_portfolio_equity(pnl: dict[str, float]) -> float:
    return STARTING_CAPITAL + pnl["total_pnl"]


def _compute_peak_equity(trades: list[dict[str, Any]]) -> float:
    running_sum = 0.0
    peak = STARTING_CAPITAL
    for trade in trades:
        running_sum += float(trade["pnl"])
        equity_at_point = STARTING_CAPITAL + running_sum
        if equity_at_point > peak:
            peak = equity_at_point
    return peak


def _compute_drawdown_pct(portfolio_equity: float, peak_equity: float) -> float:
    if peak_equity <= 0.0:
        return 0.0
    return (peak_equity - portfolio_equity) / peak_equity * 100.0


def _compute_win_rate_pct(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 100.0
    win_count = sum(1 for t in trades if float(t["pnl"]) > 0)
    return win_count / len(trades) * 100.0


def _check_consecutive_losses(trades: list[dict[str, Any]]) -> bool:
    if len(trades) < CONSECUTIVE_LOSSES_KILL_SWITCH:
        return False
    recent_5 = trades[-CONSECUTIVE_LOSSES_KILL_SWITCH:]
    return all(float(t["pnl"]) <= 0.0 for t in recent_5)


def _compute_sharpe(trades: list[dict[str, Any]]) -> float:
    daily_pnl_map: dict[str, float] = defaultdict(float)
    for trade in trades:
        day = str(trade["closed_at"])[:10]
        daily_pnl_map[day] += float(trade["pnl"])
    daily_returns = [v / STARTING_CAPITAL for v in daily_pnl_map.values()]
    if not daily_returns:
        return 0.0
    mean_r = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
    std_r = variance ** 0.5
    if std_r == 0.0:
        return 0.0
    return mean_r / std_r * (252 ** 0.5)


def _run_kill_switches(
    trades: list[dict[str, Any]],
    portfolio_equity: float,
    peak_equity: float,
) -> tuple[bool, str | None]:
    """Evaluate all four kill switches in hardcoded priority order.

    Returns (fired, reason). First trigger wins. Remaining checks still run
    but their results are only logged, not reported in kill_switch_reason.
    """
    trade_count = len(trades)

    drawdown_pct = _compute_drawdown_pct(portfolio_equity, peak_equity)
    consec_fired = _check_consecutive_losses(trades)
    win_rate_pct = _compute_win_rate_pct(trades)
    sharpe = _compute_sharpe(trades)

    checks: list[tuple[str, bool]] = [
        ("drawdown_15pct", drawdown_pct > DRAWDOWN_KILL_SWITCH_PCT),
        ("consecutive_losses_5", consec_fired),
        (
            "win_rate_below_40pct",
            trade_count >= KILL_SWITCH_MIN_TRADES and win_rate_pct < WIN_RATE_KILL_SWITCH_PCT,
        ),
        (
            "sharpe_below_0.8",
            trade_count >= KILL_SWITCH_MIN_TRADES and sharpe < SHARPE_KILL_SWITCH,
        ),
    ]

    if trade_count < KILL_SWITCH_MIN_TRADES:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"win_rate_check_skipped_insufficient_trades: {trade_count} trades",
            level="INFO",
            result="skipped",
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"sharpe_check_skipped_insufficient_trades: {trade_count} trades",
            level="INFO",
            result="skipped",
        )

    first_reason: str | None = None
    for name, fired in checks:
        if fired:
            if first_reason is None:
                first_reason = name
            else:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=f"kill_switch_also_fired (secondary): {name}",
                    level="WARNING",
                    result="logged_only",
                )

    return (first_reason is not None, first_reason)


def _fetch_entry_price(symbol: str) -> float:
    today = datetime.datetime.now(tz=IST).date()
    start_date = today - datetime.timedelta(days=PRICE_FETCH_LOOKBACK_DAYS)
    try:
        df = fetch_ohlcv(
            symbols=[symbol],
            start_date=start_date,
            end_date=today,
            cache_expiry_hours=0,
        )
        if df.empty:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"price_fetch_failed: empty dataframe for {symbol}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )
            return 0.0
        symbol_df = df[df["symbol"] == symbol] if "symbol" in df.columns else df
        if symbol_df.empty:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"price_fetch_failed: symbol not found in dataframe for {symbol}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )
            return 0.0
        return float(symbol_df["close"].iloc[-1])
    except FetchError as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"price_fetch_failed: {exc}",
            level="WARNING",
            symbol=symbol,
            result="error",
        )
        return 0.0


def _size_symbol(
    symbol: str,
    watchlist_row: sqlite3.Row,
    signal_row: sqlite3.Row | None,
    portfolio_equity: float,
    existing_open: int,
    approved_count: int,
    entry_price: float,
    run_date: datetime.date,
) -> RiskApproval:
    now = _ist_now()

    def _rejected(reason: str) -> RiskApproval:
        return RiskApproval(
            symbol=symbol,
            run_date=run_date,
            quantity=0,
            entry_price_approx=entry_price,
            stop_loss=0.0,
            take_profit=0.0,
            position_size_multiplier=float(watchlist_row["position_size_multiplier"]),
            risk_amount=0.0,
            approval_status="REJECTED",
            rejection_reason=reason,
            approved_at=now,
        )

    # Step 1 — max positions check
    if existing_open + approved_count >= MAX_OPEN_POSITIONS:
        return _rejected("max_positions_reached")

    # Step 2 — ATR from signals
    if signal_row is None or float(signal_row["atr"]) <= 0.0:
        return _rejected("zero_atr")
    atr = float(signal_row["atr"])

    # Step 3 — multiplier from watchlist
    multiplier = float(watchlist_row["position_size_multiplier"])

    # Step 4 — risk amount
    base_risk = portfolio_equity * RISK_PCT
    risk_amount = base_risk * multiplier

    # Step 5 — stop distance
    stop_distance = atr * STOP_LOSS_ATR_MULTIPLIER
    if entry_price > 0.0:
        stop_distance = min(stop_distance, entry_price * STOP_LOSS_PCT_CAP)

    # Step 6 — raw quantity (floor)
    quantity = math.floor(risk_amount / stop_distance)

    # Step 7 — quantity >= 1
    if quantity < 1:
        return _rejected("insufficient_capital")

    # Step 8 — 40% equity cap
    if entry_price > 0.0:
        position_value = entry_price * quantity
        max_position_value = portfolio_equity * MAX_POSITION_PCT
        if position_value > max_position_value:
            quantity = math.floor(max_position_value / entry_price)
            if quantity < 1:
                return _rejected("position_size_exceeds_cap")

    # Step 9 — MAX_TRADE_AMOUNT hard cap
    if entry_price > 0.0:
        position_value = entry_price * quantity
        max_trade = float(settings.max_trade_amount)
        if position_value > max_trade:
            quantity = math.floor(max_trade / entry_price)
            if quantity < 1:
                return _rejected("position_size_exceeds_cap")

    # Step 10 — stop_loss and take_profit
    if entry_price > 0.0:
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + (stop_distance * TAKE_PROFIT_RATIO)
        if stop_loss <= 0.0:
            return _rejected("invalid_stop_loss")
    else:
        stop_loss = 0.0
        take_profit = 0.0
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"entry_price_unavailable: {symbol} — execution_agent must recalculate",
            level="WARNING",
            symbol=symbol,
            result="ok",
        )

    # Step 11 — APPROVED
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"approved: {symbol} qty={quantity} sl={stop_loss:.2f} tp={take_profit:.2f}",
        level="INFO",
        symbol=symbol,
        result="ok",
    )

    return RiskApproval(
        symbol=symbol,
        run_date=run_date,
        quantity=quantity,
        entry_price_approx=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        position_size_multiplier=multiplier,
        risk_amount=risk_amount,
        approval_status="APPROVED",
        rejection_reason=None,
        approved_at=now,
    )


def _build_summary_message(result: RiskAgentResult) -> str:
    lines = [
        f"Risk Agent complete: {len(result.approved)} approved, {len(result.rejected)} rejected."
    ]
    for a in result.approved:
        lines.append(
            f"  {a.symbol}: {a.quantity} shares @ ~\u20b9{a.entry_price_approx:.0f}, "
            f"SL=\u20b9{a.stop_loss:.0f}, TP=\u20b9{a.take_profit:.0f}"
        )
    return "\n".join(lines)


def _write_approvals(db_path: str, approvals: list[RiskApproval]) -> None:
    conn = _open_connection(db_path)
    try:
        conn.execute("BEGIN")
        for approval in approvals:
            conn.execute(
                """
                INSERT OR REPLACE INTO risk_approvals
                    (symbol, run_date, quantity, entry_price_approx, stop_loss,
                     take_profit, position_size_multiplier, risk_amount,
                     approval_status, rejection_reason, approved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.symbol,
                    approval.run_date.isoformat(),
                    approval.quantity,
                    approval.entry_price_approx,
                    approval.stop_loss,
                    approval.take_profit,
                    approval.position_size_multiplier,
                    approval.risk_amount,
                    approval.approval_status,
                    approval.rejection_reason,
                    approval.approved_at.isoformat(),
                ),
            )
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        raise RiskAgentError(
            message=f"Failed to write risk_approvals: {exc}",
            phase="db_write",
        ) from exc
    conn.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_risk_agent(
    run_date: datetime.date | None = None,
    db_path_override: str | None = None,
) -> RiskAgentResult:
    """Run all kill switch checks and compute position sizing for approved watchlist symbols.

    Reads human_approved=1 rows from watchlist for run_date, runs kill switch
    checks against trades/positions history, sizes each symbol using the 1%-ATR
    formula, and writes results to risk_approvals table.

    Args:
        run_date: Date to run for. Defaults to today in IST.
        db_path_override: Absolute path to SQLite DB. When None, derived from
                          settings.database_url. Used in tests to point at a
                          temporary database.

    Returns:
        RiskAgentResult with all sizing decisions and kill switch status.

    Raises:
        RiskAgentError: On DB read failure (phase='db_read'),
                        DB write failure (phase='db_write'), or
                        PaperTrader instantiation failure (phase='paper_trader_init').
    """
    if run_date is None:
        run_date = datetime.datetime.now(tz=IST).date()

    db_path = _resolve_db_path(db_path_override)

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"risk_agent_run_started: {run_date}",
        level="INFO",
        result="ok",
    )

    _setup_table(db_path)

    # READ PHASE
    watchlist_rows: list[sqlite3.Row] = []
    signals_by_symbol: dict[str, sqlite3.Row] = {}
    trades: list[dict[str, Any]] = []

    try:
        conn = _open_connection(db_path)
        watchlist_rows = conn.execute(
            """
            SELECT symbol, rank, position_size_multiplier, combined_decision
            FROM watchlist
            WHERE run_date = ? AND human_approved = 1 AND combined_decision = 'PROCEED'
            ORDER BY rank ASC
            """,
            (run_date.isoformat(),),
        ).fetchall()

        signal_rows = conn.execute(
            """
            SELECT symbol, atr, signal_type
            FROM signals
            WHERE run_date = ? AND signal_type = 'BUY'
            """,
            (run_date.isoformat(),),
        ).fetchall()
        for row in signal_rows:
            signals_by_symbol[row["symbol"]] = row

        trade_rows = conn.execute(
            "SELECT pnl, closed_at FROM trades ORDER BY closed_at ASC"
        ).fetchall()
        trades = [dict(row) for row in trade_rows]

        conn.close()
    except sqlite3.Error as exc:
        raise RiskAgentError(
            message=f"Failed to read from DB: {exc}",
            phase="db_read",
        ) from exc

    # PAPER TRADER — equity and open positions
    try:
        pt = PaperTrader(db_path)
        pnl_data = pt.get_pnl()
        open_positions = pt.get_positions()
        existing_open_count = len(open_positions)
    except (ValueError, sqlite3.Error) as exc:
        raise RiskAgentError(
            message=f"PaperTrader initialisation failed: {exc}",
            phase="paper_trader_init",
        ) from exc

    portfolio_equity = _compute_portfolio_equity(pnl_data)
    peak_equity = _compute_peak_equity(trades)
    drawdown_pct = _compute_drawdown_pct(portfolio_equity, peak_equity)

    # KILL SWITCH PHASE
    kill_switch_fired, kill_switch_reason = _run_kill_switches(
        trades, portfolio_equity, peak_equity
    )

    now = _ist_now()

    if kill_switch_fired:
        send_alert(
            subject=f"KILL SWITCH FIRED \u2014 {kill_switch_reason}",
            message=(
                f"Kill switch triggered: {kill_switch_reason}\n"
                f"Portfolio equity: \u20b9{portfolio_equity:.2f}\n"
                f"Peak equity: \u20b9{peak_equity:.2f}\n"
                f"Drawdown: {drawdown_pct:.1f}%\n"
                f"All trading halted. Manual review required."
            ),
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"kill_switch_fired: {kill_switch_reason}",
            level="CRITICAL",
            result="error",
        )

        rejected: list[RiskApproval] = [
            RiskApproval(
                symbol=row["symbol"],
                run_date=run_date,
                quantity=0,
                entry_price_approx=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                position_size_multiplier=float(row["position_size_multiplier"]),
                risk_amount=0.0,
                approval_status="REJECTED",
                rejection_reason="kill_switch_fired",
                approved_at=now,
            )
            for row in watchlist_rows
        ]

        _write_approvals(db_path, rejected)

        result = RiskAgentResult(
            run_date=run_date,
            kill_switch_fired=True,
            kill_switch_reason=kill_switch_reason,
            approved=[],
            rejected=rejected,
            portfolio_equity=portfolio_equity,
            peak_equity=peak_equity,
            current_drawdown_pct=drawdown_pct,
            completed_at=now,
        )
        send_info(message=_build_summary_message(result))
        return result

    # SIZING PHASE
    approved_list: list[RiskApproval] = []
    rejected_list: list[RiskApproval] = []
    approved_count = 0

    for row in watchlist_rows:
        symbol = row["symbol"]
        signal_row = signals_by_symbol.get(symbol)
        entry_price = _fetch_entry_price(symbol)

        approval = _size_symbol(
            symbol=symbol,
            watchlist_row=row,
            signal_row=signal_row,
            portfolio_equity=portfolio_equity,
            existing_open=existing_open_count,
            approved_count=approved_count,
            entry_price=entry_price,
            run_date=run_date,
        )

        if approval.approval_status == "APPROVED":
            approved_list.append(approval)
            approved_count += 1
        else:
            rejected_list.append(approval)
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"rejected: {symbol} reason={approval.rejection_reason}",
                level="INFO",
                symbol=symbol,
                result="skipped",
            )
            if approval.rejection_reason == "max_positions_reached":
                # Reject remaining symbols without sizing them
                remaining = [r for r in watchlist_rows if r["symbol"] not in
                             {a.symbol for a in approved_list} and
                             r["symbol"] not in {r2.symbol for r2 in rejected_list}]
                for remaining_row in remaining:
                    rejected_list.append(
                        RiskApproval(
                            symbol=remaining_row["symbol"],
                            run_date=run_date,
                            quantity=0,
                            entry_price_approx=0.0,
                            stop_loss=0.0,
                            take_profit=0.0,
                            position_size_multiplier=float(
                                remaining_row["position_size_multiplier"]
                            ),
                            risk_amount=0.0,
                            approval_status="REJECTED",
                            rejection_reason="max_positions_reached",
                            approved_at=now,
                        )
                    )
                break

    all_approvals = approved_list + rejected_list
    _write_approvals(db_path, all_approvals)

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"risk_agent_complete: {len(approved_list)} approved, "
            f"{len(rejected_list)} rejected, drawdown={drawdown_pct:.1f}%"
        ),
        level="INFO",
        result="ok",
    )

    result = RiskAgentResult(
        run_date=run_date,
        kill_switch_fired=False,
        kill_switch_reason=None,
        approved=approved_list,
        rejected=rejected_list,
        portfolio_equity=portfolio_equity,
        peak_equity=peak_equity,
        current_drawdown_pct=drawdown_pct,
        completed_at=now,
    )

    info_result = send_info(message=_build_summary_message(result))
    if not info_result.get("telegram", True):
        log_agent_action(
            agent_name=AGENT_NAME,
            action="send_info telegram delivery failed",
            level="WARNING",
            result="error",
        )

    return result
