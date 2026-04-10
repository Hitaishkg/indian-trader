# Spec: dashboard/index.html + dashboard/server.py

**Date:** 2026-04-10
**Status:** Awaiting implementation
**Phase:** Tooling — read-only monitoring dashboard

---

## Purpose

A read-only monitoring dashboard for the Indian Trader pipeline that presents system build status and live paper-trading state in a single HTML file served by a minimal Python HTTP server. Requires no external dependencies beyond the Python standard library and Chart.js from CDN; auto-refreshes every 60 seconds from a single `/api/data` JSON endpoint backed by SQLite.

---

## Architecture

```
Browser (dashboard/index.html)
        |
        |  GET /api/data  (every 60s via setInterval)
        v
dashboard/server.py  (Python stdlib: http.server, port 8765)
        |
        |---> sqlite3  ../data/trading.db
        |---> subprocess  git log --oneline -10
        |---> subprocess  python -m pytest tests/ --collect-only -q
        |
        v
JSON blob  { "updated_at", "build": {...}, "trading": {...} }
        |
        v
Two-tab HTML page (Tab 1: Build Status | Tab 2: Paper Trading)
        |---> Chart.js CDN  (P&L equity curve)
        |---> Inline CSS  (dark theme, monospace)
```

---

## File Structure

```
indian-trader/
  dashboard/
    server.py       <- Python stdlib HTTP server + SQLite reader
    index.html      <- Single-file dashboard (all CSS/JS inline)
  data/
    trading.db      <- SQLite (read-only by dashboard)
```

---

## Data Contract

The `/api/data` endpoint returns a single JSON object.

### Top-level envelope

```json
{
  "updated_at": "<ISO 8601 timestamp IST>",
  "build":   { ... },
  "trading": { ... }
}
```

---

### `build` object

```json
{
  "phases": [
    {
      "name": "Phase 1 — Foundation",
      "modules": [
        {
          "module": "src/data/validator.py",
          "status": "Built",
          "notes": "Data quality gate; first module per phases.md"
        }
      ]
    }
  ],

  "agent_activity": [
    {
      "id": 5382,
      "logged_at": "2026-04-09T09:44:15+05:30",
      "agent_name": "validator",
      "level": "INFO",
      "action": "universe_score: ...",
      "symbol": null,
      "result": null,
      "data_quality_score": 0.9
    }
  ],

  "agent_summary": [
    {
      "agent_name": "validator",
      "log_count": 142,
      "last_seen": "2026-04-09T09:44:15+05:30"
    }
  ],

  "recent_commits": [
    {
      "hash": "76abab3",
      "message": "docs: add Known API Gotchas..."
    }
  ],

  "test_count": {
    "total": 526,
    "raw_output": "526 tests collected in 6.78s"
  }
}
```

**Sources:**
- `phases`: hardcoded Python dict in server.py from `docs/context/current-state.md`
- `agent_activity`: `SELECT id, logged_at, agent_name, level, action, symbol, result, data_quality_score FROM agent_logs ORDER BY id DESC LIMIT 20`
- `agent_summary`: `SELECT agent_name, COUNT(*) AS log_count, MAX(logged_at) AS last_seen FROM agent_logs GROUP BY agent_name ORDER BY last_seen DESC`
- `recent_commits`: `subprocess.run(["git", "log", "--oneline", "-10"], ...)`
- `test_count`: `subprocess.run(["python", "-m", "pytest", "tests/", "--collect-only", "-q"], ...)` — parse last non-empty stdout line

---

### `trading` object

