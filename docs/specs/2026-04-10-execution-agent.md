# Spec: src/agents/execution_agent.py

## 1. Module Purpose

The Execution Agent is the human checkpoint gateway between risk-approved trade signals and actual order placement via PaperTrader. It reads APPROVED rows from risk_approvals, sends a summary to both Telegram and Gmail for human confirmation, waits up to 8 minutes for a "Y" response via a checkpoint file, validates current market prices against approval prices (rejecting on >1.5% slippage, recalculating on >0.5%), and places CNC orders through PaperTrader. All orders are written to the orders table BEFORE placement. If no human confirmation arrives, the agent enters safe mode and places zero trades.

## 2. Architectural Decisions

### Decision 1 -- Human Confirmation Mechanism

No Telegram webhook or listener exists. Polling Telegram for replies would require a bot listener process that is out of scope.

**Resolution:** File-based checkpoint. The agent writes a checkpoint summary to the `execution_checkpoints` table (for audit) and creates a flag file at `/tmp/indian-trader-checkpoint-{YYYY-MM-DD}.txt`. The human confirms by writing the ISO date string to that file. The agent polls every 15 seconds for 8 minutes (32 polls), checking `content.strip() == run_date.isoformat()`. On timeout, the file is deleted and safe mode activates.

**Anti-stale-approval guard:** File must contain exactly `run_date.isoformat()` (e.g. `"2026-04-10"`), not a generic "Y". A leftover file from a prior day will never match today's date — preventing accidental auto-approval from stale state.

The Telegram/Gmail notification tells the human: `echo 2026-04-10 > /tmp/indian-trader-checkpoint-2026-04-10.txt` to confirm. This is testable without any network dependency.

### Decision 2 -- GTT Orders via PaperTrader

`PaperTrader.place_order()` already creates GTT IDs (gtt_sl_id, gtt_tp_id) in the orders table and sets stop_loss/take_profit in the positions table. The Monitor Agent's `check_gtts()` simulates GTT triggering. No separate `place_gtt_order()` call is needed. The execution agent calls `PaperTrader.place_order(side="BUY", ...)` which handles everything.

### Decision 3 -- Orders Table

PaperTrader already creates the orders table with its DDL. The execution agent does NOT create a separate orders table. It delegates all order writing to `PaperTrader.place_order()`, which writes PENDING then updates to FILLED. The execution agent writes to `execution_checkpoints` (new table) for audit trail only.

### Decision 4 -- Thesis Text Source

The watchlist table has no `rationale` column. The human checkpoint message will use: symbol, rank, sentiment, confidence, scorecard_score from the watchlist table, plus quantity/entry_price/stop_loss/take_profit from risk_approvals. This provides sufficient context without a rationale column.

### Decision 5 -- Recalculation on Price Deviation

When deviation > 0.5% but <= 1.5%: read ATR from the signals table for that symbol on that run_date. Recalculate:
- `new_stop_distance = min(atr * 2.0, current_price * 0.03)`
- `new_stop_loss = current_price - new_stop_distance`
- `new_take_profit = current_price + new_stop_distance * 2.0`
- `new_quantity = floor(risk_amount / new_stop_distance)` where risk_amount comes from risk_approvals
- If `new_quantity == 0` -> skip trade, log `recalculated_quantity_zero`
- Re-check MAX_TRADE_AMOUNT and 40% equity cap on new values

## 3. Public API

### Exception

```python
class ExecutionAgentError(Exception):
    """Raised on fatal non-recoverable errors.

    Attributes:
        message: Human-readable description.
        phase: Which phase failed. Valid values:
            'db_read', 'db_write', 'paper_trader_init',
            'price_fetch', 'order_placement'.
    """
    def __init__(self, message: str, phase: str) -> None: ...
```

### Dataclasses

```python
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
        status: 'PLACED', 'SKIPPED_SLIPPAGE', 'SKIPPED_RECALC_ZERO', 'SKIPPED_PRICE_FETCH_FAILED', 'SKIPPED_ORDER_ERROR'.
        deviation_pct: Price deviation from risk_approvals entry_price_approx (0.0 if no deviation check).
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
```

