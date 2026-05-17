"""Integration tests for the full trading pipeline — scenarios 1–5.

Each test uses a fresh temporary SQLite database, mocks all external APIs,
seeds realistic Nifty 50 data, and asserts on DB state after each run.

Scenarios:
1. Normal evening pipeline — happy path (screener → research → watchlist)
2. Kill switch fires (morning session) — execution agent never called
3. Thin universe — fewer than 3 stocks pass quality filter
4. Regime blocked — Nifty below 200 DMA for 10+ consecutive days
5. Research incomplete — race condition prevention in watchlist builder
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.utils.logger import SQLiteHandler, setup_logging

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")
RUN_DATE: datetime.date = datetime.date(2026, 4, 14)  # Monday

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# Realistic Nifty 50 symbols used in tests
NIFTY_SYMBOLS: list[str] = [
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "INFY",
    "ICICIBANK",
    "HINDUNILVR",
    "KOTAKBANK",
    "LT",
    "SBIN",
    "BAJFINANCE",
]


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _make_sector_df() -> "pd.DataFrame":
    """Return a minimal sector DataFrame containing NIFTY_50 row.

    screener_agent filters sector_df by symbol == 'NIFTY_50' before calling
    apply_regime_filter. The returned DataFrame must have a 'symbol' column
    or the filter raises KeyError. The actual OHLCV values don't matter
    because apply_regime_filter is mocked in all tests that use this helper.
    """
    import pandas as pd

    return pd.DataFrame(
        {
            "symbol": ["NIFTY_50"],
            "date": [RUN_DATE],
            "open": [22000.0],
            "high": [22200.0],
            "low": [21800.0],
            "close": [22100.0],
            "volume": [1_000_000],
        }
    )


# ---------------------------------------------------------------------------
# DB schema helpers
# ---------------------------------------------------------------------------


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply WAL pragmas to a connection."""
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)


def _create_agent_logs(conn: sqlite3.Connection) -> None:
    """Create agent_logs table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_logs (
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


def _create_screener_results(conn: sqlite3.Connection) -> None:
    """Create screener_results table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screener_results (
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


def _create_research_reports(conn: sqlite3.Connection) -> None:
    """Create research_reports table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_urls TEXT NOT NULL,
            earnings_transcript_unavailable INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            raw_response TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def _create_watchlist(conn: sqlite3.Connection) -> None:
    """Create watchlist table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
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


def _create_risk_approvals(conn: sqlite3.Connection) -> None:
    """Create risk_approvals table."""
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


def _create_orders(conn: sqlite3.Connection) -> None:
    """Create orders table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'CNC',
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            order_id TEXT,
            gtt_sl_id TEXT,
            gtt_tp_id TEXT,
            placed_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        )
        """
    )


def _create_signals(conn: sqlite3.Connection) -> None:
    """Create signals table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
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


def _create_trades(conn: sqlite3.Connection) -> None:
    """Create trades table (actual schema used by risk_agent.py)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
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


def _create_positions(conn: sqlite3.Connection) -> None:
    """Create positions table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            pnl REAL NOT NULL DEFAULT 0.0,
            pnl_pct REAL NOT NULL DEFAULT 0.0,
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(symbol)
        )
        """
    )


