# Spec: src/agents/monitor_agent.py

## 1. Module Purpose

The Monitor Agent runs every 5 minutes during market hours (09:15-15:30 IST), checking all open positions against stop-loss and take-profit levels via `PaperTrader.check_gtts()`, tightening stop-losses when the regime filter or LLM sentiment warrants it, and running GTT reconciliation every 30 minutes. It also checks for a Nifty 50 close-to-close daily drop > 3% at 15:35 IST and triggers an emergency rescreen if detected.

## 2. Public API

### Exception

```python
class MonitorAgentError(Exception):
    """Raised on fatal, non-recoverable monitor errors.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed. Valid values:
            'db_read', 'paper_trader_init', 'price_fetch',
            'gtt_check', 'gtt_reconciliation', 'stop_tighten',
            'emergency_rescreen'.
    """
    def __init__(self, message: str, phase: str) -> None: ...
```

### Dataclass

```python
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
```

### Entry Point

```python
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
```

## 3. Input Contract

### Positions table (read via PaperTrader)

Columns used: `symbol`, `quantity`, `entry_price`, `current_price`, `stop_loss`, `take_profit`, `opened_at`, `updated_at`. All floats are INR, all integers > 0.

### Current prices

Fetched via `fetch_ohlcv()` with `cache_expiry_hours=0` for each symbol with an open position. Uses most recent close price. This is the same approach `risk_agent.py` uses. In production (Phase 6), this will switch to Fyers WebSocket. For Phase 4 paper trading, yfinance intraday-equivalent close is sufficient.

### Signals table (read for ATR)

Query: `SELECT atr FROM signals WHERE symbol = ? AND run_date = ? AND signal_type = 'BUY' ORDER BY id DESC LIMIT 1`

If no row found, fall back to: `SELECT atr FROM signals WHERE symbol = ? ORDER BY run_date DESC LIMIT 1`

If still no row, set ATR to 0.0 and skip tightening for that symbol. Log `atr_unavailable_skip_tighten`.

### Screener results table (read for regime)

Query: `SELECT regime FROM screener_results WHERE run_date = (SELECT MAX(run_date) FROM screener_results) LIMIT 1`

Single read per tick. Cached in-function (no module-level cache). Regime is uniform across all stocks for a given run_date.

### Research reports table (read for LLM sentiment)

Query: `SELECT sentiment, confidence FROM research_reports WHERE symbol = ? AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1`

### Nifty close-to-close (emergency rescreen)

At 15:35 IST only. Fetch Nifty 50 index data via `fetch_sector_indices()` with 10-day lookback, `cache_expiry_hours=0`. Compare latest close to previous close. If drop > 3%, call `run_screener_agent(run_date=run_date)`.

## 4. Output Contract

- Returns `MonitorResult` (frozen dataclass). Never returns None.
- `exits_triggered` contains the exact list returned by `PaperTrader.check_gtts()`.
- `stops_tightened` counts only successful tightenings (where `update_stop_loss()` did not raise).
- `kill_switch_detected` is informational only. The monitor does not halt itself — it continues monitoring existing positions. Kill switch halting is the risk_agent's responsibility for new trades.
- `emergency_rescreen_triggered` is True only when a rescreen was actually invoked (not just when the check ran).
- `completed_at` is always IST-aware.

## 5. Execution Flow

### Step 1 — Initialize

1. Resolve `run_date` (default: today IST) and `current_time` (default: now IST).
2. Resolve `db_path` via `_resolve_db_path()` (same helper pattern as risk_agent.py).
3. Instantiate `PaperTrader(db_path)`. On failure, raise `MonitorAgentError(phase='paper_trader_init')`.
4. Log `monitor_tick_started` with `current_time`.

### Step 2 — Get open positions and current prices

5. Call `pt.get_positions()`. If empty, skip to Step 7.
6. For each position symbol, fetch current price via `fetch_ohlcv([symbol], start_date=run_date - 5 days, end_date=run_date, cache_expiry_hours=0)`. Use last close. If fetch fails for a symbol, log `price_fetch_failed` and use `current_price` from positions table as fallback (stale but safe — better than skipping the position entirely).
7. Build `current_prices: dict[str, float]`.

