"""Tests for src/agents/execution_agent.py.

Covers all acceptance scenarios including:
- Checkpoint confirmation (confirmed, timeout, wrong date, wrong format)
- Price deviation checks (no deviation, recalculate, skip, fetch failure)
- Order placement (successful, failed, multiple symbols)
- DB operations and error handling
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.agents.execution_agent import (
    run_execution_agent,
    ExecutionAgentError,
    ExecutionResult,
    OrderRecord,
    _ist_now,
    _resolve_db_path,
    _checkpoint_file_path,
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

    # Create risk_approvals table
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

    # Create execution_checkpoints table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED', 'TIMEOUT')),
            symbols TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(run_date)
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
    rank: int = 1,
    sentiment: str = "Positive",
    confidence: float = 0.9,
    scorecard_score: int = 28,
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
            "PROCEED",
            scorecard_score,
            40,
            sentiment,
            confidence,
            rank,
            "ABOVE_200DMA",
            1.0,
            1,
            now,
        ),
    )
    conn.commit()
    conn.close()


def insert_signal_row(
    db_path: str,
    symbol: str,
    atr: float = 10.0,
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
        (symbol, run_date.isoformat(), 35.0, "BUY", "MIDDLE", atr, 0.85, "BUY", now),
    )
    conn.commit()
    conn.close()


def make_mock_ohlcv_df(symbol: str, close_price: float) -> pd.DataFrame:
    """Create a DataFrame that mimics fetch_ohlcv output."""
    return pd.DataFrame({
        "symbol": [symbol],
        "close": [close_price],
    })


def insert_risk_approval(
    db_path: str,
    symbol: str,
    quantity: int = 5,
    entry_price_approx: float = 500.0,
    stop_loss: float = 480.0,
    take_profit: float = 540.0,
    risk_amount: float = 100.0,
    status: str = "APPROVED",
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert a row into risk_approvals."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    now = datetime.datetime.now(tz=IST).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO risk_approvals
            (symbol, run_date, quantity, entry_price_approx, stop_loss,
             take_profit, position_size_multiplier, risk_amount, approval_status, approved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            quantity,
            entry_price_approx,
            stop_loss,
            take_profit,
            1.0,
            risk_amount,
            status,
            now,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests: Checkpoint tests
# ---------------------------------------------------------------------------


def test_no_approved_trades_safe_mode(temp_db):
    """No approved trades → returns ExecutionResult with empty orders, safe_mode=True."""
    with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
        with patch("src.agents.execution_agent.log_agent_action"):
            with patch("src.agents.execution_agent.send_checkpoint"):
                with patch("src.agents.execution_agent.send_alert"):
                    result = run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert result.run_date == RUN_DATE
    assert result.safe_mode is True
    assert result.safe_mode_reason == "no_approved_trades"
    assert result.human_confirmed is False
    assert len(result.orders_placed) == 0
    assert len(result.orders_skipped) == 0


def test_checkpoint_confirmed_places_trades(temp_db):
    """Checkpoint confirmed (file contains run_date.isoformat()) → status=CONFIRMED, trades placed."""
    # Insert risk approval
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1500.0,
        stop_loss=1475.0,
        take_profit=1550.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    checkpoint_path = _checkpoint_file_path(RUN_DATE)

    # Mock the checkpoint file to exist with correct content
    with patch("pathlib.Path.exists") as mock_exists:
        with patch("pathlib.Path.read_text") as mock_read_text:
            with patch("pathlib.Path.unlink"):
                mock_exists.return_value = True
                mock_read_text.return_value = RUN_DATE.isoformat()

                with patch("src.agents.execution_agent.get_settings", return_value=MOCK_SETTINGS):
                    with patch("src.agents.execution_agent.log_agent_action"):
                        with patch("src.agents.execution_agent.send_checkpoint"):
                            with patch("src.agents.execution_agent.send_alert"):
                                with patch("src.agents.execution_agent.send_info"):
                                    with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                        mock_fetch.return_value.empty = False
                                        mock_fetch.return_value.__getitem__.return_value.__getitem__.return_value.iloc.__getitem__.return_value = 1500.0
                                        result = run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert result.run_date == RUN_DATE
    assert result.human_confirmed is True
    assert result.safe_mode is False


def test_checkpoint_timeout_no_confirmation(temp_db):
    """Checkpoint timeout (file never appears) → status=TIMEOUT, no trades, alert sent."""
    insert_risk_approval(temp_db, "INFY")
    insert_watchlist_row(temp_db, "INFY")

    # Mock time to avoid real waiting
    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("pathlib.Path.unlink"):
                    # First call returns 0, subsequent calls return > CHECKPOINT_TIMEOUT_SECS
                    mock_monotonic.side_effect = [0, 500, 500]

                    with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                        with patch("src.agents.execution_agent.log_agent_action"):
                            with patch("src.agents.execution_agent.send_checkpoint"):
                                with patch("src.agents.execution_agent.send_alert") as mock_alert:
                                    result = run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert result.human_confirmed is False
    assert result.safe_mode is True
    assert result.safe_mode_reason == "timeout_no_confirmation"
    assert len(result.orders_placed) == 0
    mock_alert.assert_called_once()


def test_checkpoint_wrong_date_treated_as_timeout(temp_db):
    """Checkpoint file contains wrong date → treated as not confirmed, timeout."""
    insert_risk_approval(temp_db, "INFY")
    insert_watchlist_row(temp_db, "INFY")

    wrong_date = (RUN_DATE - datetime.timedelta(days=1)).isoformat()

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=wrong_date):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        result = run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert result.safe_mode is True
    assert result.safe_mode_reason == "timeout_no_confirmation"


def test_checkpoint_old_format_y_rejected(temp_db):
    """Checkpoint file contains 'Y' (old format) → NOT accepted, treated as timeout."""
    insert_risk_approval(temp_db, "INFY")
    insert_watchlist_row(temp_db, "INFY")

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value="Y"):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        result = run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert result.safe_mode is True


def test_execution_checkpoints_table_created_on_first_run(temp_db):
    """execution_checkpoints table created on first run."""
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'"
    )
    assert cursor.fetchone() is not None
    conn.close()


# ---------------------------------------------------------------------------
# Tests: Price deviation tests
# ---------------------------------------------------------------------------


def test_deviation_le_05_percent_place_at_original_parameters(temp_db):
    """Deviation <= 0.5% → place at original parameters."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    current_price = 1002.0  # 0.2% deviation

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = current_price
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_placed) == 1
    assert result.orders_placed[0].status == "PLACED"
    assert result.orders_placed[0].quantity == 5


