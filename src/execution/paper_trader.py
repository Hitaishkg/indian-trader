"""Simulated CNC swing trade execution engine for paper trading phases.

Tracks open positions, closed trades, and running P&L in SQLite without
touching any broker API. Every order is written to the orders table BEFORE
execution. GTT (Good Till Triggered) stop-loss and take-profit orders are
simulated in-memory and checked on each price update via check_gtts().

This module is the sole execution engine during Phases 1-5. It enforces the
LIVE_TRADING=false gate at construction time and raises immediately if live
trading is enabled.
"""

from __future__ import annotations

import datetime
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
_AGENT_NAME: str = "paper_trader"
_VALID_SIDES: frozenset[str] = frozenset({"BUY", "SELL"})
_VALID_EXIT_REASONS: frozenset[str] = frozenset({
    "STOP_LOSS", "TAKE_PROFIT", "MANUAL_EXIT", "REGIME_TIGHTENED",
})

# WAL pragmas -- same as logger.py, applied at connection time
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    order_type   TEXT    NOT NULL DEFAULT 'CNC',
    side         TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    entry_price  REAL    NOT NULL CHECK (entry_price > 0),
    stop_loss    REAL    NOT NULL CHECK (stop_loss > 0),
    take_profit  REAL    NOT NULL CHECK (take_profit > 0),
    order_id     TEXT    NOT NULL,
    gtt_sl_id    TEXT,
    gtt_tp_id    TEXT,
    placed_at    TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('PENDING', 'FILLED', 'REJECTED'))
);
"""

_DDL_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    entry_price  REAL    NOT NULL CHECK (entry_price > 0),
    exit_price   REAL    NOT NULL CHECK (exit_price > 0),
    pnl          REAL    NOT NULL,
    pnl_pct      REAL    NOT NULL,
    exit_reason  TEXT    NOT NULL CHECK (exit_reason IN ('STOP_LOSS', 'TAKE_PROFIT', 'MANUAL_EXIT', 'REGIME_TIGHTENED')),
    opened_at    TEXT    NOT NULL,
    closed_at    TEXT    NOT NULL
);
"""

_DDL_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL UNIQUE,
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    entry_price   REAL    NOT NULL CHECK (entry_price > 0),
    current_price REAL    NOT NULL CHECK (current_price > 0),
    stop_loss     REAL    NOT NULL CHECK (stop_loss > 0),
    take_profit   REAL    NOT NULL CHECK (take_profit > 0),
    pnl           REAL    NOT NULL,
    pnl_pct       REAL    NOT NULL,
    opened_at     TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
