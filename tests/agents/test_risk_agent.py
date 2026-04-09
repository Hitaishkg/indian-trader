"""Tests for src/agents/risk_agent.py.

Covers all 20+ acceptance scenarios including:
- Kill switch checks (drawdown, consecutive losses, win rate, Sharpe)
- Position sizing (formula, multiplier, caps, max positions)
- DB operations and error handling
"""

from __future__ import annotations

import datetime
import math
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.agents.risk_agent import (
    run_risk_agent,
    RiskAgentError,
    RiskAgentResult,
    RiskApproval,
)
from src.config.settings import Settings
from src.utils.logger import setup_logging

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
    max_trade_amount=10000,
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

    # Create watchlist table
    conn.execute(
        """
        CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            combined_decision TEXT NOT NULL,
            scorecard_score INTEGER NOT NULL,
            scorecard_max INTEGER NOT NULL,
            sentiment TEXT NOT NULL,
            confidence REAL NOT NULL,
            rank INTEGER NOT NULL,
            regime TEXT NOT NULL,
            position_size_multiplier REAL NOT NULL,
            human_approved INTEGER NOT NULL DEFAULT 0,
            approval_source TEXT,
            added_at TEXT NOT NULL,
            UNIQUE(symbol, run_date)
        )
    """
    )

    # Create signals table
    conn.execute(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            rsi REAL NOT NULL,
            macd_signal TEXT NOT NULL,
            bollinger_position TEXT NOT NULL,
            atr REAL NOT NULL,
            groq_confidence REAL NOT NULL,
            signal_type TEXT NOT NULL,
            skip_reason TEXT,
            signalled_at TEXT NOT NULL
        )
    """
    )

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

    # Create positions table
    conn.execute(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_date TEXT NOT NULL,
            entry_order_id INTEGER NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            UNIQUE(symbol)
        )
    """
    )

    # Create orders table (for PaperTrader)
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            status TEXT NOT NULL,
            placed_at TEXT NOT NULL
        )
    """
    )

    # Create risk_approvals table (will be created by risk_agent, but create it here for consistency)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            entry_price_approx REAL NOT NULL DEFAULT 0.0,
            stop_loss REAL NOT NULL DEFAULT 0.0,
            take_profit REAL NOT NULL DEFAULT 0.0,
            position_size_multiplier REAL NOT NULL DEFAULT 1.0,
            risk_amount REAL NOT NULL DEFAULT 0.0,
            approval_status TEXT NOT NULL CHECK (approval_status IN ('APPROVED', 'REJECTED')),
            rejection_reason TEXT,
            approved_at TEXT NOT NULL,
            UNIQUE(symbol, run_date)
        )
    """
    )

    # Create agent_logs table for logging
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

    conn.commit()
    conn.close()

    setup_logging(db_path)

    yield db_path

    # Cleanup
    Path(db_path).unlink(missing_ok=True)
    Path(f"{db_path}-wal").unlink(missing_ok=True)
    Path(f"{db_path}-shm").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Helper functions to seed data
# ---------------------------------------------------------------------------


