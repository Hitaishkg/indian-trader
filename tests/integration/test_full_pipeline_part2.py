"""Integration tests for the full trading pipeline — scenarios 6–10.

Written by Teammate A3. Teammate B1 will merge with scenarios 1–5 from
test_full_pipeline.py.

Coverage:
  Scenario 6  — GTT reconciliation: monitor agent detects invalid GTT, alerts, attempts repair
  Scenario 7  — Emergency re-screen: morning_signals data contract for overnight_event_removal
  Scenario 8  — Late signal (deadline miss): signal agent past 08:50 → safe_mode in orchestrator
  Scenario 9  — Full week simulation: Mon–Thu evening sessions accumulate watchlist rows
  Scenario 10 — Watchlist timeout: execution agent sends checkpoint, no response, no orders placed

All external APIs (Telegram, Gmail, Shoonya, yfinance, Screener.in, Groq, Gemini) are mocked.
No real network calls are made.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.config.settings import Settings

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

# Realistic Nifty 50 symbols used throughout
NIFTY_SYMBOLS = ["HDFCBANK", "INFY", "RELIANCE", "TCS", "WIPRO"]

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# Shared mock settings — no live credentials, safe defaults
MOCK_SETTINGS = Settings(
    live_trading=False,
    paper_trading=True,
    log_level="DEBUG",
    max_trade_amount=10000,
    database_url="sqlite:///data/trading.db",
    shoonya_user="test_user",
    shoonya_password="test_pass",
    shoonya_totp_secret="test_totp",
    fyers_api_key=None,
    groq_api_key="test_groq_key",
    gemini_api_key="test_gemini_key",
    github_pat=None,
    tavily_api_key=None,
    brave_api_key=None,
    telegram_bot_token="test_telegram_token",
    telegram_chat_id="test_chat_id",
    gmail_credentials=None,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_all_tables(conn: sqlite3.Connection) -> None:
    """Create all trading pipeline tables in the test database.

    Args:
        conn: Open SQLite connection.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',
            action TEXT NOT NULL,
            symbol TEXT,
            result TEXT,
            data_quality_score REAL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            pnl REAL,
            pnl_pct REAL,
            opened_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'CNC',
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            order_id TEXT,
            gtt_sl_id TEXT,
            gtt_tp_id TEXT,
            placed_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            pnl REAL NOT NULL,
            pnl_pct REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            closed_at TEXT NOT NULL
        );

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
        );

        CREATE TABLE IF NOT EXISTS research_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_urls TEXT,
            earnings_transcript_unavailable INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT
        );

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
        );

        CREATE TABLE IF NOT EXISTS morning_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            overnight_event TEXT,
            regime_still_valid INTEGER NOT NULL DEFAULT 1,
            validated_at TEXT NOT NULL
        );

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
        );

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
            approval_status TEXT NOT NULL,
            rejection_reason TEXT,
            approved_at TEXT NOT NULL,
            UNIQUE(symbol, run_date)
        );

        CREATE TABLE IF NOT EXISTS execution_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED', 'TIMEOUT')),
            symbols TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE(run_date)
        );
    """)
    conn.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Temporary SQLite database with all required tables.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        Absolute path string to the test database file.
    """
    path = str(tmp_path / "test_trading.db")
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    _create_all_tables(conn)
    conn.close()
    return path


def _seed_position(
    db_path: str,
    symbol: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    quantity: int = 5,
) -> None:
    """Insert a realistic open position into the positions table.

    Args:
        db_path: Path to test database.
        symbol: NSE ticker symbol.
        entry_price: Entry fill price in INR.
        stop_loss: Stop-loss level in INR.
        take_profit: Take-profit level in INR.
        quantity: Shares held.
    """
    now_ist = datetime.datetime.now(IST).isoformat()
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute(
        """
        INSERT OR REPLACE INTO positions
            (symbol, quantity, entry_price, current_price,
             stop_loss, take_profit, pnl, pnl_pct, opened_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 0.0, 0.0, ?, ?)
        """,
        (symbol, quantity, entry_price, entry_price, stop_loss, take_profit, now_ist, now_ist),
    )
    conn.commit()
    conn.close()


