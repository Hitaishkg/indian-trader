"""Tests for src/agents/morning_validator_agent.py.

Tests the morning validation logic, DB patterns, deadline enforcement, and
API integrations. External calls (Tavily, Gemini, OHLCV, regime) are mocked
at the module level.
"""

from __future__ import annotations

import datetime
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.agents.morning_validator_agent import (
    run_morning_validator_agent,
    MorningValidatorResult,
    MorningValidatorError,
    MaterialEventVerdict,
    AGENT_NAME,
    DEADLINE_HOUR,
    DEADLINE_MINUTE,
    NEWS_LOOKBACK_HOURS,
    TAVILY_MAX_RESULTS,
)

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Temporary SQLite database path for test isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        yield db_path


def _setup_watchlist(db_path: str, stocks: list[dict]) -> None:
    """Setup watchlist table with test data.

    Args:
        db_path: Database file path.
        stocks: List of dicts with keys symbol, sentiment, confidence, rank,
                regime, position_size_multiplier, scorecard_score, scorecard_max.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            sentiment TEXT,
            confidence REAL,
            rank INTEGER,
            regime TEXT,
            position_size_multiplier REAL,
            scorecard_score REAL,
            scorecard_max REAL,
            human_approved INTEGER DEFAULT 0,
            watchlist_date TEXT
        )
    """)
    for stock in stocks:
        conn.execute("""
            INSERT INTO watchlist
            (symbol, run_date, sentiment, confidence, rank, regime,
             position_size_multiplier, scorecard_score, scorecard_max, human_approved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stock["symbol"],
            stock.get("run_date", "2026-05-17"),
            stock.get("sentiment", "Positive"),
            stock.get("confidence", 0.8),
            stock.get("rank", 1),
            stock.get("regime", "ABOVE_200DMA"),
            stock.get("position_size_multiplier", 1.0),
            stock.get("scorecard_score", 30),
            stock.get("scorecard_max", 40),
            1,  # human_approved=1
        ))
    conn.commit()
    conn.close()


def _setup_screener_results(db_path: str, run_date: str, regime: str) -> None:
    """Setup screener_results table with prior regime data."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS screener_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            rank INTEGER,
            momentum_score REAL,
            quality_passed INTEGER,
            regime TEXT,
            position_size_multiplier REAL,
            screened_at TEXT,
            UNIQUE(symbol, run_date)
        )
    """)
    conn.execute("""
        INSERT INTO screener_results
        (symbol, run_date, rank, momentum_score, quality_passed, regime,
         position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "TCS",
        run_date,
        1,
        0.85,
        1,
        regime,
        1.0,
        "2026-05-17T08:00:00+05:30",
    ))
    conn.commit()
    conn.close()


def _read_morning_signals(db_path: str, run_date: str) -> list[dict]:
    """Read all morning_signals rows for run_date."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("""
        SELECT symbol, run_date, latest_price, regime, position_size_multiplier,
               overnight_news_checked
        FROM morning_signals
        WHERE run_date = ?
        ORDER BY symbol
    """, (run_date,))
    rows = [
        {
            "symbol": row[0],
            "run_date": row[1],
            "latest_price": row[2],
            "regime": row[3],
            "position_size_multiplier": row[4],
            "overnight_news_checked": row[5],
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Test 1: Empty watchlist
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
def test_empty_watchlist(mock_ist_now, mock_log, mock_settings, temp_db):
    """Empty watchlist (no human_approved=1) returns immediately with zero counts."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [])  # table exists but 0 human_approved rows

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.watchlist_size == 0
    assert result.validated_count == 0
    assert result.removed_count == 0
    assert result.safe_mode is False
    assert result.removal_reasons == []