def insert_watchlist_row(
    db_path: str,
    symbol: str,
    human_approved: int = 1,
    combined_decision: str = "PROCEED",
    position_size_multiplier: float = 1.0,
    rank: int = 1,
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert a row into watchlist."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    now = datetime.datetime.now(tz=IST).isoformat()
    conn.execute(
        """
        INSERT INTO watchlist
            (symbol, run_date, combined_decision, scorecard_score,
             scorecard_max, sentiment, confidence, rank, regime,
             position_size_multiplier, human_approved, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            combined_decision,
            28,
            40,
            "Positive",
            0.9,
            rank,
            "ABOVE_200DMA",
            position_size_multiplier,
            human_approved,
            now,
        ),
    )
    conn.commit()
    conn.close()


def insert_signal_row(
    db_path: str,
    symbol: str,
    atr: float = 10.0,
    signal_type: str = "BUY",
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert a row into signals."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    now = datetime.datetime.now(tz=IST).isoformat()
    conn.execute(
        """
        INSERT INTO signals
            (symbol, run_date, rsi, macd_signal, bollinger_position, atr,
             groq_confidence, signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, run_date.isoformat(), 35.0, "BUY", "MIDDLE", atr, 0.85, signal_type, now),
    )
    conn.commit()
    conn.close()


def insert_trade_row(
    db_path: str,
    pnl: float,
    closed_at: datetime.datetime | None = None,
) -> None:
    """Insert a row into trades."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    if closed_at is None:
        closed_at = datetime.datetime.now(tz=IST)

    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity,
             pnl, closed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "TEST",
            RUN_DATE.isoformat(),
            100.0,
            1,
            pnl,
            closed_at.isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def read_risk_approval(db_path: str, symbol: str, run_date: datetime.date) -> dict | None:
    """Read a risk_approvals row."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    row = conn.execute(
        "SELECT * FROM risk_approvals WHERE symbol = ? AND run_date = ?",
        (symbol, run_date.isoformat()),
    ).fetchone()
    conn.close()

    if row is None:
        return None
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Tests: Kill Switch Scenarios
# ---------------------------------------------------------------------------


def test_01_drawdown_15pct_fires(temp_db):
    """Kill switch: drawdown > 15% fires with reason 'drawdown_15pct'."""
    db_path = temp_db

    # Insert 1 approved watchlist row
    insert_watchlist_row(db_path, "HDFC", human_approved=1, combined_decision="PROCEED")
    insert_signal_row(db_path, "HDFC", atr=10.0)

    # Insert trades: drawdown > 15% (need > 1500 loss from 10000)
    # Insert 3 winning trades first to establish a peak
    for i in range(3):
        insert_trade_row(db_path, 200.0)  # Peak becomes 10600
    # Now insert large losses: need (10600 - 10600*0.85) / 10600 > 0.15
    for i in range(5):
        insert_trade_row(db_path, -500.0)  # Total loss = 2500, equity = 8300, drawdown = 21.7%

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": -2300.0}  # 600 - 2500
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert result.kill_switch_fired is True
    assert result.kill_switch_reason == "drawdown_15pct"
    mock_alert.assert_called_once()


def test_02_consecutive_losses_5_fires(temp_db):
    """Kill switch: 5 consecutive losses fire with reason 'consecutive_losses_5'."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC", human_approved=1, combined_decision="PROCEED")
    insert_signal_row(db_path, "HDFC")

    # Insert 5 trades with pnl <= 0
    for i in range(5):
        insert_trade_row(db_path, -100.0)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt:
        # Mock price fetch
        df_mock = MagicMock()
        df_mock.empty = False
        df_mock.__getitem__ = lambda self, key: MagicMock(empty=True) if key == "symbol" else df_mock
        mock_fetch.return_value = df_mock

        # Setup paper trader
        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": -500.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert result.kill_switch_fired is True
    assert result.kill_switch_reason == "consecutive_losses_5"


def test_03_win_rate_below_40pct_fires(temp_db):
    """Kill switch: win_rate < 40% after 20+ trades fires with reason 'win_rate_below_40pct'."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    # Insert 20 trades with < 40% win rate, avoiding consecutive 5 losses
    # 7 wins, 13 losses = 35% win rate (< 40%)
    # Carefully interleave to avoid 5 consecutive losses
    base_date = datetime.datetime.now(tz=IST)
    # Pattern: W L L L L W W L L L L W L L L W L W L L
    # Trades: 7 wins, 13 losses, max 4 consecutive losses
    trades_data = [
        (100, 19),     # W
        (-50, 18),     # L: 1
        (-50, 17),     # L: 2
        (-50, 16),     # L: 3
        (-50, 15),     # L: 4
        (100, 14),     # W (breaks)
        (100, 13),     # W
        (-50, 12),     # L: 1
        (-50, 11),     # L: 2
        (-50, 10),     # L: 3
        (-50, 9),      # L: 4
        (100, 8),      # W (breaks)
        (-50, 7),      # L: 1
        (-50, 6),      # L: 2
        (-50, 5),      # L: 3
        (100, 4),      # W (breaks)
        (-50, 3),      # L: 1
        (100, 2),      # W (breaks)
        (-50, 1),      # L: 1
        (-50, 0),      # L: 2
    ]
    for pnl, day_offset in trades_data:
        closed_at = base_date - datetime.timedelta(days=day_offset)
        insert_trade_row(db_path, float(pnl), closed_at=closed_at)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        # 7 * 100 - 13 * 50 = 700 - 650 = 50
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 50.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert result.kill_switch_fired is True
    assert result.kill_switch_reason == "win_rate_below_40pct"


