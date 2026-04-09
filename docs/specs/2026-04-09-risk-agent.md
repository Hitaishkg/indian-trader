# Spec: src/agents/risk_agent.py

**Date:** 2026-04-09
**Status:** Awaiting approval
**Author:** Architect Agent (claude-opus-4-5)
**Phase:** Phase 4 — Full Trading Pipeline (step 3 of 9)

---

## 1. Module Purpose

`src/agents/risk_agent.py` is the gatekeeper between human approval and order execution. It runs at 08:50 IST every trading morning, immediately before the Execution Agent.

It does four things in strict order:
1. Reads `human_approved=1` watchlist rows for today's `run_date`
2. Runs all four kill switch checks — if any fires, halts entirely and returns
3. Sizes each approved symbol using the 1%-ATR position sizing formula
4. Writes one `risk_approvals` row per symbol (APPROVED or REJECTED with reason)

This module does **not** place orders. The Execution Agent reads `risk_approvals` to decide what to place.

---

## 2. Architectural Decisions (5 critical questions resolved)

### Decision 1 — Portfolio equity source

**Resolved:** Instantiate `PaperTrader` and call `get_pnl()` to obtain `total_pnl`. Portfolio equity = `STARTING_CAPITAL + total_pnl`.

**Rationale:** `PaperTrader.get_pnl()` already aggregates realized P&L from `trades` and unrealized P&L from `positions` in a single, tested call. Reading raw tables directly would duplicate that aggregation logic and risk divergence. `STARTING_CAPITAL = 10_000.0 INR` is a module-level constant (matches `MAX_TRADE_AMOUNT` cap). This gives current equity at call time, inclusive of open position mark-to-market from the last `check_gtts()` update.

**Note on phase guard:** `PaperTrader.__init__` raises `ValueError` if `settings.live_trading is True`. Since `risk_agent.py` runs in paper mode only (Phase 4–5), this is the correct guard. In Phase 6 (live), the broker module replaces `PaperTrader`.

### Decision 2 — Peak equity for drawdown

**Resolved:** Compute peak equity from the full history of the `trades` table directly in `risk_agent.py` — no dedicated table needed yet.

**Formula:**
```
peak_equity = max(
    STARTING_CAPITAL,
    max over all prefixes of (STARTING_CAPITAL + cumulative_realized_pnl)
)
```

Scan every `trades` row ordered by `closed_at ASC`, compute a running cumulative sum of `pnl`, and track the running maximum of `(STARTING_CAPITAL + cumulative)`. This gives the historical equity peak from realized P&L only. Unrealized P&L is excluded from peak tracking — using mark-to-market peaks would create phantom drawdown triggers when an open position's unrealized P&L later reverses.

**Rationale:** `reporter_agent` (which will maintain `strategy_perf.equity`) is not built yet. A dedicated peak-equity table is premature. The trades table has the full history. This calculation runs once per risk_agent invocation (not hot path). When `reporter_agent` is built in Phase 4 step 6, it will write `strategy_perf.equity`; the spec for `monitor_agent`/`reporter_agent` should migrate peak tracking there at that point.

### Decision 3 — Sharpe calculation

**Resolved:** Compute Sharpe from the `trades` table directly. Daily returns are approximated by grouping closed trades by `DATE(closed_at)` and summing `pnl` per day. Days with no closed trades get a return of `0.0`. This daily P&L series is then divided by `STARTING_CAPITAL` to get daily return percentages.

**Formula:**
```
daily_returns = [sum(pnl) / STARTING_CAPITAL for each trading day]
sharpe = mean(daily_returns) / std(daily_returns) * sqrt(252)
```

**Condition:** Sharpe check only activates after `>= 20` completed trades. Below 20 trades, the check is skipped (logged as `sharpe_check_skipped_insufficient_trades`). This prevents false kill switch triggers in the early run.

**Rationale:** `daily_pnl` table (written by `reporter_agent`) does not exist yet. The trades table is the only source of truth. This approach is consistent with how `backtest/validator.py` computes Sharpe from trade returns. When `reporter_agent` is built, the orchestrator can switch to `daily_pnl` for efficiency — but `risk_agent.py` should not depend on an unbuilt module.