```json
{
  "regime": {
    "status": "BELOW_200DMA_10DAYS",
    "badge": "RED",
    "label": "Nifty Below 200 DMA (10+ days)",
    "note": "market_data table not yet built — regime sourced from screener_results"
  },

  "portfolio": {
    "starting_capital": 10000.0,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "total_equity": 10000.0,
    "open_positions_count": 1
  },

  "kill_switches": {
    "trade_count": 0,
    "min_trades_required": 20,
    "drawdown": {
      "value_pct": 0.0,
      "threshold_pct": 15.0,
      "status": "GREEN",
      "label": "Drawdown"
    },
    "win_rate": {
      "value_pct": 100.0,
      "threshold_pct": 40.0,
      "status": "GREEN",
      "label": "Win Rate",
      "skipped": true
    },
    "consecutive_losses": {
      "last_5_pnl": [],
      "fired": false,
      "status": "GREEN",
      "label": "Consecutive Losses (last 5)"
    },
    "sharpe": {
      "value": 0.0,
      "threshold": 0.8,
      "status": "GREEN",
      "label": "Sharpe Ratio",
      "skipped": true
    },
    "overall_fired": false
  },

  "positions": [...],
  "signals_today": [...],
  "screener_top5": [...],
  "research_sentiment": [...],
  "watchlist": [...],
  "risk_approvals_today": [...],

  "pnl_chart": {
    "labels": ["2026-03-24", "2026-03-25"],
    "cumulative_pnl": [0.0, -5.0],
    "equity_curve": [10000.0, 9995.0]
  },

  "trade_history": [...]
}
```

---

## All SQL Queries

```sql
-- Agent activity (last 20)
SELECT id, logged_at, agent_name, level, action, symbol, result, data_quality_score
FROM agent_logs ORDER BY id DESC LIMIT 20;

-- Agent summary
SELECT agent_name, COUNT(*) AS log_count, MAX(logged_at) AS last_seen
FROM agent_logs GROUP BY agent_name ORDER BY last_seen DESC;

-- Realized P&L
SELECT COALESCE(SUM(pnl), 0.0) AS total_pnl FROM trades;

-- Unrealized P&L
SELECT COALESCE(SUM(pnl), 0.0) AS unrealized_pnl FROM positions;

-- Open positions count
SELECT COUNT(*) AS open_count FROM positions;

-- All trades for kill switch computation
SELECT id, symbol, pnl, closed_at FROM trades ORDER BY id ASC;

-- Last 5 trades for consecutive-losses check
SELECT pnl FROM trades ORDER BY id DESC LIMIT 5;

-- Daily P&L grouped (for Sharpe)
SELECT date(closed_at) AS trade_date, SUM(pnl) AS daily_pnl
FROM trades GROUP BY date(closed_at) ORDER BY trade_date ASC;

-- Win count
SELECT COUNT(*) AS total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins FROM trades;

-- Open positions
SELECT symbol, quantity, entry_price, current_price, stop_loss, take_profit,
       pnl, pnl_pct, opened_at, updated_at
FROM positions ORDER BY opened_at DESC;

-- Today's signals (latest run_date)
SELECT symbol, rsi, macd_signal, bollinger_position, atr, groq_confidence,
       signal_type, skip_reason, signalled_at
FROM signals WHERE run_date = (SELECT MAX(run_date) FROM signals) ORDER BY id ASC;

-- Screener top 5 (latest run_date)
SELECT symbol, rank, momentum_score, quality_passed, regime, position_size_multiplier, run_date
FROM screener_results
WHERE run_date = (SELECT MAX(run_date) FROM screener_results)
ORDER BY rank ASC LIMIT 5;

-- Research sentiment (latest per symbol)
SELECT r.symbol, r.run_date, r.sentiment, r.confidence, r.completed_at
FROM research_reports r
INNER JOIN (
    SELECT symbol, MAX(run_date) AS max_date
    FROM research_reports WHERE completed_at IS NOT NULL GROUP BY symbol
) latest ON r.symbol = latest.symbol AND r.run_date = latest.max_date
ORDER BY r.run_date DESC, r.symbol ASC;

-- Watchlist (latest run_date)
SELECT symbol, run_date, combined_decision, scorecard_score, scorecard_max,
       sentiment, confidence, rank, regime, position_size_multiplier,
       human_approved, approval_source, added_at
FROM watchlist WHERE run_date = (SELECT MAX(run_date) FROM watchlist) ORDER BY rank ASC;

-- Risk approvals (today — ? = today's date string)
SELECT symbol, run_date, quantity, entry_price_approx, stop_loss, take_profit,
       position_size_multiplier, risk_amount, approval_status, rejection_reason, approved_at
FROM risk_approvals WHERE run_date = ? ORDER BY approved_at ASC;

-- Regime (from screener_results — market_data not yet built)
SELECT regime FROM screener_results
WHERE run_date = (SELECT MAX(run_date) FROM screener_results) LIMIT 1;

-- P&L chart data
SELECT date(closed_at) AS trade_date, SUM(pnl) AS daily_pnl
FROM trades GROUP BY date(closed_at) ORDER BY trade_date ASC;

-- Trade history (last 20)
SELECT id, symbol, quantity, entry_price, exit_price, pnl, pnl_pct,
       exit_reason, opened_at, closed_at
FROM trades ORDER BY id DESC LIMIT 20;
```

