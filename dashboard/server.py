"""
dashboard/server.py — Read-only monitoring dashboard HTTP server.

Serves GET / (index.html) and GET /api/data (JSON from SQLite).
Python stdlib only. Port 8765.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
import zoneinfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "trading.db"
)
PROJECT_ROOT: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INDEX_HTML: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
PORT: int = 8765
STARTING_CAPITAL: float = 10_000.0
IST: zoneinfo.ZoneInfo = zoneinfo.ZoneInfo("Asia/Kolkata")

REGIME_MAP: dict[str, tuple[str, str]] = {
    "ABOVE_200DMA": ("GREEN", "Nifty Above 200 DMA"),
    "BELOW_200DMA": ("YELLOW", "Nifty Below 200 DMA"),
    "BELOW_200DMA_10DAYS": ("RED", "Nifty Below 200 DMA (10+ days)"),
}

DRAWDOWN_THRESHOLD: float = 15.0
WIN_RATE_THRESHOLD: float = 40.0
SHARPE_THRESHOLD: float = 0.8
MIN_TRADES_KS: int = 20

# ---------------------------------------------------------------------------
# Phase / module data — transcribed from docs/context/current-state.md
# ---------------------------------------------------------------------------

PHASES_DATA: list[dict[str, Any]] = [
    {
        "name": "Phase 1 — Foundation",
        "modules": [
            {
                "module": "src/data/validator.py",
                "status": "Built",
                "notes": "Data quality gate; writes to agent_logs",
            },
            {
                "module": "src/config/settings.py",
                "status": "Built",
                "notes": "Environment loading; Settings singleton",
            },
            {
                "module": "src/data/fetcher.py",
                "status": "Built",
                "notes": "OHLCV fetcher; yfinance + jugaad-data with CSV cache",
            },
            {
                "module": "src/data/cleaner.py",
                "status": "Built",
                "notes": "Data repair; forward-fill missing, remove duplicates, flag anomalies",
            },
            {
                "module": "src/data/fundamentals.py",
                "status": "Built",
                "notes": (
                    "Screener.in scraper; 45-day JSON cache, yfinance fallback; "
                    "historical additions: fetch_historical_fundamentals, "
                    "get_fundamentals_for_date, get_nifty_universe_for_year"
                ),
            },
            {
                "module": "src/utils/logger.py",
                "status": "Built",
                "notes": "SQLite logging; StreamHandler + SQLiteHandler",
            },
            {
                "module": "src/utils/notifier.py",
                "status": "Built",
                "notes": "Telegram + Gmail notifications (both channels, always)",
            },
            {
                "module": "src/execution/paper_trader.py",
                "status": "Built",
                "notes": "Simulated CNC orders; orders/positions/trades tables; GTT simulation; WAL mode",
            },
            {
                "module": "main.py",
                "status": "Code Review Passed",
                "notes": "Step 9: End-to-end dry-run pipeline. Spec: docs/specs/2026-03-24-main.md",
            },
        ],
    },
    {
        "name": "Phase 2 — Strategy Core",
        "modules": [
            {
                "module": "src/indicators/technical.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-03-24-technical-indicators.md",
            },
            {
                "module": "src/strategy/quality_filter.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-03-24-quality-filter.md",
            },
            {
                "module": "src/strategy/momentum.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-03-25-momentum.md",
            },
            {
                "module": "src/strategy/regime.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-03-25-regime.md",
            },
            {
                "module": "src/data/fundamentals.py (historical)",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-03-25-historical-fundamentals.md",
            },
            {
                "module": "src/backtest/runner.py",
                "status": "Built",
                "notes": (
                    "Spec: docs/specs/2026-03-25-backtest-runner.md. Integration: "
                    "backtesting.py wrapper with _PortfolioTracker; weekly rebalance; "
                    "400-day warm-up; weekend guard."
                ),
            },
            {
                "module": "src/backtest/validator.py",
                "status": "Built",
                "notes": (
                    "Spec: docs/specs/2026-03-29-backtest-validator.md. Pure gate checker; "
                    "5 gates; gates_passed=True via dataclasses.replace() only; frozen dataclasses."
                ),
            },
        ],
    },
    {
        "name": "Phase 3 — Intelligence Layer",
        "modules": [
            {
                "module": "src/agents/research_agent.py",
                "status": "Code Review Passed",
                "notes": (
                    "Spec: docs/specs/2026-03-30-research-agent.md. "
                    "Tavily SDK replaces Brave Search; two-step INSERT+UPDATE completed_at preserved."
                ),
            },
            {
                "module": "src/agents/signal_agent.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-04-05-signal-agent.md",
            },
            {
                "module": "src/agents/screener_agent.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-04-05-screener-agent.md",
            },
            {
                "module": "src/agents/watchlist_agent.py",
                "status": "Code Review Passed",
                "notes": "Spec: docs/specs/2026-04-05-watchlist-agent.md",
            },
        ],
    },
    {
        "name": "Phase 4 — Full Trading Pipeline",
        "modules": [
            {
                "module": "src/agents/risk_agent.py",
                "status": "Built",
                "notes": "Kill switches, position sizing, risk_approvals table",
            },
            {
                "module": "src/agents/execution_agent.py",
                "status": "Pending",
                "notes": "Reads risk_approvals, human checkpoint, order placement",
            },
            {
                "module": "src/agents/monitor_agent.py",
                "status": "Not Started",
                "notes": "Stop-loss/take-profit loop + GTT reconciliation every 30 min",
            },
            {
                "module": "src/agents/reporter_agent.py",
                "status": "Not Started",
                "notes": "Daily P&L report at 15:45 IST",
            },
            {
                "module": "src/agents/orchestrator.py",
                "status": "Not Started",
                "notes": "Python Agent SDK pipeline scheduling all agents",
            },
        ],
    },
    {
        "name": "Phase 5–6 — Validation & Live Trading",
        "modules": [
            {
                "module": "Phase 5 — Paper Trading Validation",
                "status": "Not Started",
                "notes": "8 weeks of paper trading; go/no-go decision document required",
            },
            {
                "module": "Phase 6 — Live Trading",
                "status": "Not Started",
                "notes": "Real money; Oracle Cloud VM; start with ₹5,000 reserve",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _db_connect() -> sqlite3.Connection:
    """Open a read-only SQLite connection with WAL pragmas."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _fetch_agent_activity(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch last 20 agent log entries from agent_logs."""
    try:
        cur = conn.execute(
            """
            SELECT id, logged_at, agent_name, level, action, symbol, result, data_quality_score
            FROM agent_logs ORDER BY id DESC LIMIT 20
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _fetch_agent_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch per-agent log count and last-seen timestamp."""
    try:
        cur = conn.execute(
            """
            SELECT agent_name, COUNT(*) AS log_count, MAX(logged_at) AS last_seen
            FROM agent_logs GROUP BY agent_name ORDER BY last_seen DESC
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _run_git_log() -> list[dict[str, str]]:
    """Run git log --oneline -10 and return list of {hash, message}."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=15,
            text=True,
        )
        if result.returncode != 0:
            return []
        commits: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            commits.append(
                {
                    "hash": parts[0],
                    "message": parts[1] if len(parts) > 1 else "",
                }
            )
        return commits
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []


def _run_pytest_count() -> dict[str, Any]:
    """Run pytest --collect-only -q and parse the test count."""
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=15,
            text=True,
        )
        stdout_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        raw_output = stdout_lines[-1] if stdout_lines else ""
        # Parse e.g. "526 tests collected in 6.78s" or "no tests ran"
        total = 0
        for word in raw_output.split():
            try:
                total = int(word)
                break
            except ValueError:
                continue
        return {"total": total, "raw_output": raw_output}
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {"total": 0, "raw_output": "unavailable", "error": "subprocess failed"}