**std == 0 guard:** If `std(daily_returns) == 0` (all days same return, e.g. all zeros): Sharpe = 0.0. This fails the 0.8 gate and fires the kill switch. Correct behavior — a system producing no variation in daily returns has not demonstrated anything.

### Decision 4 — Entry price approximation

**Resolved:** Fetch fresh price via `yfinance` for each approved symbol, using the most recent close (1-day OHLCV). The `entry_price_approx` written to `risk_approvals` is the most recent close price from yfinance at the time risk_agent runs.

**Implementation:** Call `fetch_ohlcv([symbol], start_date=today - 5 days, end_date=today, cache_expiry_hours=0)` (cache bypass to get fresh data). Take the most recent `close` value. If `fetch_ohlcv` raises `FetchError`, set `entry_price_approx = 0.0` and log `price_fetch_failed`. The Execution Agent will use the live market price anyway — `entry_price_approx` is an approximation for sizing validation.

**Rationale:** The signals table has ATR but no price — `signal_agent.py` fetches 60-day OHLCV for indicator calculation but does not write the latest close to `signals`. The watchlist table has no price. Options (b) and (c) both mean the risk_agent writes a stale or zero price. The Execution Agent has a 0.5%/1.5% deviation check at execution time, which makes the risk_approvals price advisory. A fresh yfinance close is better than 0.0 for the Execution Agent to display to the human.

**Exception handling:** `FetchError` is caught per-symbol. A single price fetch failure does not abort the entire run — the symbol proceeds with `entry_price_approx = 0.0`.

### Decision 5 — Take-profit with approximate price

**Resolved:** Take-profit is computed from `entry_price_approx`:
```
take_profit = entry_price_approx + (stop_distance * TAKE_PROFIT_RATIO)
```

where `stop_distance = atr * STOP_LOSS_ATR_MULTIPLIER` and `TAKE_PROFIT_RATIO = 2.0`.

When `entry_price_approx = 0.0` (price fetch failed): set both `stop_loss = 0.0` and `take_profit = 0.0`. The Execution Agent must recalculate these from live price before placing orders. Log `entry_price_unavailable` for any symbol where this occurs.

**Rationale:** The 1:2 risk-reward is directionally correct regardless of whether the exact entry price is known at sizing time. The Execution Agent performs its own price validity check (0.5%/1.5% deviation) and will recalculate stop-loss and take-profit from the actual fill price. The values in `risk_approvals` are therefore planning values, not execution values. The `deviation > 0.5% → recalculate` rule in `execution_agent.py` (spec per agents-trading.md) handles this correctly.

---

## 3. Public Exception Class

```python
class RiskAgentError(Exception):
    """Raised when the Risk Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase of the agent failed.
    """

    def __init__(self, message: str, phase: str) -> None: ...
```

Valid `phase` values:
- `"db_read"` — failure reading watchlist, signals, or trades tables
- `"db_write"` — failure writing to risk_approvals table
- `"paper_trader_init"` — PaperTrader instantiation failed

Kill switch fires and sizing errors do **not** raise `RiskAgentError` — they are handled gracefully, logged, and reflected in `RiskAgentResult.kill_switch_fired` or individual `RiskApproval.approval_status`.

---

## 4. Public Dataclasses

### 4.1 RiskApproval

```python
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
        position_size_multiplier: Regime multiplier applied (1.0 or 0.5). Not 0.0 — blocked stocks never reach here.
        risk_amount: Actual risk in INR used for sizing (after multiplier, after floor).
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
    approval_status: str        # "APPROVED" or "REJECTED"
    rejection_reason: str | None
    approved_at: datetime.datetime  # IST-aware
```

### 4.2 RiskAgentResult

```python
@dataclass(frozen=True)
class RiskAgentResult:
    """Full output of run_risk_agent().

    Attributes:
        run_date: Date the risk checks were run for.
        kill_switch_fired: True if any kill switch triggered. All approvals empty when True.
        kill_switch_reason: Which kill switch fired (e.g. "drawdown_15pct"). None if no kill switch.
        approved: List of RiskApproval objects with approval_status="APPROVED".
        rejected: List of RiskApproval objects with approval_status="REJECTED".
        portfolio_equity: Current portfolio equity in INR at time of run.
        peak_equity: Historical peak equity in INR (from trades table computation).
        current_drawdown_pct: (peak_equity - portfolio_equity) / peak_equity * 100. 0.0 if no trades.
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
    completed_at: datetime.datetime  # IST-aware
```