```python
@dataclass(frozen=True)
class ExecutionResult:
    """Full output of run_execution_agent().

    Attributes:
        run_date: Date the execution ran for.
        human_confirmed: True if human wrote Y to checkpoint file.
        safe_mode: True if no trades placed due to timeout or other safe mode trigger.
        safe_mode_reason: 'timeout_no_confirmation', 'no_approved_trades', 'kill_switch_active', None.
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
```

### Entry Point

```python
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
        ExecutionAgentError: On DB read/write failure, PaperTrader init failure.
            Price fetch and order placement failures are handled per-symbol
            (logged and skipped), not raised.
    """
```

## 4. Input Contract

### From risk_approvals table

Query: `SELECT * FROM risk_approvals WHERE run_date = ? AND approval_status = 'APPROVED'`

Required columns: symbol, run_date, quantity, entry_price_approx, stop_loss, take_profit, position_size_multiplier, risk_amount.

Preconditions:
- risk_agent has run for this run_date
- At least one APPROVED row expected (but zero is valid -- safe mode with reason `no_approved_trades`)
- entry_price_approx > 0.0 (risk_agent guarantees this for APPROVED rows)

### From watchlist table

Query: `SELECT symbol, rank, sentiment, confidence, scorecard_score FROM watchlist WHERE run_date = ? AND symbol IN (...)`

Used only for building the human checkpoint message. Not required for order placement.

### From signals table

Query: `SELECT symbol, atr FROM signals WHERE run_date = ? AND symbol = ?`

Used only when deviation > 0.5% and recalculation is needed. If ATR not found, skip the trade with `SKIPPED_PRICE_FETCH_FAILED`.

## 5. Output Contract

- Returns `ExecutionResult` -- never None.
- `orders_placed` contains only successfully placed orders (PaperTrader returned an order ID).
- `orders_skipped` contains all symbols that were not placed, with specific status string.
- `safe_mode=True` when: (a) timeout, (b) no approved trades, (c) kill switch was active in risk_approvals (all REJECTED).
- On safe mode, `orders_placed` is always empty.
- All IST timestamps use `ZoneInfo("Asia/Kolkata")`.

## 6. Execution Flow

1. Resolve run_date (default: today IST) and db_path.
2. Create `execution_checkpoints` table if not exists.
3. Read APPROVED rows from risk_approvals for run_date.
4. If zero APPROVED rows -> return safe mode with `no_approved_trades`.
5. Read watchlist context (symbol, rank, sentiment, confidence, scorecard_score) for checkpoint message.
6. Build human checkpoint message (see format below).
7. Send checkpoint via `send_checkpoint(subject, message)`.
8. Write checkpoint record to `execution_checkpoints` table with status='PENDING'.
9. Poll checkpoint file (`/tmp/indian-trader-checkpoint-{run_date}.txt`) every 15 seconds for 8 minutes.
10. If timeout -> update checkpoint to status='TIMEOUT', send alert, return safe mode.
11. If confirmed (file content.strip() == run_date.isoformat()) -> update checkpoint to status='CONFIRMED', delete file.
12. For each APPROVED symbol:
    a. Fetch current price via `fetch_ohlcv(symbols=[symbol], start_date=today-5, end_date=today, cache_expiry_hours=0)`, take last close.
    b. If price fetch fails -> log, add to skipped with `SKIPPED_PRICE_FETCH_FAILED`, continue.
    c. Compute deviation: `abs(current_price - entry_price_approx) / entry_price_approx`.
    d. If deviation > 1.5% -> skip with `SKIPPED_SLIPPAGE`, continue.
    e. If deviation > 0.5% -> recalculate (see Decision 5). If new_quantity == 0 -> skip with `SKIPPED_RECALC_ZERO`.
    f. If deviation <= 0.5% -> use original risk_approvals values.
    g. Call `PaperTrader.place_order(symbol, "BUY", quantity, entry_price, stop_loss, take_profit)`.
    h. If place_order raises ValueError or sqlite3.Error -> log, add to skipped with `SKIPPED_ORDER_ERROR`.
    i. On success -> add to orders_placed.