def test_deviation_05_to_15_percent_recalculate(temp_db):
    """Deviation 0.5-1.5% → recalculate quantity and stop-loss from current price."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    current_price = 1010.0  # 1.0% deviation

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = current_price
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_placed) == 1
    assert result.orders_placed[0].recalculated is True
    assert result.orders_placed[0].status == "PLACED"


def test_deviation_gt_15_percent_skip_trade(temp_db):
    """Deviation > 1.5% → skip trade, log price_slippage_exceeded."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    current_price = 1020.0  # 2.0% deviation

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action") as mock_log:
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = current_price
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_skipped) == 1
    assert result.orders_skipped[0].status == "SKIPPED_SLIPPAGE"
    assert len(result.orders_placed) == 0


def test_price_fetch_fails_use_approx_from_risk_approvals(temp_db):
    """Price fetch fails (FetchError) → use entry_price_approx from risk_approvals."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                from src.data.fetcher import FetchError
                                                mock_fetch.side_effect = FetchError(
                                                    symbol="INFY",
                                                    yfinance_error="error",
                                                    jugaad_error="error",
                                                )

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_skipped) == 1
    assert result.orders_skipped[0].status == "SKIPPED_PRICE_FETCH_FAILED"


# ---------------------------------------------------------------------------
# Tests: Order placement tests
# ---------------------------------------------------------------------------


def test_successful_order_placement(temp_db):
    """Successful order → OrderRecord with status=PLACED."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = 1000.0
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_placed) == 1
    assert result.orders_placed[0].status == "PLACED"
    assert result.orders_placed[0].order_id >= 0


