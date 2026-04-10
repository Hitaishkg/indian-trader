# Spec: src/agents/reporter_agent.py

## Module Purpose

Daily end-of-day reporting agent that runs at 15:45 IST after market close. Reads all trade and position data from SQLite, computes daily P&L / cumulative P&L / drawdown / Sharpe / win rate / profit factor, writes results to `daily_pnl` and `strategy_perf` tables, generates a markdown report file at `reports/YYYY-MM-DD.md`, and sends a summary notification via both Telegram and email.

---

## Architectural Decisions

### Decision 1 -- daily_pnl Table DDL

Replaces the placeholder schema in db-schema.md with a production-ready definition:

```sql
CREATE TABLE IF NOT EXISTS daily_pnl (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date         TEXT    NOT NULL UNIQUE,
    daily_pnl           REAL    NOT NULL DEFAULT 0.0,
    cumulative_pnl      REAL    NOT NULL DEFAULT 0.0,
    equity              REAL    NOT NULL,
    drawdown_pct        REAL    NOT NULL DEFAULT 0.0,
    peak_equity         REAL    NOT NULL,
    trades_closed_today INTEGER NOT NULL DEFAULT 0,
    win_count_today     INTEGER NOT NULL DEFAULT 0,
    open_positions      INTEGER NOT NULL DEFAULT 0,
    recorded_at         TEXT    NOT NULL
);
```

`INSERT OR REPLACE` on report_date UNIQUE constraint so re-runs on the same date overwrite.

### Decision 2 -- strategy_perf Table DDL

```sql
CREATE TABLE IF NOT EXISTS strategy_perf (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_date      TEXT    NOT NULL UNIQUE,
    total_trades     INTEGER NOT NULL DEFAULT 0,
    win_rate_pct     REAL    NOT NULL DEFAULT 0.0,
    sharpe_ratio     REAL    NOT NULL DEFAULT 0.0,
    max_drawdown_pct REAL    NOT NULL DEFAULT 0.0,
    profit_factor    REAL    NOT NULL DEFAULT 0.0,
    updated_at       TEXT    NOT NULL
);
```

`profit_factor = sum(pnl where pnl > 0) / abs(sum(pnl where pnl < 0))`. Guard: if no losses, profit_factor = 0.0 (insufficient data), not infinity. `INSERT OR REPLACE` on metric_date UNIQUE constraint.

### Decision 3 -- Report File Format

File: `reports/YYYY-MM-DD.md`. Create `reports/` directory via `os.makedirs(exist_ok=True)`.

Template:
```markdown
# Daily Report -- YYYY-MM-DD

## P&L Summary
- Today's P&L: Rs X.XX
- Cumulative P&L: Rs Y.YY
- Portfolio Equity: Rs Z.ZZ

## Risk Metrics
- Drawdown from peak: N.N%
- Win rate: W% (N of M trades)
- Sharpe ratio: S.SS (annualized)
- Profit factor: P.PP

## Kill Switch Status
- Drawdown: [SAFE / APPROACHING / TRIGGERED]
- Win rate: [SAFE / N/A -- insufficient trades / APPROACHING / TRIGGERED]
- Consecutive losses: N of 5
- Sharpe: [SAFE / N/A -- insufficient trades / APPROACHING / TRIGGERED]

## Today's Activity
- Trades closed: N
- Wins today: N | Losses today: N
- Open positions: N

## Open Positions
| Symbol | Entry | Current | SL | TP | P&L |
|--------|-------|---------|----|----|-----|
```

### Decision 4 -- Sharpe and Drawdown Computation

Uses IDENTICAL formulas as `risk_agent.py` to ensure consistency:

- **Peak equity**: `STARTING_CAPITAL + running_sum(trades.pnl)` ordered by `closed_at ASC`, tracking the maximum.
- **Drawdown**: `(peak_equity - portfolio_equity) / peak_equity * 100.0`. If peak <= 0, return 0.0.
- **Sharpe**: Group trades by `DATE(closed_at)`. Daily return = daily_pnl_sum / STARTING_CAPITAL. Annualize with `sqrt(252)`. Population std dev (not sample). If std = 0 or no trades, return 0.0.
- **Win rate**: `win_count / total_trades * 100.0`. If no trades, 0.0 (reporter never runs before any trade exists, but guard anyway).
- **N<20 guard**: For kill switch status display, show "N/A -- insufficient trades" when total_trades < 20 for win_rate and sharpe checks.
- **Kill switch thresholds for display**:
  - Drawdown: >10% = APPROACHING, >15% = TRIGGERED, else SAFE
  - Win rate: <45% = APPROACHING, <40% = TRIGGERED, else SAFE (only when total_trades >= 20)
  - Consecutive losses: show count "N of 5"
  - Sharpe: <1.0 = APPROACHING, <0.8 = TRIGGERED, else SAFE (only when total_trades >= 20)

