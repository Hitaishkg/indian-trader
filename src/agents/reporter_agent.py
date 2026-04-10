"""Reporter Agent — daily end-of-day P&L reporting for the Indian Trader pipeline.

Runs at 15:45 IST after market close. Reads all trade and position data from
SQLite, computes daily P&L / cumulative P&L / drawdown / Sharpe / win rate /
profit factor, writes results to daily_pnl and strategy_perf tables, generates
a markdown report file at reports/YYYY-MM-DD.md, and sends a summary notification
via both Telegram and email.

Does NOT place or modify orders, trigger kill switches, or fetch external prices.
Uses PaperTrader data only.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.execution.paper_trader import PaperTrader
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

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

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

_DDL_DAILY_PNL: str = """
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
"""

_DDL_STRATEGY_PERF: str = """
CREATE TABLE IF NOT EXISTS strategy_perf (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_date      TEXT    NOT NULL UNIQUE,
    total_trades     INTEGER NOT NULL DEFAULT 0,
    win_rate_pct     REAL    NOT NULL DEFAULT 0.0,
    sharpe_ratio     REAL    NOT NULL DEFAULT 0.0,
    max_drawdown_pct REAL    NOT NULL DEFAULT 0.0,
    profit_factor    REAL,
    updated_at       TEXT    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KillSwitchStatus:
    """Display status of each kill switch for the report.

    Attributes:
        drawdown_status: "SAFE", "APPROACHING", or "TRIGGERED".
        win_rate_status: "SAFE", "APPROACHING", "TRIGGERED", or
                         "N/A -- insufficient trades".
        consecutive_losses: Current consecutive loss count (int, 0-based).
        sharpe_status: "SAFE", "APPROACHING", "TRIGGERED", or
                       "N/A -- insufficient trades".
    """

    drawdown_status: str
    win_rate_status: str
    consecutive_losses: int
    sharpe_status: str


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
        profit_factor: Sum(wins) / abs(Sum(losses)). None if no losses
                       (NULL in DB, N/A in display).
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
    profit_factor: float | None
    trades_closed_today: int
    wins_today: int
    losses_today: int
    open_positions: list[dict[str, object]]
    open_position_count: int
    kill_switch_status: KillSwitchStatus
    computed_at: datetime.datetime


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


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ist_now() -> datetime.datetime:
    return datetime.datetime.now(tz=IST)


def _resolve_db_path(db_path_override: str | None) -> str:
    """Resolve SQLite path from override or settings.database_url."""
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
    """Open a WAL-mode SQLite connection with row_factory."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _setup_tables(db_path: str) -> None:
    """Create daily_pnl and strategy_perf tables if not present."""
    try:
        conn = _open_connection(db_path)
        conn.execute(_DDL_DAILY_PNL)
        conn.execute(_DDL_STRATEGY_PERF)
        conn.close()
    except sqlite3.Error as exc:
        raise ReporterAgentError(
            message=f"Failed to create reporter tables: {exc}",
            phase="db_write",
        ) from exc


# --- Identical formulas copied from risk_agent.py (no import to avoid circular) ---

def _compute_peak_equity(trades: list[dict[str, Any]]) -> float:
    """Compute historical peak equity from chronologically ordered trades."""
    running_sum = 0.0
    peak = STARTING_CAPITAL
    for trade in trades:
        running_sum += float(trade["pnl"])
        equity_at_point = STARTING_CAPITAL + running_sum
        if equity_at_point > peak:
            peak = equity_at_point
    return peak


def _compute_drawdown_pct(portfolio_equity: float, peak_equity: float) -> float:
    """Compute drawdown percentage from peak equity."""
    if peak_equity <= 0.0:
        return 0.0
    return (peak_equity - portfolio_equity) / peak_equity * 100.0


def _compute_sharpe(trades: list[dict[str, Any]]) -> float:
    """Annualized Sharpe ratio using population std dev, daily grouping."""
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


# --- Kill switch status helper ---

def _ks_status(value: float, approaching: float, triggered: float, lower_is_worse: bool) -> str:
    """Return SAFE / APPROACHING / TRIGGERED string for a metric.

    Args:
        value: Current metric value.
        approaching: Threshold where APPROACHING fires.
        triggered: Threshold where TRIGGERED fires.
        lower_is_worse: True when lower values are worse (win_rate, sharpe).
                        False when higher values are worse (drawdown).
    """
    if lower_is_worse:
        if value < triggered:
            return "TRIGGERED"
        if value < approaching:
            return "APPROACHING"
        return "SAFE"
    else:
        # higher is worse (drawdown)
        if value > triggered:
            return "TRIGGERED"
        if value > approaching:
            return "APPROACHING"
        return "SAFE"


def _compute_consecutive_losses(trades: list[dict[str, Any]]) -> int:
    """Count consecutive losing trades from the end of the trades list."""
    count = 0
    for trade in reversed(trades):
        if float(trade["pnl"]) <= 0.0:
            count += 1
        else:
            break
    return count


def _compute_profit_factor(trades: list[dict[str, Any]]) -> float | None:
    """Compute profit factor. Returns None when no losing trades (denominator == 0)."""
    total_wins = sum(float(t["pnl"]) for t in trades if float(t["pnl"]) > 0)
    total_losses = sum(float(t["pnl"]) for t in trades if float(t["pnl"]) < 0)
    if total_losses == 0.0:
        return None
    return total_wins / abs(total_losses)


def _compute_kill_switch_status(
    drawdown_pct: float,
    win_rate_pct: float,
    total_trades: int,
    sharpe_ratio: float,
    consecutive_losses: int,
) -> KillSwitchStatus:
    """Compute display status of all kill switches.

    Win rate and Sharpe show N/A when fewer than KILL_SWITCH_MIN_TRADES trades exist.
    """
    drawdown_status = _ks_status(
        drawdown_pct,
        approaching=DRAWDOWN_APPROACHING_PCT,
        triggered=DRAWDOWN_TRIGGERED_PCT,
        lower_is_worse=False,
    )

    if total_trades < KILL_SWITCH_MIN_TRADES:
        win_rate_status = "N/A -- insufficient trades"
        sharpe_status = "N/A -- insufficient trades"
    else:
        win_rate_status = _ks_status(
            win_rate_pct,
            approaching=WIN_RATE_APPROACHING_PCT,
            triggered=WIN_RATE_TRIGGERED_PCT,
            lower_is_worse=True,
        )
        sharpe_status = _ks_status(
            sharpe_ratio,
            approaching=SHARPE_APPROACHING,
            triggered=SHARPE_TRIGGERED,
            lower_is_worse=True,
        )

    return KillSwitchStatus(
        drawdown_status=drawdown_status,
        win_rate_status=win_rate_status,
        consecutive_losses=consecutive_losses,
        sharpe_status=sharpe_status,
    )


def _resolve_reports_dir() -> str:
    """Return absolute path to the reports/ directory."""
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(project_root, REPORTS_DIR)


def _build_open_positions_table(open_positions: list[dict[str, object]]) -> str:
    """Render the open positions markdown table section."""
    if not open_positions:
        return "_No open positions._\n"
    header = "| Symbol | Entry | Current | SL | TP | P&L |\n"
    separator = "|--------|-------|---------|----|----|-----|\n"
    rows = []
    for pos in open_positions:
        symbol = pos.get("symbol", "")
        entry = pos.get("entry_price", 0.0)
        current = pos.get("current_price", 0.0)
        sl = pos.get("stop_loss", 0.0)
        tp = pos.get("take_profit", 0.0)
        pnl = pos.get("pnl", 0.0)
        rows.append(
            f"| {symbol} | {entry:.2f} | {current:.2f} | {sl:.2f} | {tp:.2f} | {pnl:+.2f} |"
        )
    return header + separator + "\n".join(rows) + "\n"


def _build_markdown_report(report: DailyReport) -> str:
    """Render the full daily markdown report string."""
    ks = report.kill_switch_status
    date_str = report.report_date.isoformat()

    # Sharpe display
    if report.total_trades < KILL_SWITCH_MIN_TRADES:
        sharpe_display = f"{report.sharpe_ratio:.2f} (annualized) [N/A — fewer than 20 trades]"
    else:
        sharpe_display = f"{report.sharpe_ratio:.2f} (annualized)"

    # Win rate display
    win_rate_display = (
        f"{report.win_rate_pct:.0f}% ({report.win_count} of {report.total_trades} trades)"
    )

    # Profit factor display
    if report.profit_factor is None:
        pf_display = "N/A — no losing trades"
    else:
        pf_display = f"{report.profit_factor:.2f}"

    lines = [
        f"# Daily Report — {date_str}",
        "",
        "## P&L Summary",
        f"- Today's P&L: ₹{report.daily_pnl:+.2f}",
        f"- Cumulative P&L: ₹{report.cumulative_pnl:+.2f}",
        f"- Portfolio Equity: ₹{report.equity:.2f}",
        "",
        "## Risk Metrics",
        f"- Drawdown from peak: {report.drawdown_pct:.1f}%",
        f"- Win rate: {win_rate_display}",
        f"- Sharpe ratio: {sharpe_display}",
        f"- Profit factor: {pf_display}",
        "",
        "## Kill Switch Status",
        f"- Drawdown: {ks.drawdown_status}",
        f"- Win rate: {ks.win_rate_status}",
        f"- Consecutive losses: {ks.consecutive_losses} of {CONSECUTIVE_LOSSES_LIMIT}",
        f"- Sharpe: {ks.sharpe_status}",
        "",
        "## Today's Activity",
        f"- Trades closed: {report.trades_closed_today}",
        f"- Wins: {report.wins_today} | Losses: {report.losses_today}",
        f"- Open positions: {report.open_position_count}",
        "",
        "## Open Positions",
        _build_open_positions_table(report.open_positions),
    ]
    return "\n".join(lines)


def _build_notification_message(report: DailyReport) -> str:
    """Build the summary notification string."""
    ks = report.kill_switch_status
    statuses = [ks.drawdown_status, ks.win_rate_status, ks.sharpe_status]
    if "TRIGGERED" in statuses:
        ks_line = "Kill switch status: KILL SWITCH TRIGGERED"
    elif "APPROACHING" in statuses:
        approaching = [s for s in ["drawdown", "win rate", "sharpe"]
                       if [ks.drawdown_status, ks.win_rate_status, ks.sharpe_status][
                           ["drawdown", "win rate", "sharpe"].index(s)
                       ] == "APPROACHING"]
        ks_line = f"Kill switch status: WARNING — {', '.join(approaching)} approaching"
    else:
        ks_line = "Kill switch status: SAFE"

    return (
        f"Daily Report {report.report_date.isoformat()}\n"
        f"P&L today: ₹{report.daily_pnl:+.2f} | Running total: ₹{report.cumulative_pnl:+.2f}\n"
        f"Equity: ₹{report.equity:.2f} | Drawdown: {report.drawdown_pct:.1f}%\n"
        f"Win rate: {report.win_rate_pct:.0f}% (after {report.total_trades} trades) | "
        f"Sharpe: {report.sharpe_ratio:.2f}\n"
        f"Open positions: {report.open_position_count}\n"
        f"{ks_line}"
    )


def _log_kill_switch_warnings(report: DailyReport) -> None:
    """Log approaching / triggered kill switch states."""
    ks = report.kill_switch_status
    checks = [
        ("drawdown", ks.drawdown_status, f"{report.drawdown_pct:.1f}%"),
        ("win_rate", ks.win_rate_status, f"{report.win_rate_pct:.0f}%"),
        ("sharpe", ks.sharpe_status, f"{report.sharpe_ratio:.2f}"),
    ]
    for name, status, value in checks:
        if status == "APPROACHING":
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"kill_switch_approaching: {name} at {value}",
                level="WARNING",
                result="warning",
            )
        elif status == "TRIGGERED":
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"kill_switch_triggered_display: {name}",
                level="WARNING",
                result="warning",
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


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
    if report_date is None:
        report_date = _ist_now().date()

    db_path = _resolve_db_path(db_path_override)

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"reporter_agent_run_started: {report_date}",
        level="INFO",
        result="ok",
    )

    _setup_tables(db_path)

    # ------------------------------------------------------------------
    # DB READ PHASE
    # ------------------------------------------------------------------
    all_trades: list[dict[str, Any]] = []
    today_trades: list[dict[str, Any]] = []

    try:
        conn = _open_connection(db_path)

        trade_rows = conn.execute(
            "SELECT pnl, closed_at FROM trades ORDER BY closed_at ASC"
        ).fetchall()
        all_trades = [dict(row) for row in trade_rows]

        today_rows = conn.execute(
            "SELECT pnl FROM trades WHERE DATE(closed_at) = ?",
            (report_date.isoformat(),),
        ).fetchall()
        today_trades = [dict(row) for row in today_rows]

        conn.close()
    except sqlite3.Error as exc:
        raise ReporterAgentError(
            message=f"Failed to read trades from DB: {exc}",
            phase="db_read",
        ) from exc

    try:
        pt = PaperTrader(db_path)
        pnl_data = pt.get_pnl()
        open_positions = pt.get_positions()
    except ValueError as exc:
        raise ReporterAgentError(
            message=f"PaperTrader initialisation failed: {exc}",
            phase="db_read",
        ) from exc
    except sqlite3.Error as exc:
        raise ReporterAgentError(
            message=f"PaperTrader DB error: {exc}",
            phase="db_read",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"db_read_complete: {len(all_trades)} trades, {len(open_positions)} open",
        level="INFO",
        result="ok",
    )

    # ------------------------------------------------------------------
    # COMPUTE PHASE
    # ------------------------------------------------------------------
    daily_pnl_val = sum(float(t["pnl"]) for t in today_trades)
    cumulative_pnl = float(pnl_data["realized_pnl"])
    unrealized_pnl = float(pnl_data["unrealized_pnl"])
    equity = STARTING_CAPITAL + cumulative_pnl + unrealized_pnl

    peak_equity = _compute_peak_equity(all_trades)
    drawdown_pct = _compute_drawdown_pct(equity, peak_equity)

    total_trades = int(pnl_data["trade_count"])
    win_count = int(pnl_data["win_count"])
    loss_count = int(pnl_data["loss_count"])
    win_rate_pct = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0

    sharpe_ratio = _compute_sharpe(all_trades)
    profit_factor = _compute_profit_factor(all_trades)

    trades_closed_today = len(today_trades)
    wins_today = sum(1 for t in today_trades if float(t["pnl"]) > 0)
    losses_today = sum(1 for t in today_trades if float(t["pnl"]) <= 0)

    consecutive_losses = _compute_consecutive_losses(all_trades)

    ks_status = _compute_kill_switch_status(
        drawdown_pct=drawdown_pct,
        win_rate_pct=win_rate_pct,
        total_trades=total_trades,
        sharpe_ratio=sharpe_ratio,
        consecutive_losses=consecutive_losses,
    )

    now = _ist_now()

    report = DailyReport(
        report_date=report_date,
        daily_pnl=daily_pnl_val,
        cumulative_pnl=cumulative_pnl,
        unrealized_pnl=unrealized_pnl,
        equity=equity,
        peak_equity=peak_equity,
        drawdown_pct=drawdown_pct,
        total_trades=total_trades,
        win_count=win_count,
        loss_count=loss_count,
        win_rate_pct=win_rate_pct,
        sharpe_ratio=sharpe_ratio,
        profit_factor=profit_factor,
        trades_closed_today=trades_closed_today,
        wins_today=wins_today,
        losses_today=losses_today,
        open_positions=open_positions,
        open_position_count=len(open_positions),
        kill_switch_status=ks_status,
        computed_at=now,
    )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"metrics_computed: equity={equity:.2f}, dd={drawdown_pct:.1f}%, sharpe={sharpe_ratio:.2f}",
        level="INFO",
        result="ok",
    )

    _log_kill_switch_warnings(report)

    # ------------------------------------------------------------------
    # DB WRITE PHASE
    # ------------------------------------------------------------------
    db_written = False
    try:
        conn = _open_connection(db_path)
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_pnl
                (report_date, daily_pnl, cumulative_pnl, equity, drawdown_pct,
                 peak_equity, trades_closed_today, win_count_today, open_positions,
                 recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_date.isoformat(),
                daily_pnl_val,
                cumulative_pnl,
                equity,
                drawdown_pct,
                peak_equity,
                trades_closed_today,
                wins_today,
                len(open_positions),
                now.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO strategy_perf
                (metric_date, total_trades, win_rate_pct, sharpe_ratio,
                 max_drawdown_pct, profit_factor, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_date.isoformat(),
                total_trades,
                win_rate_pct,
                sharpe_ratio,
                drawdown_pct,
                profit_factor,  # None → NULL in SQLite
                now.isoformat(),
            ),
        )
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.close()
        db_written = True
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise ReporterAgentError(
            message=f"Failed to write reporter tables: {exc}",
            phase="db_write",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action="db_write_complete: daily_pnl + strategy_perf",
        level="INFO",
        result="ok",
    )

    # ------------------------------------------------------------------
    # REPORT WRITE PHASE
    # ------------------------------------------------------------------
    reports_dir = _resolve_reports_dir()
    report_filename = f"{report_date.isoformat()}.md"
    report_file_path = os.path.join(reports_dir, report_filename)

    try:
        os.makedirs(reports_dir, exist_ok=True)
        markdown_content = _build_markdown_report(report)
        with open(report_file_path, "w", encoding="utf-8") as fh:
            fh.write(markdown_content)
    except OSError as exc:
        raise ReporterAgentError(
            message=f"Failed to write report file {report_file_path}: {exc}",
            phase="report_write",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"report_written: {report_file_path}",
        level="INFO",
        result="ok",
    )

    # ------------------------------------------------------------------
    # NOTIFICATION PHASE
    # ------------------------------------------------------------------
    subject = f"Daily Report {report_date.isoformat()}"
    message = _build_notification_message(report)

    notification_result = send_alert(subject=subject, message=message)

    telegram_ok = notification_result.get("telegram", False)
    gmail_ok = notification_result.get("gmail", False)

    if not telegram_ok or not gmail_ok:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"notification_partial_failure: telegram={telegram_ok}, gmail={gmail_ok}",
            level="WARNING",
            result="error",
        )

    if not telegram_ok and not gmail_ok:
        raise ReporterAgentError(
            message="Both notification channels (Telegram and Gmail) failed to deliver the daily report.",
            phase="notification",
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action="notification_sent",
        level="INFO",
        result="ok",
    )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"reporter_agent_complete: {report_date}",
        level="INFO",
        result="ok",
    )

    return ReporterResult(
        report_date=report_date,
        report=report,
        report_file_path=report_file_path,
        db_written=db_written,
        notification_sent={"telegram": telegram_ok, "gmail": gmail_ok},
        completed_at=_ist_now(),
    )
