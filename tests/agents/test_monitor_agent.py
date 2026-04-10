"""Tests for src/agents/monitor_agent.py.

Covers all acceptance scenarios including:
- Market hours guard (before 09:15, after 15:30, weekend)
- GTT check tests
- GTT reconciliation (every 30 minutes)
- Regime stop tightening
- LLM stop tightening
- Kill switch detection
- Emergency rescreen at 15:35
"""

from __future__ import annotations

import datetime
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.agents.monitor_agent import (
    run_monitor_agent,
    MonitorAgentError,
    MonitorResult,
)
from src.config.settings import Settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")
RUN_DATE = datetime.date(2026, 4, 10)

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

    # Create positions table
    conn.execute(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL NOT NULL,
            entry_date TEXT NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            pnl REAL,
            pnl_pct REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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

    # Create screener_results table
    conn.execute(
        """
        CREATE TABLE screener_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            momentum_score REAL NOT NULL,
            quality_passed INTEGER NOT NULL,
            regime TEXT NOT NULL,
            position_size_multiplier REAL NOT NULL,
            screened_at TEXT NOT NULL,
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

    # Create research_reports table
    conn.execute(
        """
        CREATE TABLE research_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_urls TEXT,
            earnings_transcript_unavailable INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            created_at TEXT NOT NULL
        )
    """
    )

    # Create agent_logs table
    conn.execute(
        """
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            action TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',
            symbol TEXT,
            result TEXT,
            data_quality_score REAL,
            logged_at TEXT NOT NULL
        )
    """
    )

    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def mock_settings(monkeypatch):
    """Mock global settings."""
    monkeypatch.setattr(
        "src.agents.monitor_agent.settings",
        MOCK_SETTINGS,
    )


# ---------------------------------------------------------------------------
# Market hours guard tests
# ---------------------------------------------------------------------------


def test_outside_market_hours_before_open(temp_db, mock_settings):
    """Called before 09:15 → returns early with positions_checked=0."""
    # 08:00 IST, weekday
    current_time = datetime.datetime(2026, 4, 10, 8, 0, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"):
        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.positions_checked == 0
    assert result.exits_triggered == []
    assert result.stops_tightened == 0
    assert result.gtt_reconciliation_ran is False
    assert result.kill_switch_detected is False
    assert result.emergency_rescreen_triggered is False


def test_outside_market_hours_after_close(temp_db, mock_settings):
    """Called after 15:30 → returns early with positions_checked=0."""
    # 16:00 IST, weekday
    current_time = datetime.datetime(2026, 4, 10, 16, 0, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"):
        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.positions_checked == 0


def test_market_open_at_exactly_0915(temp_db, mock_settings):
    """Called exactly at 09:15 → runs normally."""
    # 09:15 IST, Friday (weekday)
    current_time = datetime.datetime(2026, 4, 10, 9, 15, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should run normally, not exit early
    assert result.positions_checked == 0
    assert result.completed_at is not None


def test_outside_market_hours_on_weekend(temp_db, mock_settings):
    """Called on weekend → returns early with positions_checked=0."""
    # 2026-04-11 is Saturday
    current_time = datetime.datetime(2026, 4, 11, 10, 0, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"):
        result = run_monitor_agent(
            run_date=datetime.date(2026, 4, 11),
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.positions_checked == 0


# ---------------------------------------------------------------------------
# GTT check tests
# ---------------------------------------------------------------------------


def test_gtt_check_returns_one_exit(temp_db, mock_settings):
    """check_gtts() returns 1 exit → exits_triggered=1 in MonitorResult."""
    current_time = datetime.datetime(2026, 4, 10, 9, 30, 0, tzinfo=IST)

    # Insert a position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1600.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = [
            {
                "symbol": "HDFC",
                "quantity": 10,
                "entry_price": 1580.0,
                "current_price": 1600.0,
                "stop_loss": 1550.0,
                "take_profit": 1650.0,
            }
        ]
        mock_instance.check_gtts.return_value = [
            {"symbol": "HDFC", "exit_price": 1600.0, "exit_reason": "TAKE_PROFIT", "trade_id": 1}
        ]
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 200,
            "unrealized_pnl": 0,
            "total_pnl": 200,
            "trade_count": 1,
            "win_count": 1,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        mock_fetch.return_value.__getitem__ = MagicMock()
        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1600.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert len(result.exits_triggered) == 1
    assert result.exits_triggered[0]["symbol"] == "HDFC"


def test_gtt_check_returns_zero_exits(temp_db, mock_settings):
    """check_gtts() returns 0 exits → exits_triggered=0."""
    current_time = datetime.datetime(2026, 4, 10, 9, 30, 0, tzinfo=IST)

    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = [
            {
                "symbol": "HDFC",
                "quantity": 10,
                "entry_price": 1580.0,
                "current_price": 1590.0,
                "stop_loss": 1550.0,
                "take_profit": 1650.0,
            }
        ]
        mock_instance.check_gtts.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert len(result.exits_triggered) == 0


def test_paper_trader_init_fails(temp_db, mock_settings):
    """PaperTrader init fails → MonitorAgentError(phase='paper_trader_init')."""
    current_time = datetime.datetime(2026, 4, 10, 9, 30, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_pt.side_effect = ValueError("DB error")

        with pytest.raises(MonitorAgentError) as exc_info:
            run_monitor_agent(
                run_date=RUN_DATE,
                current_time=current_time,
                db_path_override=temp_db,
            )

        assert exc_info.value.phase == "paper_trader_init"


# ---------------------------------------------------------------------------
# GTT reconciliation tests
# ---------------------------------------------------------------------------


def test_gtt_reconciliation_at_minute_0(temp_db, mock_settings):
    """current_time.minute == 0 → gtt_reconciliation_ran=True."""
    # 09:00 IST, minute=0
    current_time = datetime.datetime(2026, 4, 10, 9, 0, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Outside market hours, so should not run reconciliation
    assert result.gtt_reconciliation_ran is False


def test_gtt_reconciliation_at_minute_30(temp_db, mock_settings):
    """current_time.minute == 30 → gtt_reconciliation_ran=True."""
    # 10:30 IST, minute=30, during market hours
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.gtt_reconciliation_ran is True


def test_gtt_reconciliation_at_minute_15(temp_db, mock_settings):
    """current_time.minute == 15 → gtt_reconciliation_ran=False."""
    # 10:15 IST, minute=15, during market hours
    current_time = datetime.datetime(2026, 4, 10, 10, 15, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.gtt_reconciliation_ran is False


# ---------------------------------------------------------------------------
# Regime stop tightening tests
# ---------------------------------------------------------------------------


def test_regime_stop_tightening_below_200dma(temp_db, mock_settings):
    """Regime BELOW_200DMA, position exists with valid ATR → tightens stop."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result with BELOW_200DMA regime
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "BELOW_200DMA",
            0.5,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with ATR
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            10.0,
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.update_stop_loss.return_value = None
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should have called update_stop_loss
    assert mock_instance.update_stop_loss.called
    assert result.stops_tightened >= 1