def test_04_sharpe_below_0_8_fires(temp_db):
    """Kill switch: Sharpe < 0.8 after 20+ trades fires with reason 'sharpe_below_0.8'."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    # Insert trades distributed across days to avoid consecutive 5 losses
    # and keep drawdown < 15%
    base_date = datetime.datetime.now(tz=IST)
    # Interleave wins and losses: 10 wins, 10 losses = 50% win rate (> 40%)
    for i in range(20):
        closed_at = base_date - datetime.timedelta(days=20 - i)
        if i % 2 == 0:
            insert_trade_row(db_path, 25.0, closed_at=closed_at)
        else:
            insert_trade_row(db_path, -25.0, closed_at=closed_at)
    # Now Sharpe should be very low (mean close to 0)
    # mean = 0, std > 0, Sharpe = 0 < 0.8

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert result.kill_switch_fired is True
    assert result.kill_switch_reason == "sharpe_below_0.8"


def test_05_kill_switch_priority_drawdown_wins(temp_db):
    """Kill switch priority: drawdown fires first, even if others also fire."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    # Trigger both drawdown AND consecutive losses
    for i in range(19):
        insert_trade_row(db_path, -100.0)
    insert_trade_row(db_path, -500.0)  # 20 total, 20 losses, drawdown > 15%

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt:
        df_mock = MagicMock()
        df_mock.empty = False
        mock_fetch.return_value = df_mock

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": -2500.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    # Drawdown should fire first in priority
    assert result.kill_switch_reason == "drawdown_15pct"


def test_06_no_kill_switch_when_healthy(temp_db):
    """No kill switch fires when all metrics are healthy."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    # Insert 20 trades with strong profitability
    # 18 wins, 2 losses = 90% win rate (>> 40%)
    # Spread across 20 days for good Sharpe ratio
    base_date = datetime.datetime.now(tz=IST)
    trades_data = [
        (200, 19), (200, 18), (200, 17), (200, 16), (200, 15), (200, 14), (200, 13),
        (200, 12), (200, 11), (200, 10), (200, 9), (200, 8), (200, 7), (200, 6),
        (200, 5), (200, 4), (200, 3), (200, 2),  # 18 wins
        (-100, 1), (-100, 0),  # 2 losses
    ]
    for pnl, day_offset in trades_data:
        closed_at = base_date - datetime.timedelta(days=day_offset)
        insert_trade_row(db_path, float(pnl), closed_at=closed_at)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        # 18 * 200 - 2 * 100 = 3600 - 200 = 3400
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 3400.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert result.kill_switch_fired is False
    assert result.kill_switch_reason is None


def test_07_win_rate_check_skipped_below_20_trades(temp_db):
    """Win rate check skipped when < 20 trades, no kill switch fires."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    # Insert only 10 trades with bad win rate but avoid consecutive 5 losses
    # Pattern: W, L, W, L, W, L, W, L, W, L (alternating, max 1 consecutive)
    trades = [100, -50, 100, -50, 100, -50, 100, -50, 100, -50]
    for pnl in trades:
        insert_trade_row(db_path, float(pnl))

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        # 5 * 100 - 5 * 50 = 500 - 250 = 250
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 250.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    # Win rate check should be skipped (< 20 trades), no kill switches should fire
    assert result.kill_switch_fired is False