# ---------------------------------------------------------------------------
# Test 2: All survive, regime unchanged
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_all_survive_regime_unchanged(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """3 stocks, benign news, Gemini returns is_material=False → all survive."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
        {"symbol": "INFY", "rank": 2},
        {"symbol": "HDFCBANK", "rank": 3},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    fake_articles = [{"title": "TCS reports steady demand", "url": "https://ex.com/1"}]
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": fake_articles}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini
    verdict_benign = MaterialEventVerdict(
        is_material=False, event_type="none", reasoning="analyst commentary"
    )
    mock_chain = MagicMock()
    mock_chain.invoke.return_value = verdict_benign
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV
    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS", "INFY", "HDFCBANK"],
        "date": [run_date] * 3,
        "open": [3500.0, 2300.0, 1550.0],
        "high": [3550.0, 2350.0, 1580.0],
        "low": [3490.0, 2290.0, 1540.0],
        "close": [3530.0, 2330.0, 1570.0],
        "volume": [500000, 400000, 300000],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime result
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.watchlist_size == 3
    assert result.validated_count == 3
    assert result.removed_count == 0
    assert result.regime_confirmed is True
    assert result.safe_mode is False

    # Verify DB writes
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 3
    assert all(s["regime"] == "ABOVE_200DMA" for s in signals)
    assert all(s["position_size_multiplier"] == 1.0 for s in signals)


# ---------------------------------------------------------------------------
# Test 3: One stock removed for earnings
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
@patch("src.agents.morning_validator_agent.send_alert")
def test_one_stock_removed_for_earnings(
    mock_send_alert,
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Gemini returns is_material=True for one stock → removed from morning_signals."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
        {"symbol": "INFY", "rank": 2},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    fake_articles = [{"title": "TCS Q1 earnings released", "url": "https://ex.com/1"}]
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": fake_articles}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini: first call removes TCS, second call keeps INFY
    verdict_material = MaterialEventVerdict(
        is_material=True, event_type="earnings_dropped", reasoning="Q1 earnings released"
    )
    verdict_benign = MaterialEventVerdict(
        is_material=False, event_type="none", reasoning="analyst commentary"
    )
    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = [verdict_material, verdict_benign]
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV (only INFY should be fetched)
    mock_ohlcv = pd.DataFrame({
        "symbol": ["INFY"],
        "date": [run_date],
        "open": [2300.0],
        "high": [2350.0],
        "low": [2290.0],
        "close": [2330.0],
        "volume": [400000],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.watchlist_size == 2
    assert result.validated_count == 1
    assert result.removed_count == 1
    assert "TCS: earnings_dropped" in result.removal_reasons
    assert result.safe_mode is False

    # Verify DB: only INFY
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 1
    assert signals[0]["symbol"] == "INFY"


# ---------------------------------------------------------------------------
# Test 4: Tavily failure for one stock
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_tavily_failure_one_stock(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Tavily raises exception for one stock → stock kept with overnight_news_checked=0."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
        {"symbol": "INFY", "rank": 2},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily: first call raises, second succeeds
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.side_effect = [
        Exception("timeout"),
        {"results": []},
    ]
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini (should be called once for INFY, not for TCS)
    verdict_benign = MaterialEventVerdict(
        is_material=False, event_type="none", reasoning="ok"
    )
    mock_chain = MagicMock()
    mock_chain.invoke.return_value = verdict_benign
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV
    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS", "INFY"],
        "date": [run_date] * 2,
        "close": [3530.0, 2330.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.validated_count == 2  # Both kept

    # Check DB: TCS should have overnight_news_checked=0
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    tcs_signal = [s for s in signals if s["symbol"] == "TCS"][0]
    assert tcs_signal["overnight_news_checked"] == 0


# ---------------------------------------------------------------------------
# Test 5: Gemini failure for one stock
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_gemini_failure_one_stock(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Gemini raises exception for one stock → stock kept with overnight_news_checked=True."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
        {"symbol": "INFY", "rank": 2},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": [{"title": "news"}]}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini: first call raises, second succeeds
    verdict_benign = MaterialEventVerdict(
        is_material=False, event_type="none", reasoning="ok"
    )
    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = [
        Exception("quota exceeded"),
        verdict_benign,
    ]
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV
    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS", "INFY"],
        "date": [run_date] * 2,
        "close": [3530.0, 2330.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.validated_count == 2  # Both kept

    # Check DB: both should have overnight_news_checked=1 (Gemini failure means fail-open)
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert all(s["overnight_news_checked"] == 1 for s in signals)


# ---------------------------------------------------------------------------
# Test 6: OHLCV missing for one survivor
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
@patch("src.agents.morning_validator_agent.send_alert")
def test_ohlcv_missing_one_survivor(
    mock_send_alert,
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """OHLCV fetch returns no rows for one stock → stock dropped with removal reason."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
        {"symbol": "INFY", "rank": 2},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": []}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini (not called with zero articles)
    mock_chain = MagicMock()
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV: only INFY returned
    mock_ohlcv = pd.DataFrame({
        "symbol": ["INFY"],
        "date": [run_date],
        "close": [2330.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.validated_count == 1
    assert result.removed_count == 1
    assert "TCS: ohlcv_unavailable" in result.removal_reasons

    # Verify DB: only INFY
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 1
    assert signals[0]["symbol"] == "INFY"


# ---------------------------------------------------------------------------
# Test 7: Regime changed overnight
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_regime_changed_overnight(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Prior regime ABOVE_200DMA, current BELOW_200DMA → regime_confirmed=False."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
    ])
    # Setup screener with ABOVE_200DMA
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": []}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini
    mock_chain = MagicMock()
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV
    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS"],
        "date": [run_date],
        "close": [3530.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime: current is BELOW_200DMA
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "BELOW_200DMA"
    regime_result.position_size_multiplier = 0.5
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.regime_confirmed is False
    assert result.regime_now == "BELOW_200DMA"

    # Verify DB: written with new regime
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 1
    assert signals[0]["regime"] == "BELOW_200DMA"
    assert signals[0]["position_size_multiplier"] == 0.5


# ---------------------------------------------------------------------------
# Test 8: Deadline exceeded
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.send_alert")
def test_deadline_exceeded(
    mock_send_alert,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """_ist_now returns 08:16 → safe_mode=True, no DB writes."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
    ])

    # Mock _ist_now to return after deadline
    deadline_time = datetime.datetime(
        2026, 5, 17, 8, 16, tzinfo=IST
    )
    mock_ist_now.return_value = deadline_time

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.safe_mode is True
    assert result.validated_count == 0

    # Verify no DB writes
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 0

    # Verify alert was sent
    assert mock_send_alert.called


# ---------------------------------------------------------------------------
# Test 9: Missing Tavily key
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
def test_missing_tavily_key(mock_log, mock_settings, temp_db):
    """Missing TAVILY_API_KEY raises MorningValidatorError(phase='config')."""
    mock_settings.tavily_api_key = None
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"

    with pytest.raises(MorningValidatorError) as exc_info:
        run_morning_validator_agent(
            run_date=datetime.date(2026, 5, 17),
            db_path_override=temp_db,
        )

    assert exc_info.value.phase == "config"
    assert "TAVILY_API_KEY" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 10: Missing Gemini key
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
def test_missing_gemini_key(mock_log, mock_settings, temp_db):
    """Missing GEMINI_API_KEY raises MorningValidatorError(phase='config')."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = None
    mock_settings.database_url = f"sqlite:///{temp_db}"

    with pytest.raises(MorningValidatorError) as exc_info:
        run_morning_validator_agent(
            run_date=datetime.date(2026, 5, 17),
            db_path_override=temp_db,
        )

    assert exc_info.value.phase == "config"
    assert "GEMINI_API_KEY" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 11: INSERT OR REPLACE idempotency
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_insert_or_replace_idempotency(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Running agent twice with same run_date writes no duplicates."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Setup mocks
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": []}
    mock_tavily_class.return_value = mock_tavily_instance

    mock_chain = MagicMock()
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS"],
        "date": [run_date],
        "close": [3530.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    # Run twice
    run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )
    run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    # Verify only 1 row
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 1


# ---------------------------------------------------------------------------
# Test 12: db_path_override honoured
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_db_path_override_honoured(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """db_path_override parameter is used; default database_url ignored."""
    # Set a different path in settings
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = "sqlite:///some/other/path.db"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Setup mocks
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": []}
    mock_tavily_class.return_value = mock_tavily_instance

    mock_chain = MagicMock()
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS"],
        "date": [run_date],
        "close": [3530.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    # Run with override
    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    # Verify data written to temp_db, not some/other/path.db
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 1
    assert result.validated_count == 1


# ---------------------------------------------------------------------------
# Test 13: Zero articles from Tavily (Gemini skipped)
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_zero_articles_gemini_not_called(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Tavily returns 0 articles → Gemini invoke NOT called; stock kept."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily: returns no articles
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": []}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini
    mock_chain = MagicMock()
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV
    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS"],
        "date": [run_date],
        "close": [3530.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.validated_count == 1

    # Verify Gemini invoke was NOT called
    assert mock_chain.invoke.call_count == 0

    # Verify DB: stock kept with overnight_news_checked=1
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert signals[0]["overnight_news_checked"] == 1


# ---------------------------------------------------------------------------
# Test 14: All stocks removed
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.send_alert")
def test_all_stocks_removed(
    mock_send_alert,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """All stocks flagged material → validated_count=0, alert sent."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
        {"symbol": "INFY", "rank": 2},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": [{"title": "news"}]}
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini: both material
    verdict_material = MaterialEventVerdict(
        is_material=True, event_type="earnings_dropped", reasoning="Q1 dropped"
    )
    mock_chain = MagicMock()
    mock_chain.invoke.return_value = verdict_material
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.validated_count == 0
    assert result.removed_count == 2
    assert mock_send_alert.called

    # Verify no DB rows
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 0


# ---------------------------------------------------------------------------
# Test 15: Non-material news (analyst upgrade)
# ---------------------------------------------------------------------------


@patch("src.agents.morning_validator_agent.settings")
@patch("src.agents.morning_validator_agent.log_agent_action")
@patch("src.agents.morning_validator_agent._ist_now")
@patch("src.agents.morning_validator_agent.TavilyClient")
@patch("src.agents.morning_validator_agent.ChatGoogleGenerativeAI")
@patch("src.agents.morning_validator_agent.fetch_ohlcv")
@patch("src.agents.morning_validator_agent.fetch_sector_indices")
@patch("src.agents.morning_validator_agent.apply_regime_filter")
def test_non_material_news_analyst_upgrade(
    mock_regime_filter,
    mock_fetch_sector,
    mock_fetch_ohlcv,
    mock_llm_class,
    mock_tavily_class,
    mock_ist_now,
    mock_log,
    mock_settings,
    temp_db,
):
    """Analyst upgrade (non-material) → Gemini returns is_material=False → stock kept."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_ist_now.return_value = datetime.datetime(2026, 5, 17, 8, 0, tzinfo=IST)

    run_date = datetime.date(2026, 5, 17)
    _setup_watchlist(temp_db, [
        {"symbol": "TCS", "rank": 1},
    ])
    _setup_screener_results(temp_db, run_date.isoformat(), "ABOVE_200DMA")

    # Mock Tavily
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {
        "results": [{"title": "Goldman Sachs upgrades TCS"}]
    }
    mock_tavily_class.return_value = mock_tavily_instance

    # Mock Gemini: non-material
    verdict_benign = MaterialEventVerdict(
        is_material=False, event_type="none", reasoning="analyst opinion only"
    )
    mock_chain = MagicMock()
    mock_chain.invoke.return_value = verdict_benign
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value = mock_chain
    mock_llm_class.return_value = mock_llm

    # Mock OHLCV
    mock_ohlcv = pd.DataFrame({
        "symbol": ["TCS"],
        "date": [run_date],
        "close": [3530.0],
    })
    mock_fetch_ohlcv.return_value = mock_ohlcv

    # Mock sector indices
    nifty_df = pd.DataFrame({
        "symbol": ["NIFTY_50"] * 200,
        "date": pd.date_range("2026-01-01", periods=200),
        "close": [22000.0 + i * 10 for i in range(200)],
    })
    mock_fetch_sector.return_value = nifty_df

    # Mock regime
    from src.strategy.regime import RegimeResult
    regime_result = MagicMock(spec=RegimeResult)
    regime_result.regime = "ABOVE_200DMA"
    regime_result.position_size_multiplier = 1.0
    mock_regime_filter.return_value = (pd.DataFrame(), regime_result)

    result = run_morning_validator_agent(
        run_date=run_date,
        db_path_override=temp_db,
    )

    assert result.validated_count == 1
    assert result.removed_count == 0
    assert result.removal_reasons == []

    # Verify DB
    signals = _read_morning_signals(temp_db, run_date.isoformat())
    assert len(signals) == 1
    assert signals[0]["symbol"] == "TCS"