---

## Server Implementation (`dashboard/server.py`)

### Key decisions
- Python stdlib only: `http.server`, `sqlite3`, `subprocess`, `json`, `datetime`, `os`, `zoneinfo`
- Single `BaseHTTPRequestHandler` with `GET /api/data` and `GET /` (serve index.html)
- DB path: `os.path.join(os.path.dirname(__file__), '..', 'data', 'trading.db')`
- `PRAGMA query_only = ON` on every connection (read-only enforcement)
- WAL pragmas: `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`
- `sqlite3.Row` row factory
- Each `_fetch_*` function catches its own `sqlite3.Error` and returns empty structure — one failed query never crashes the whole response
- CORS headers on every response: `Access-Control-Allow-Origin: *`
- `subprocess` calls: `timeout=15`, `cwd=PROJECT_ROOT`, `capture_output=True`

### Module structure

```python
# Constants
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'trading.db')
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
PORT = 8765
STARTING_CAPITAL = 10_000.0
IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# Hardcoded phase data (from current-state.md)
PHASES_DATA = [...]  # transcribed from docs/context/current-state.md

# Regime badge mapping
REGIME_MAP = {
    "ABOVE_200DMA": ("GREEN", "Nifty Above 200 DMA"),
    "BELOW_200DMA": ("YELLOW", "Nifty Below 200 DMA"),
    "BELOW_200DMA_10DAYS": ("RED", "Nifty Below 200 DMA (10+ days)"),
}

# Kill switch thresholds
DRAWDOWN_THRESHOLD = 15.0
WIN_RATE_THRESHOLD = 40.0
SHARPE_THRESHOLD = 0.8
MIN_TRADES_KS = 20

# Kill switch color rules
# drawdown:  < 10% -> GREEN, 10-15% -> YELLOW, >= 15% -> RED
# win_rate:  > 50% -> GREEN, 40-50% -> YELLOW, < 40% -> RED (N/A if < 20 trades)
# consec:    not fired -> GREEN, fired -> RED
# sharpe:    > 1.2 -> GREEN, 0.8-1.2 -> YELLOW, < 0.8 -> RED (N/A if < 20 trades)

def _db_connect() -> sqlite3.Connection: ...
def _fetch_agent_activity(conn) -> list[dict]: ...
def _fetch_agent_summary(conn) -> list[dict]: ...
def _run_git_log() -> list[dict]: ...
def _run_pytest_count() -> dict: ...
def _fetch_regime(conn) -> dict: ...
def _fetch_portfolio(conn) -> dict: ...
def _compute_kill_switches(conn) -> dict: ...  # peak equity loop + Sharpe + win rate + consec
def _fetch_positions(conn) -> list[dict]: ...
def _fetch_signals_today(conn) -> list[dict]: ...
def _fetch_screener_top5(conn) -> list[dict]: ...
def _fetch_research_sentiment(conn) -> list[dict]: ...
def _fetch_watchlist(conn) -> list[dict]: ...
def _fetch_risk_approvals_today(conn, today_str: str) -> list[dict]: ...  # try/except OperationalError -> []
def _build_pnl_chart(conn) -> dict: ...
def _fetch_trade_history(conn) -> list[dict]: ...
def _build_response() -> dict: ...  # assembles all of the above; always closes conn in finally

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self): ...
    def log_message(self, format, *args): pass  # suppress request logging
```