def test_08_sharpe_std_zero_fires_kill_switch(temp_db):
    """Sharpe with std == 0 returns 0.0, fires kill switch if 20+ trades."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    # Insert 20 trades all on same day with same P&L (std = 0)
    same_day = datetime.datetime.now(tz=IST)
    for i in range(20):
        insert_trade_row(db_path, 10.0, closed_at=same_day)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt:
        df_mock = MagicMock()
        df_mock.empty = False
        mock_fetch.return_value = df_mock

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 200.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    # Sharpe = 0.0 < 0.8, should fire
    assert result.kill_switch_fired is True
    assert result.kill_switch_reason == "sharpe_below_0.8"


# ---------------------------------------------------------------------------
# Tests: Position Sizing
# ---------------------------------------------------------------------------


def test_09_standard_position_sizing(temp_db):
    """Position sizing: equity * 0.01 / (atr * 2.0), floored."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC", position_size_multiplier=1.0)
    insert_signal_row(db_path, "HDFC", atr=10.0)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert len(result.approved) == 1
    approval = result.approved[0]
    # risk = 10000 * 0.01 = 100
    # stop_distance = 10 * 2 = 20
    # quantity = floor(100 / 20) = 5
    assert approval.quantity == 5
    assert approval.entry_price_approx == 500.0
    assert approval.stop_loss == 480.0  # 500 - 20
    assert approval.take_profit == 540.0  # 500 + 20*2


def test_10_regime_multiplier_0_5_applied(temp_db):
    """Regime multiplier 0.5 reduces position size by half."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC", position_size_multiplier=0.5)
    insert_signal_row(db_path, "HDFC", atr=10.0)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    approval = result.approved[0]
    # risk = 10000 * 0.01 * 0.5 = 50
    # stop_distance = 10 * 2 = 20
    # quantity = floor(50 / 20) = 2
    assert approval.quantity == 2
    assert approval.position_size_multiplier == 0.5


def test_11_quantity_zero_rejected(temp_db):
    """Position sizing: quantity < 1 → REJECTED with reason 'insufficient_capital'."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    # Very high ATR relative to capital
    insert_signal_row(db_path, "HDFC", atr=1000.0)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert len(result.rejected) == 1
    assert result.rejected[0].rejection_reason == "insufficient_capital"


def test_12_position_exceeds_40pct_cap(temp_db):
    """Position size capped at 40% of equity."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    # Low ATR to produce large quantity
    insert_signal_row(db_path, "HDFC", atr=1.0)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    approval = result.approved[0]
    # Position value should not exceed 40% of 10000 = 4000
    assert approval.entry_price_approx * approval.quantity <= 4000.0


def test_13_max_2_open_positions(temp_db):
    """Third symbol rejected with reason 'max_positions_reached'."""
    db_path = temp_db

    # Insert 3 approved watchlist rows
    for i, symbol in enumerate(["HDFC", "TCS", "INFY"], start=1):
        insert_watchlist_row(db_path, symbol, rank=i)
        insert_signal_row(db_path, symbol)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    # Only 2 should be approved
    assert len(result.approved) == 2
    # INFY (3rd) should be rejected
    assert len(result.rejected) == 1
    assert result.rejected[0].symbol == "INFY"
    assert result.rejected[0].rejection_reason == "max_positions_reached"


def test_14_price_fetch_failed_returns_zero(temp_db):
    """Price fetch failed → entry_price_approx=0.0, stop_loss=0.0, take_profit=0.0."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=0.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    approval = result.approved[0]
    assert approval.entry_price_approx == 0.0
    assert approval.stop_loss == 0.0
    assert approval.take_profit == 0.0


# ---------------------------------------------------------------------------
# Tests: Database and Flow
# ---------------------------------------------------------------------------