### Step 3 — Check GTTs (every tick)

8. Call `pt.check_gtts(current_prices)`. Returns list of triggered exits.
9. For each triggered exit, log `gtt_exit_triggered` with symbol, exit_reason, exit_price.

### Step 4 — Stop-loss tightening (every tick, only if positions remain open)

10. Re-read positions (some may have been closed in Step 3).
11. Read regime from `screener_results` table (latest `run_date`).
12. Determine `tighten_stops`: True when regime is `BELOW_200DMA` or `BELOW_200DMA_10DAYS`.
13. For each remaining open position:
    a. Read ATR from `signals` table (see Input Contract for query).
    b. If ATR unavailable (0.0), log `atr_unavailable_skip_tighten`, skip.
    c. **Regime tightening**: if `tighten_stops` is True:
       - `new_stop = entry_price - (atr * 1.0)` (down from 2.0)
       - Only tighten if `new_stop > current_stop_loss` (never loosen a stop-loss).
       - Call `pt.update_stop_loss(symbol, new_stop)`.
       - Log `stop_tightened_regime` with old and new values.
    d. **LLM tightening**: read latest `research_reports` for this symbol.
       - If `sentiment == "Negative" AND confidence > 0.8`:
         - `new_stop = entry_price - (atr * 1.0)`
         - Only tighten if `new_stop > current_stop_loss`.
         - Call `pt.update_stop_loss(symbol, new_stop)`.
         - Log `stop_tightened_llm` with symbol, confidence, old and new values.
    e. Count successful tightenings (regime and LLM counted separately but summed in `stops_tightened`).

### Step 5 — GTT reconciliation (every 30 minutes)

14. Run when `current_time.minute % 30 == 0`.
15. Re-read positions (after Step 3 and Step 4 may have changed them).
16. For each open position, verify that `stop_loss > 0` and `take_profit > 0` and `stop_loss < entry_price` and `take_profit > entry_price`.
17. If any position fails these invariants:
    a. Log `gtt_missing_or_invalid` with symbol and which field is invalid.
    b. Send alert via `send_alert()`: "GTT reconciliation: {symbol} has invalid stop_loss={sl} or take_profit={tp}. Manual check required."
    c. Attempt to re-derive correct values from `signals` table ATR. If ATR available: `stop_loss = entry_price - (atr * 2.0)` (or `atr * 1.0` if regime tightened), `take_profit = entry_price + (atr * 2.0 * 2.0)`. Call `pt.update_stop_loss()` for stop, and directly UPDATE take_profit in positions table.
    d. If ATR not available, alert only — do not attempt repair.
18. Log `gtt_reconciliation_complete` with count of positions checked and issues found.

### Step 6 — Kill switch check (every tick)

19. Read all trades from `trades` table (same query as risk_agent: `SELECT pnl, closed_at FROM trades ORDER BY closed_at ASC`).
20. Compute portfolio equity via `pt.get_pnl()` + `STARTING_CAPITAL`.
21. Compute peak equity from trades (same `_compute_peak_equity` logic as risk_agent).
22. Evaluate drawdown and consecutive losses only (the two kill switches that can fire with few trades). Full kill switch eval uses same thresholds as risk_agent.py constants.
23. If any kill switch fires:
    a. Set `kill_switch_detected = True`.
    b. Send alert via `send_alert()`: "Monitor: kill switch detected — {reason}. No new trades will be opened. Existing positions continue to be monitored."
    c. Log `kill_switch_detected_monitor` with reason.
    d. Do NOT halt monitoring. Continue checking GTTs and stop-losses for existing positions.

### Step 7 — Emergency rescreen check (15:35 IST only)

24. Run when `current_time.hour == 15 and current_time.minute == 35`.
25. Fetch Nifty 50 index via `fetch_sector_indices(start_date=run_date - timedelta(days=10), end_date=run_date, cache_expiry_hours=0)`.
26. Compute close-to-close drop: `(prev_close - latest_close) / prev_close * 100`.
27. If drop > 3.0%:
    a. Log `emergency_rescreen_triggered` with drop percentage.
    b. Call `run_screener_agent(run_date=run_date)`.
    c. Send alert: "Nifty 50 dropped {pct:.1f}% today. Emergency rescreen completed."
    d. Set `emergency_rescreen_triggered = True`.