def _fetch_regime(conn: sqlite3.Connection) -> dict[str, Any]:
    """Fetch regime status from screener_results (market_data not yet built)."""
    try:
        cur = conn.execute(
            """
            SELECT regime FROM screener_results
            WHERE run_date = (SELECT MAX(run_date) FROM screener_results) LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return {
                "status": None,
                "badge": "GRAY",
                "label": "No screener data yet",
                "note": "screener_results table empty — regime not yet determined",
            }
        status: str = row["regime"]
        badge, label = REGIME_MAP.get(status, ("GRAY", status))
        return {
            "status": status,
            "badge": badge,
            "label": label,
            "note": "market_data table not yet built — regime sourced from screener_results",
        }
    except sqlite3.Error:
        return {
            "status": None,
            "badge": "GRAY",
            "label": "No screener data yet",
            "note": "screener_results table not yet created",
        }


def _fetch_portfolio(conn: sqlite3.Connection) -> dict[str, Any]:
    """Fetch realized P&L, unrealized P&L, open positions count."""
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    open_count = 0

    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS total_pnl FROM trades"
        ).fetchone()
        if row:
            realized_pnl = float(row["total_pnl"])
    except sqlite3.Error:
        pass

    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS unrealized_pnl FROM positions"
        ).fetchone()
        if row:
            unrealized_pnl = float(row["unrealized_pnl"])
    except sqlite3.Error:
        pass

    try:
        row = conn.execute(
            "SELECT COUNT(*) AS open_count FROM positions"
        ).fetchone()
        if row:
            open_count = int(row["open_count"])
    except sqlite3.Error:
        pass

    total_equity = STARTING_CAPITAL + realized_pnl + unrealized_pnl

    return {
        "starting_capital": STARTING_CAPITAL,
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_equity": round(total_equity, 2),
        "open_positions_count": open_count,
    }


def _compute_kill_switches(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute all four kill switch metrics from trades table."""
    trades: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT id, symbol, pnl, closed_at FROM trades ORDER BY id ASC"
        )
        trades = [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        pass

    trade_count = len(trades)

    # --- Drawdown ---
    peak_equity = STARTING_CAPITAL
    current_equity = STARTING_CAPITAL
    max_drawdown_pct = 0.0
    for t in trades:
        current_equity += float(t["pnl"])
        if current_equity > peak_equity:
            peak_equity = current_equity
        if peak_equity > 0:
            dd = (peak_equity - current_equity) / peak_equity * 100.0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

    if max_drawdown_pct < 10.0:
        dd_status = "GREEN"
    elif max_drawdown_pct < 15.0:
        dd_status = "YELLOW"
    else:
        dd_status = "RED"

    drawdown_fired = max_drawdown_pct >= DRAWDOWN_THRESHOLD

    # --- Win rate ---
    wins = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins FROM trades"
        ).fetchone()
        if row:
            wins = int(row["wins"] or 0)
    except sqlite3.Error:
        pass

    win_rate_pct = (wins / trade_count * 100.0) if trade_count > 0 else 0.0
    wr_skipped = trade_count < MIN_TRADES_KS

    if wr_skipped:
        wr_status = "GREEN"
    elif win_rate_pct > 50.0:
        wr_status = "GREEN"
    elif win_rate_pct >= 40.0:
        wr_status = "YELLOW"
    else:
        wr_status = "RED"

    wr_fired = (not wr_skipped) and (win_rate_pct < WIN_RATE_THRESHOLD)

    # --- Consecutive losses ---
    last5_pnl: list[float] = []
    try:
        cur = conn.execute(
            "SELECT pnl FROM trades ORDER BY id DESC LIMIT 5"
        )
        last5_pnl = [float(r["pnl"]) for r in cur.fetchall()]
    except sqlite3.Error:
        pass

    consec_fired = len(last5_pnl) >= 5 and all(p <= 0 for p in last5_pnl)
    consec_status = "RED" if consec_fired else "GREEN"

    # --- Sharpe ---
    daily_pnl_rows: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            """
            SELECT date(closed_at) AS trade_date, SUM(pnl) AS daily_pnl
            FROM trades GROUP BY date(closed_at) ORDER BY trade_date ASC
            """
        )
        daily_pnl_rows = [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        pass

    sharpe_skipped = trade_count < MIN_TRADES_KS
    sharpe_value = 0.0
    if len(daily_pnl_rows) >= 2:
        daily_vals = [float(r["daily_pnl"]) for r in daily_pnl_rows]
        mean_pnl = sum(daily_vals) / len(daily_vals)
        variance = sum((x - mean_pnl) ** 2 for x in daily_vals) / len(daily_vals)
        std_pnl = math.sqrt(variance)
        if std_pnl > 0:
            sharpe_value = (mean_pnl / std_pnl) * math.sqrt(252)

    if sharpe_skipped:
        sharpe_status = "GREEN"
    elif sharpe_value > 1.2:
        sharpe_status = "GREEN"
    elif sharpe_value >= SHARPE_THRESHOLD:
        sharpe_status = "YELLOW"
    else:
        sharpe_status = "RED"

    sharpe_fired = (not sharpe_skipped) and (sharpe_value < SHARPE_THRESHOLD)

    overall_fired = drawdown_fired or wr_fired or consec_fired or sharpe_fired

    return {
        "trade_count": trade_count,
        "min_trades_required": MIN_TRADES_KS,
        "drawdown": {
            "value_pct": round(max_drawdown_pct, 2),
            "threshold_pct": DRAWDOWN_THRESHOLD,
            "status": dd_status,
            "label": "Drawdown",
            "fired": drawdown_fired,
        },
        "win_rate": {
            "value_pct": round(win_rate_pct, 2),
            "threshold_pct": WIN_RATE_THRESHOLD,
            "status": wr_status,
            "label": "Win Rate",
            "skipped": wr_skipped,
            "fired": wr_fired,
        },
        "consecutive_losses": {
            "last_5_pnl": last5_pnl,
            "fired": consec_fired,
            "status": consec_status,
            "label": "Consecutive Losses (last 5)",
        },
        "sharpe": {
            "value": round(sharpe_value, 3),
            "threshold": SHARPE_THRESHOLD,
            "status": sharpe_status,
            "label": "Sharpe Ratio",
            "skipped": sharpe_skipped,
            "fired": sharpe_fired,
        },
        "overall_fired": overall_fired,
    }


def _fetch_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch all open positions."""
    try:
        cur = conn.execute(
            """
            SELECT symbol, quantity, entry_price, current_price, stop_loss, take_profit,
                   pnl, pnl_pct, opened_at, updated_at
            FROM positions ORDER BY opened_at DESC
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _fetch_signals_today(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch signals for the latest run_date."""
    try:
        cur = conn.execute(
            """
            SELECT symbol, rsi, macd_signal, bollinger_position, atr, groq_confidence,
                   signal_type, skip_reason, signalled_at
            FROM signals
            WHERE run_date = (SELECT MAX(run_date) FROM signals)
            ORDER BY id ASC
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _fetch_screener_top5(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch top 5 screener results for the latest run_date."""
    try:
        cur = conn.execute(
            """
            SELECT symbol, rank, momentum_score, quality_passed, regime,
                   position_size_multiplier, run_date
            FROM screener_results
            WHERE run_date = (SELECT MAX(run_date) FROM screener_results)
            ORDER BY rank ASC LIMIT 5
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _fetch_research_sentiment(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch latest completed research sentiment per symbol (no raw_response)."""
    try:
        cur = conn.execute(
            """
            SELECT r.symbol, r.run_date, r.sentiment, r.confidence, r.completed_at
            FROM research_reports r
            INNER JOIN (
                SELECT symbol, MAX(run_date) AS max_date
                FROM research_reports WHERE completed_at IS NOT NULL GROUP BY symbol
            ) latest ON r.symbol = latest.symbol AND r.run_date = latest.max_date
            ORDER BY r.run_date DESC, r.symbol ASC
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _fetch_watchlist(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch watchlist for the latest run_date."""
    try:
        cur = conn.execute(
            """
            SELECT symbol, run_date, combined_decision, scorecard_score, scorecard_max,
                   sentiment, confidence, rank, regime, position_size_multiplier,
                   human_approved, approval_source, added_at
            FROM watchlist
            WHERE run_date = (SELECT MAX(run_date) FROM watchlist)
            ORDER BY rank ASC
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _fetch_risk_approvals_today(
    conn: sqlite3.Connection, today_str: str
) -> list[dict[str, Any]]:
    """Fetch risk approvals for today. Returns [] if table doesn't exist yet."""
    try:
        cur = conn.execute(
            """
            SELECT symbol, run_date, quantity, entry_price_approx, stop_loss, take_profit,
                   position_size_multiplier, risk_amount, approval_status, rejection_reason,
                   approved_at
            FROM risk_approvals WHERE run_date = ? ORDER BY approved_at ASC
            """,
            (today_str,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


def _build_pnl_chart(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build P&L chart data: labels, cumulative_pnl, equity_curve."""
    try:
        cur = conn.execute(
            """
            SELECT date(closed_at) AS trade_date, SUM(pnl) AS daily_pnl
            FROM trades GROUP BY date(closed_at) ORDER BY trade_date ASC
            """
        )
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        rows = []

    labels: list[str] = []
    cumulative_pnl: list[float] = []
    equity_curve: list[float] = []

    running_pnl = 0.0
    for r in rows:
        labels.append(r["trade_date"])
        running_pnl += float(r["daily_pnl"])
        cumulative_pnl.append(round(running_pnl, 2))
        equity_curve.append(round(STARTING_CAPITAL + running_pnl, 2))

    return {
        "labels": labels,
        "cumulative_pnl": cumulative_pnl,
        "equity_curve": equity_curve,
    }


def _fetch_trade_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Fetch last 20 closed trades."""
    try:
        cur = conn.execute(
            """
            SELECT id, symbol, quantity, entry_price, exit_price, pnl, pnl_pct,
                   exit_reason, opened_at, closed_at
            FROM trades ORDER BY id DESC LIMIT 20
            """
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _build_response() -> dict[str, Any]:
    """Assemble the full /api/data JSON response."""
    now_ist = datetime.now(tz=IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    updated_at = now_ist.isoformat()

    conn: sqlite3.Connection | None = None
    try:
        conn = _db_connect()

        build_data: dict[str, Any] = {
            "phases": PHASES_DATA,
            "agent_activity": _fetch_agent_activity(conn),
            "agent_summary": _fetch_agent_summary(conn),
            "recent_commits": _run_git_log(),
            "test_count": _run_pytest_count(),
        }

        trading_data: dict[str, Any] = {
            "regime": _fetch_regime(conn),
            "portfolio": _fetch_portfolio(conn),
            "kill_switches": _compute_kill_switches(conn),
            "positions": _fetch_positions(conn),
            "signals_today": _fetch_signals_today(conn),
            "screener_top5": _fetch_screener_top5(conn),
            "research_sentiment": _fetch_research_sentiment(conn),
            "watchlist": _fetch_watchlist(conn),
            "risk_approvals_today": _fetch_risk_approvals_today(conn, today_str),
            "pnl_chart": _build_pnl_chart(conn),
            "trade_history": _fetch_trade_history(conn),
        }

        return {
            "updated_at": updated_at,
            "build": build_data,
            "trading": trading_data,
        }
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """Minimal HTTP request handler for the monitoring dashboard."""

    def do_GET(self) -> None:
        """Handle GET requests: / serves index.html, /api/data returns JSON."""
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
        elif self.path == "/api/data":
            self._serve_api_data()
        else:
            self._send_not_found()

    def _send_cors_headers(self) -> None:
        """Add CORS headers to every response."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_index(self) -> None:
        """Serve dashboard/index.html."""
        try:
            with open(INDEX_HTML, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
        except OSError as e:
            self.send_response(500)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(f"Error reading index.html: {e}".encode())

    def _serve_api_data(self) -> None:
        """Build and serve the /api/data JSON response."""
        try:
            data = _build_response()
            body = json.dumps(data, default=str).encode("utf-8")
        except Exception as e:
            error_body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(error_body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_not_found(self) -> None:
        """Return 404 for unknown paths."""
        body = b"Not found"
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress request logging."""
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the dashboard HTTP server on PORT."""
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Dashboard running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