---

## 5. Public Entry Point

```python
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
```

### Execution Flow

1. Resolve `run_date` (default: today IST), resolve `db_path`.
2. Log `risk_agent_run_started: {run_date}` via `log_agent_action` (OUTSIDE any transaction).
3. Ensure `risk_approvals` table exists (run DDL, close connection).
4. **READ PHASE** (single connection, close immediately after):
   - Read `watchlist` rows: `WHERE run_date = ? AND human_approved = 1 AND combined_decision = 'PROCEED'`, `ORDER BY rank ASC`.
   - Read `signals` rows for today: `WHERE run_date = ? AND signal_type = 'BUY'`, keyed by symbol.
   - Read `trades` rows: ALL rows, `ORDER BY closed_at ASC` — needed for drawdown and Sharpe.
5. Instantiate `PaperTrader(db_path)`. Call `get_pnl()` for total_pnl. Call `get_positions()` for open position count.
   - Wrap in `try/except (ValueError, sqlite3.Error) as exc → raise RiskAgentError(phase='paper_trader_init')`.
6. **KILL SWITCH PHASE** (pure Python, no DB connection held):
   - Compute all four kill switch checks (see Section 6).
   - If any fires: call `send_alert()`, log CRITICAL, write REJECTED rows for all watchlist symbols with `rejection_reason="kill_switch_fired"`, return `RiskAgentResult(kill_switch_fired=True, ...)`.
7. **SIZING PHASE** (pure Python, no DB connection held):
   - For each watchlist symbol (in rank order):
     - Fetch `entry_price_approx` via yfinance.
     - Look up ATR from signals table.
     - Apply position sizing algorithm (see Section 7).
     - Build `RiskApproval` object.
     - Stop processing new approvals once `(existing_open_positions + approved_count) >= 2`.
8. Log all sizing decisions OUTSIDE any transaction.
9. **WRITE PHASE** (fresh connection, explicit `BEGIN`/`COMMIT`):
   - INSERT OR REPLACE all `RiskApproval` objects (both APPROVED and REJECTED) to `risk_approvals`.
   - `PRAGMA wal_checkpoint(PASSIVE)` after COMMIT.
10. Call `send_info()` with completion summary.
11. Return `RiskAgentResult`.

---

## 6. Kill Switch Logic

All four checks use data already fetched in the READ PHASE. No additional DB reads during this phase. If any check fires, the agent logs the specific trigger and halts.

### Kill Switch 1 — Drawdown > 15% from peak

```
STARTING_CAPITAL = 10_000.0
portfolio_equity = STARTING_CAPITAL + pnl["total_pnl"]

# Compute peak from trades table (ordered by closed_at ASC)
running_sum = 0.0
peak_equity = STARTING_CAPITAL
for trade in trades_ordered_asc:
    running_sum += trade["pnl"]
    equity_at_point = STARTING_CAPITAL + running_sum
    if equity_at_point > peak_equity:
        peak_equity = equity_at_point

drawdown_pct = (peak_equity - portfolio_equity) / peak_equity * 100.0

if drawdown_pct > 15.0:
    → kill switch fires, reason = "drawdown_15pct"
```

When no trades exist: `peak_equity = STARTING_CAPITAL`, `portfolio_equity = STARTING_CAPITAL`, `drawdown_pct = 0.0`. No kill switch.

### Kill Switch 2 — Win rate < 40% after 20+ completed trades

```
trade_count = len(trades)
win_count = sum(1 for t in trades if t["pnl"] > 0)
win_rate_pct = win_count / trade_count * 100.0 if trade_count > 0 else 100.0

if trade_count >= 20 and win_rate_pct < 40.0:
    → kill switch fires, reason = "win_rate_below_40pct"
```

Below 20 trades: check skipped, logged as `win_rate_check_skipped_insufficient_trades`.