13. Log summary. Send info notification with placement results.
14. Return ExecutionResult.

### Human Checkpoint Message Format

```
EXECUTION CHECKPOINT - {run_date}

{N} trades approved for execution:

1. {SYMBOL}: BUY {qty} shares @ ~Rs.{entry_price:.0f}
   SL: Rs.{stop_loss:.0f} | TP: Rs.{take_profit:.0f}
   Rank: {rank} | Sentiment: {sentiment} ({confidence:.0%}) | Score: {scorecard_score}

To confirm: echo {run_date} > /tmp/indian-trader-checkpoint-{run_date}.txt
Deadline: 09:13 IST (8 minutes from now)
```

## 7. New Table DDL

### execution_checkpoints

```sql
CREATE TABLE IF NOT EXISTS execution_checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT    NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED', 'TIMEOUT')),
    symbols     TEXT    NOT NULL,  -- JSON list of symbols in this checkpoint
    message     TEXT    NOT NULL,  -- full checkpoint message sent to human
    created_at  TEXT    NOT NULL,  -- IST timestamp
    resolved_at TEXT,              -- IST timestamp when confirmed or timed out; NULL while PENDING
    UNIQUE(run_date)
);
```

No new DDL needed for orders/positions/trades -- PaperTrader owns those tables.

## 8. Constants

```python
AGENT_NAME: str = "execution_agent"
CHECKPOINT_FILE_PREFIX: str = "/tmp/indian-trader-checkpoint-"
CHECKPOINT_POLL_INTERVAL_SECONDS: int = 15
CHECKPOINT_TIMEOUT_SECONDS: int = 480  # 8 minutes
SLIPPAGE_SKIP_THRESHOLD: float = 0.015  # 1.5%
SLIPPAGE_RECALC_THRESHOLD: float = 0.005  # 0.5%
STOP_LOSS_ATR_MULTIPLIER: float = 2.0
STOP_LOSS_PCT_CAP: float = 0.03
TAKE_PROFIT_RATIO: float = 2.0
MAX_TRADE_AMOUNT: float = 10_000.0
MAX_POSITION_PCT: float = 0.40
STARTING_CAPITAL: float = 10_000.0
PRICE_FETCH_LOOKBACK_DAYS: int = 5
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
```

## 9. Logging

| When | agent_name | action | level | result |
|------|-----------|--------|-------|--------|
| Agent starts | execution_agent | execution_agent_run_started: {run_date} | INFO | ok |
| No approved trades | execution_agent | safe_mode: no_approved_trades | WARNING | skipped |
| Checkpoint sent | execution_agent | checkpoint_sent: {n} symbols | INFO | ok |
| Checkpoint confirmed | execution_agent | checkpoint_confirmed | INFO | ok |
| Checkpoint timeout | execution_agent | checkpoint_timeout: safe_mode_activated | WARNING | timeout |
| Price fetch failed | execution_agent | price_fetch_failed: {symbol} | WARNING | error |
| Slippage exceeded | execution_agent | price_slippage_exceeded: {symbol} deviation={pct:.2%} | WARNING | skipped |
| Recalculated | execution_agent | recalculated: {symbol} new_qty={qty} deviation={pct:.2%} | INFO | ok |
| Recalc zero qty | execution_agent | recalculated_quantity_zero: {symbol} | WARNING | skipped |
| Order placed | execution_agent | order_placed: {symbol} qty={qty} @ {price:.2f} | INFO | ok |
| Order error | execution_agent | order_placement_failed: {symbol} {error} | ERROR | error |
| Agent complete | execution_agent | execution_agent_complete: {placed}/{total} placed | INFO | ok |

## 10. Error Handling

