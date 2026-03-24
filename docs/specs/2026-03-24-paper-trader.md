# Spec: src/execution/paper_trader.py

**Date**: 2026-03-24
**Author**: Architect Agent
**Phase**: 1, step 8 of 9
**Status**: Awaiting approval

---

## 1. Purpose

Simulates CNC (Cash and Carry) swing trade orders against real NSE price data
without touching any broker API. Tracks open positions, closed trades, and
running P&L in SQLite. Every simulated order is written to the `orders` table
before execution, and every completed round-trip trade is written to the
`trades` table. This module is the sole execution engine during paper trading
(Phases 1-5). It enforces the `LIVE_TRADING=false` gate at construction time
and raises immediately if live trading is enabled. Position sizing is
calculated by the caller (Risk Agent in later phases); paper_trader accepts
and executes the given size. GTT (Good Till Triggered) stop-loss and
take-profit orders are simulated in-memory and checked on each price update
via `check_gtts()`.

---

## 2. Public API

### 2.1 Class: `PaperTrader`

```python
class PaperTrader:
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
```

### 2.2 `place_order()`

```python
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

    Writes the order to the `orders` table with status='PENDING' BEFORE
    simulating execution. Then updates status to 'FILLED' and creates a
    row in the `positions` table (for BUY) or closes the position and
    writes to the `trades` table (for SELL).

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
        The integer row ID of the order in the `orders` table.

    Raises:
        ValueError: If any argument fails validation (see Error Handling, Section 7).
    """
```

### 2.3 `close_position()`

```python
def close_position(
    self,
    symbol: str,
    exit_price: float,
    exit_reason: str,
) -> int:
    """Close an open position at the given exit price.

    Removes the position from the `positions` table, writes a completed
    round-trip to the `trades` table, and places a SELL order in the
    `orders` table (written BEFORE the simulated execution).

    Args:
        symbol: NSE ticker symbol of the position to close.
        exit_price: Simulated exit fill price in INR. Must be positive.
        exit_reason: One of "STOP_LOSS", "TAKE_PROFIT", "MANUAL_EXIT",
                     "REGIME_TIGHTENED". Any other value raises ValueError.

    Returns:
        The integer row ID of the trade in the `trades` table.

    Raises:
        ValueError: If symbol has no open position, or if exit_price <= 0,
                    or if exit_reason is not one of the allowed values.
    """
```

### 2.4 `get_positions()`

```python
def get_positions(self) -> list[dict[str, object]]:
    """Return all currently open positions.

    Reads from the `positions` table. Each dict contains all columns from
    the positions table: symbol, quantity, entry_price, current_price,
    stop_loss, take_profit, pnl, pnl_pct, opened_at, updated_at.

    Returns:
        List of dicts, one per open position. Empty list if no positions.
    """
```

### 2.5 `get_pnl()`

```python
def get_pnl(self) -> dict[str, float]:
    """Return aggregate P&L summary.

    Calculates realized P&L from the `trades` table and unrealized P&L
    from the `positions` table.

    Returns:
        Dict with keys:
        - "realized_pnl": float -- sum of pnl from all closed trades (INR)
        - "unrealized_pnl": float -- sum of pnl from all open positions (INR)
        - "total_pnl": float -- realized + unrealized (INR)
        - "trade_count": int -- number of completed round-trip trades
        - "win_count": int -- trades with pnl > 0
        - "loss_count": int -- trades with pnl <= 0
    """
```

### 2.6 `check_gtts()`

```python
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
```

### 2.7 `update_stop_loss()`

```python
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
```

---

## 3. Database Schema

### 3.1 `orders` table

```sql
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
```

Column notes:
- `order_id`: For paper trading, generated as `"PAPER-{uuid4_hex[:12]}"`. For live trading (future), this will be the Shoonya broker order ID.
- `gtt_sl_id`: For paper trading, generated as `"GTT-SL-{uuid4_hex[:12]}"`. NULL for SELL orders that close positions.
- `gtt_tp_id`: For paper trading, generated as `"GTT-TP-{uuid4_hex[:12]}"`. NULL for SELL orders that close positions.
- `placed_at`: IST ISO 8601 timestamp with timezone offset (e.g. `"2026-03-24T09:15:00+05:30"`).
- `order_type`: Always `"CNC"` for swing trades. Column exists for schema compatibility with the live trading layer.