### Kill Switch 3 — 5 consecutive losses

```
# Examine the last 5 closed trades in chronological order (closed_at ASC)
recent_5 = trades_ordered_asc[-5:]  # last 5 entries
if len(recent_5) == 5 and all(t["pnl"] <= 0 for t in recent_5):
    → kill switch fires, reason = "consecutive_losses_5"
```

Fewer than 5 trades: check skipped.

**Note:** `pnl <= 0` (not `< 0`) counts break-even trades as losses. This is the conservative interpretation from risk.md.

### Kill Switch 4 — Sharpe < 0.8 after 20+ completed trades

```
# Group trades by DATE(closed_at), sum pnl per day
from collections import defaultdict
daily_pnl_map: dict[str, float] = defaultdict(float)
for trade in trades:
    day = trade["closed_at"][:10]  # "YYYY-MM-DD" prefix
    daily_pnl_map[day] += trade["pnl"]

daily_returns = [v / STARTING_CAPITAL for v in daily_pnl_map.values()]

if len(trades) >= 20:
    mean_r = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
    std_r = variance ** 0.5
    sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0.0 else 0.0

    if sharpe < 0.8:
        → kill switch fires, reason = "sharpe_below_0.8"
```

Below 20 trades: check skipped, logged as `sharpe_check_skipped_insufficient_trades`.

### On Kill Switch Fire

1. `send_alert(subject="KILL SWITCH FIRED", message=f"Reason: {reason}. All trading halted.")`
2. `log_agent_action(agent_name=AGENT_NAME, action=f"kill_switch_fired: {reason}", level="CRITICAL")`
3. Build `RiskApproval` for each watchlist symbol with `quantity=0, approval_status="REJECTED", rejection_reason="kill_switch_fired"`.
4. Write all rejections to `risk_approvals` (WRITE PHASE still executes).
5. Return `RiskAgentResult(kill_switch_fired=True, kill_switch_reason=reason, approved=[], rejected=[...], ...)`.

---

## 7. Position Sizing Algorithm

Applied per symbol in rank order (rank=1 first). Stop approving new symbols once `(existing_open_positions + approved_count) >= 2`.

### Constants

```python
STARTING_CAPITAL: float = 10_000.0         # INR
RISK_PCT: float = 0.01                      # 1% of current equity
STOP_LOSS_ATR_MULTIPLIER: float = 2.0       # normal regime
TAKE_PROFIT_RATIO: float = 2.0              # 1:2 risk-reward minimum
MAX_POSITION_PCT: float = 0.40              # 40% of equity hard cap
```

`MAX_TRADE_AMOUNT` is read from `settings.max_trade_amount` (int from env, ≤ 10_000).

### Step-by-step per symbol

**Step 1 — Check max positions already reached**
```
if existing_open_positions + approved_count >= 2:
    rejection_reason = "max_positions_reached"
    → REJECTED, stop processing remaining symbols
```

**Step 2 — Read ATR from signals table**
```
signal_row = signals_by_symbol.get(symbol)
if signal_row is None or signal_row["atr"] <= 0.0:
    rejection_reason = "zero_atr"
    → REJECTED, continue to next symbol
atr = signal_row["atr"]
```

**Step 3 — Read position_size_multiplier from watchlist row**
```
multiplier = watchlist_row["position_size_multiplier"]
# multiplier is 1.0 (ABOVE_200DMA) or 0.5 (BELOW_200DMA)
# 0.0 (BELOW_200DMA_10DAYS) never reaches here — combined_decision="SKIP" blocks it
```

**Step 4 — Compute risk amount**
```
base_risk_amount = portfolio_equity * RISK_PCT       # e.g. 0.01 * 10000 = 100.0
risk_amount = base_risk_amount * multiplier          # e.g. 100.0 * 0.5 = 50.0 in bear regime
```

**Step 5 — Compute stop distance**
```
stop_distance = atr * STOP_LOSS_ATR_MULTIPLIER       # e.g. 10.0 * 2.0 = 20.0
```

**Step 6 — Compute raw quantity**
```
raw_quantity = risk_amount / stop_distance            # e.g. 50.0 / 20.0 = 2.5
quantity = int(raw_quantity)                         # floor: 2 (never round up)
```

