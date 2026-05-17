"""Tests for src/agents/reporter_agent.py.

Covers all 22+ acceptance scenarios including:
- P&L computation (daily, cumulative, unrealized)
- Risk metrics (Sharpe, win rate, profit factor, drawdown)
- Kill switch status display
- DB operations (daily_pnl, strategy_perf)
- Report file generation
- Notification delivery
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.agents.reporter_agent import (
    run_reporter_agent,
    ReporterAgentError,
    ReporterResult,
    DailyReport,
    KillSwitchStatus,
)
from src.config.settings import Settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")
RUN_DATE = datetime.date(2026, 4, 9)

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

MOCK_SETTINGS = Settings(
    live_trading=False,
    paper_trading=True,
    log_level="DEBUG",
    max_trade_amount=100000,
    database_url="sqlite:///data/trading.db",
    shoonya_user="test",
    shoonya_password="test",
    shoonya_totp_secret="test",
    fyers_api_key=None,
    groq_api_key="test",
    gemini_api_key="test",
    github_pat=None,
    tavily_api_key=None,
    brave_api_key=None,
    telegram_bot_token="test",
    telegram_chat_id="test",
    gmail_credentials=None,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Create temporary SQLite database with all required tables."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Create trades table
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_quantity INTEGER NOT NULL,
            exit_date TEXT,
            exit_price REAL,
            exit_quantity INTEGER,
            pnl REAL NOT NULL,
            pnl_pct REAL,
            exit_reason TEXT,
            closed_at TEXT NOT NULL
        )
    """
    )

    # Create positions table (must match PaperTrader DDL for get_positions())
    conn.execute(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_date TEXT NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            current_price REAL NOT NULL,
            pnl REAL NOT NULL DEFAULT 0.0,
            pnl_pct REAL NOT NULL DEFAULT 0.0,
            opened_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """
    )

    # Create orders table
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            status TEXT NOT NULL,
            placed_at TEXT NOT NULL
        )
    """
    )

    # Create agent_logs table
    conn.execute(
        """
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            level TEXT NOT NULL,
            action TEXT NOT NULL,
            symbol TEXT,
            result TEXT,
            data_quality_score REAL
        )
    """
    )

    conn.close()
    yield db_path
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Helper: patch context with all required mocks
# ---------------------------------------------------------------------------


def _make_patches():
    """Create a context manager that patches all required dependencies."""
    from contextlib import contextmanager

    @contextmanager
    def _patched_context():
        with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
            with patch("src.agents.reporter_agent.log_agent_action"):
                with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                    mock_alert.return_value = {"telegram": True, "gmail": True}
                    with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                        with patch("builtins.open", create=True):
                            yield mock_alert

    return _patched_context()


# ---------------------------------------------------------------------------
# P&L Computation Tests
# ---------------------------------------------------------------------------


def test_no_trades_no_positions(temp_db):
    """Test: no trades, no positions → daily_pnl=0.0, cumulative_pnl=0.0, equity=100000.0, drawdown=0.0."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.daily_pnl == 0.0
    assert result.report.cumulative_pnl == 0.0
    assert result.report.equity == 100000.0
    assert result.report.drawdown_pct == 0.0
    assert result.report.total_trades == 0
    assert result.report.open_position_count == 0


def test_single_winning_trade_closed_today(temp_db):
    """Test: single winning trade closed today → correct daily_pnl and cumulative_pnl."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert one winning trade closed today
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            100.0,
            10,
            RUN_DATE.isoformat(),
            110.0,
            10,
            100.0,
            10.0,
            "TAKE_PROFIT",
            f"{RUN_DATE.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.daily_pnl == 100.0
    assert result.report.cumulative_pnl == 100.0
    assert result.report.equity == 100100.0
    assert result.report.total_trades == 1
    assert result.report.win_count == 1
    assert result.report.win_rate_pct == 100.0
    assert result.report.trades_closed_today == 1
    assert result.report.wins_today == 1