### 3.2 `trades` table

```sql
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
```

### 3.3 `positions` table

```sql
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
```

Column notes:
- `symbol` is `UNIQUE` because the system allows maximum 2 open positions, never two positions in the same stock simultaneously.
- `current_price`, `pnl`, and `pnl_pct` are updated on every `check_gtts()` call.

### 3.4 Table creation

All three tables are created in `__init__()` using `CREATE TABLE IF NOT EXISTS`. WAL pragmas are applied at connection time:

```python
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA cache_size=-64000;
PRAGMA synchronous=NORMAL;
```

---

## 4. GTT Simulation Logic

`check_gtts(current_prices)` operates as follows:

1. Read all rows from the `positions` table.
2. For each position:
   a. If `symbol` is not in `current_prices` dict, log a warning via `log_agent_action(agent_name="paper_trader", action=f"No price for {symbol} in check_gtts, skipping", level="WARNING", symbol=symbol)` and skip.
   b. Get `current_price = current_prices[symbol]`.
   c. **Stop-loss check**: If `current_price <= position.stop_loss`, trigger exit with `exit_reason="STOP_LOSS"` and `exit_price=position.stop_loss` (fill at stop-loss level, not market price -- simulates a GTT trigger order that fires at the set level).
   d. **Take-profit check**: If `current_price >= position.take_profit`, trigger exit with `exit_reason="TAKE_PROFIT"` and `exit_price=position.take_profit` (same rationale).
   e. **Priority**: Stop-loss is checked before take-profit. If both trigger simultaneously (extremely unlikely but possible with a gap), stop-loss wins. This is conservative.
   f. If no trigger: update the position's `current_price`, `pnl`, `pnl_pct`, and `updated_at` in the `positions` table.
3. For each triggered exit, call `close_position(symbol, exit_price, exit_reason)` internally.
4. Log every trigger via `log_agent_action()` with `result="triggered"`.
5. Return the list of triggered exits.

---

## 5. Fill Price Logic

**Entry fills**: The caller provides `entry_price` directly. In Phase 1, this will be the last close from `fetch_ohlcv()`. The paper_trader does NOT call `fetch_ohlcv()` itself -- the caller is responsible for determining the fill price. This separation exists because:
- In Phase 1 (`main.py` dry-run), the caller uses last close as a simple approximation.
- In Phase 4 (Execution Agent), the caller will use the pre-market indicative price or live price with slippage checks (deviation > 0.5% triggers recalculation, > 1.5% skips the trade entirely).
- Paper_trader should not encode price discovery logic -- it is a pure execution simulator.

**Exit fills (GTT triggers)**: When a GTT triggers, the exit price is set to the GTT level (stop_loss or take_profit), not the current market price. This simulates the behavior of a real GTT order which fires at the trigger level. In reality, slippage may cause fills at slightly different prices, but for paper trading simulation this is acceptable and conservative (stop-loss fills at the level you set, not worse).

**Exit fills (manual close)**: The caller provides `exit_price` directly via `close_position()`.

---

## 6. P&L Calculation

### 6.1 Per-trade realized P&L (written to `trades` table on close)

```
pnl = (exit_price - entry_price) * quantity
pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
```

Both are stored as float. `pnl` is in INR. `pnl_pct` is a percentage (e.g. 2.5 means 2.5%).

### 6.2 Per-position unrealized P&L (updated in `positions` table on each check_gtts call)

```
pnl = (current_price - entry_price) * quantity
pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
```

### 6.3 Aggregate P&L (returned by `get_pnl()`)

```
realized_pnl = SUM(pnl) FROM trades
unrealized_pnl = SUM(pnl) FROM positions
total_pnl = realized_pnl + unrealized_pnl
```

All values are float. Trade counts and win/loss counts are derived from the `trades` table (`pnl > 0` = win, `pnl <= 0` = loss).

---

## 7. Error Handling

### 7.1 `__init__()`
- If `settings.live_trading` is `True`: raise `ValueError("PaperTrader cannot run when LIVE_TRADING=true. Set LIVE_TRADING=false in .env.")`.
- If database path cannot be resolved: raise `ValueError` with descriptive message.