**Step 7 — Check quantity >= 1**
```
if quantity < 1:
    rejection_reason = "insufficient_capital"
    → REJECTED, continue to next symbol
```

**Step 8 — Apply 40% equity cap**
```
if entry_price_approx > 0.0:
    position_value = entry_price_approx * quantity
    max_position_value = portfolio_equity * MAX_POSITION_PCT   # e.g. 0.40 * 10000 = 4000
    if position_value > max_position_value:
        # Reduce quantity to fit within cap
        quantity = int(max_position_value / entry_price_approx)  # floor again
        if quantity < 1:
            rejection_reason = "position_size_exceeds_cap"
            → REJECTED, continue to next symbol
# If entry_price_approx == 0.0, skip cap check (can't apply it without price)
```

**Step 9 — Apply MAX_TRADE_AMOUNT hard cap**
```
if entry_price_approx > 0.0:
    position_value = entry_price_approx * quantity
    if position_value > float(settings.max_trade_amount):
        quantity = int(float(settings.max_trade_amount) / entry_price_approx)  # floor
        if quantity < 1:
            rejection_reason = "position_size_exceeds_cap"
            → REJECTED, continue to next symbol
# If entry_price_approx == 0.0, skip this cap too
```

**Step 10 — Compute stop_loss and take_profit**
```
if entry_price_approx > 0.0:
    stop_loss = entry_price_approx - stop_distance
    take_profit = entry_price_approx + (stop_distance * TAKE_PROFIT_RATIO)
    if stop_loss <= 0.0:
        # Pathological: stop below zero (penny stock with huge ATR)
        rejection_reason = "invalid_stop_loss"
        → REJECTED, continue to next symbol
else:
    stop_loss = 0.0
    take_profit = 0.0
    # Log entry_price_unavailable — execution_agent must recalculate
```

**Step 11 — APPROVED**
```
approved_count += 1
log_agent_action(
    agent_name=AGENT_NAME,
    action=f"approved: {symbol} qty={quantity} sl={stop_loss:.2f} tp={take_profit:.2f}",
    level="INFO",
    symbol=symbol,
    result="ok",
)
```

---

## 8. Database Access Pattern

Follows the pattern established in `watchlist_agent.py` and captured in `decisions-log.md`: `isolation_level=None`, explicit `BEGIN`/`COMMIT`, `log_agent_action` calls always OUTSIDE any transaction.

### Connection helper (private)

```python
def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open SQLite connection with WAL pragmas and isolation_level=None.

    Creates the parent directory if it does not exist.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn
```

### WAL pragmas (module-level constant)

```python
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)
```

### Transaction discipline

- READ PHASE: no explicit transaction — reads under WAL snapshot consistency. Close connection before computing.
- WRITE PHASE: fresh connection → explicit `BEGIN` → all INSERTs → `COMMIT` → `PRAGMA wal_checkpoint(PASSIVE)` → close.
- On write failure: `try ROLLBACK`, then close connection, then raise `RiskAgentError(phase='db_write')`.

### log_agent_action placement

`log_agent_action` must be called:
- AFTER the READ PHASE connection is closed
- AFTER the WRITE PHASE connection is closed
- NEVER inside a `BEGIN`/`COMMIT` block

Rationale from decisions-log.md: calling `log_agent_action` inside a transaction causes `SQLITE_BUSY_SNAPSHOT` errors because `logger.py`'s SQLiteHandler opens a second connection while the first holds a write lock.

### DB path resolution

```python
def _resolve_db_path(db_path_override: str | None) -> str:
    """Resolve absolute path to SQLite DB from override or settings."""
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
```

---

## 9. risk_approvals DDL

```sql
CREATE TABLE IF NOT EXISTS risk_approvals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT    NOT NULL,
    run_date                TEXT    NOT NULL,
    quantity                INTEGER NOT NULL DEFAULT 0,
    entry_price_approx      REAL    NOT NULL DEFAULT 0.0,
    stop_loss               REAL    NOT NULL DEFAULT 0.0,
    take_profit             REAL    NOT NULL DEFAULT 0.0,
    position_size_multiplier REAL   NOT NULL DEFAULT 1.0,
    risk_amount             REAL    NOT NULL DEFAULT 0.0,
    approval_status         TEXT    NOT NULL CHECK (approval_status IN ('APPROVED', 'REJECTED')),
    rejection_reason        TEXT,
    approved_at             TEXT    NOT NULL,
    UNIQUE(symbol, run_date)
);
```