---

## Public Exception

```python
class ReporterAgentError(Exception):
    """Raised on fatal, non-recoverable errors.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed. Valid values:
               'db_read', 'db_write', 'report_write', 'notification'.
    """
    def __init__(self, message: str, phase: str) -> None:
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")
```

---

## Public Dataclasses

```python
@dataclass(frozen=True)
class KillSwitchStatus:
    """Display status of each kill switch for the report.

    Attributes:
        drawdown_status: "SAFE", "APPROACHING", or "TRIGGERED".
        win_rate_status: "SAFE", "APPROACHING", "TRIGGERED", or "N/A -- insufficient trades".
        consecutive_losses: Current consecutive loss count (int, 0-based).
        sharpe_status: "SAFE", "APPROACHING", "TRIGGERED", or "N/A -- insufficient trades".
    """
    drawdown_status: str
    win_rate_status: str
    consecutive_losses: int
    sharpe_status: str
```

```python
@dataclass(frozen=True)
class DailyReport:
    """All computed metrics for a single trading day.

    Attributes:
        report_date: The trading date this report covers.
        daily_pnl: P&L from trades closed today (INR).
        cumulative_pnl: Total realized P&L across all trades (INR).
        unrealized_pnl: Unrealized P&L from open positions (INR).
        equity: STARTING_CAPITAL + realized_pnl + unrealized_pnl.
        peak_equity: Historical peak equity (INR).
        drawdown_pct: (peak - equity) / peak * 100.
        total_trades: Count of all closed trades.
        win_count: Total winning trades.
        loss_count: Total losing trades.
        win_rate_pct: win_count / total_trades * 100 (0.0 if no trades).
        sharpe_ratio: Annualized Sharpe from daily returns.
        profit_factor: Sum(wins) / abs(Sum(losses)). 0.0 if no losses.
        trades_closed_today: Number of trades closed on report_date.
        wins_today: Winning trades closed today.
        losses_today: Losing trades closed today.
        open_positions: List of dicts from PaperTrader.get_positions().
        open_position_count: len(open_positions).
        kill_switch_status: KillSwitchStatus for report display.
        computed_at: IST timestamp.
    """
    report_date: datetime.date
    daily_pnl: float
    cumulative_pnl: float
    unrealized_pnl: float
    equity: float
    peak_equity: float
    drawdown_pct: float
    total_trades: int
    win_count: int
    loss_count: int
    win_rate_pct: float
    sharpe_ratio: float
    profit_factor: float
    trades_closed_today: int
    wins_today: int
    losses_today: int
    open_positions: list[dict[str, object]]
    open_position_count: int
    kill_switch_status: KillSwitchStatus
    computed_at: datetime.datetime
```

```python
@dataclass(frozen=True)
class ReporterResult:
    """Return value of run_reporter_agent().

    Attributes:
        report_date: Date this report covers.
        report: The full DailyReport with all metrics.
        report_file_path: Absolute path to the generated markdown file.
        db_written: True if daily_pnl + strategy_perf rows inserted successfully.
        notification_sent: Dict with {"telegram": bool, "gmail": bool}.
        completed_at: IST timestamp.
    """
    report_date: datetime.date
    report: DailyReport
    report_file_path: str
    db_written: bool
    notification_sent: dict[str, bool]
    completed_at: datetime.datetime
```

---

## Public Entry Point

```python
def run_reporter_agent(
    report_date: datetime.date | None = None,
    db_path_override: str | None = None,
) -> ReporterResult:
    """Generate end-of-day report, persist metrics, and send notification.

    Args:
        report_date: Date to report on. Defaults to today in IST.
        db_path_override: Absolute path to SQLite DB. When None, derived from
                          settings.database_url. Used in tests.

    Returns:
        ReporterResult with full metrics, file path, and notification status.

    Raises:
        ReporterAgentError: On DB read failure (phase='db_read'),
                            DB write failure (phase='db_write'),
                            report file write failure (phase='report_write'),
                            or both notification channels failing (phase='notification').
    """
```

---

## Execution Flow

1. Default `report_date` to today IST if None.
2. Resolve DB path via `_resolve_db_path(db_path_override)` (same pattern as risk_agent.py).
3. Log `reporter_agent_run_started` via `log_agent_action`.
4. Create `daily_pnl` and `strategy_perf` tables via DDL (idempotent).
5. **DB Read Phase** (wrap in try/except sqlite3.Error -> ReporterAgentError phase='db_read'):
   a. Read ALL trades ordered by `closed_at ASC`.
   b. Read trades closed today: `WHERE DATE(closed_at) = report_date`.
   c. Instantiate `PaperTrader(db_path)` and call `get_pnl()` and `get_positions()`.