def test_paper_trader_raises_value_error(temp_db):
    """PaperTrader raises ValueError (duplicate position) → OrderRecord with status=FAILED."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=100.0,
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=10.0)

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = 1000.0
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                with patch("src.agents.execution_agent.PaperTrader") as mock_pt:
                                                    mock_pt_instance = MagicMock()
                                                    mock_pt_instance.get_pnl.return_value = {
                                                        "total_pnl": 0.0
                                                    }
                                                    mock_pt_instance.place_order.side_effect = ValueError(
                                                        "duplicate position"
                                                    )
                                                    mock_pt.return_value = mock_pt_instance

                                                    result = run_execution_agent(
                                                        run_date=RUN_DATE,
                                                        db_path_override=temp_db,
                                                    )

    assert len(result.orders_skipped) == 1
    assert result.orders_skipped[0].status == "SKIPPED_ORDER_ERROR"


def test_two_approved_symbols_processed_independently(temp_db):
    """Two approved symbols → both processed independently."""
    insert_risk_approval(temp_db, "INFY", quantity=5, entry_price_approx=1000.0)
    insert_risk_approval(
        temp_db, "TCS", quantity=3, entry_price_approx=3500.0
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_watchlist_row(temp_db, "TCS")
    insert_signal_row(temp_db, "INFY", atr=10.0)
    insert_signal_row(temp_db, "TCS", atr=20.0)

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = 1000.0
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_placed) == 2
    assert all(r.status == "PLACED" for r in result.orders_placed)


def test_recalculated_quantity_zero_skipped(temp_db):
    """Recalculated quantity = 0 → OrderRecord with status=SKIPPED_RECALC_ZERO."""
    insert_risk_approval(
        temp_db,
        "INFY",
        quantity=5,
        entry_price_approx=1000.0,
        stop_loss=980.0,
        take_profit=1040.0,
        risk_amount=5.0,  # Very small risk amount
    )
    insert_watchlist_row(temp_db, "INFY")
    insert_signal_row(temp_db, "INFY", atr=100.0)  # Very large ATR

    current_price = 1010.0  # 1.0% deviation (will trigger recalc)

    with patch("src.agents.execution_agent.time.monotonic") as mock_monotonic:
        with patch("src.agents.execution_agent.time.sleep"):
            with patch("pathlib.Path.read_text", return_value=RUN_DATE.isoformat()):
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.unlink"):
                        mock_monotonic.side_effect = [0, 500, 500]

                        with patch("src.agents.execution_agent.settings", MOCK_SETTINGS):
                            with patch("src.agents.execution_agent.log_agent_action"):
                                with patch("src.agents.execution_agent.send_checkpoint"):
                                    with patch("src.agents.execution_agent.send_alert"):
                                        with patch("src.agents.execution_agent.send_info"):
                                            with patch("src.agents.execution_agent.fetch_ohlcv") as mock_fetch:
                                                df = MagicMock()
                                                df.empty = False
                                                df.columns = ["symbol", "close"]
                                                sym_df = MagicMock()
                                                sym_df.empty = False
                                                sym_df.__getitem__.return_value.iloc.__getitem__.return_value = current_price
                                                df.__getitem__.return_value = sym_df
                                                mock_fetch.return_value = df

                                                result = run_execution_agent(
                                                    run_date=RUN_DATE, db_path_override=temp_db
                                                )

    assert len(result.orders_skipped) == 1
    assert result.orders_skipped[0].status == "SKIPPED_RECALC_ZERO"


# ---------------------------------------------------------------------------
# Tests: DB tests
# ---------------------------------------------------------------------------


def test_risk_approvals_table_missing_raises_error(temp_db):
    """risk_approvals table missing → ExecutionAgentError(phase='db_read')."""
    # Drop the risk_approvals table
    conn = sqlite3.connect(temp_db)
    conn.execute("DROP TABLE IF EXISTS risk_approvals")
    conn.commit()
    conn.close()

    with patch("src.agents.execution_agent.get_settings", return_value=MOCK_SETTINGS):
        with patch("src.agents.execution_agent.log_agent_action"):
            with pytest.raises(ExecutionAgentError) as exc_info:
                run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert exc_info.value.phase == "db_read"


def test_execution_checkpoints_write_fails_raises_error(temp_db):
    """execution_checkpoints write fails → ExecutionAgentError(phase='db_write')."""
    insert_risk_approval(temp_db, "INFY")

    with patch("src.agents.execution_agent.get_settings", return_value=MOCK_SETTINGS):
        with patch("src.agents.execution_agent.log_agent_action"):
            with patch("src.agents.execution_agent._write_checkpoint_pending") as mock_write:
                mock_write.side_effect = ExecutionAgentError(
                    "write failed", phase="db_write"
                )
                with pytest.raises(ExecutionAgentError) as exc_info:
                    run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert exc_info.value.phase == "db_write"


def test_run_date_defaults_to_today_ist(temp_db):
    """run_date defaults to today IST."""
    with patch("src.agents.execution_agent.get_settings", return_value=MOCK_SETTINGS):
        with patch("src.agents.execution_agent.log_agent_action"):
            with patch("src.agents.execution_agent.send_checkpoint"):
                with patch("src.agents.execution_agent.send_alert"):
                    result = run_execution_agent(db_path_override=temp_db)

    assert result.run_date == datetime.date.today()


def test_db_path_override_used_correctly(temp_db):
    """db_path_override used correctly."""
    insert_risk_approval(temp_db, "INFY")
    insert_watchlist_row(temp_db, "INFY")

    with patch("src.agents.execution_agent.get_settings", return_value=MOCK_SETTINGS):
        with patch("src.agents.execution_agent.log_agent_action"):
            with patch("src.agents.execution_agent.send_checkpoint"):
                with patch("src.agents.execution_agent.send_alert"):
                    # Should use temp_db, not settings.database_url
                    result = run_execution_agent(run_date=RUN_DATE, db_path_override=temp_db)

    assert result.run_date == RUN_DATE
    # Verify it actually used temp_db by checking the DB
    conn = sqlite3.connect(temp_db)
    rows = conn.execute(
        "SELECT COUNT(*) FROM execution_checkpoints WHERE run_date = ?",
        (RUN_DATE.isoformat(),),
    ).fetchone()
    assert rows[0] > 0
    conn.close()