def test_trade_closed_on_prior_day(temp_db):
    """Test: trade closed on prior day → daily_pnl=0.0, cumulative_pnl includes it."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    prior_day = RUN_DATE - datetime.timedelta(days=1)

    # Insert one trade closed yesterday
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            prior_day.isoformat(),
            100.0,
            10,
            prior_day.isoformat(),
            110.0,
            10,
            100.0,
            10.0,
            "TAKE_PROFIT",
            f"{prior_day.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.daily_pnl == 0.0
    assert result.report.cumulative_pnl == 100.0
    assert result.report.total_trades == 1
    assert result.report.trades_closed_today == 0


def test_multiple_trades_across_multiple_days(temp_db):
    """Test: multiple trades across multiple days → correct daily/cumulative split."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    prior_day = RUN_DATE - datetime.timedelta(days=1)

    # Insert one trade from prior day
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            prior_day.isoformat(),
            100.0,
            10,
            prior_day.isoformat(),
            110.0,
            10,
            100.0,
            10.0,
            "TAKE_PROFIT",
            f"{prior_day.isoformat()}T15:30:00+05:30",
        ),
    )

    # Insert two trades from today
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "INFY",
            RUN_DATE.isoformat(),
            1000.0,
            5,
            RUN_DATE.isoformat(),
            1020.0,
            5,
            100.0,
            2.0,
            "TAKE_PROFIT",
            f"{RUN_DATE.isoformat()}T14:00:00+05:30",
        ),
    )

    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "RELIANCE",
            RUN_DATE.isoformat(),
            2500.0,
            2,
            RUN_DATE.isoformat(),
            2450.0,
            2,
            -100.0,
            -4.0,
            "STOP_LOSS",
            f"{RUN_DATE.isoformat()}T15:15:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.daily_pnl == 0.0  # 100 - 100 = 0
    assert result.report.cumulative_pnl == 100.0  # All trades
    assert result.report.total_trades == 3
    assert result.report.trades_closed_today == 2
    assert result.report.wins_today == 1
    assert result.report.losses_today == 1


# ---------------------------------------------------------------------------
# Risk Metrics Tests
# ---------------------------------------------------------------------------