6. **Compute Phase** (pure computation, no exceptions expected):
   a. `daily_pnl` = sum of pnl from trades closed today.
   b. `cumulative_pnl` = pnl_data["realized_pnl"] from PaperTrader.get_pnl().
   c. `unrealized_pnl` = pnl_data["unrealized_pnl"].
   d. `equity` = STARTING_CAPITAL + cumulative_pnl + unrealized_pnl.
   e. `peak_equity` = `_compute_peak_equity(all_trades)` (identical to risk_agent.py).
   f. `drawdown_pct` = `_compute_drawdown_pct(equity, peak_equity)`.
   g. `total_trades` = pnl_data["trade_count"].
   h. `win_count` = pnl_data["win_count"], `loss_count` = pnl_data["loss_count"].
   i. `win_rate_pct` = win_count / total_trades * 100 if total_trades > 0 else 0.0.
   j. `sharpe_ratio` = `_compute_sharpe(all_trades)` (identical to risk_agent.py).
   k. `profit_factor` = sum(pnl for t where pnl > 0) / abs(sum(pnl for t where pnl < 0)). If denominator is 0, return 0.0.
   l. `trades_closed_today`, `wins_today`, `losses_today` from today's trades.
   m. `consecutive_losses` = count of consecutive trades with pnl <= 0 from end of all_trades.
   n. `kill_switch_status` = _compute_kill_switch_status(drawdown_pct, win_rate_pct, total_trades, sharpe_ratio, consecutive_losses).
7. Build `DailyReport` frozen dataclass.
8. **DB Write Phase** (wrap in try/except sqlite3.Error -> ReporterAgentError phase='db_write'):
   a. `INSERT OR REPLACE INTO daily_pnl` with report metrics.
   b. `INSERT OR REPLACE INTO strategy_perf` with aggregated metrics.
   c. Commit + `PRAGMA wal_checkpoint(PASSIVE)`.
9. **Report Write Phase** (wrap in try/except OSError -> ReporterAgentError phase='report_write'):
   a. `os.makedirs("reports", exist_ok=True)`.
   b. Write markdown to `reports/YYYY-MM-DD.md`.
10. **Notification Phase**:
    a. Build summary message (see format below).
    b. Call `send_alert(subject, message)` to send via both Telegram + Gmail.
    c. If both channels return False -> raise ReporterAgentError(phase='notification').
    d. If one channel fails -> log warning, do not raise.
11. Log `reporter_agent_complete` via `log_agent_action`.
12. Return `ReporterResult`.

---

## Notification Message Format

```
Daily Report YYYY-MM-DD
P&L today: Rs X.XX | Running total: Rs Y.YY
Equity: Rs Z.ZZ | Drawdown: N.N%
Win rate: W% (after N trades) | Sharpe: S.SS
Open positions: N
[Kill switch status: SAFE / WARNING: drawdown at X%]
```

Use `send_alert()` (not `send_info()`) so notification goes to BOTH Telegram and Gmail. Subject: `"Daily Report YYYY-MM-DD"`.

---

## SQL Queries

### Read all trades
```sql
SELECT pnl, closed_at FROM trades ORDER BY closed_at ASC
```

### Read today's trades
```sql
SELECT pnl FROM trades WHERE DATE(closed_at) = ?
```
Parameter: `report_date.isoformat()`