### 7.2 `place_order()`
- `symbol` is empty string or not a string: raise `ValueError(f"symbol must be a non-empty string, got: {symbol!r}")`.
- `side` not in `("BUY", "SELL")`: raise `ValueError(f"side must be 'BUY' or 'SELL', got: {side!r}")`.
- `quantity <= 0` or not an int: raise `ValueError(f"quantity must be a positive integer, got: {quantity!r}")`.
- `entry_price <= 0`: raise `ValueError(f"entry_price must be positive, got: {entry_price}")`.
- `entry_price * quantity > settings.max_trade_amount`: raise `ValueError(f"Order value {entry_price * quantity:.2f} exceeds MAX_TRADE_AMOUNT {settings.max_trade_amount}")`.
- `stop_loss <= 0` or `take_profit <= 0`: raise `ValueError` with descriptive message.
- For BUY: `stop_loss >= entry_price`: raise `ValueError(f"For BUY orders, stop_loss ({stop_loss}) must be below entry_price ({entry_price})")`.
- For BUY: `take_profit <= entry_price`: raise `ValueError(f"For BUY orders, take_profit ({take_profit}) must be above entry_price ({entry_price})")`.
- BUY when a position for the same symbol already exists: raise `ValueError(f"Position already open for {symbol}")`.
- SELL when no position exists for the symbol: raise `ValueError(f"No open position for {symbol} to sell")`.

### 7.3 `close_position()`
- No open position for `symbol`: raise `ValueError(f"No open position for {symbol}")`.
- `exit_price <= 0`: raise `ValueError`.
- `exit_reason` not in allowed set: raise `ValueError(f"exit_reason must be one of STOP_LOSS, TAKE_PROFIT, MANUAL_EXIT, REGIME_TIGHTENED, got: {exit_reason!r}")`.

### 7.4 `check_gtts()`
- Never raises. Missing symbols in `current_prices` are logged as warnings and skipped. Database errors are logged via `log_agent_action()` with `level="ERROR"` and the method returns an empty list.

### 7.5 `update_stop_loss()`
- No open position for `symbol`: raise `ValueError`.
- `new_stop_loss <= 0`: raise `ValueError`.
- `new_stop_loss >= entry_price` of the position: raise `ValueError`.

---

## 8. Module-level State

### 8.1 Held in memory
- `self._conn: sqlite3.Connection` -- single SQLite connection, opened in `__init__()`, WAL mode.
- `self._db_path: str` -- resolved absolute path to the database file.

### 8.2 Always read from SQLite
- Open positions: always queried from the `positions` table. Never cached in Python dicts.
- Closed trades: always queried from the `trades` table.
- Orders: always queried from the `orders` table.

**Rationale**: The Monitor Agent, Execution Agent, and Reporter Agent all read from the same SQLite database. If paper_trader cached positions in memory, other agents would see stale data. SQLite with WAL mode handles concurrent reads efficiently. The performance cost of reading from SQLite on every call is negligible for the expected volume (max 2 open positions, ~50 price checks per market day).

---

## 9. Constants

```python
# Module-level constants
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
```

---

## 10. Dependencies

```python
from __future__ import annotations

import datetime
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.utils.logger import log_agent_action
```

No external packages required beyond the standard library and existing project modules. Does NOT import `fetch_ohlcv` -- the caller provides prices.

---

## 11. MAX_TRADE_AMOUNT Enforcement

The `place_order()` method computes `order_value = entry_price * quantity` and compares it against `settings.max_trade_amount` (hard cap of 10,000 INR). If `order_value > settings.max_trade_amount`, the order is rejected with a `ValueError` before any database write occurs. This check happens after all other input validation but before the PENDING order is written to the `orders` table.

This means even simulated paper orders respect the capital constraint. The caller (Risk Agent) should have already sized the position correctly, but paper_trader enforces the cap as a safety net.

---

## 12. Timestamp Handling

All timestamps are generated as IST (Asia/Kolkata) ISO 8601 strings with timezone offset:

```python
datetime.datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
# Example: "2026-03-24T09:15:00+05:30"
```

This is consistent with the timestamp format used by `logger.py` and `validator.py`.

---

## 13. Open Questions (none)

All design decisions are covered by the existing rules files and context. No ambiguities remain for the Coder Agent.