- `ExecutionAgentError(message, phase='db_read')` -- raised on sqlite3.Error reading risk_approvals or watchlist.
- `ExecutionAgentError(message, phase='db_write')` -- raised on sqlite3.Error writing execution_checkpoints.
- `ExecutionAgentError(message, phase='paper_trader_init')` -- raised on PaperTrader construction failure.
- `FetchError` from fetch_ohlcv -- caught per-symbol, logged, trade skipped.
- `ValueError` / `sqlite3.Error` from PaperTrader.place_order -- caught per-symbol, logged, trade skipped.
- Never bare except. Only catch: `sqlite3.Error`, `ValueError`, `FetchError`, `OSError` (file operations).
- File I/O for checkpoint: catch `OSError` on read/write/delete. Missing file = not confirmed.

## 11. Out of Scope

- Shoonya broker integration (Phase 6).
- Telegram webhook/listener for receiving replies.
- Morning Validator Agent (separate module).
- GTT order placement -- handled internally by PaperTrader.place_order().
- Monitoring of placed orders after execution -- Monitor Agent's responsibility.
- Intraday orders -- CNC delivery only.
- Recalculating risk_amount from portfolio equity (use risk_amount as-is from risk_approvals).

## 12. Test Hints (minimum 15 scenarios)

1. **Happy path**: 2 APPROVED trades, human confirms, both placed. Verify orders_placed has 2 entries.
2. **Timeout**: No file written within 8 minutes. Verify safe_mode=True, reason='timeout_no_confirmation', zero orders placed.
3. **No approved trades**: Zero APPROVED rows in risk_approvals. Verify safe_mode=True, reason='no_approved_trades'.
4. **Slippage > 1.5%**: Current price 2% above entry_price_approx. Verify trade skipped with SKIPPED_SLIPPAGE.
5. **Slippage 0.5-1.5%**: Current price 1% above. Verify recalculation occurs, new quantity and stop_loss differ from originals.
6. **Slippage <= 0.5%**: Current price 0.3% above. Verify original values used unchanged.
7. **Recalculated quantity zero**: After recalc, quantity floors to 0. Verify SKIPPED_RECALC_ZERO.
8. **Price fetch failure**: fetch_ohlcv raises FetchError. Verify SKIPPED_PRICE_FETCH_FAILED, other trades still processed.
9. **PaperTrader.place_order raises ValueError**: Duplicate position. Verify SKIPPED_ORDER_ERROR, other trades still processed.
10. **Checkpoint file contains 'y' (lowercase)**: Case-insensitive. Verify confirmation accepted.
11. **Checkpoint file contains 'N'**: Not Y. Verify treated as timeout (file present but wrong content).
12. **Checkpoint file contains 'Y' with whitespace**: "  Y  \n". Verify confirmed.
13. **execution_checkpoints table**: Verify row written with status PENDING, then updated to CONFIRMED or TIMEOUT.
14. **Notification sent**: Verify send_checkpoint called with correct subject and message format.
15. **Safe mode alert**: On timeout, verify send_alert called.
16. **MAX_TRADE_AMOUNT cap on recalculation**: Recalculated position exceeds 10000. Verify quantity capped.
17. **Mixed outcomes**: 3 approved trades: one placed, one slippage skipped, one price fetch failed. Verify all three in correct lists.
18. **db_path_override**: Verify custom path used for DB connection.
19. **Checkpoint file deleted after read**: Verify file cleaned up on both confirm and timeout.
20. **ExecutionAgentError on DB read failure**: Verify phase='db_read' raised.

## 13. File Locations

- **Source**: `src/agents/execution_agent.py`
- **Tests**: `tests/agents/test_execution_agent.py`
- **No new __init__.py needed**: `src/agents/__init__.py` already exists (from risk_agent, signal_agent, etc.)

## 14. pyproject.toml

No new dependencies. All imports already available: sqlite3 (stdlib), datetime (stdlib), math (stdlib), os (stdlib), time (stdlib), json (stdlib), src.config.settings, src.data.fetcher, src.execution.paper_trader, src.utils.logger, src.utils.notifier.