"""


def _now_ist() -> str:
    """Return the current IST timestamp as ISO 8601 string with timezone offset."""
    return datetime.datetime.now(_IST).isoformat(timespec="seconds")


def _new_order_id() -> str:
    """Generate a paper trading order ID."""
    return f"PAPER-{uuid.uuid4().hex[:12]}"


def _new_gtt_sl_id() -> str:
    """Generate a paper trading GTT stop-loss order ID."""
    return f"GTT-SL-{uuid.uuid4().hex[:12]}"


def _new_gtt_tp_id() -> str:
    """Generate a paper trading GTT take-profit order ID."""
    return f"GTT-TP-{uuid.uuid4().hex[:12]}"


class PaperTrader:
    """Simulated CNC delivery order execution engine for paper trading.

    All state is persisted to SQLite. Positions are never cached in Python
    dicts -- all reads go directly to the database so that other agents
    (Monitor, Reporter) see consistent state.
    """

    def __init__(self, db_path: str | None = None) -> None:
        """Initialise the paper trader with a SQLite database connection.

        Args:
            db_path: Absolute path to the SQLite database file. When None,
                     derived from settings.database_url by stripping the
                     'sqlite:///' prefix and resolving relative to project root.

        Raises:
            ValueError: If settings.live_trading is True. Paper trader must
                        never run when LIVE_TRADING=true.
        """
        if settings.live_trading:
            raise ValueError(
                "PaperTrader cannot run when LIVE_TRADING=true. "
                "Set LIVE_TRADING=false in .env."
            )

        if db_path is None:
            raw_url: str = settings.database_url
            if raw_url.startswith("sqlite:///"):
                resolved = raw_url[len("sqlite:///"):]
            elif raw_url.startswith("sqlite://"):
                resolved = raw_url[len("sqlite://"):]
            else:
                resolved = raw_url
            if not resolved:
                raise ValueError(
                    f"Cannot derive database path from database_url: {raw_url!r}"
                )
            self._db_path: str = resolved
        else:
            self._db_path = db_path

        self._conn: sqlite3.Connection = sqlite3.connect(
            self._db_path, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row

        try:
            for pragma in _WAL_PRAGMAS:
                self._conn.execute(pragma)

            self._conn.execute(_DDL_ORDERS)
            self._conn.execute(_DDL_TRADES)
            self._conn.execute(_DDL_POSITIONS)
            self._conn.commit()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action="PaperTrader __init__ database setup failed",
                level="ERROR",
                result=str(exc),
            )
            raise

        log_agent_action(
            agent_name=_AGENT_NAME,
            action="PaperTrader initialised",
            level="INFO",
            result="ok",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> int:
        """Place a simulated CNC delivery order.

        Writes the order to the orders table with status='PENDING' BEFORE
        simulating execution. Then updates status to 'FILLED' and creates a
        row in the positions table (for BUY) or closes the position and
        writes to the trades table (for SELL).

        Args:
            symbol: NSE ticker symbol (e.g. "RELIANCE"). Must be a non-empty
                    string.
            side: "BUY" or "SELL". Any other value raises ValueError.
            quantity: Number of shares. Must be a positive integer.
            entry_price: Simulated fill price in INR. Must be positive.
            stop_loss: Stop-loss trigger price in INR. For BUY orders, must be
                       below entry_price. For SELL orders, must be above entry_price.
            take_profit: Take-profit trigger price in INR. For BUY orders, must be
                         above entry_price. For SELL orders, must be below entry_price.

        Returns:
            The integer row ID of the order in the orders table.

        Raises:
            ValueError: If any argument fails validation (see Error Handling, Section 7).
        """
        # --- Input validation ---
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"symbol must be a non-empty string, got: {symbol!r}")
        if side not in _VALID_SIDES:
            raise ValueError(f"side must be 'BUY' or 'SELL', got: {side!r}")
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError(f"quantity must be a positive integer, got: {quantity!r}")
        if entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got: {entry_price}")
        if stop_loss <= 0:
            raise ValueError(f"stop_loss must be positive, got: {stop_loss}")
        if take_profit <= 0:
            raise ValueError(f"take_profit must be positive, got: {take_profit}")

        order_value: float = entry_price * quantity
        if order_value > settings.max_trade_amount:
            raise ValueError(
                f"Order value {order_value:.2f} exceeds MAX_TRADE_AMOUNT "
                f"{settings.max_trade_amount}"
            )

        if side == "BUY":
            if stop_loss >= entry_price:
                raise ValueError(
                    f"For BUY orders, stop_loss ({stop_loss}) must be below "
                    f"entry_price ({entry_price})"
                )
            if take_profit <= entry_price:
                raise ValueError(
                    f"For BUY orders, take_profit ({take_profit}) must be above "
                    f"entry_price ({entry_price})"
                )
            # Check for duplicate open position
            try:
                row = self._conn.execute(
                    "SELECT id FROM positions WHERE symbol = ?", (symbol,)
                ).fetchone()
            except sqlite3.Error as exc:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"place_order BUY duplicate check failed for {symbol}",
                    level="ERROR",
                    result=str(exc),
                )
                raise
            if row is not None:
                raise ValueError(f"Position already open for {symbol}")
        else:  # SELL
            if stop_loss <= entry_price:
                raise ValueError(
                    f"For SELL orders, stop_loss ({stop_loss}) must be above "
                    f"entry_price ({entry_price})"
                )
            if take_profit >= entry_price:
                raise ValueError(
                    f"For SELL orders, take_profit ({take_profit}) must be below "
                    f"entry_price ({entry_price})"
                )
            try:
                row = self._conn.execute(
                    "SELECT id FROM positions WHERE symbol = ?", (symbol,)
                ).fetchone()
            except sqlite3.Error as exc:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"place_order SELL position check failed for {symbol}",
                    level="ERROR",
                    result=str(exc),
                )
                raise
            if row is None:
                raise ValueError(f"No open position for {symbol} to sell")

        now: str = _now_ist()
        order_id: str = _new_order_id()

        # GTT IDs are only generated for BUY orders that open a new position.
        gtt_sl_id: str | None = _new_gtt_sl_id() if side == "BUY" else None
        gtt_tp_id: str | None = _new_gtt_tp_id() if side == "BUY" else None

        # --- Write PENDING order BEFORE simulating execution ---
        try:
            cursor = self._conn.execute(
                """
                INSERT INTO orders
                    (symbol, order_type, side, quantity, entry_price, stop_loss,
                     take_profit, order_id, gtt_sl_id, gtt_tp_id, placed_at, status)
                VALUES (?, 'CNC', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (
                    symbol, side, quantity, entry_price, stop_loss, take_profit,
                    order_id, gtt_sl_id, gtt_tp_id, now,
                ),
            )
            db_order_id: int = cursor.lastrowid  # type: ignore[assignment]
            self._conn.commit()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"place_order INSERT PENDING failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        # --- Simulate execution ---
        if side == "BUY":
            pnl: float = 0.0
            pnl_pct: float = 0.0
            try:
                self._conn.execute(
                    """
                    INSERT INTO positions
                        (symbol, quantity, entry_price, current_price, stop_loss,
                         take_profit, pnl, pnl_pct, opened_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol, quantity, entry_price, entry_price,
                        stop_loss, take_profit, pnl, pnl_pct, now, now,
                    ),
                )
            except sqlite3.Error as exc:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"place_order INSERT positions failed for {symbol}",
                    level="ERROR",
                    result=str(exc),
                )
                raise
        else:
            # SELL: close the existing position using the entry price stored there
            try:
                pos_row = self._conn.execute(
                    """
                    SELECT quantity, entry_price, opened_at
                    FROM positions WHERE symbol = ?
                    """,
                    (symbol,),
                ).fetchone()
            except sqlite3.Error as exc:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"place_order SELL SELECT position failed for {symbol}",
                    level="ERROR",
                    result=str(exc),
                )
                raise
            pos_quantity: int = pos_row["quantity"]
            pos_entry_price: float = pos_row["entry_price"]
            pos_opened_at: str = pos_row["opened_at"]

            realized_pnl: float = (entry_price - pos_entry_price) * pos_quantity
            realized_pnl_pct: float = (
                (entry_price - pos_entry_price) / pos_entry_price
            ) * 100.0

            try:
                self._conn.execute(
                    """
                    INSERT INTO trades
                        (symbol, quantity, entry_price, exit_price, pnl, pnl_pct,
                         exit_reason, opened_at, closed_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'MANUAL_EXIT', ?, ?)
                    """,
                    (
                        symbol, pos_quantity, pos_entry_price, entry_price,
                        realized_pnl, realized_pnl_pct, pos_opened_at, now,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM positions WHERE symbol = ?", (symbol,)
                )
            except sqlite3.Error as exc:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"place_order INSERT trades / DELETE positions failed for {symbol}",
                    level="ERROR",
                    result=str(exc),
                )
                raise

        # --- Mark order as FILLED ---
        try:
            self._conn.execute(
                "UPDATE orders SET status = 'FILLED' WHERE id = ?", (db_order_id,)
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"place_order UPDATE orders FILLED failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"place_order {side} {quantity} {symbol} @ {entry_price:.2f}",
            level="INFO",
            symbol=symbol,
            result="ok",
        )
        return db_order_id

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
    ) -> int:
        """Close an open position at the given exit price.

        Removes the position from the positions table, writes a completed
        round-trip to the trades table, and places a SELL order in the
        orders table (written BEFORE the simulated execution).

        Args:
            symbol: NSE ticker symbol of the position to close.
            exit_price: Simulated exit fill price in INR. Must be positive.
            exit_reason: One of "STOP_LOSS", "TAKE_PROFIT", "MANUAL_EXIT",
                         "REGIME_TIGHTENED". Any other value raises ValueError.

        Returns:
            The integer row ID of the trade in the trades table.

        Raises:
            ValueError: If symbol has no open position, or if exit_price <= 0,
                        or if exit_reason is not one of the allowed values.
        """
        if exit_reason not in _VALID_EXIT_REASONS:
            raise ValueError(
                f"exit_reason must be one of STOP_LOSS, TAKE_PROFIT, MANUAL_EXIT, "
                f"REGIME_TIGHTENED, got: {exit_reason!r}"
            )
        if exit_price <= 0:
            raise ValueError(f"exit_price must be positive, got: {exit_price}")

        try:
            pos_row = self._conn.execute(
                """
                SELECT quantity, entry_price, stop_loss, take_profit, opened_at
                FROM positions WHERE symbol = ?
                """,
                (symbol,),
            ).fetchone()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"close_position SELECT positions failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise
        if pos_row is None:
            raise ValueError(f"No open position for {symbol}")

        pos_quantity: int = pos_row["quantity"]
        pos_entry_price: float = pos_row["entry_price"]
        pos_stop_loss: float = pos_row["stop_loss"]
        pos_take_profit: float = pos_row["take_profit"]
        pos_opened_at: str = pos_row["opened_at"]

        now: str = _now_ist()
        order_id: str = _new_order_id()

        # Write PENDING SELL order BEFORE simulated execution (no GTT IDs for closing orders)
        try:
            self._conn.execute(
                """
                INSERT INTO orders
                    (symbol, order_type, side, quantity, entry_price, stop_loss,
                     take_profit, order_id, gtt_sl_id, gtt_tp_id, placed_at, status)
                VALUES (?, 'CNC', 'SELL', ?, ?, ?, ?, ?, NULL, NULL, ?, 'PENDING')
                """,
                (
                    symbol, pos_quantity, exit_price, pos_stop_loss, pos_take_profit,
                    order_id, now,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"close_position INSERT PENDING SELL order failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        # Compute P&L
        realized_pnl: float = (exit_price - pos_entry_price) * pos_quantity
        realized_pnl_pct: float = (
            (exit_price - pos_entry_price) / pos_entry_price
        ) * 100.0

        # Write completed trade
        try:
            trade_cursor = self._conn.execute(
                """
                INSERT INTO trades
                    (symbol, quantity, entry_price, exit_price, pnl, pnl_pct,
                     exit_reason, opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, pos_quantity, pos_entry_price, exit_price,
                    realized_pnl, realized_pnl_pct, exit_reason,
                    pos_opened_at, now,
                ),
            )
            trade_id: int = trade_cursor.lastrowid  # type: ignore[assignment]
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"close_position INSERT trades failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        # Remove from positions
        try:
            self._conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"close_position DELETE positions failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        # Mark SELL order as FILLED
        try:
            self._conn.execute(
                """
                UPDATE orders SET status = 'FILLED'
                WHERE symbol = ? AND order_id = ?
                """,
                (symbol, order_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"close_position UPDATE orders FILLED failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        log_agent_action(
            agent_name=_AGENT_NAME,
            action=(
                f"close_position {symbol} @ {exit_price:.2f} "
                f"reason={exit_reason} pnl={realized_pnl:.2f}"
            ),
            level="INFO",
            symbol=symbol,
            result="ok",
        )
        return trade_id

    def get_positions(self) -> list[dict[str, object]]:
        """Return all currently open positions.

        Reads from the positions table. Each dict contains all columns from
        the positions table: symbol, quantity, entry_price, current_price,
        stop_loss, take_profit, pnl, pnl_pct, opened_at, updated_at.

        Returns:
            List of dicts, one per open position. Empty list if no positions.
        """
        try:
            rows = self._conn.execute(
                """
                SELECT symbol, quantity, entry_price, current_price,
                       stop_loss, take_profit, pnl, pnl_pct, opened_at, updated_at
                FROM positions
                """
            ).fetchall()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action="get_positions SELECT failed",
                level="ERROR",
                result=str(exc),
            )
            raise
        return [dict(row) for row in rows]

    def get_pnl(self) -> dict[str, float]:
        """Return aggregate P&L summary.

        Calculates realized P&L from the trades table and unrealized P&L
        from the positions table.

        Returns:
            Dict with keys:
            - "realized_pnl": float -- sum of pnl from all closed trades (INR)
            - "unrealized_pnl": float -- sum of pnl from all open positions (INR)
            - "total_pnl": float -- realized + unrealized (INR)
            - "trade_count": int -- number of completed round-trip trades
            - "win_count": int -- trades with pnl > 0
            - "loss_count": int -- trades with pnl <= 0
        """
        try:
            trade_row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(pnl), 0.0)                        AS realized_pnl,
                    COALESCE(COUNT(*), 0)                           AS trade_count,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) AS win_count,
                    COALESCE(SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END), 0) AS loss_count
                FROM trades
                """
            ).fetchone()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action="get_pnl SELECT trades aggregate failed",
                level="ERROR",
                result=str(exc),
            )
            raise

        try:
            pos_row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS unrealized_pnl FROM positions"
            ).fetchone()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action="get_pnl SELECT positions aggregate failed",
                level="ERROR",
                result=str(exc),
            )
            raise

        realized_pnl: float = float(trade_row["realized_pnl"])
        unrealized_pnl: float = float(pos_row["unrealized_pnl"])

        return {
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": realized_pnl + unrealized_pnl,
            "trade_count": int(trade_row["trade_count"]),
            "win_count": int(trade_row["win_count"]),
            "loss_count": int(trade_row["loss_count"]),
        }

    def check_gtts(self, current_prices: dict[str, float]) -> list[dict[str, object]]:
        """Check all open positions against their GTT stop-loss and take-profit levels.

        Called by Monitor Agent every 5 minutes during market hours. For each
        open position, compares the current price against stored stop_loss and
        take_profit levels. If triggered, automatically closes the position.

        Args:
            current_prices: Dict mapping NSE ticker symbol to current market
                            price in INR (float). Symbols not present in the
                            dict are skipped (logged as a warning).

        Returns:
            List of dicts describing triggered exits, each containing:
            - "symbol": str
            - "exit_price": float (the current price that triggered the GTT)
            - "exit_reason": "STOP_LOSS" or "TAKE_PROFIT"
            - "trade_id": int (row ID from trades table)
            Empty list if nothing triggered.
        """
        triggered: list[dict[str, object]] = []

        try:
            rows = self._conn.execute(
                """
                SELECT symbol, quantity, entry_price, current_price,
                       stop_loss, take_profit, pnl, pnl_pct, opened_at, updated_at
                FROM positions
                """
            ).fetchall()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"check_gtts database read failed: {exc}",
                level="ERROR",
                result="error",
            )
            return []

        now: str = _now_ist()

        for row in rows:
            symbol: str = row["symbol"]

            if symbol not in current_prices:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"No price for {symbol} in check_gtts, skipping",
                    level="WARNING",
                    symbol=symbol,
                )
                continue

            current_price: float = current_prices[symbol]
            stop_loss: float = row["stop_loss"]
            take_profit: float = row["take_profit"]
            entry_price: float = row["entry_price"]
            quantity: int = row["quantity"]

            # Stop-loss checked before take-profit (conservative)
            if current_price <= stop_loss:
                exit_price: float = stop_loss
                exit_reason: str = "STOP_LOSS"
            elif current_price >= take_profit:
                exit_price = take_profit
                exit_reason = "TAKE_PROFIT"
            else:
                # No trigger -- update unrealized P&L and current price
                unrealized_pnl: float = (current_price - entry_price) * quantity
                unrealized_pnl_pct: float = (
                    (current_price - entry_price) / entry_price
                ) * 100.0
                try:
                    self._conn.execute(
                        """
                        UPDATE positions
                        SET current_price = ?, pnl = ?, pnl_pct = ?, updated_at = ?
                        WHERE symbol = ?
                        """,
                        (current_price, unrealized_pnl, unrealized_pnl_pct, now, symbol),
                    )
                    self._conn.commit()
                except sqlite3.Error as exc:
                    log_agent_action(
                        agent_name=_AGENT_NAME,
                        action=f"check_gtts update failed for {symbol}: {exc}",
                        level="ERROR",
                        symbol=symbol,
                        result="error",
                    )
                continue

            # Trigger: close the position
            try:
                trade_id: int = self.close_position(symbol, exit_price, exit_reason)
            except (ValueError, sqlite3.Error) as exc:
                log_agent_action(
                    agent_name=_AGENT_NAME,
                    action=f"check_gtts close_position failed for {symbol}: {exc}",
                    level="ERROR",
                    symbol=symbol,
                    result="error",
                )
                continue

            log_agent_action(
                agent_name=_AGENT_NAME,
                action=(
                    f"GTT triggered {exit_reason} for {symbol} "
                    f"@ {exit_price:.2f}"
                ),
                level="INFO",
                symbol=symbol,
                result="triggered",
            )

            triggered.append({
                "symbol": symbol,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "trade_id": trade_id,
            })

        return triggered

    def update_stop_loss(self, symbol: str, new_stop_loss: float) -> None:
        """Update the stop-loss level for an open position.

        Used by Monitor Agent when regime filter tightens (2x ATR -> 1x ATR)
        or when LLM sentiment turns Negative with confidence > 0.8.

        Args:
            symbol: NSE ticker symbol.
            new_stop_loss: New stop-loss price in INR. Must be positive and
                           below the current entry_price for long positions.

        Raises:
            ValueError: If symbol has no open position or new_stop_loss is invalid.
        """
        if new_stop_loss <= 0:
            raise ValueError(f"new_stop_loss must be positive, got: {new_stop_loss}")

        try:
            pos_row = self._conn.execute(
                "SELECT entry_price FROM positions WHERE symbol = ?", (symbol,)
            ).fetchone()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"update_stop_loss SELECT entry_price failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise
        if pos_row is None:
            raise ValueError(f"No open position for {symbol}")

        entry_price: float = pos_row["entry_price"]
        if new_stop_loss >= entry_price:
            raise ValueError(
                f"new_stop_loss ({new_stop_loss}) must be below entry_price "
                f"({entry_price}) for long position {symbol}"
            )

        now: str = _now_ist()
        try:
            self._conn.execute(
                "UPDATE positions SET stop_loss = ?, updated_at = ? WHERE symbol = ?",
                (new_stop_loss, now, symbol),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"update_stop_loss UPDATE failed for {symbol}",
                level="ERROR",
                result=str(exc),
            )
            raise

        log_agent_action(
            agent_name=_AGENT_NAME,
            action=(
                f"update_stop_loss {symbol} -> {new_stop_loss:.2f} "
                f"(entry={entry_price:.2f})"
            ),
            level="INFO",
            symbol=symbol,
            result="ok",
        )