`INSERT OR REPLACE` on `UNIQUE(symbol, run_date)` — re-runs on the same date overwrite prior results.

**Column notes:**
- `quantity`: 0 for rejected rows.
- `entry_price_approx`: 0.0 if yfinance fetch failed.
- `stop_loss`, `take_profit`: 0.0 when `entry_price_approx = 0.0`.
- `position_size_multiplier`: 1.0 or 0.5 (never 0.0 in approved rows — regime-blocked symbols never reach risk_agent).
- `risk_amount`: actual risk INR used for sizing after multiplier applied. 0.0 for rejected rows.
- `rejection_reason`: NULL for APPROVED rows. Exactly one of the defined strings for REJECTED rows (see below).

**Valid rejection_reason values:**
- `"kill_switch_fired"` — a kill switch triggered; no sizing was attempted
- `"insufficient_capital"` — computed quantity < 1 after sizing
- `"max_positions_reached"` — already have 2 open + approved positions
- `"zero_atr"` — ATR is missing or 0.0 in signals table
- `"position_size_exceeds_cap"` — even after cap reduction, quantity < 1
- `"invalid_stop_loss"` — stop_loss would be ≤ 0 (pathological case)

---

## 10. Helper Functions (private)

All private helpers are prefixed with `_`.

| Function | Signature | Purpose |
|----------|-----------|---------|
| `_resolve_db_path` | `(db_path_override: str \| None) -> str` | DB path from override or settings |
| `_open_connection` | `(db_path: str) -> sqlite3.Connection` | WAL pragmas, isolation_level=None, row_factory |
| `_setup_table` | `(db_path: str) -> None` | DDL for risk_approvals; raises RiskAgentError(phase='db_write') on failure |
| `_ist_now` | `() -> datetime.datetime` | Current IST-aware datetime |
| `_compute_portfolio_equity` | `(pnl: dict[str, float]) -> float` | `STARTING_CAPITAL + pnl["total_pnl"]` |
| `_compute_peak_equity` | `(trades: list[dict]) -> float` | Running max of (STARTING_CAPITAL + cumulative pnl), ordered by closed_at |
| `_compute_drawdown_pct` | `(portfolio_equity: float, peak_equity: float) -> float` | `(peak - current) / peak * 100`, 0.0 if peak == 0 |
| `_compute_win_rate_pct` | `(trades: list[dict]) -> float` | win_count / total * 100; returns 100.0 if no trades |
| `_check_consecutive_losses` | `(trades: list[dict]) -> bool` | True if last 5 trades all have pnl <= 0 |
| `_compute_sharpe` | `(trades: list[dict]) -> float` | Daily-return Sharpe * sqrt(252); 0.0 if std==0 |
| `_run_kill_switches` | `(trades: list[dict], portfolio_equity: float, peak_equity: float) -> tuple[bool, str \| None]` | Returns (fired, reason) |
| `_fetch_entry_price` | `(symbol: str) -> float` | yfinance close; 0.0 on FetchError |
| `_size_symbol` | `(symbol: str, watchlist_row: dict, signal_row: dict \| None, portfolio_equity: float, existing_open: int, approved_count: int, entry_price: float, run_date: datetime.date) -> RiskApproval` | All sizing steps for one symbol |
| `_build_summary_message` | `(result: RiskAgentResult) -> str` | Format send_info() message body |

---

## 11. Module-Level Constants