### Insert daily_pnl
```sql
INSERT OR REPLACE INTO daily_pnl
    (report_date, daily_pnl, cumulative_pnl, equity, drawdown_pct,
     peak_equity, trades_closed_today, win_count_today, open_positions,
     recorded_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

### Insert strategy_perf
```sql
INSERT OR REPLACE INTO strategy_perf
    (metric_date, total_trades, win_rate_pct, sharpe_ratio,
     max_drawdown_pct, profit_factor, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
```

---

## Constants

```python
AGENT_NAME: str = "reporter_agent"
STARTING_CAPITAL: float = 10_000.0
KILL_SWITCH_MIN_TRADES: int = 20
DRAWDOWN_APPROACHING_PCT: float = 10.0
DRAWDOWN_TRIGGERED_PCT: float = 15.0
WIN_RATE_APPROACHING_PCT: float = 45.0
WIN_RATE_TRIGGERED_PCT: float = 40.0
SHARPE_APPROACHING: float = 1.0
SHARPE_TRIGGERED: float = 0.8
CONSECUTIVE_LOSSES_LIMIT: int = 5
IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
REPORTS_DIR: str = "reports"
```

WAL pragmas: same tuple as risk_agent.py.

---

## Logging

All via `log_agent_action(agent_name=AGENT_NAME, ...)`.

| When | action | level | result |
|------|--------|-------|--------|
| Start | `reporter_agent_run_started: {report_date}` | INFO | ok |
| DB read complete | `db_read_complete: {total_trades} trades, {open_count} open` | INFO | ok |
| Compute complete | `metrics_computed: equity={equity}, dd={dd}%, sharpe={sharpe}` | INFO | ok |
| DB write complete | `db_write_complete: daily_pnl + strategy_perf` | INFO | ok |
| Report file written | `report_written: {file_path}` | INFO | ok |
| Notification sent | `notification_sent` | INFO | ok |
| Kill switch approaching | `kill_switch_approaching: {which} at {value}` | WARNING | warning |
| Kill switch triggered (display only) | `kill_switch_triggered_display: {which}` | WARNING | warning |
| Notification partial failure | `notification_partial_failure: telegram={t}, gmail={g}` | WARNING | error |
| Complete | `reporter_agent_complete: {report_date}` | INFO | ok |

---

## Error Handling

- `sqlite3.Error` during reads -> `ReporterAgentError(message, phase="db_read")`
- `sqlite3.Error` during writes -> `ReporterAgentError(message, phase="db_write")`
- `OSError` / `IOError` during report file write -> `ReporterAgentError(message, phase="report_write")`
- Both notification channels returning False -> `ReporterAgentError(message, phase="notification")`
- `ValueError` from PaperTrader init (live_trading=True) -> re-raise as `ReporterAgentError(phase="db_read")`
- Never bare except. Never swallow exceptions silently.

---

## Out of Scope

- Does NOT place or modify any orders.
- Does NOT trigger kill switches (only displays their status).
- Does NOT halt trading. That is risk_agent.py's job.
- Does NOT compute intraday P&L. Only end-of-day.
- Does NOT read from or write to market_data table.
- Does NOT fetch prices from external APIs. Uses PaperTrader data only.

---

## Test Hints (15 scenarios)

1. **No trades, no positions**: Report shows all zeros, equity = STARTING_CAPITAL, drawdown 0%, kill switch all SAFE or N/A.
2. **One winning trade closed today**: daily_pnl > 0, cumulative_pnl > 0, win_rate 100%.
3. **One losing trade closed today**: daily_pnl < 0, win_rate 0%.
4. **Multiple trades, some today some yesterday**: daily_pnl only counts today's, cumulative counts all.
5. **Open positions with unrealized P&L**: equity includes unrealized_pnl, open_positions populated in report.
6. **Drawdown approaching (11%)**: kill_switch_status.drawdown_status == "APPROACHING".
7. **Drawdown triggered (16%)**: kill_switch_status.drawdown_status == "TRIGGERED".
8. **Win rate approaching (43%) with 20+ trades**: win_rate_status == "APPROACHING".
9. **Win rate triggered (38%) with 20+ trades**: win_rate_status == "TRIGGERED".
10. **Win rate with < 20 trades**: win_rate_status == "N/A -- insufficient trades".
11. **Sharpe below 0.8 with 20+ trades**: sharpe_status == "TRIGGERED".
12. **Consecutive losses = 4**: consecutive_losses == 4, displayed as "4 of 5".
13. **Profit factor with no losses**: profit_factor == 0.0, not infinity.
14. **Profit factor normal**: sum(wins) / abs(sum(losses)) computed correctly.
15. **Re-run same date**: INSERT OR REPLACE overwrites both tables without error.
16. **Report file created**: `reports/YYYY-MM-DD.md` exists with correct content.
17. **DB write failure**: ReporterAgentError raised with phase='db_write'.
18. **Report file write failure (read-only dir)**: ReporterAgentError raised with phase='report_write'.
19. **Both notifications fail**: ReporterAgentError raised with phase='notification'.
20. **One notification fails**: Warning logged, no exception raised, notification_sent shows partial.

---

## File Locations

- **Source**: `src/agents/reporter_agent.py`
- **Tests**: `tests/agents/test_reporter_agent.py`
- **Init files needed**: `src/agents/__init__.py` (should already exist), `tests/agents/__init__.py` (should already exist)

---

## pyproject.toml

No new dependencies. Uses only: sqlite3 (stdlib), datetime (stdlib), os (stdlib), zoneinfo (stdlib), dataclasses (stdlib), math (stdlib), collections (stdlib). All external deps (PaperTrader, logger, notifier, settings) already in the project.