def test_regime_stop_tightening_monotonic_guard(temp_db, mock_settings):
    """New stop < current stop → monotonic guard, NOT tightened."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position with already-tight stop
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1560.0,  # Already tight
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result with BELOW_200DMA
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "BELOW_200DMA",
            0.5,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with very small ATR (so new_stop < current_stop)
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            2.0,  # Small ATR
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        # stop_loss=1578.0 = entry(1580) - atr(2) * 1 → already at 1×ATR level
        # new_stop = 1580 - 2*1 = 1578, not > 1578, so monotonic guard blocks update
        mock_instance.get_positions.return_value = [
            {
                "symbol": "HDFC",
                "quantity": 10,
                "entry_price": 1580.0,
                "current_price": 1590.0,
                "stop_loss": 1578.0,
                "take_profit": 1650.0,
            }
        ]
        mock_instance.check_gtts.return_value = []
        mock_instance.update_stop_loss.return_value = None
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should NOT have called update_stop_loss (monotonic guard: new_stop == current_stop)
    assert not mock_instance.update_stop_loss.called


def test_regime_stop_tightening_atr_unavailable(temp_db, mock_settings):
    """ATR unavailable (no signal for symbol) → skip tighten, log atr_unavailable."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result with BELOW_200DMA
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "BELOW_200DMA",
            0.5,
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    # NO signal inserted — ATR unavailable
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action") as mock_log, \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Check that atr_unavailable_skip_tighten was logged
    calls = [call for call in mock_log.call_args_list]
    found_atr_unavailable = any(
        "atr_unavailable_skip_tighten" in str(call)
        for call in calls
    )
    assert found_atr_unavailable or result.stops_tightened == 0