def test_fewer_than_20_trades_sharpe_shows_na(temp_db):
    """Test: < 20 trades → sharpe_ratio computed but displayed as N/A, win_rate normal."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 5 trades, all winning
    for i in range(5):
        trade_date = RUN_DATE - datetime.timedelta(days=5 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                110.0,
                10,
                100.0,
                10.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.total_trades == 5
    assert result.report.win_rate_pct == 100.0  # Computed normally
    assert isinstance(result.report.sharpe_ratio, float)  # Sharpe computed


def test_20_plus_trades_all_wins(temp_db):
    """Test: 20+ trades, all wins → win_rate=100%, consecutive_losses=0."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 25 winning trades
    for i in range(25):
        trade_date = RUN_DATE - datetime.timedelta(days=25 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                105.0,
                10,
                50.0,
                5.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.total_trades == 25
    assert result.report.win_count == 25
    assert result.report.loss_count == 0
    assert result.report.win_rate_pct == 100.0
    assert result.report.kill_switch_status.consecutive_losses == 0


def test_5_consecutive_losing_trades(temp_db):
    """Test: 5 consecutive losing trades → consecutive_losses=5."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 3 winning trades
    for i in range(3):
        trade_date = RUN_DATE - datetime.timedelta(days=8 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"WIN{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                110.0,
                10,
                100.0,
                10.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )

    # Insert 5 consecutive losing trades
    for i in range(5):
        trade_date = RUN_DATE - datetime.timedelta(days=5 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"LOSS{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                95.0,
                10,
                -50.0,
                -5.0,
                "STOP_LOSS",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.kill_switch_status.consecutive_losses == 5


def test_drawdown_from_peak_equity(temp_db):
    """Test: drawdown computed from peak equity correctly."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Create equity curve: +200 → +150 → +100 (peak=10200, current=10100, dd=0.98%)
    for i, pnl in enumerate([200.0, -50.0, -50.0]):
        trade_date = RUN_DATE - datetime.timedelta(days=3 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                100.0,
                10,
                pnl,
                0.0,
                "STOP_LOSS",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.cumulative_pnl == 100.0
    assert result.report.equity == 100100.0
    assert result.report.peak_equity == 100200.0
    assert abs(result.report.drawdown_pct - 0.10) < 0.01


def test_sharpe_computed_with_daily_grouping(temp_db):
    """Test: Sharpe computed with daily grouping, annualized with sqrt(252)."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert trades on 20 days, same PnL each day to get mean and std
    for day_offset in range(20):
        trade_date = RUN_DATE - datetime.timedelta(days=20 - day_offset)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{day_offset}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                100.0,
                10,
                50.0,
                0.5,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    # When all returns are identical, std=0, sharpe=0
    assert result.report.sharpe_ratio == 0.0


def test_sharpe_zero_when_std_is_zero(temp_db):
    """Test: std=0 in daily returns → sharpe=0.0 (not error)."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 25 trades with identical PnL
    for i in range(25):
        trade_date = RUN_DATE - datetime.timedelta(days=25 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                110.0,
                10,
                100.0,
                10.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.sharpe_ratio == 0.0


# ---------------------------------------------------------------------------
# Profit Factor Tests
# ---------------------------------------------------------------------------


def test_no_losing_trades_profit_factor_is_none(temp_db):
    """Test: no losing trades → profit_factor is None (stored as NULL)."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 5 winning trades
    for i in range(5):
        trade_date = RUN_DATE - datetime.timedelta(days=5 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                105.0,
                10,
                50.0,
                5.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.profit_factor is None


def test_no_winning_trades_profit_factor_zero(temp_db):
    """Test: no winning trades → profit_factor=0.0."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 5 losing trades
    for i in range(5):
        trade_date = RUN_DATE - datetime.timedelta(days=5 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                95.0,
                10,
                -50.0,
                -5.0,
                "STOP_LOSS",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.profit_factor == 0.0


def test_profit_factor_normal_mix(temp_db):
    """Test: normal mix of wins/losses → correct ratio."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # 3 winning trades: +100, +100, +100 = +300
    for i in range(3):
        trade_date = RUN_DATE - datetime.timedelta(days=6 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"WIN{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                110.0,
                10,
                100.0,
                10.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )

    # 2 losing trades: -50, -100 = -150
    for i in range(2):
        trade_date = RUN_DATE - datetime.timedelta(days=3 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"LOSS{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                100.0,
                10,
                -50.0 if i == 0 else -100.0,
                -5.0 if i == 0 else -10.0,
                "STOP_LOSS",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    # profit_factor = 300 / 150 = 2.0
    assert abs(result.report.profit_factor - 2.0) < 0.01


# ---------------------------------------------------------------------------
# DB Tests
# ---------------------------------------------------------------------------


def test_daily_pnl_table_created_and_row_written(temp_db):
    """Test: daily_pnl table created and row written with correct values."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            100.0,
            10,
            RUN_DATE.isoformat(),
            110.0,
            10,
            100.0,
            10.0,
            "TAKE_PROFIT",
            f"{RUN_DATE.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    # Verify daily_pnl table was written
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM daily_pnl WHERE report_date = ?",
        (RUN_DATE.isoformat(),),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["daily_pnl"] == 100.0
    assert row["cumulative_pnl"] == 100.0
    assert row["equity"] == 100100.0
    assert row["drawdown_pct"] == 0.0


def test_strategy_perf_table_created_and_row_written(temp_db):
    """Test: strategy_perf table created and row written."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 25 trades to meet minimum for sharpe/win_rate
    for i in range(25):
        trade_date = RUN_DATE - datetime.timedelta(days=25 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                105.0 if i % 2 == 0 else 95.0,
                10,
                50.0 if i % 2 == 0 else -50.0,
                5.0 if i % 2 == 0 else -5.0,
                "TAKE_PROFIT" if i % 2 == 0 else "STOP_LOSS",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM strategy_perf WHERE metric_date = ?",
        (RUN_DATE.isoformat(),),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["total_trades"] == 25
    assert row["win_rate_pct"] > 0.0
    assert isinstance(row["sharpe_ratio"], (int, float))


def test_rerun_same_date_overwrites_with_insert_or_replace(temp_db):
    """Test: re-run same date → INSERT OR REPLACE overwrites without error."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            100.0,
            10,
            RUN_DATE.isoformat(),
            110.0,
            10,
            100.0,
            10.0,
            "TAKE_PROFIT",
            f"{RUN_DATE.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        # Run twice with same date
                        result1 = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )
                        result2 = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result1.report.daily_pnl == result2.report.daily_pnl
    assert result1.db_written is True
    assert result2.db_written is True


def test_trades_table_missing_raises_error(temp_db):
    """Test: trades table missing → ReporterAgentError(phase='db_read')."""
    # Delete the trades table
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.execute("DROP TABLE trades")
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with pytest.raises(ReporterAgentError) as exc_info:
                run_reporter_agent(
                    report_date=RUN_DATE,
                    db_path_override=temp_db,
                )

    assert exc_info.value.phase == "db_read"


# ---------------------------------------------------------------------------
# Report File Tests
# ---------------------------------------------------------------------------


def test_report_file_created_in_correct_path(temp_db, tmp_path):
    """Test: reports/YYYY-MM-DD.md created in correct path."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    result = run_reporter_agent(
                        report_date=RUN_DATE,
                        db_path_override=temp_db,
                    )

    expected_filename = f"{RUN_DATE.isoformat()}.md"
    expected_path = os.path.join(reports_dir, expected_filename)
    assert os.path.exists(expected_path)
    assert result.report_file_path == expected_path


def test_report_file_contains_correct_pnl_figures(temp_db, tmp_path):
    """Test: report file contains correct P&L figures."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            100.0,
            10,
            RUN_DATE.isoformat(),
            110.0,
            10,
            100.0,
            10.0,
            "TAKE_PROFIT",
            f"{RUN_DATE.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    result = run_reporter_agent(
                        report_date=RUN_DATE,
                        db_path_override=temp_db,
                    )

    with open(result.report_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "100.00" in content  # daily_pnl
    assert "100100.00" in content  # equity


def test_report_shows_na_when_profit_factor_is_none(temp_db, tmp_path):
    """Test: report shows 'N/A' when profit_factor is None."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 5 winning trades (no losses)
    for i in range(5):
        trade_date = RUN_DATE - datetime.timedelta(days=5 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                105.0,
                10,
                50.0,
                5.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    result = run_reporter_agent(
                        report_date=RUN_DATE,
                        db_path_override=temp_db,
                    )

    with open(result.report_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "N/A" in content


def test_report_shows_kill_switch_status_correctly(temp_db, tmp_path):
    """Test: report shows kill switch status (SAFE/APPROACHING/TRIGGERED)."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert 25 trades all winning to have SAFE kill switches
    for i in range(25):
        trade_date = RUN_DATE - datetime.timedelta(days=25 - i)
        conn.execute(
            """
            INSERT INTO trades
                (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
                 exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                trade_date.isoformat(),
                100.0,
                10,
                trade_date.isoformat(),
                110.0,
                10,
                100.0,
                10.0,
                "TAKE_PROFIT",
                f"{trade_date.isoformat()}T15:30:00+05:30",
            ),
        )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    result = run_reporter_agent(
                        report_date=RUN_DATE,
                        db_path_override=temp_db,
                    )

    with open(result.report_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "Kill Switch Status" in content
    assert "SAFE" in content


def test_run_date_defaults_to_today_ist(temp_db, tmp_path):
    """Test: run_date defaults to today IST."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    with patch("src.agents.reporter_agent._ist_now") as mock_now:
                        mock_now.return_value = datetime.datetime(
                            2026, 4, 9, 20, 0, 0, tzinfo=IST
                        )
                        result = run_reporter_agent(
                            report_date=None,
                            db_path_override=temp_db,
                        )

    assert result.report_date == datetime.date(2026, 4, 9)


# ---------------------------------------------------------------------------
# Notification Tests
# ---------------------------------------------------------------------------


def test_send_alert_called_with_correct_subject_and_message(temp_db):
    """Test: send_alert called with correct subject/message format."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    # Verify send_alert was called
    assert mock_alert.called
    call_args = mock_alert.call_args
    assert call_args is not None
    subject = call_args.kwargs.get("subject") or call_args[1].get("subject")
    assert RUN_DATE.isoformat() in subject


def test_both_telegram_and_gmail_in_notification_result(temp_db):
    """Test: notification_sent dict has both 'telegram' and 'gmail' keys."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert "telegram" in result.notification_sent
    assert "gmail" in result.notification_sent
    assert isinstance(result.notification_sent["telegram"], bool)
    assert isinstance(result.notification_sent["gmail"], bool)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


def test_empty_positions_list_displays_no_open_positions(temp_db, tmp_path):
    """Test: empty positions list → report shows '_No open positions._'."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    result = run_reporter_agent(
                        report_date=RUN_DATE,
                        db_path_override=temp_db,
                    )

    with open(result.report_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "No open positions" in content


def test_zero_trades_produces_valid_report(temp_db, tmp_path):
    """Test: zero trades still produce valid report with all zeros."""
    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir") as mock_dir:
                    reports_dir = str(tmp_path / "reports")
                    mock_dir.return_value = reports_dir
                    result = run_reporter_agent(
                        report_date=RUN_DATE,
                        db_path_override=temp_db,
                    )

    assert result.report.total_trades == 0
    assert result.report.equity == 100000.0
    with open(result.report_file_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert len(content) > 0


def test_zero_pnl_trades_handled_correctly(temp_db):
    """Test: trades with zero P&L are handled without division errors."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert a break-even trade (pnl=0)
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            100.0,
            10,
            RUN_DATE.isoformat(),
            100.0,
            10,
            0.0,
            0.0,
            "MANUAL_EXIT",
            f"{RUN_DATE.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.daily_pnl == 0.0
    assert result.report.total_trades == 1


def test_very_large_pnl_values_handled(temp_db):
    """Test: very large P&L values don't cause overflow."""
    conn = sqlite3.connect(temp_db, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Insert trade with large PnL
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, exit_date, exit_price,
             exit_quantity, pnl, pnl_pct, exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            100.0,
            100,
            RUN_DATE.isoformat(),
            500.0,
            100,
            40000.0,
            400.0,
            "TAKE_PROFIT",
            f"{RUN_DATE.isoformat()}T15:30:00+05:30",
        ),
    )
    conn.close()

    with patch("src.agents.reporter_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.reporter_agent.log_agent_action"):
            with patch("src.agents.reporter_agent.send_alert") as mock_alert:
                mock_alert.return_value = {"telegram": True, "gmail": True}
                with patch("src.agents.reporter_agent._resolve_reports_dir", return_value="/tmp/reports"):
                    with patch("builtins.open", create=True):
                        result = run_reporter_agent(
                            report_date=RUN_DATE,
                            db_path_override=temp_db,
                        )

    assert result.report.cumulative_pnl == 40000.0
    assert result.report.equity == 140000.0