def test_15_empty_watchlist(temp_db):
    """Empty watchlist → RiskAgentResult with empty approved/rejected lists."""
    db_path = temp_db

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt:
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert len(result.approved) == 0
    assert len(result.rejected) == 0
    assert result.kill_switch_fired is False


def test_16_all_symbols_rejected_by_kill_switch(temp_db):
    """All symbols rejected when kill switch fires."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_watchlist_row(db_path, "TCS", rank=2)
    insert_signal_row(db_path, "HDFC")
    insert_signal_row(db_path, "TCS")

    # Trigger drawdown kill switch
    for i in range(20):
        insert_trade_row(db_path, -200.0)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt:
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": -4000.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    assert result.kill_switch_fired is True
    assert len(result.approved) == 0
    assert len(result.rejected) == 2
    for rejection in result.rejected:
        assert rejection.rejection_reason == "kill_switch_fired"


def test_17_db_path_override_used(temp_db):
    """db_path_override parameter used in all operations."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    # Verify data was written to the override path
    approval = read_risk_approval(db_path, "HDFC", RUN_DATE)
    assert approval is not None
    assert approval["approval_status"] == "APPROVED"


def test_18_risk_approvals_table_created(temp_db):
    """risk_approvals table created if not exists."""
    db_path = temp_db

    # Delete the risk_approvals table to test creation
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    conn.execute("DROP TABLE IF EXISTS risk_approvals")
    conn.commit()
    conn.close()

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

    # Verify table was created and data written
    approval = read_risk_approval(db_path, "HDFC", RUN_DATE)
    assert approval is not None


def test_19_run_date_defaults_to_today_ist(temp_db):
    """run_date defaults to today in IST when None passed."""
    db_path = temp_db

    today_ist = datetime.datetime.now(tz=IST).date()
    insert_watchlist_row(db_path, "HDFC", run_date=today_ist)
    insert_signal_row(db_path, "HDFC", run_date=today_ist)

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0):
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        # Pass None for run_date
        result = run_risk_agent(run_date=None, db_path_override=db_path)

    # Result should have today's date
    assert result.run_date == today_ist


# ---------------------------------------------------------------------------
# Tests: Error Handling
# ---------------------------------------------------------------------------


def test_20_missing_watchlist_table_raises_error(temp_db):
    """Missing watchlist table → RiskAgentError(phase='db_read')."""
    db_path = temp_db

    # Delete watchlist table
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    conn.execute("DROP TABLE IF EXISTS watchlist")
    conn.commit()
    conn.close()

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt:
        mock_fetch.return_value = MagicMock(empty=False)
        mock_pt.return_value = MagicMock()

        with pytest.raises(RiskAgentError) as exc_info:
            run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

        assert exc_info.value.phase == "db_read"


def test_21_db_write_failure_raises_error(temp_db):
    """DB write failure → RiskAgentError(phase='db_write')."""
    db_path = temp_db

    insert_watchlist_row(db_path, "HDFC")
    insert_signal_row(db_path, "HDFC")

    with patch("src.config.settings.settings", MOCK_SETTINGS), \
         patch("src.agents.risk_agent.fetch_ohlcv") as mock_fetch, \
         patch("src.agents.risk_agent.send_alert") as mock_alert, \
         patch("src.agents.risk_agent.send_info") as mock_info, \
         patch("src.agents.risk_agent.PaperTrader") as mock_pt, \
         patch("src.agents.risk_agent._fetch_entry_price", return_value=500.0), \
         patch("src.agents.risk_agent._write_approvals") as mock_write:
        mock_fetch.return_value = MagicMock(empty=False)

        mock_pt_instance = MagicMock()
        mock_pt_instance.get_pnl.return_value = {"total_pnl": 0.0}
        mock_pt_instance.get_positions.return_value = []
        mock_pt.return_value = mock_pt_instance

        # Simulate write failure
        mock_write.side_effect = RiskAgentError("Simulated write error", "db_write")

        with pytest.raises(RiskAgentError) as exc_info:
            run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

        assert exc_info.value.phase == "db_write"