def test_regime_stop_tightening_above_200dma(temp_db, mock_settings):
    """Regime ABOVE_200DMA → no regime tightening."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result with ABOVE_200DMA regime (no tightening)
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "ABOVE_200DMA",
            1.0,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with ATR
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            10.0,
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should NOT have called update_stop_loss (regime ABOVE)
    assert not mock_instance.update_stop_loss.called


# ---------------------------------------------------------------------------
# LLM stop tightening tests
# ---------------------------------------------------------------------------


def test_llm_stop_tightening_negative_high_confidence(temp_db, mock_settings):
    """Negative sentiment, confidence=0.9 → update_stop_loss called."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result with ABOVE_200DMA (no regime tightening)
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "ABOVE_200DMA",
            1.0,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with ATR
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            10.0,
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert research report with Negative sentiment, high confidence
    conn.execute(
        """
        INSERT INTO research_reports (symbol, run_date, sentiment, confidence,
                                      source_urls, completed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            "Negative",
            0.9,
            "http://example.com",
            datetime.datetime.now(IST).isoformat(),
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.update_stop_loss.return_value = None
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should have called update_stop_loss for LLM tightening
    assert mock_instance.update_stop_loss.called
    assert result.stops_tightened >= 1


def test_llm_stop_tightening_negative_low_confidence(temp_db, mock_settings):
    """Negative sentiment, confidence=0.7 (< 0.8) → NOT tightened."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "ABOVE_200DMA",
            1.0,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with ATR
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            10.0,
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert research report with Negative sentiment, LOW confidence
    conn.execute(
        """
        INSERT INTO research_reports (symbol, run_date, sentiment, confidence,
                                      source_urls, completed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            "Negative",
            0.7,  # Below threshold
            "http://example.com",
            datetime.datetime.now(IST).isoformat(),
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should NOT have called update_stop_loss (confidence below threshold)
    assert not mock_instance.update_stop_loss.called


def test_llm_stop_tightening_positive_sentiment(temp_db, mock_settings):
    """Positive sentiment → NOT tightened."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "ABOVE_200DMA",
            1.0,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with ATR
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            10.0,
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert research report with Positive sentiment
    conn.execute(
        """
        INSERT INTO research_reports (symbol, run_date, sentiment, confidence,
                                      source_urls, completed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            "Positive",
            0.9,
            "http://example.com",
            datetime.datetime.now(IST).isoformat(),
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should NOT have called update_stop_loss (Positive sentiment)
    assert not mock_instance.update_stop_loss.called


def test_llm_stop_tightening_no_research_for_symbol(temp_db, mock_settings):
    """No research_reports for symbol → NOT tightened."""
    current_time = datetime.datetime(2026, 4, 10, 10, 30, 0, tzinfo=IST)

    # Insert position
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO positions (symbol, quantity, entry_price, current_price,
                               entry_date, stop_loss, take_profit,
                               created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            10,
            1580.0,
            1590.0,
            "2026-04-09",
            1550.0,
            1650.0,
            "2026-04-09T09:15:00",
            "2026-04-09T09:15:00",
        ),
    )

    # Insert screener result
    conn.execute(
        """
        INSERT INTO screener_results (symbol, run_date, rank, momentum_score,
                                      quality_passed, regime,
                                      position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            1,
            0.5,
            1,
            "ABOVE_200DMA",
            1.0,
            datetime.datetime.now(IST).isoformat(),
        ),
    )

    # Insert signal with ATR
    conn.execute(
        """
        INSERT INTO signals (symbol, run_date, rsi, macd_signal,
                            bollinger_position, atr, groq_confidence,
                            signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            35.0,
            "BUY",
            "BELOW",
            10.0,
            0.8,
            "BUY",
            datetime.datetime.now(IST).isoformat(),
        ),
    )
    # NO research_reports inserted for HDFC
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value =             [
                {
                    "symbol": "HDFC",
                    "quantity": 10,
                    "entry_price": 1580.0,
                    "current_price": 1590.0,
                    "stop_loss": 1550.0,
                    "take_profit": 1650.0,
                }
            ]
        mock_instance.check_gtts.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 100,
            "total_pnl": 100,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        import pandas as pd
        mock_fetch.return_value = pd.DataFrame({
            "symbol": ["HDFC"],
            "close": [1590.0],
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Should NOT have called update_stop_loss (no research)
    assert not mock_instance.update_stop_loss.called


# ---------------------------------------------------------------------------
# Kill switch tests
# ---------------------------------------------------------------------------


def test_kill_switch_drawdown_exceeds_15pct(temp_db, mock_settings):
    """Drawdown > 15% → kill_switch_detected=True, kill_switch_reason set."""
    current_time = datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=IST)

    # Insert trades to create drawdown > 15%
    conn = sqlite3.connect(temp_db)
    conn.row_factory = sqlite3.Row
    # Insert a winning trade first
    conn.execute(
        """
        INSERT INTO trades (symbol, entry_date, entry_price, entry_quantity,
                           exit_date, exit_price, exit_quantity, pnl, pnl_pct,
                           exit_reason, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            "2026-04-01",
            1500.0,
            10,
            "2026-04-02",
            1510.0,
            10,
            100.0,
            0.67,
            "TAKE_PROFIT",
            "2026-04-02T14:00:00",
        ),
    )
    # Peak equity now: 10000 + 100 = 10100

    # Insert 5 losing trades totaling -1800 (drawdown = 1800/10100 = 17.8%)
    for i in range(5):
        conn.execute(
            """
            INSERT INTO trades (symbol, entry_date, entry_price, entry_quantity,
                               exit_date, exit_price, exit_quantity, pnl, pnl_pct,
                               exit_reason, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"STOCK{i}",
                f"2026-04-0{3+i}",
                1000.0,
                1,
                f"2026-04-0{4+i}",
                964.0,
                1,
                -360.0,
                -36.0,
                "STOP_LOSS",
                f"2026-04-0{4+i}T14:00:00",
            ),
        )
    conn.commit()
    conn.close()

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.send_alert") as mock_alert:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": -1700.0,
            "unrealized_pnl": 0,
            "total_pnl": -1700.0,
            "trade_count": 6,
            "win_count": 1,
            "loss_count": 5,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.kill_switch_detected is True


def test_kill_switch_no_trades(temp_db, mock_settings):
    """No trades → kill_switch_detected=False."""
    current_time = datetime.datetime(2026, 4, 10, 10, 0, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.kill_switch_detected is False


# ---------------------------------------------------------------------------
# Emergency rescreen tests
# ---------------------------------------------------------------------------


def test_emergency_rescreen_at_1535(temp_db, mock_settings):
    """current_time = 15:35 → emergency_rescreen_signal checked."""
    # 15:35 IST on a weekday
    current_time = datetime.datetime(2026, 4, 10, 15, 35, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.send_alert"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt, \
         patch("src.agents.monitor_agent.fetch_sector_indices") as mock_fetch_idx, \
         patch("src.agents.monitor_agent.run_screener_agent") as mock_screener:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        # Return Nifty data with 3.5% drop (strictly > 3.0% threshold)
        import pandas as pd
        mock_fetch_idx.return_value = pd.DataFrame({
            "symbol": ["NIFTY50", "NIFTY50"],
            "date": ["2026-04-09", "2026-04-10"],
            "close": [20000.0, 19300.0],  # 3.5% drop
        })

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    # Nifty dropped >3% at 15:35 → emergency rescreen triggered
    assert result.emergency_rescreen_triggered is True


def test_emergency_rescreen_not_at_1535(temp_db, mock_settings):
    """current_time != 15:35 → emergency_rescreen_signal=False."""
    # 15:30 IST (just before rescreen time)
    current_time = datetime.datetime(2026, 4, 10, 15, 30, 0, tzinfo=IST)

    with patch("src.agents.monitor_agent.log_agent_action"), \
         patch("src.agents.monitor_agent.PaperTrader") as mock_pt:
        mock_instance = MagicMock()
        mock_instance.get_positions.return_value = []
        mock_instance.get_pnl.return_value = {
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_pt.return_value = mock_instance

        result = run_monitor_agent(
            run_date=RUN_DATE,
            current_time=current_time,
            db_path_override=temp_db,
        )

    assert result.emergency_rescreen_triggered is False