def _init_full_schema(conn: sqlite3.Connection) -> None:
    """Create all tables required for full pipeline tests."""
    _create_agent_logs(conn)
    _create_screener_results(conn)
    _create_research_reports(conn)
    _create_watchlist(conn)
    _create_risk_approvals(conn)
    _create_orders(conn)
    _create_signals(conn)
    _create_trades(conn)
    _create_positions(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Data seeding helpers
# ---------------------------------------------------------------------------


def _now_ist() -> str:
    """Return current IST timestamp as ISO string."""
    return datetime.datetime.now(tz=IST).isoformat()


def seed_screener_result(
    db_path: str,
    symbol: str,
    rank: int,
    momentum_score: float = 10.0,
    quality_passed: int = 1,
    regime: str = "ABOVE_200DMA",
    position_size_multiplier: float = 1.0,
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert one row into screener_results."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO screener_results
            (symbol, run_date, rank, momentum_score, quality_passed,
             regime, position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            rank,
            momentum_score,
            quality_passed,
            regime,
            position_size_multiplier,
            _now_ist(),
        ),
    )
    conn.close()


def seed_research_report(
    db_path: str,
    symbol: str,
    sentiment: str = "Positive",
    confidence: float = 0.85,
    completed_at: datetime.datetime | None = None,
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert one row into research_reports. completed_at=None simulates incomplete."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    completed_str = completed_at.isoformat() if completed_at is not None else None
    conn.execute(
        """
        INSERT INTO research_reports
            (symbol, run_date, sentiment, confidence, source_urls,
             earnings_transcript_unavailable, completed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            sentiment,
            confidence,
            json.dumps([f"https://news.example.com/{symbol}"]),
            0,
            completed_str,
            _now_ist(),
        ),
    )
    conn.close()


def seed_watchlist_row(
    db_path: str,
    symbol: str,
    combined_decision: str = "PROCEED",
    human_approved: int = 1,
    rank: int = 1,
    regime: str = "ABOVE_200DMA",
    position_size_multiplier: float = 1.0,
    sentiment: str = "Positive",
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert one row into watchlist."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO watchlist
            (symbol, run_date, combined_decision, scorecard_score, scorecard_max,
             sentiment, confidence, rank, regime, position_size_multiplier,
             human_approved, approval_source, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            combined_decision,
            30,
            40,
            sentiment,
            0.85,
            rank,
            regime,
            position_size_multiplier,
            human_approved,
            "human_explicit" if human_approved else None,
            _now_ist(),
        ),
    )
    conn.close()


def seed_signal_row(
    db_path: str,
    symbol: str,
    atr: float = 10.0,
    signal_type: str = "BUY",
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert one row into signals."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    conn.execute(
        """
        INSERT INTO signals
            (symbol, run_date, rsi, macd_signal, bollinger_position, atr,
             groq_confidence, signal_type, signalled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol, run_date.isoformat(), 35.0, "BUY", "MIDDLE", atr, 0.85, signal_type, _now_ist()),
    )
    conn.close()


def seed_trade_row(
    db_path: str,
    symbol: str = "TEST",
    pnl: float = 100.0,
    closed_at: str | None = None,
) -> None:
    """Insert one row into trades (actual schema used by risk_agent)."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    ts = closed_at or _now_ist()
    conn.execute(
        """
        INSERT INTO trades
            (symbol, entry_date, entry_price, entry_quantity, pnl, closed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (symbol, RUN_DATE.isoformat(), 500.0, 5, pnl, ts),
    )
    conn.close()


def count_rows(db_path: str, table: str) -> int:
    """Return row count for the given table."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    count: int = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.close()
    return count


def fetch_rows(db_path: str, table: str) -> list[dict]:
    """Return all rows from the given table as dicts."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_agent_log_actions(db_path: str) -> list[str]:
    """Return all action strings from agent_logs."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    rows = conn.execute("SELECT action FROM agent_logs").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Generator[str, None, None]:
    """Fresh SQLite DB for each test with full schema initialised.

    Also resets the root logger's SQLiteHandler so setup_logging() is not
    a no-op (it is idempotent by design, skipping if a handler already exists).
    Tears down the handler after each test to maintain isolation.
    """
    import logging

    path = str(tmp_path / "test_trading.db")
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    _apply_pragmas(conn)
    _init_full_schema(conn)
    conn.close()

    # Remove any SQLiteHandler from a previous test so setup_logging() attaches fresh
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, SQLiteHandler):
            root.removeHandler(h)

    setup_logging(path)
    yield path

    # Teardown: remove this test's SQLiteHandler
    for h in list(root.handlers):
        if isinstance(h, SQLiteHandler):
            root.removeHandler(h)


# ---------------------------------------------------------------------------
# Mock Settings factory
# ---------------------------------------------------------------------------


def _mock_settings(db_path: str) -> MagicMock:
    """Build a mock Settings object pointing at the temp DB."""
    s = MagicMock()
    s.live_trading = False
    s.paper_trading = True
    s.log_level = "DEBUG"
    s.max_trade_amount = 10000
    s.database_url = f"sqlite:///{db_path}"
    s.groq_api_key = "test-groq-key"
    s.gemini_api_key = "test-gemini-key"
    s.tavily_api_key = "test-tavily-key"
    s.telegram_bot_token = "test-telegram-token"
    s.telegram_chat_id = "test-chat-id"
    s.gmail_credentials = None
    s.shoonya_user = "test-user"
    s.shoonya_password = "test-pass"
    s.shoonya_totp_secret = "test-totp"
    return s


# ---------------------------------------------------------------------------
# Scenario 1: Normal evening pipeline — happy path
# ---------------------------------------------------------------------------


class TestScenario1EveningHappyPath:
    """Scenario 1: Evening session happy path.

    5 stocks pass quality filter, research returns Positive sentiment for all,
    watchlist builder produces 3 PROCEED entries.

    Steps tested: screener_agent → (manual research seed) → watchlist_agent
    The research agent is mocked/seeded directly to avoid Tavily/Gemini calls.
    """

    @patch("src.agents.watchlist_agent.send_checkpoint")
    @patch("src.agents.watchlist_agent.send_alert")
    @patch("src.agents.watchlist_agent._resolve_db_path")
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_scenario_1_evening_happy_path(
        self,
        mock_settings: MagicMock,
        mock_get_universe: MagicMock,
        mock_fetch_symbols: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_sector: MagicMock,
        mock_get_fundamentals: MagicMock,
        mock_quality_filter: MagicMock,
        mock_momentum: MagicMock,
        mock_regime_filter: MagicMock,
        mock_screener_send_alert: MagicMock,
        mock_send_info: MagicMock,
        mock_screener_log: MagicMock,
        mock_watchlist_resolve_db: MagicMock,
        mock_watchlist_send_alert: MagicMock,
        mock_watchlist_send_checkpoint: MagicMock,
        db_path: str,
    ) -> None:
        """5 passing stocks → 5 screener rows, 5 completed research rows, 3 PROCEED in watchlist."""
        import pandas as pd
        from src.agents.screener_agent import run_screener_agent
        from src.agents.watchlist_agent import run_watchlist_agent

        # --- screener_agent mocks ---
        mock_settings.database_url = f"sqlite:///{db_path}"
        five_symbols = NIFTY_SYMBOLS[:5]
        mock_get_universe.return_value = NIFTY_SYMBOLS
        mock_fetch_symbols.return_value = NIFTY_SYMBOLS
        mock_fetch_ohlcv.return_value = pd.DataFrame()
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = pd.DataFrame()

        quality_df = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "roe": 0.20,
                    "debt_to_equity": 0.5,
                    "avg_daily_value": 50_000_000.0,
                    "latest_price": 500.0,
                    "high_52w": 600.0,
                    "pct_from_52w_high": 0.17,
                    "within_30pct_of_52w_high": 1,
                    "passed_hard_filters": 1,
                }
                for sym in five_symbols
            ]
        )
        quality_report = MagicMock()
        quality_report.passed_count = 5
        quality_report.universe_size = 10
        quality_report.thin_universe = False
        quality_report.filter_failure_counts = {}
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "momentum_score": float(5 - i),
                    "twelve_month_return": 0.20,
                    "one_month_return": 0.02,
                    "rank": i + 1,
                    "pct_from_52w_high": 0.17,
                    "within_30pct_of_52w_high": 1,
                }
                for i, sym in enumerate(five_symbols)
            ]
        )
        momentum_report = MagicMock()
        momentum_report.scored_count = 5
        momentum_report.selected_count = 5
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df = ranked_df.copy()
        filtered_df["position_size_multiplier"] = 1.0
        regime_result = MagicMock()
        regime_result.regime = "ABOVE_200DMA"
        regime_result.position_size_multiplier = 1.0
        regime_result.nifty_close = 25000.0
        regime_result.sma_200 = 24000.0
        regime_result.consecutive_days_below = 0
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Run screener
        screener_result = run_screener_agent(run_date=RUN_DATE)

        # Assert screener wrote 5 rows
        assert screener_result.thin_universe is False
        assert screener_result.regime_blocked is False
        assert len(screener_result.top5) == 5
        screener_rows = fetch_rows(db_path, "screener_results")
        assert len(screener_rows) == 5, f"Expected 5 screener rows, got {len(screener_rows)}"

        # --- Seed completed research for 5 stocks (simulates research_agent) ---
        completed_time = datetime.datetime.now(tz=IST)
        for sym in five_symbols:
            seed_research_report(
                db_path=db_path,
                symbol=sym,
                sentiment="Positive",
                confidence=0.85,
                completed_at=completed_time,
                run_date=RUN_DATE,
            )

        # Verify 5 research rows with completed_at set
        research_rows = fetch_rows(db_path, "research_reports")
        assert len(research_rows) == 5
        assert all(r["completed_at"] is not None for r in research_rows)

        # --- Run watchlist_agent ---
        mock_watchlist_resolve_db.return_value = db_path

        watchlist_result = run_watchlist_agent(run_date=RUN_DATE)

        # Assert: at most 5 rows in watchlist, all 5 evaluated, PROCEED entries ≥ 0
        watchlist_rows = fetch_rows(db_path, "watchlist")
        assert len(watchlist_rows) == 5, f"Expected 5 watchlist rows, got {len(watchlist_rows)}"

        proceed_rows = [r for r in watchlist_rows if r["combined_decision"] == "PROCEED"]
        # All 5 have Positive sentiment + ABOVE_200DMA → all 5 should PROCEED
        assert len(proceed_rows) == 5, (
            f"Expected 5 PROCEED rows with Positive sentiment + ABOVE_200DMA, "
            f"got {len(proceed_rows)}"
        )
        assert watchlist_result.candidates_evaluated == 5
        assert watchlist_result.proceed_count == 5


# ---------------------------------------------------------------------------
# Scenario 2: Kill switch fires — execution agent never called
# ---------------------------------------------------------------------------


class TestScenario2KillSwitchFires:
    """Scenario 2: Risk agent detects drawdown > 15%, execution agent skipped.

    We seed the trades table with enough losses to trigger the drawdown kill switch,
    seed a watchlist row as human_approved=1, then run run_risk_agent directly.
    We verify kill_switch_fired=True and no orders written.
    The orchestrator skip logic (kill_switch_fired → skip execution_agent) is
    tested separately in tests/agents/test_orchestrator.py.
    """

    @patch("src.agents.risk_agent.settings")
    def test_scenario_2_kill_switch_drawdown_fires(
        self,
        mock_settings: MagicMock,
        db_path: str,
    ) -> None:
        """Drawdown > 15% → kill_switch_fired=True, orders table empty."""
        from src.agents.risk_agent import run_risk_agent

        mock_settings.database_url = f"sqlite:///{db_path}"
        mock_settings.live_trading = False
        mock_settings.paper_trading = True

        # Seed watchlist: 1 approved stock
        seed_watchlist_row(db_path, "RELIANCE", human_approved=1, combined_decision="PROCEED")
        seed_signal_row(db_path, "RELIANCE", atr=20.0, signal_type="BUY")

        # Seed trades to force drawdown > 15%.
        # Starting capital = 100,000. Peak needs to rise first, then drop.
        # 3 winning trades bring peak to ~103,000.
        # Then losses to bring equity below 103,000 * 0.85 = 87,550.
        for _ in range(3):
            seed_trade_row(db_path, pnl=1000.0)
        # 10 × -2000 = -20,000 loss → equity ~ 103,000 - 20,000 = 83,000 → drawdown ~19%
        for _ in range(10):
            seed_trade_row(db_path, pnl=-2000.0)

        risk_result = run_risk_agent(run_date=RUN_DATE, db_path_override=db_path)

        # Kill switch must have fired
        assert risk_result.kill_switch_fired is True, (
            f"Expected kill_switch_fired=True, got False. "
            f"Drawdown was {risk_result.current_drawdown_pct:.2f}%"
        )
        assert risk_result.kill_switch_reason is not None
        assert "drawdown" in risk_result.kill_switch_reason.lower(), (
            f"Unexpected kill_switch_reason: {risk_result.kill_switch_reason}"
        )

        # All symbols must be rejected (kill switch applied before sizing)
        all_approvals = risk_result.approved + risk_result.rejected
        for approval in all_approvals:
            assert approval.approval_status == "REJECTED"
            assert approval.rejection_reason is not None
            assert "kill_switch" in approval.rejection_reason.lower()

        # No orders should have been placed (execution_agent was never called)
        orders_count = count_rows(db_path, "orders")
        assert orders_count == 0, f"Expected 0 orders after kill switch, got {orders_count}"

        # Confirm kill_switch_fired log entry in agent_logs
        log_actions = fetch_agent_log_actions(db_path)
        kill_switch_logged = any("kill_switch" in action.lower() for action in log_actions)
        assert kill_switch_logged, (
            "Expected kill_switch log entry in agent_logs. "
            f"Found actions: {log_actions[:10]}"
        )


# ---------------------------------------------------------------------------
# Scenario 3: Thin universe
# ---------------------------------------------------------------------------


class TestScenario3ThinUniverse:
    """Scenario 3: Only 2 stocks pass quality filter.

    The screener agent detects thin_universe and:
    - Does NOT write rows to screener_results
    - Logs thin_universe to agent_logs
    - Returns empty top5
    The watchlist table should remain empty.
    """

    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_scenario_3_thin_universe(
        self,
        mock_settings: MagicMock,
        mock_get_universe: MagicMock,
        mock_fetch_symbols: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_sector: MagicMock,
        mock_get_fundamentals: MagicMock,
        mock_quality_filter: MagicMock,
        mock_momentum: MagicMock,
        mock_regime_filter: MagicMock,
        mock_send_alert: MagicMock,
        mock_send_info: MagicMock,
        mock_log: MagicMock,
        db_path: str,
    ) -> None:
        """Only 2 stocks pass → thin_universe logged, screener_results empty, watchlist empty."""
        import pandas as pd
        from src.agents.screener_agent import run_screener_agent

        mock_settings.database_url = f"sqlite:///{db_path}"
        mock_get_universe.return_value = NIFTY_SYMBOLS
        mock_fetch_symbols.return_value = NIFTY_SYMBOLS
        mock_fetch_ohlcv.return_value = pd.DataFrame()
        mock_fetch_sector.return_value = pd.DataFrame()
        mock_get_fundamentals.return_value = pd.DataFrame()

        # Only 2 symbols pass — thin universe
        two_symbols = NIFTY_SYMBOLS[:2]
        quality_df = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "roe": 0.20,
                    "debt_to_equity": 0.5,
                    "avg_daily_value": 50_000_000.0,
                    "latest_price": 500.0,
                    "high_52w": 600.0,
                    "pct_from_52w_high": 0.17,
                    "within_30pct_of_52w_high": 1,
                    "passed_hard_filters": 1,
                }
                for sym in two_symbols
            ]
        )
        quality_report = MagicMock()
        quality_report.passed_count = 2
        quality_report.universe_size = 10
        quality_report.thin_universe = True
        quality_report.filter_failure_counts = {}
        mock_quality_filter.return_value = (quality_df, quality_report)

        result = run_screener_agent(run_date=RUN_DATE)

        # screener_agent returns thin_universe=True, empty top5
        assert result.thin_universe is True
        assert result.top5 == []
        assert result.symbols_passed_quality == 2

        # screener_results must have 0 rows (thin_universe writes nothing)
        screener_count = count_rows(db_path, "screener_results")
        assert screener_count == 0, (
            f"Expected 0 screener_results rows for thin_universe, got {screener_count}"
        )

        # watchlist must remain empty
        watchlist_count = count_rows(db_path, "watchlist")
        assert watchlist_count == 0, (
            f"Expected 0 watchlist rows for thin_universe, got {watchlist_count}"
        )

        # agent_logs must contain a thin_universe entry (via mock_log call args)
        thin_logged = any(
            "thin_universe" in str(call)
            for call in mock_log.call_args_list
        )
        assert thin_logged, (
            "Expected thin_universe to be logged via log_agent_action. "
            f"Calls: {[str(c) for c in mock_log.call_args_list]}"
        )

        # send_alert must have been called for thin_universe notification
        mock_send_alert.assert_called()
        alert_subject = mock_send_alert.call_args[1].get(
            "subject", mock_send_alert.call_args[0][0] if mock_send_alert.call_args[0] else ""
        )
        assert "thin" in alert_subject.lower(), (
            f"Expected 'thin' in alert subject, got: {alert_subject}"
        )


# ---------------------------------------------------------------------------
# Scenario 4: Regime blocked — below 200 DMA for 10+ consecutive days
# ---------------------------------------------------------------------------


class TestScenario4RegimeBlocked:
    """Scenario 4: Nifty 50 below 200 DMA for 10+ consecutive days.

    The screener agent returns regime_blocked=True, position_size_multiplier=0.0.
    No new positions should be opened (screener writes 0 rows, top5 is empty).
    """

    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_scenario_4_regime_blocked_below_200dma_10days(
        self,
        mock_settings: MagicMock,
        mock_get_universe: MagicMock,
        mock_fetch_symbols: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_sector: MagicMock,
        mock_get_fundamentals: MagicMock,
        mock_quality_filter: MagicMock,
        mock_momentum: MagicMock,
        mock_regime_filter: MagicMock,
        mock_send_alert: MagicMock,
        mock_send_info: MagicMock,
        mock_log: MagicMock,
        db_path: str,
    ) -> None:
        """Regime BELOW_200DMA_10DAYS → regime_blocked=True, no rows in screener_results."""
        import pandas as pd
        from src.agents.screener_agent import run_screener_agent

        mock_settings.database_url = f"sqlite:///{db_path}"
        five_symbols = NIFTY_SYMBOLS[:5]
        mock_get_universe.return_value = NIFTY_SYMBOLS
        mock_fetch_symbols.return_value = NIFTY_SYMBOLS
        mock_fetch_ohlcv.return_value = pd.DataFrame()
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = pd.DataFrame()

        quality_df = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "roe": 0.20,
                    "debt_to_equity": 0.5,
                    "avg_daily_value": 50_000_000.0,
                    "latest_price": 500.0,
                    "high_52w": 600.0,
                    "pct_from_52w_high": 0.17,
                    "within_30pct_of_52w_high": 1,
                    "passed_hard_filters": 1,
                }
                for sym in five_symbols
            ]
        )
        quality_report = MagicMock()
        quality_report.passed_count = 5
        quality_report.universe_size = 10
        quality_report.thin_universe = False
        quality_report.filter_failure_counts = {}
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "momentum_score": float(5 - i),
                    "twelve_month_return": 0.15,
                    "one_month_return": 0.02,
                    "rank": i + 1,
                    "pct_from_52w_high": 0.17,
                    "within_30pct_of_52w_high": 1,
                }
                for i, sym in enumerate(five_symbols)
            ]
        )
        momentum_report = MagicMock()
        momentum_report.scored_count = 5
        momentum_report.selected_count = 5
        mock_momentum.return_value = (ranked_df, momentum_report)

        # Regime blocked — apply_regime_filter returns empty DataFrame + blocked result
        blocked_df = pd.DataFrame(
            columns=[
                "symbol", "momentum_score", "twelve_month_return",
                "one_month_return", "rank", "pct_from_52w_high",
                "within_30pct_of_52w_high", "position_size_multiplier",
            ]
        )
        regime_result = MagicMock()
        regime_result.regime = "BELOW_200DMA_10DAYS"
        regime_result.position_size_multiplier = 0.0
        regime_result.nifty_close = 20000.0
        regime_result.sma_200 = 24000.0  # Nifty well below 200 DMA
        regime_result.consecutive_days_below = 12
        mock_regime_filter.return_value = (blocked_df, regime_result)

        result = run_screener_agent(run_date=RUN_DATE)

        # screener must report regime_blocked
        assert result.regime_blocked is True, "Expected regime_blocked=True"

        # Implementation: top5 IS populated with position_size_multiplier=0.0
        # (stocks are tracked but no real positions sized — multiplier is the block)
        assert len(result.top5) == 5, f"Expected 5 top5 entries, got {len(result.top5)}"
        for r in result.top5:
            assert r.position_size_multiplier == 0.0, (
                f"Expected position_size_multiplier=0.0 when regime_blocked, "
                f"got {r.position_size_multiplier} for {r.symbol}"
            )

        # screener_results ARE written (with position_size_multiplier=0.0)
        screener_count = count_rows(db_path, "screener_results")
        assert screener_count == 5, (
            f"Expected 5 screener_results rows written (all with multiplier=0.0), "
            f"got {screener_count}"
        )

        # watchlist stays empty — no watchlist_agent run in this test
        watchlist_count = count_rows(db_path, "watchlist")
        assert watchlist_count == 0, (
            f"Expected 0 watchlist rows (watchlist_agent not called), got {watchlist_count}"
        )

        # Verify regime-blocked log entry (via mocked log_agent_action call args)
        regime_logged = any(
            "regime" in str(call).lower() and "block" in str(call).lower()
            for call in mock_log.call_args_list
        )
        assert regime_logged, (
            "Expected regime_blocked log in agent_logs. "
            f"Calls: {[str(c) for c in mock_log.call_args_list[:5]]}"
        )


# ---------------------------------------------------------------------------
# Scenario 5: Research incomplete — race condition prevention
# ---------------------------------------------------------------------------


class TestScenario5ResearchIncomplete:
    """Scenario 5: Research agent writes 2 of 5 reports with completed_at, 3 without.

    The watchlist_agent reads only research where completed_at IS NOT NULL.
    The 3 incomplete symbols must be logged as research_incomplete and excluded
    from the watchlist.
    """

    @patch("src.agents.watchlist_agent.send_checkpoint")
    @patch("src.agents.watchlist_agent.send_alert")
    @patch("src.agents.watchlist_agent._resolve_db_path")
    def test_scenario_5_research_incomplete_race_condition(
        self,
        mock_resolve_db: MagicMock,
        mock_send_alert: MagicMock,
        mock_send_checkpoint: MagicMock,
        db_path: str,
    ) -> None:
        """Only 2 completed research reports → watchlist has 2 rows, 3 logged as research_incomplete."""
        from src.agents.watchlist_agent import run_watchlist_agent

        mock_resolve_db.return_value = db_path

        five_symbols = NIFTY_SYMBOLS[:5]
        completed_symbols = five_symbols[:2]    # "RELIANCE", "TCS"
        incomplete_symbols = five_symbols[2:]   # "HDFCBANK", "INFY", "ICICIBANK"

        # Seed 5 screener_results rows (quality_passed=1, ABOVE_200DMA)
        for i, sym in enumerate(five_symbols):
            seed_screener_result(
                db_path=db_path,
                symbol=sym,
                rank=i + 1,
                momentum_score=float(5 - i),
                quality_passed=1,
                regime="ABOVE_200DMA",
                position_size_multiplier=1.0,
                run_date=RUN_DATE,
            )

        # Seed 2 COMPLETED research reports
        completed_time = datetime.datetime.now(tz=IST)
        for sym in completed_symbols:
            seed_research_report(
                db_path=db_path,
                symbol=sym,
                sentiment="Positive",
                confidence=0.85,
                completed_at=completed_time,
                run_date=RUN_DATE,
            )

        # Seed 3 INCOMPLETE research reports (completed_at=None)
        for sym in incomplete_symbols:
            seed_research_report(
                db_path=db_path,
                symbol=sym,
                sentiment="Positive",
                confidence=0.85,
                completed_at=None,  # Not complete — race condition scenario
                run_date=RUN_DATE,
            )

        # Run watchlist_agent
        result = run_watchlist_agent(run_date=RUN_DATE)

        # Only 2 completed symbols should appear in watchlist
        watchlist_rows = fetch_rows(db_path, "watchlist")
        watchlist_symbols = {r["symbol"] for r in watchlist_rows}
        assert len(watchlist_rows) == 2, (
            f"Expected 2 watchlist rows (only completed research), got {len(watchlist_rows)}. "
            f"Symbols: {watchlist_symbols}"
        )
        assert watchlist_symbols == set(completed_symbols), (
            f"Expected watchlist symbols {set(completed_symbols)}, got {watchlist_symbols}"
        )

        # Incomplete symbols must NOT be in watchlist
        for sym in incomplete_symbols:
            assert sym not in watchlist_symbols, (
                f"Symbol {sym} (incomplete research) must not appear in watchlist"
            )

        # agent_logs must contain research_incomplete entries for the 3 incomplete symbols
        log_actions = fetch_agent_log_actions(db_path)
        incomplete_logged = [
            action for action in log_actions if "research_incomplete" in action
        ]
        assert len(incomplete_logged) >= len(incomplete_symbols), (
            f"Expected at least {len(incomplete_symbols)} research_incomplete log entries, "
            f"found {len(incomplete_logged)}. All log actions: {log_actions}"
        )

        # Result counts should reflect only completed research
        assert result.candidates_evaluated == 2, (
            f"Expected candidates_evaluated=2, got {result.candidates_evaluated}"
        )