28. If `screener_agent` raises, catch `ScreenerAgentError`, log it, send alert, but do NOT raise from monitor.

### Step 8 — Return

29. Build and return `MonitorResult`.

## 6. Constants

```python
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
```

Import kill switch constants from `risk_agent` rather than duplicating:

```python
from src.agents.risk_agent import (
    DRAWDOWN_KILL_SWITCH_PCT,
    CONSECUTIVE_LOSSES_KILL_SWITCH,
    WIN_RATE_KILL_SWITCH_PCT,
    SHARPE_KILL_SWITCH,
    KILL_SWITCH_MIN_TRADES,
)
```

## 7. Logging

All via `log_agent_action(agent_name=AGENT_NAME, ...)`.

| Action | Level | When |
|--------|-------|------|
| `monitor_tick_started` | INFO | Every call |
| `no_open_positions` | INFO | positions list empty |
| `price_fetch_failed` | WARNING | yfinance fails for a symbol |
| `gtt_exit_triggered` | INFO | stop-loss or take-profit hit |
| `stop_tightened_regime` | INFO | regime tightening applied |
| `stop_tightened_llm` | INFO | LLM sentiment tightening applied |
| `atr_unavailable_skip_tighten` | WARNING | No ATR found for symbol |
| `stop_not_tightened_already_tight` | DEBUG | new_stop <= current_stop |
| `gtt_reconciliation_complete` | INFO | Every 30-min reconciliation |
| `gtt_missing_or_invalid` | ERROR | Reconciliation finds bad GTT |
| `kill_switch_detected_monitor` | CRITICAL | Kill switch fires |
| `emergency_rescreen_triggered` | WARNING | Nifty > 3% drop |
| `emergency_rescreen_failed` | ERROR | ScreenerAgentError caught |
| `monitor_tick_complete` | INFO | Every call, with result summary |

## 8. Error Handling

- `MonitorAgentError(phase='paper_trader_init')` — raised if `PaperTrader(db_path)` fails. Fatal.
- `MonitorAgentError(phase='db_read')` — raised if the initial positions/trades read fails. Fatal.
- `FetchError` from `fetch_ohlcv()` — caught per-symbol, fallback to stale `current_price`. Never fatal.
- `ValueError` from `pt.update_stop_loss()` — caught per-symbol, logged, counted as not tightened.
- `sqlite3.Error` on regime/signals/research reads — caught, logged, skip tightening for that tick. Not fatal.
- `ScreenerAgentError` from emergency rescreen — caught, logged, alerted. Not fatal.
- No bare `except` clauses.

## 9. Out of Scope

- Does NOT place new orders. That is execution_agent.py's job.
- Does NOT decide whether to enter new positions. That is risk_agent.py's job.
- Does NOT use Shoonya API. All GTT operations go through PaperTrader.
- Does NOT maintain its own scheduling loop. The orchestrator calls `run_monitor_agent()` on schedule.
- Does NOT write to `daily_pnl` or `strategy_perf` tables. That is reporter_agent.py's job.
- Does NOT implement Fyers WebSocket price streaming. Phase 6 concern.
- Does NOT update take_profit levels (only stop_loss is tightened per strategy rules). Exception: GTT reconciliation repair.

## 10. Test Hints (minimum 15 scenarios)

All tests use a temporary SQLite database. PaperTrader is instantiated with `db_path` pointing to the temp DB. Mock `fetch_ohlcv` and `fetch_sector_indices` to avoid network calls.