```python
AGENT_NAME: str = "risk_agent"
STARTING_CAPITAL: float = 10_000.0          # INR
RISK_PCT: float = 0.01                       # 1% risk per trade
STOP_LOSS_ATR_MULTIPLIER: float = 2.0        # normal regime stop-loss distance
TAKE_PROFIT_RATIO: float = 2.0               # 1:2 risk-reward minimum
MAX_POSITION_PCT: float = 0.40               # max 40% of equity per position
MAX_OPEN_POSITIONS: int = 2                  # max simultaneous open positions
DRAWDOWN_KILL_SWITCH_PCT: float = 15.0       # drawdown threshold
WIN_RATE_KILL_SWITCH_PCT: float = 40.0       # minimum win rate
CONSECUTIVE_LOSSES_KILL_SWITCH: int = 5      # consecutive losses threshold
SHARPE_KILL_SWITCH: float = 0.8              # minimum Sharpe ratio
KILL_SWITCH_MIN_TRADES: int = 20             # win rate and Sharpe inactive below this
PRICE_FETCH_LOOKBACK_DAYS: int = 5           # calendar days for yfinance price fetch
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
```

---

## 12. Imports Required

```python
from __future__ import annotations

import datetime
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.data.fetcher import FetchError, fetch_ohlcv
from src.execution.paper_trader import PaperTrader
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_info
```

`math` is used for `math.isnan` guard on float fields from DB.

---

## 13. Hard Rules Enforcement

| Rule | How enforced |
|------|-------------|
| No bare except | All except clauses specify exact exception types: `sqlite3.Error`, `ValueError`, `FetchError` |
| Type hints everywhere | Every function (public and private) has full type hints |
| Docstring on every public function | `run_risk_agent`, `RiskAgentError`, `RiskApproval`, `RiskAgentResult` all have docstrings |
| All timestamps IST | `_ist_now()` returns IST-aware datetime; ISO format includes `+05:30` offset |
| Quantities as int | `quantity` field is `int`, all sizing computes use `int(...)` (floor) |
| Prices as float | All price fields are `float` |
| MAX_TRADE_AMOUNT never exceeded | Step 9 of sizing applies `settings.max_trade_amount` cap before APPROVED |
| Order written before execution | N/A — risk_agent does not place orders |

---

## 14. Notifications

### On kill switch fire

```python
send_alert(
    subject=f"KILL SWITCH FIRED — {reason}",
    message=(
        f"Kill switch triggered: {reason}\n"
        f"Portfolio equity: ₹{portfolio_equity:.2f}\n"
        f"Peak equity: ₹{peak_equity:.2f}\n"
        f"Drawdown: {drawdown_pct:.1f}%\n"
        f"All trading halted. Manual review required."
    )
)
```

### On normal completion

```python
send_info(
    message=(
        f"Risk Agent complete: {approved_count} approved, {rejected_count} rejected.\n"
        + "\n".join(
            f"  {a.symbol}: {a.quantity} shares @ ~₹{a.entry_price_approx:.0f}, "
            f"SL=₹{a.stop_loss:.0f}, TP=₹{a.take_profit:.0f}"
            for a in result.approved
        )
    )
)
```

`send_info` returns a dict. Log a WARNING if `telegram` key is `False`, but do not raise — notification failure is non-fatal after the write phase succeeds.

---

## 15. Acceptance Criteria / Test Plan

Tests go in `tests/agents/test_risk_agent.py`, mirroring the `src/agents/` structure.

Use a temporary SQLite file per test (`tmp_path` pytest fixture). Pre-populate tables with known data. Test `run_risk_agent(run_date=..., db_path_override=tmp_db)`.

### Test scenarios (minimum 15)