---

## HTML Structure (`dashboard/index.html`)

### Page 1 — Build Status widgets
1. Phase progress bar — `(built_count / total_count) * 100%` width; label "X / Y modules complete"
2. Module status grid — `.module-card` per module, status badge (Built=green, In Progress=yellow, Pending=yellow, Not Started=gray)
3. Test count — single card: "526 tests passing" or "unavailable"
4. Agent activity — summary table (agent | count | last seen) + scrollable feed of last 20 logs with level badges
5. Recent commits — ordered list of hash + message

### Page 2 — Paper Trading widgets
1. Regime badge — large colored badge (GREEN/YELLOW/RED)
2. Portfolio equity card — Starting Capital | Realized P&L | Unrealized P&L | Total Equity
3. Kill switch panel — 4 rows (drawdown | win rate | consec losses | Sharpe), each with value + badge
4. Today's signals table — Symbol | RSI | MACD | Signal | Groq Confidence | Reason
5. Open positions table — Symbol | Qty | Entry | SL | TP | P&L
6. P&L equity curve — Chart.js line chart (equity_curve + cumulative_pnl, dark theme)
7. Screener top 5 — Symbol | Rank | Momentum Score | Regime | Size Mult
8. Research sentiment — Symbol | Sentiment (colored) | Confidence | Date
9. Watchlist — Symbol | Decision | Score | Sentiment | Human Approved
10. Trade history — last 20 closed trades
11. Risk approvals today — APPROVED/REJECTED with reasons

---

## Chart.js Config (P&L Equity Curve)

```javascript
new Chart(ctx, {
  type: 'line',
  data: {
    labels: chartData.labels,          // date strings
    datasets: [
      {
        label: 'Equity (INR)',
        data: chartData.equity_curve,
        borderColor: '#3fb950',        // green
        backgroundColor: 'rgba(63, 185, 80, 0.08)',
        fill: true, tension: 0.3, pointRadius: 3, borderWidth: 1.5
      },
      {
        label: 'Cumulative P&L (INR)',
        data: chartData.cumulative_pnl,
        borderColor: '#58a6ff',        // blue dashed
        fill: false, tension: 0.3, pointRadius: 2, borderWidth: 1,
        borderDash: [4, 4]
      }
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      tooltip: {
        callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ₹${ctx.parsed.y.toFixed(2)}` }
      }
    },
    scales: {
      x: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
      y: {
        ticks: { callback: (v) => '₹' + v.toLocaleString('en-IN') },
        grid: { color: '#21262d' }
      }
    }
  },
  plugins: [{
    // Horizontal dashed line at ₹10,000 starting capital
    id: 'startingCapitalLine',
    beforeDraw(chart) { /* draw dashed line at y=10000 */ }
  }]
});

// On refresh: chart.data.labels = ...; chart.data.datasets[0].data = ...; chart.update('none');
```

---

## Edge Cases and Constraints

1. `market_data` table doesn't exist yet — regime badge sources from `screener_results.regime`, not from DMA computation. Surface a tooltip note.
2. `risk_approvals` table is created at runtime — wrap query in `try/except sqlite3.OperationalError`, return `[]`.
3. `trades` table currently empty — all kill switch fields render as N/A; no division-by-zero.
4. `groq_confidence = -1.0` sentinel — display "N/A (LLMs unavailable)" not raw value.
5. `research_reports.raw_response` — never include in JSON response (size + security).
6. `research_reports.source_urls` is JSON-encoded string — server parses with `json.loads()` but dashboard doesn't expose URLs.
7. Chart update uses `chart.update('none')` on refresh to avoid canvas flicker.
8. `subprocess` calls: `timeout=15`, return `{"error": "..."}` on failure, render gray "unavailable" state.
9. `positions` currently has 1 row with zero unrealized P&L (entry == current price) — display `₹0.00 (0.00%)`.

---

## Run Instructions

```bash
cd /home/hitaish/projects/indian-trader
python dashboard/server.py
# Open http://localhost:8765 in browser
```