1. **No open positions** — returns `MonitorResult` with `positions_checked=0`, empty `exits_triggered`.
2. **Stop-loss triggered** — position with price below stop_loss. `exits_triggered` has one entry with `exit_reason="STOP_LOSS"`.
3. **Take-profit triggered** — position with price above take_profit. `exits_triggered` has one entry with `exit_reason="TAKE_PROFIT"`.
4. **No trigger** — price between stop_loss and take_profit. `exits_triggered` empty. Position `current_price` updated.
5. **Regime tightening — BELOW_200DMA** — screener_results has `regime="BELOW_200DMA"`. Position stop-loss moves from `entry - 2*ATR` to `entry - 1*ATR`. `stops_tightened=1`.
6. **Regime tightening — already tight** — current stop is already tighter than `entry - 1*ATR`. `stops_tightened=0`. Logged as `stop_not_tightened_already_tight`.
7. **LLM tightening — Negative >0.8** — research_reports has `sentiment="Negative"`, `confidence=0.85`. Stop tightened.
8. **LLM tightening — Negative <=0.8** — `confidence=0.7`. No tightening.
9. **LLM tightening — Positive** — No tightening regardless of confidence.
10. **ATR unavailable** — no signals row for symbol. Tightening skipped. Logged.
11. **GTT reconciliation runs at minute 0** — `current_time` with minute=0. `gtt_reconciliation_ran=True`.
12. **GTT reconciliation skipped at minute 5** — `current_time` with minute=5. `gtt_reconciliation_ran=False`.
13. **GTT reconciliation detects invalid stop_loss=0** — alert sent, repair attempted.
14. **Kill switch — drawdown >15%** — trades produce >15% drawdown. `kill_switch_detected=True`. Alert sent. Monitoring continues.
15. **Kill switch — consecutive 5 losses** — last 5 trades all negative. `kill_switch_detected=True`.
16. **Emergency rescreen — 4% drop** — at 15:35 IST, Nifty drops 4%. `emergency_rescreen_triggered=True`. `run_screener_agent` called.
17. **Emergency rescreen — 2% drop** — at 15:35 IST, Nifty drops 2%. `emergency_rescreen_triggered=False`.
18. **Emergency rescreen — not 15:35** — at 14:00 IST, even with >3% drop data available, no rescreen check runs.
19. **Price fetch failure fallback** — `fetch_ohlcv` raises `FetchError`. Uses stale `current_price` from positions. No crash.
20. **Multiple positions, mixed triggers** — 2 open positions: one hits stop-loss, one does not. `exits_triggered` has 1 entry, `positions_checked=2`.
21. **Both regime and LLM tighten same position** — both conditions met. Stop tightened once (to same value). `stops_tightened=1` (not 2, since second tighten is a no-op as `new_stop <= current_stop`).
22. **PaperTrader init failure** — `MonitorAgentError(phase='paper_trader_init')` raised.

## 11. File Locations

- Implementation: `src/agents/monitor_agent.py`
- Tests: `tests/agents/test_monitor_agent.py`
- No new `__init__.py` files needed (`src/agents/__init__.py` already exists from risk_agent).

## 12. pyproject.toml

No new dependencies. All imports already available: `src.execution.paper_trader`, `src.data.fetcher`, `src.agents.risk_agent`, `src.agents.screener_agent`, `src.utils.logger`, `src.utils.notifier`.

## 13. Architectural Decisions Resolved

**Decision 1 — Scheduling**: `run_monitor_agent()` is stateless per-tick. The orchestrator calls it every 5 minutes. The function accepts `current_time` to determine whether the 30-minute reconciliation or 15:35 emergency rescreen should run. No internal sleep loops.

**Decision 2 — Current price source**: `fetch_ohlcv()` with `cache_expiry_hours=0` per symbol (same as risk_agent). `check_gtts()` in PaperTrader compares prices passed in `current_prices` dict against stored `stop_loss`/`take_profit`. Stop-loss checked before take-profit (conservative). On fetch failure, falls back to stale `current_price` from positions table.

**Decision 3 — Regime check frequency**: Reads from `screener_results` table (latest `run_date`). Does NOT call `compute_regime()`. The screener already computed the regime. One query per tick, no module-level caching.

**Decision 4 — ATR source for tightening**: ATR from `signals` table, most recent `run_date` for that symbol with `signal_type='BUY'`. Fallback to any most recent signal for that symbol. If nothing found, ATR=0.0 and tightening is skipped with `atr_unavailable_skip_tighten` log.