| # | Scenario | Expected behaviour |
|---|----------|-------------------|
| 1 | No `human_approved=1` watchlist rows for run_date | Returns `RiskAgentResult(approved=[], rejected=[], kill_switch_fired=False)` |
| 2 | Kill switch 1 fires: drawdown > 15% | `kill_switch_fired=True`, reason=`"drawdown_15pct"`, all watchlist symbols appear in `rejected` with `rejection_reason="kill_switch_fired"`, `send_alert` called |
| 3 | Kill switch 2 fires: win rate < 40% with >= 20 trades | `kill_switch_fired=True`, reason=`"win_rate_below_40pct"` |
| 4 | Kill switch 2 does NOT fire with < 20 trades even if win rate is 0% | `kill_switch_fired=False`; check logged as skipped |
| 5 | Kill switch 3 fires: exactly 5 consecutive losing trades | `kill_switch_fired=True`, reason=`"consecutive_losses_5"` |
| 6 | Kill switch 3 does NOT fire: 4 consecutive losses then 1 win | `kill_switch_fired=False` |
| 7 | Kill switch 4 fires: Sharpe < 0.8 with >= 20 trades | `kill_switch_fired=True`, reason=`"sharpe_below_0.8"` |
| 8 | Kill switch 4 does NOT fire with < 20 trades | `kill_switch_fired=False` |
| 9 | Normal sizing: 1 approved symbol, ABOVE_200DMA regime | `quantity = floor(equity * 0.01 / (atr * 2))`, approval_status="APPROVED" |
| 10 | Regime multiplier applied: BELOW_200DMA | `risk_amount = equity * 0.01 * 0.5`; resulting quantity is halved vs normal |
| 11 | 40% cap applied: stock price × raw_quantity > 40% of equity | quantity reduced to `floor(equity * 0.40 / price)`; if still >= 1 → APPROVED |
| 12 | insufficient_capital: ATR so large that quantity floors to 0 | REJECTED with `rejection_reason="insufficient_capital"` |
| 13 | max_positions_reached: 2 already open positions, 1 approved watchlist symbol | REJECTED with `rejection_reason="max_positions_reached"` |
| 14 | zero_atr: symbol has no matching signals row for run_date | REJECTED with `rejection_reason="zero_atr"` |
| 15 | max_positions_reached stops at 2 approvals: 3 watchlist symbols but 0 open positions | First 2 symbols APPROVED, 3rd REJECTED with `rejection_reason="max_positions_reached"` |
| 16 | entry_price_approx = 0.0 (FetchError from yfinance): symbol approved | `stop_loss=0.0`, `take_profit=0.0`, `approval_status="APPROVED"` (if quantity computable) — cap checks skipped |
| 17 | DB write failure during risk_approvals INSERT | `RiskAgentError(phase='db_write')` raised |
| 18 | DB read failure during watchlist SELECT | `RiskAgentError(phase='db_read')` raised |
| 19 | Two kill switches fire simultaneously (drawdown AND consecutive losses) | Only the first checked switch reason is reported; both are logged; result shows single reason |
| 20 | Full happy path: 2 approved symbols, normal regime, prices available, no kill switches | 2 APPROVED rows in risk_approvals, send_info called, RiskAgentResult.approved has 2 items |

### Test fixtures required

- `make_watchlist_row(symbol, rank, position_size_multiplier, run_date)` — helper to INSERT a row with `human_approved=1, combined_decision='PROCEED'`
- `make_signals_row(symbol, atr, run_date)` — helper to INSERT a BUY signal with given ATR
- `make_trades_rows(pnl_list, closed_at_list)` — helper to batch INSERT trade history
- `make_positions_row(symbol)` — helper to INSERT an open position (for max_positions tests)
- Mock `fetch_ohlcv` via `unittest.mock.patch` to return a known DataFrame or raise `FetchError`
- Mock `send_alert` and `send_info` to avoid real network calls

---

## 16. Connections

- **Reads from:** `watchlist` (human_approved=1 rows), `signals` (ATR + signal_type), `trades` (all rows for kill switch math), `positions` (via PaperTrader.get_positions())
- **Writes to:** `risk_approvals`
- **Calls:** `PaperTrader.get_pnl()`, `PaperTrader.get_positions()`, `fetch_ohlcv()`, `log_agent_action()`, `send_alert()`, `send_info()`
- **Called by:** `orchestrator.py` (Phase 4 step 3 of morning session), runs at 08:50 IST
- **Next module:** `src/agents/execution_agent.py` reads `risk_approvals` where `approval_status='APPROVED'`

---

## 17. File Checklist for Coder Agent

1. `src/agents/risk_agent.py` — implement everything in this spec
2. `tests/agents/test_risk_agent.py` — 20 test scenarios above (Tester Agent writes this)

No new tables beyond `risk_approvals`. No new env variables. No new dependencies beyond those already in `pyproject.toml` (`sqlite3` is stdlib, `yfinance` is already a dependency via `fetch_ohlcv`).