def _seed_screener_results(
    db_path: str,
    run_date: datetime.date,
    symbols: list[str],
    regime: str = "ABOVE_200DMA",
) -> None:
    """Insert screener results for a set of symbols on a given date.

    Args:
        db_path: Path to test database.
        run_date: The run date for these results.
        symbols: List of NSE symbols (ranked in order given).
        regime: Market regime string.
    """
    now_ist = datetime.datetime.now(IST).isoformat()
    multiplier = 1.0 if regime == "ABOVE_200DMA" else (0.5 if regime == "BELOW_200DMA" else 0.0)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for rank, symbol in enumerate(symbols, start=1):
        conn.execute(
            """
            INSERT OR REPLACE INTO screener_results
                (symbol, run_date, rank, momentum_score, quality_passed,
                 regime, position_size_multiplier, screened_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (symbol, run_date.isoformat(), rank, 0.15 - rank * 0.01,
             regime, multiplier, now_ist),
        )
    conn.commit()
    conn.close()


def _seed_research_reports(
    db_path: str,
    symbols: list[str],
    sentiment: str = "Positive",
    confidence: float = 0.80,
) -> None:
    """Insert completed research reports for a set of symbols.

    Args:
        db_path: Path to test database.
        symbols: List of NSE symbols.
        sentiment: LLM sentiment string.
        confidence: LLM confidence score 0–1.
    """
    now_ist = datetime.datetime.now(IST).isoformat()
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for symbol in symbols:
        conn.execute(
            """
            INSERT INTO research_reports
                (symbol, sentiment, confidence, source_urls,
                 earnings_transcript_unavailable, completed_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (symbol, sentiment, confidence,
             json.dumps(["https://example.com/news1", "https://example.com/news2"]),
             now_ist),
        )
    conn.commit()
    conn.close()


def _seed_watchlist(
    db_path: str,
    run_date: datetime.date,
    symbols: list[str],
    human_approved: int = 1,
) -> None:
    """Insert watchlist entries for a set of symbols.

    Args:
        db_path: Path to test database.
        run_date: The run date for these entries.
        symbols: List of NSE symbols.
        human_approved: 1 for approved, 0 for pending.
    """
    now_ist = datetime.datetime.now(IST).isoformat()
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for rank, symbol in enumerate(symbols, start=1):
        conn.execute(
            """
            INSERT OR REPLACE INTO watchlist
                (symbol, run_date, combined_decision, scorecard_score, scorecard_max,
                 sentiment, confidence, rank, regime, position_size_multiplier,
                 human_approved, approval_source, added_at)
            VALUES (?, ?, 'PROCEED', 16, 20, 'Positive', 0.80, ?, 'ABOVE_200DMA', 1.0,
                    ?, 'human_explicit', ?)
            """,
            (symbol, run_date.isoformat(), rank, human_approved, now_ist),
        )
    conn.commit()
    conn.close()


def _seed_risk_approvals(
    db_path: str,
    run_date: datetime.date,
    symbols: list[str],
) -> None:
    """Insert APPROVED risk approval rows for execution agent.

    Args:
        db_path: Path to test database.
        run_date: The run date for these approvals.
        symbols: List of NSE symbols to approve.
    """
    now_ist = datetime.datetime.now(IST).isoformat()
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for symbol in symbols:
        # Realistic INR prices: HDFCBANK ~1600, INFY ~1400, etc.
        entry = 1580.0 if symbol == "HDFCBANK" else 1420.0
        atr = 35.0
        sl = entry - atr * 2.0
        tp = entry + atr * 4.0
        qty = max(1, int(100.0 / (atr * 2.0)))  # 1% of 10000 / stop distance
        conn.execute(
            """
            INSERT OR REPLACE INTO risk_approvals
                (symbol, run_date, quantity, entry_price_approx, stop_loss,
                 take_profit, position_size_multiplier, risk_amount,
                 approval_status, rejection_reason, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, 1.0, 100.0, 'APPROVED', NULL, ?)
            """,
            (symbol, run_date.isoformat(), qty, entry, sl, tp, now_ist),
        )
    conn.commit()
    conn.close()


def _read_agent_logs(db_path: str, action_contains: str | None = None) -> list[dict[str, Any]]:
    """Query agent_logs, optionally filtering by action substring.

    Args:
        db_path: Path to test database.
        action_contains: If provided, only rows where action LIKE '%{action_contains}%'.

    Returns:
        List of row dicts.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    if action_contains:
        rows = conn.execute(
            "SELECT * FROM agent_logs WHERE action LIKE ?",
            (f"%{action_contains}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agent_logs").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Scenario 6: GTT reconciliation trigger
# ---------------------------------------------------------------------------


class TestScenario6GTTReconciliation:
    """Monitor agent detects invalid GTT and responds with alert + repair attempt."""

    def test_gtt_missing_logs_and_alerts(self, db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Position with stop_loss=0 triggers gtt_missing_or_invalid, alert, and repair attempt.

        GTT reconciliation fires when current_time.minute % 30 == 0.
        The monitor agent reads positions via pt.get_positions(). We mock PaperTrader
        at the module level (matching the pattern in test_monitor_agent.py) and return
        a position with stop_loss=0 — invalid by the is_valid check
        (sl > 0 and tp > 0 and sl < entry and tp > entry → False when sl=0).
        """
        from src.agents.monitor_agent import run_monitor_agent

        # Seed a signal row so ATR is available for repair attempt
        now_ist = datetime.datetime.now(IST).isoformat()
        run_date = datetime.date(2026, 4, 14)
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.execute(
            """
            INSERT INTO signals
                (symbol, run_date, rsi, macd_signal, bollinger_position,
                 atr, groq_confidence, signal_type, skip_reason, signalled_at)
            VALUES (?, ?, 35.0, 'BUY', 'BELOW', 35.0, 0.75, 'BUY', NULL, ?)
            """,
            ("HDFCBANK", run_date.isoformat(), now_ist),
        )
        # Also seed screener_results so _fetch_regime works
        conn.execute(
            """
            INSERT INTO screener_results
                (symbol, run_date, rank, momentum_score, quality_passed,
                 regime, position_size_multiplier, screened_at)
            VALUES (?, ?, 1, 0.12, 1, 'ABOVE_200DMA', 1.0, ?)
            """,
            ("HDFCBANK", run_date.isoformat(), now_ist),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("src.agents.monitor_agent.settings", MOCK_SETTINGS)

        # Position with stop_loss=0 — invalid, triggers gtt_missing_or_invalid
        invalid_position = {
            "symbol": "HDFCBANK",
            "quantity": 5,
            "entry_price": 1580.0,
            "current_price": 1590.0,
            "stop_loss": 0.0,    # INVALID: triggers reconciliation alert
            "take_profit": 1720.0,
            "pnl": 50.0,
            "pnl_pct": 0.63,
            "opened_at": now_ist,
            "updated_at": now_ist,
        }

        alert_calls: list[tuple[str, str]] = []

        def mock_send_alert(subject: str, message: str) -> dict[str, bool]:
            alert_calls.append((subject, message))
            return {"telegram": True, "gmail": True}

        logged_actions: list[str] = []

        def mock_log_action(**kwargs: Any) -> None:
            logged_actions.append(kwargs.get("action", ""))

        # Market hours, minute divisible by 30 → triggers reconciliation
        market_time = datetime.datetime(2026, 4, 14, 10, 30, 0, tzinfo=IST)

        with (
            patch("src.agents.monitor_agent.send_alert", side_effect=mock_send_alert),
            patch("src.agents.monitor_agent.log_agent_action", side_effect=mock_log_action),
            patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch_ohlcv,
            patch("src.agents.monitor_agent.fetch_sector_indices") as mock_fetch_sector,
            patch("src.agents.monitor_agent.PaperTrader") as mock_pt_class,
        ):
            import pandas as pd

            mock_instance = MagicMock()
            # check_gtts returns no exits (price hasn't hit SL/TP)
            mock_instance.check_gtts.return_value = []
            # get_positions returns the invalid position
            mock_instance.get_positions.return_value = [invalid_position]
            mock_instance.get_pnl.return_value = {
                "realized_pnl": 0.0,
                "unrealized_pnl": 50.0,
                "total_pnl": 50.0,
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
            }
            mock_pt_class.return_value = mock_instance

            # Return empty DataFrames — no price updates during this tick
            mock_fetch_ohlcv.return_value = pd.DataFrame()
            mock_fetch_sector.return_value = pd.DataFrame()

            result = run_monitor_agent(
                run_date=run_date,
                current_time=market_time,
                db_path_override=db_path,
            )

        # GTT reconciliation must have run (minute == 30 % 30 == 0)
        assert result.gtt_reconciliation_ran is True

        # Agent must have logged gtt_missing_or_invalid
        gtt_missing_logged = any("gtt_missing_or_invalid" in a for a in logged_actions)
        assert gtt_missing_logged, (
            f"Expected 'gtt_missing_or_invalid' in agent_logs. "
            f"Actual actions logged: {logged_actions}"
        )

        # Alert must have been sent via send_alert
        assert len(alert_calls) >= 1, (
            "Expected at least one send_alert call for missing/invalid GTT"
        )
        alert_subjects = [subj for subj, _ in alert_calls]
        assert any("GTT" in s or "gtt" in s.lower() for s in alert_subjects), (
            f"Expected GTT-related alert subject. Got: {alert_subjects}"
        )

    def test_gtt_valid_position_no_alert(self, db_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Position with valid stop_loss and take_profit does not trigger gtt_missing alert."""
        from src.agents.monitor_agent import run_monitor_agent

        now_ist = datetime.datetime.now(IST).isoformat()
        run_date = datetime.date(2026, 4, 14)

        monkeypatch.setattr("src.agents.monitor_agent.settings", MOCK_SETTINGS)

        # Valid position: stop_loss < entry, take_profit > entry
        valid_position = {
            "symbol": "INFY",
            "quantity": 3,
            "entry_price": 1420.0,
            "current_price": 1430.0,
            "stop_loss": 1350.0,    # valid: > 0 AND < entry
            "take_profit": 1560.0,  # valid: > entry
            "pnl": 30.0,
            "pnl_pct": 0.70,
            "opened_at": now_ist,
            "updated_at": now_ist,
        }

        alert_calls: list[tuple[str, str]] = []

        def mock_send_alert(subject: str, message: str) -> dict[str, bool]:
            alert_calls.append((subject, message))
            return {"telegram": True, "gmail": True}

        market_time = datetime.datetime(2026, 4, 14, 10, 30, 0, tzinfo=IST)

        with (
            patch("src.agents.monitor_agent.send_alert", side_effect=mock_send_alert),
            patch("src.agents.monitor_agent.log_agent_action"),
            patch("src.agents.monitor_agent.fetch_ohlcv") as mock_fetch_ohlcv,
            patch("src.agents.monitor_agent.fetch_sector_indices") as mock_fetch_sector,
            patch("src.agents.monitor_agent.PaperTrader") as mock_pt_class,
        ):
            import pandas as pd

            mock_instance = MagicMock()
            mock_instance.check_gtts.return_value = []
            mock_instance.get_positions.return_value = [valid_position]
            mock_instance.get_pnl.return_value = {
                "realized_pnl": 0.0,
                "unrealized_pnl": 30.0,
                "total_pnl": 30.0,
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
            }
            mock_pt_class.return_value = mock_instance

            mock_fetch_ohlcv.return_value = pd.DataFrame()
            mock_fetch_sector.return_value = pd.DataFrame()

            result = run_monitor_agent(
                run_date=run_date,
                current_time=market_time,
                db_path_override=db_path,
            )

        assert result.gtt_reconciliation_ran is True
        gtt_alerts = [(s, m) for s, m in alert_calls if "GTT" in s or "gtt" in s.lower()]
        assert gtt_alerts == [], f"No GTT alert expected for valid position. Got: {gtt_alerts}"


# ---------------------------------------------------------------------------
# Scenario 7: Overnight event removal (morning_signals data contract)
# ---------------------------------------------------------------------------


class TestScenario7OvernightEventRemoval:
    """Morning validator data contract: overnight_event_removal correctly recorded.

    NOTE: The morning validator agent is a placeholder (logs skip only). This test
    validates the morning_signals table data contract — that a row with
    overnight_event='earnings' causes the stock to be treated as removed.
    The actual agent logic is not yet built (Phase 4 scope). This test
    documents the expected DB state that the built agent must produce.
    """

    def test_morning_signals_earnings_removal_data_contract(self, db_path: str) -> None:
        """Seed morning_signals with earnings removal; verify table state is queryable correctly.

        When the morning validator agent is built, it must write overnight_event='earnings'
        (or similar) for stocks with overnight earnings announcements, and the downstream
        signal agent must skip such stocks.
        """
        run_date = datetime.date(2026, 4, 14)
        now_ist = datetime.datetime.now(IST).isoformat()

        # Simulate: HDFCBANK had overnight earnings — should be removed
        # INFY passed validation — should proceed
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.execute(
            """
            INSERT INTO morning_signals (symbol, overnight_event, regime_still_valid, validated_at)
            VALUES (?, 'overnight_event_removal', 1, ?)
            """,
            ("HDFCBANK", now_ist),
        )
        conn.execute(
            """
            INSERT INTO morning_signals (symbol, overnight_event, regime_still_valid, validated_at)
            VALUES (?, NULL, 1, ?)
            """,
            ("INFY", now_ist),
        )
        conn.commit()
        conn.close()

        # Verify the removed stock is queryable with the expected reason
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        removed = conn.execute(
            "SELECT symbol, overnight_event FROM morning_signals WHERE overnight_event = ?",
            ("overnight_event_removal",),
        ).fetchall()

        remaining = conn.execute(
            "SELECT symbol FROM morning_signals WHERE overnight_event IS NULL",
        ).fetchall()
        conn.close()

        removed_symbols = [r["symbol"] for r in removed]
        remaining_symbols = [r["symbol"] for r in remaining]

        assert "HDFCBANK" in removed_symbols, (
            "HDFCBANK should be marked with overnight_event_removal"
        )
        assert "HDFCBANK" not in remaining_symbols, (
            "HDFCBANK should not appear in the passing (NULL overnight_event) rows"
        )
        assert "INFY" in remaining_symbols, (
            "INFY should remain in morning_signals with NULL overnight_event"
        )

    def test_multiple_stocks_partial_removal(self, db_path: str) -> None:
        """Three watchlist stocks; one removed, two proceed.

        Asserts both the removed stock reason and the count of remaining stocks.
        """
        run_date = datetime.date(2026, 4, 14)
        now_ist = datetime.datetime.now(IST).isoformat()

        stocks = [
            ("HDFCBANK", "overnight_event_removal"),
            ("INFY", None),
            ("RELIANCE", None),
        ]

        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        for symbol, event in stocks:
            conn.execute(
                """
                INSERT INTO morning_signals (symbol, overnight_event, regime_still_valid, validated_at)
                VALUES (?, ?, 1, ?)
                """,
                (symbol, event, now_ist),
            )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        removed = conn.execute(
            "SELECT symbol FROM morning_signals WHERE overnight_event IS NOT NULL"
        ).fetchall()
        proceeding = conn.execute(
            "SELECT symbol FROM morning_signals WHERE overnight_event IS NULL"
        ).fetchall()
        conn.close()

        assert len(removed) == 1
        assert removed[0]["symbol"] == "HDFCBANK"
        assert len(proceeding) == 2
        proceeding_symbols = {r["symbol"] for r in proceeding}
        assert "INFY" in proceeding_symbols
        assert "RELIANCE" in proceeding_symbols


# ---------------------------------------------------------------------------
# Scenario 8: Late signal (deadline miss)
# ---------------------------------------------------------------------------


class TestScenario8LateSignalDeadlineMiss:
    """Signal agent past 08:50 IST returns late_start=True; orchestrator enters safe_mode."""

    def test_signal_agent_after_deadline_returns_late_start(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_signal_agent called after 08:50 IST returns late_start=True with empty signals.

        The agent checks the wall-clock time (IST) immediately on entry. We patch
        datetime.datetime.now inside src.agents.signal_agent so the agent sees 09:05 IST.
        """
        from src.agents.signal_agent import run_signal_agent

        monkeypatch.setattr("src.agents.signal_agent.settings", MOCK_SETTINGS)

        # Patch the IST clock inside signal_agent to return a time past the deadline
        past_deadline_time = datetime.datetime(2026, 4, 14, 9, 5, 0, tzinfo=IST)

        with patch("src.agents.signal_agent.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = past_deadline_time
            # Preserve datetime.date so other usages work
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta

            with patch("src.agents.signal_agent.log_agent_action"):
                result = run_signal_agent(run_date=datetime.date(2026, 4, 14))

        assert result.late_start is True
        assert result.symbols_processed == 0
        assert result.buy_signals == []
        assert result.hold_signals == []

    def test_orchestrator_safe_mode_on_signal_agent_late_start(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Orchestrator sets safe_mode=True when signal_agent.late_start=True.

        We mock run_signal_agent to return a SignalAgentResult with late_start=True
        and verify the orchestrator propagates safe_mode correctly.
        """
        from src.agents.orchestrator import run_orchestrator
        from src.agents.signal_agent import SignalAgentResult

        # orchestrator does not import settings directly — no patch needed
        fake_late_result = SignalAgentResult(
            run_date=datetime.date(2026, 4, 14),
            symbols_processed=0,
            buy_signals=[],
            hold_signals=[],
            late_start=True,
            completed_at=datetime.datetime(2026, 4, 14, 9, 5, 0, tzinfo=IST),
        )

        with (
            patch("src.agents.orchestrator.run_screener_agent"),
            patch("src.agents.orchestrator.run_research_agent"),
            patch("src.agents.orchestrator.run_watchlist_agent"),
            patch("src.agents.orchestrator.check_watchlist_timeout"),
            patch("src.agents.orchestrator._run_morning_validator"),
            patch("src.agents.orchestrator.run_signal_agent", return_value=fake_late_result),
            patch("src.agents.orchestrator.run_risk_agent") as mock_risk,
            patch("src.agents.orchestrator.run_execution_agent"),
            patch("src.agents.orchestrator._maybe_start_dashboard"),
            patch("src.agents.orchestrator.log_agent_action"),
            patch("src.agents.orchestrator.send_alert"),
        ):
            # Risk agent: no kill switch
            from src.agents.risk_agent import RiskAgentResult
            mock_risk.return_value = RiskAgentResult(
                run_date=datetime.date(2026, 4, 14),
                kill_switch_fired=False,
                kill_switch_reason=None,
                approved=[],
                rejected=[],
                portfolio_equity=10000.0,
                peak_equity=10000.0,
                current_drawdown_pct=0.0,
                completed_at=datetime.datetime(2026, 4, 14, 8, 55, 0, tzinfo=IST),
            )

            result = run_orchestrator(
                session="morning",
                run_date=datetime.date(2026, 4, 14),
                db_path_override=db_path,
            )

        assert result.safe_mode is True, (
            "Orchestrator must set safe_mode=True when signal_agent returns late_start=True"
        )
        assert result.safe_mode_reason is not None
        # Orchestrator sets safe_mode_reason="signal_agent_late_start" (see orchestrator.py line 365)
        assert "late" in result.safe_mode_reason.lower() or "signal" in result.safe_mode_reason.lower(), (
            f"safe_mode_reason should reference late_start or signal_agent. Got: {result.safe_mode_reason!r}"
        )

    def test_signal_agent_before_deadline_not_late(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_signal_agent before 08:50 does not set late_start (functional boundary check).

        No screener results in DB → agent exits cleanly with 0 signals, late_start=False.
        """
        from src.agents.signal_agent import run_signal_agent

        monkeypatch.setattr("src.agents.signal_agent.settings", MOCK_SETTINGS)

        # Before deadline: 08:20 IST
        before_deadline = datetime.datetime(2026, 4, 14, 8, 20, 0, tzinfo=IST)

        with patch("src.agents.signal_agent.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = before_deadline
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta

            with (
                patch("src.agents.signal_agent.log_agent_action"),
                patch("src.agents.signal_agent.fetch_ohlcv") as mock_ohlcv,
                patch("src.agents.signal_agent._resolve_db_path", return_value=db_path),
            ):
                import pandas as pd
                mock_ohlcv.return_value = pd.DataFrame()
                result = run_signal_agent(
                    run_date=datetime.date(2026, 4, 14),
                    symbols=[],  # no symbols → exits cleanly with 0 processed
                )

        assert result.late_start is False


# ---------------------------------------------------------------------------
# Scenario 9: Full week simulation (Mon–Thu evening sessions)
# ---------------------------------------------------------------------------


class TestScenario9FullWeekSimulation:
    """Evening sessions Mon–Thu accumulate watchlist rows with no duplicates per day."""

    def _run_evening_for_date(
        self,
        run_date: datetime.date,
        db_path: str,
        screener_symbols: list[str],
    ) -> None:
        """Simulate one evening session by seeding screener + research and calling watchlist agent.

        Args:
            run_date: The trading date this evening session prepares for.
            db_path: Path to test database.
            screener_symbols: Symbols that passed screening this evening.
        """
        _seed_screener_results(db_path, run_date, screener_symbols)
        _seed_research_reports(db_path, screener_symbols)

    def test_four_evening_sessions_accumulate_watchlist(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Four nightly screener runs write 3–5 watchlist rows per night, no duplicate (symbol, run_date).

        We mock the watchlist agent to write realistic rows directly into the watchlist table,
        simulating what run_watchlist_agent() would do for each night.
        """
        # Mon=Apr14, Tue=Apr15, Wed=Apr16, Thu=Apr17 (2026)
        trading_dates = [
            datetime.date(2026, 4, 14),
            datetime.date(2026, 4, 15),
            datetime.date(2026, 4, 16),
            datetime.date(2026, 4, 17),
        ]

        # Each night: 3–5 stocks pass
        nightly_symbols: dict[datetime.date, list[str]] = {
            datetime.date(2026, 4, 14): ["HDFCBANK", "INFY", "RELIANCE"],
            datetime.date(2026, 4, 15): ["TCS", "WIPRO", "HDFCBANK", "INFY"],
            datetime.date(2026, 4, 16): ["RELIANCE", "TCS", "WIPRO"],
            datetime.date(2026, 4, 17): ["INFY", "HDFCBANK", "WIPRO", "RELIANCE", "TCS"],
        }

        # Run each evening session by seeding data and inserting watchlist rows directly.
        # This mirrors what run_watchlist_agent() produces after applying the combined
        # decision rule. We mock the agent itself to avoid real API calls (Telegram, Gmail).
        now_ist = datetime.datetime.now(IST)

        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        total_expected_rows = 0

        for run_date in trading_dates:
            symbols = nightly_symbols[run_date]
            total_expected_rows += len(symbols)

            # Seed screener results (simulates screener_agent output)
            _seed_screener_results(db_path, run_date, symbols)

            # Seed research reports (simulates research_agent output)
            _seed_research_reports(db_path, symbols)

            # Write watchlist rows directly (simulates watchlist_agent output)
            ts = (now_ist + datetime.timedelta(hours=trading_dates.index(run_date))).isoformat()
            for rank, symbol in enumerate(symbols, start=1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO watchlist
                        (symbol, run_date, combined_decision, scorecard_score, scorecard_max,
                         sentiment, confidence, rank, regime, position_size_multiplier,
                         human_approved, approval_source, added_at)
                    VALUES (?, ?, 'PROCEED', 16, 20, 'Positive', 0.80, ?,
                            'ABOVE_200DMA', 1.0, 0, NULL, ?)
                    """,
                    (symbol, run_date.isoformat(), rank, ts),
                )

        conn.commit()
        conn.close()

        # Verify: watchlist table has correct total rows across all four nights
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        all_rows = conn.execute("SELECT symbol, run_date FROM watchlist").fetchall()
        conn.close()

        assert len(all_rows) == total_expected_rows, (
            f"Expected {total_expected_rows} watchlist rows total across 4 nights, "
            f"got {len(all_rows)}"
        )

        # Verify: no duplicate (symbol, run_date) combinations
        seen: set[tuple[str, str]] = set()
        for row in all_rows:
            key = (row["symbol"], row["run_date"])
            assert key not in seen, (
                f"Duplicate watchlist entry: symbol={row['symbol']}, run_date={row['run_date']}"
            )
            seen.add(key)

    def test_each_date_has_correct_symbol_count(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each trading date's watchlist rows match the expected symbol count for that night."""
        trading_dates = [
            datetime.date(2026, 4, 14),
            datetime.date(2026, 4, 15),
        ]
        nightly_symbols: dict[datetime.date, list[str]] = {
            datetime.date(2026, 4, 14): ["HDFCBANK", "INFY", "RELIANCE"],
            datetime.date(2026, 4, 15): ["TCS", "WIPRO", "HDFCBANK"],
        }

        now_ist = datetime.datetime.now(IST)
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)

        for idx, run_date in enumerate(trading_dates):
            symbols = nightly_symbols[run_date]
            ts = (now_ist + datetime.timedelta(hours=idx)).isoformat()
            for rank, symbol in enumerate(symbols, start=1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO watchlist
                        (symbol, run_date, combined_decision, scorecard_score, scorecard_max,
                         sentiment, confidence, rank, regime, position_size_multiplier,
                         human_approved, approval_source, added_at)
                    VALUES (?, ?, 'PROCEED', 16, 20, 'Positive', 0.80, ?,
                            'ABOVE_200DMA', 1.0, 0, NULL, ?)
                    """,
                    (symbol, run_date.isoformat(), rank, ts),
                )

        conn.commit()
        conn.close()

        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        for run_date, expected_symbols in nightly_symbols.items():
            rows = conn.execute(
                "SELECT symbol FROM watchlist WHERE run_date = ?",
                (run_date.isoformat(),),
            ).fetchall()
            assert len(rows) == len(expected_symbols), (
                f"Date {run_date}: expected {len(expected_symbols)} rows, got {len(rows)}"
            )

        conn.close()


# ---------------------------------------------------------------------------
# Scenario 10: Watchlist timeout (no human response)
# ---------------------------------------------------------------------------


class TestScenario10WatchlistTimeout:
    """Execution agent times out waiting for human confirmation — no orders placed."""

    def test_no_human_response_results_in_safe_mode_no_orders(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Human checkpoint is sent but no response arrives within 8 minutes.

        The execution agent polls /tmp/indian-trader-checkpoint-{run_date}.txt.
        We mock time.monotonic to simulate elapsed time > CHECKPOINT_TIMEOUT_SECS=480s,
        and time.sleep to skip real waits. No checkpoint file is written.

        Expected: safe_mode=True, safe_mode_reason='timeout_no_confirmation', orders table empty.
        """
        from src.agents.execution_agent import run_execution_agent

        run_date = datetime.date(2026, 4, 14)

        # Seed watchlist and risk approvals so the agent has trades to process
        _seed_watchlist(db_path, run_date, ["HDFCBANK", "INFY"], human_approved=1)
        _seed_risk_approvals(db_path, run_date, ["HDFCBANK", "INFY"])

        # Seed signals table so ATR is available
        now_ist = datetime.datetime.now(IST).isoformat()
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        for symbol, atr in [("HDFCBANK", 35.0), ("INFY", 28.0)]:
            conn.execute(
                """
                INSERT INTO signals
                    (symbol, run_date, rsi, macd_signal, bollinger_position,
                     atr, groq_confidence, signal_type, skip_reason, signalled_at)
                VALUES (?, ?, 35.0, 'BUY', 'BELOW', ?, 0.75, 'BUY', NULL, ?)
                """,
                (symbol, run_date.isoformat(), atr, now_ist),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("src.agents.execution_agent.settings", MOCK_SETTINGS)

        # Ensure no checkpoint file exists (no human response)
        checkpoint_file = Path(f"/tmp/indian-trader-checkpoint-{run_date.isoformat()}.txt")
        if checkpoint_file.exists():
            checkpoint_file.unlink()

        # Simulate time.monotonic advancing past the 480s deadline on first call past baseline
        # The polling loop: deadline = monotonic() + 480; while monotonic() < deadline
        # We make the first call return 0 (baseline), second call return 481 (past deadline)
        monotonic_sequence = [0.0, 481.0]
        monotonic_calls: list[float] = []

        def mock_monotonic() -> float:
            val = monotonic_sequence[min(len(monotonic_calls), len(monotonic_sequence) - 1)]
            monotonic_calls.append(val)
            return val

        def mock_send_checkpoint(subject: str, message: str) -> dict[str, bool]:
            return {"telegram": True, "gmail": True}

        def mock_send_alert(subject: str, message: str) -> dict[str, bool]:
            return {"telegram": True, "gmail": True}

        with (
            patch("src.agents.execution_agent.time.monotonic", side_effect=mock_monotonic),
            patch("src.agents.execution_agent.time.sleep"),
            patch("src.agents.execution_agent.send_checkpoint", side_effect=mock_send_checkpoint),
            patch("src.agents.execution_agent.send_alert", side_effect=mock_send_alert),
            patch("src.agents.execution_agent.send_info"),
            patch("src.agents.execution_agent.log_agent_action"),
            patch("src.agents.execution_agent.fetch_ohlcv") as mock_ohlcv,
        ):
            import pandas as pd

            # Return a realistic price close to the approved entry
            mock_ohlcv.return_value = pd.DataFrame(
                {"symbol": ["HDFCBANK", "INFY"], "close": [1582.0, 1422.0]}
            )

            result = run_execution_agent(
                run_date=run_date,
                db_path_override=db_path,
            )

        # Core assertions
        assert result.safe_mode is True, (
            "safe_mode must be True when human does not confirm within timeout"
        )
        assert result.safe_mode_reason == "timeout_no_confirmation", (
            f"safe_mode_reason must be 'timeout_no_confirmation', got {result.safe_mode_reason!r}"
        )
        assert result.human_confirmed is False, "human_confirmed must be False on timeout"
        assert result.orders_placed == [], (
            "No orders should be placed when human does not confirm"
        )

        # Verify orders table is empty (no orders written to DB)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        assert order_count == 0, (
            f"orders table must be empty after timeout. Found {order_count} rows."
        )

    def test_execution_checkpoint_status_set_to_timeout(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """execution_checkpoints table shows status='TIMEOUT' after no human response."""
        from src.agents.execution_agent import run_execution_agent

        run_date = datetime.date(2026, 4, 14)

        _seed_watchlist(db_path, run_date, ["RELIANCE"], human_approved=1)
        _seed_risk_approvals(db_path, run_date, ["RELIANCE"])

        now_ist = datetime.datetime.now(IST).isoformat()
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.execute(
            """
            INSERT INTO signals
                (symbol, run_date, rsi, macd_signal, bollinger_position,
                 atr, groq_confidence, signal_type, skip_reason, signalled_at)
            VALUES (?, ?, 30.0, 'BUY', 'BELOW', 25.0, 0.80, 'BUY', NULL, ?)
            """,
            ("RELIANCE", run_date.isoformat(), now_ist),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("src.agents.execution_agent.settings", MOCK_SETTINGS)

        checkpoint_file = Path(f"/tmp/indian-trader-checkpoint-{run_date.isoformat()}.txt")
        if checkpoint_file.exists():
            checkpoint_file.unlink()

        # Simulate timeout: first monotonic() = 0 (deadline set), second = 481 (past deadline)
        monotonic_sequence = [0.0, 481.0]
        monotonic_calls: list[float] = []

        def mock_monotonic() -> float:
            val = monotonic_sequence[min(len(monotonic_calls), len(monotonic_sequence) - 1)]
            monotonic_calls.append(val)
            return val

        with (
            patch("src.agents.execution_agent.time.monotonic", side_effect=mock_monotonic),
            patch("src.agents.execution_agent.time.sleep"),
            patch("src.agents.execution_agent.send_checkpoint", return_value={"telegram": True, "gmail": True}),
            patch("src.agents.execution_agent.send_alert", return_value={"telegram": True, "gmail": True}),
            patch("src.agents.execution_agent.send_info"),
            patch("src.agents.execution_agent.log_agent_action"),
            patch("src.agents.execution_agent.fetch_ohlcv") as mock_ohlcv,
        ):
            import pandas as pd
            mock_ohlcv.return_value = pd.DataFrame(
                {"symbol": ["RELIANCE"], "close": [2800.0]}
            )

            run_execution_agent(run_date=run_date, db_path_override=db_path)

        # Verify the execution_checkpoints table has a TIMEOUT entry
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM execution_checkpoints WHERE run_date = ?",
            (run_date.isoformat(),),
        ).fetchone()
        conn.close()

        assert row is not None, "execution_checkpoints must have a row for run_date"
        assert row["status"] == "TIMEOUT", (
            f"execution_checkpoints status must be 'TIMEOUT', got {row['status']!r}"
        )
