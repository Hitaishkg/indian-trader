"""Tests for src/agents/signal_agent.py.

Covers all 15 scenarios from the spec at docs/specs/2026-04-05-signal-agent.md.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from src.agents.signal_agent import (
    run_signal_agent,
    SignalAgentError,
    StockSignal,
    SignalAgentResult,
    OHLCV_LOOKBACK_DAYS,
    MAX_SYMBOLS,
    LLM_UNAVAILABLE_SENTINEL,
)

# ---------------------------------------------------------------------------
# Timezone constant for test use
# ---------------------------------------------------------------------------
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Fixed run date and a "safe" time (before 08:50) used across most tests
RUN_DATE = datetime.date(2026, 4, 5)
# 08:20 IST — well before the 08:50 deadline
SAFE_TIME_IST = datetime.datetime(2026, 4, 5, 8, 20, 0, tzinfo=IST)


# ---------------------------------------------------------------------------
# Helpers to build realistic test DataFrames
# ---------------------------------------------------------------------------


def _make_ohlcv_df(
    symbols: list[str],
    num_rows: int = 35,
    base_date: datetime.date | None = None,
) -> pd.DataFrame:
    """Create a realistic multi-symbol OHLCV DataFrame."""
    if base_date is None:
        base_date = RUN_DATE

    rows = []
    for symbol in symbols:
        for i in range(num_rows):
            date = base_date - datetime.timedelta(days=(num_rows - 1 - i))
            rows.append(
                {
                    "symbol": symbol,
                    "date": date.isoformat(),
                    "open": 100.0 + i,
                    "high": 105.0 + i,
                    "low": 98.0 + i,
                    "close": 101.0 + i,
                    "volume": 1_000_000 + i * 1000,
                }
            )
    return pd.DataFrame(rows)


def _make_indicators_df(
    symbol: str,
    rsi: float = 35.0,
    macd_hist: float = 0.5,
    close: float = 101.0,
    bb_upper: float = 110.0,
    bb_lower: float = 90.0,
    bb_mid: float = 100.0,
    atr: float = 5.0,
    run_date: datetime.date | None = None,
) -> pd.DataFrame:
    """Create a multi-row indicators DataFrame for a symbol, with control over the latest row."""
    if run_date is None:
        run_date = RUN_DATE

    rows = []
    num_rows = 30
    for i in range(num_rows):
        date = run_date - datetime.timedelta(days=(num_rows - 1 - i))
        is_latest = i == num_rows - 1
        rows.append(
            {
                "symbol": symbol,
                "date": date.isoformat(),
                "open": 100.0,
                "high": 105.0,
                "low": 95.0,
                "close": close if is_latest else 100.0,
                "volume": 1_000_000,
                "rsi": rsi if is_latest else 50.0,
                "macd": 0.1,
                "macd_signal": 0.05,
                "macd_hist": macd_hist if is_latest else 0.0,
                "bb_upper": bb_upper if is_latest else 110.0,
                "bb_mid": bb_mid if is_latest else 100.0,
                "bb_lower": bb_lower if is_latest else 90.0,
                "atr": atr if is_latest else 5.0,
            }
        )
    return pd.DataFrame(rows)


def _groq_response(confidence: float = 0.75, reasoning: str = "thesis holds") -> MagicMock:
    """Return a mock requests.Response for Groq with given confidence."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"confidence": confidence, "reasoning": reasoning}
                    )
                }
            }
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _gemini_response(confidence: float = 0.65, reasoning: str = "thesis holds") -> MagicMock:
    """Return a mock Gemini response object."""
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(
        {"confidence": confidence, "reasoning": reasoning}
    )
    return mock_resp


def _mock_safe_datetime(mock_datetime: MagicMock, time_ist: datetime.datetime | None = None) -> None:
    """Configure mock_datetime to return a given IST time from now(), default SAFE_TIME_IST.

    Keeps datetime.date, timedelta, and datetime constructor working as real objects.
    """
    if time_ist is None:
        time_ist = SAFE_TIME_IST
    mock_datetime.datetime.now.return_value = time_ist
    mock_datetime.date = datetime.date
    mock_datetime.timedelta = datetime.timedelta
    # Allow datetime constructor calls to work normally
    mock_datetime.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)


# ---------------------------------------------------------------------------
# Shared fixture for temp DB
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Yield a path to a fresh temporary SQLite DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_signals.db")
        yield db_path


_CREATE_RESEARCH_REPORTS_SQL = """
    CREATE TABLE IF NOT EXISTS research_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        sentiment TEXT,
        confidence REAL,
        source_urls TEXT,
        earnings_transcript_unavailable INTEGER DEFAULT 0,
        completed_at TEXT
    )
"""


def _ensure_research_table(db_path: str) -> None:
    """Create the research_reports table in temp_db without inserting rows."""
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_RESEARCH_REPORTS_SQL)
    conn.commit()
    conn.close()


def _insert_research(db_path: str, symbol: str, sentiment: str, confidence: float) -> None:
    """Insert a completed research_reports row into temp_db."""
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_RESEARCH_REPORTS_SQL)
    now_ist = datetime.datetime.now(tz=IST).isoformat()
    conn.execute(
        """
        INSERT INTO research_reports (symbol, sentiment, confidence, source_urls, completed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (symbol, sentiment, confidence, json.dumps([]), now_ist),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: BUY signal — RSI=35, macd_hist=0.5, Positive sentiment, Groq 0.75
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_buy_signal_positive_sentiment_groq_confirms(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 1: BUY signal with positive sentiment and Groq confirming confidence=0.75."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.8)

    ohlcv_df = _make_ohlcv_df(["TCS"])
    indicators_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=0.5, close=100.0, bb_upper=110.0, bb_lower=90.0, atr=5.0)
    mock_fetch_ohlcv.return_value = ohlcv_df
    mock_add_indicators.return_value = indicators_df

    mock_requests_post.return_value = _groq_response(confidence=0.75)

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert result.symbols_processed == 1
    assert len(result.buy_signals) == 1
    assert len(result.hold_signals) == 0
    signal = result.buy_signals[0]
    assert signal.symbol == "TCS"
    assert signal.signal_type == "BUY"
    assert signal.groq_confidence == pytest.approx(0.75, abs=0.01)
    assert signal.skip_reason is None

    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT signal_type, groq_confidence, skip_reason FROM signals WHERE symbol=?",
        ("TCS",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "BUY"
    assert abs(row[1] - 0.75) < 0.01
    assert row[2] is None


# ---------------------------------------------------------------------------
# Test 2: Technical HOLD — RSI=55, macd_hist=0.5 (RSI too high)
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_technical_hold_rsi_too_high(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 2: RSI=55 (above threshold) → HOLD, Groq NOT called."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.8)

    indicators_df = _make_indicators_df("TCS", rsi=55.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert result.symbols_processed == 1
    assert len(result.hold_signals) == 1
    signal = result.hold_signals[0]
    assert signal.signal_type == "HOLD"
    assert signal.skip_reason == "no_technical_buy_signal"

    mock_requests_post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Technical HOLD — RSI=35, macd_hist=-0.3 (MACD bearish)
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_technical_hold_macd_bearish(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 3: macd_hist=-0.3 (bearish) → HOLD, Groq NOT called."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.8)

    indicators_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=-0.3)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert result.symbols_processed == 1
    assert len(result.hold_signals) == 1
    assert result.hold_signals[0].skip_reason == "no_technical_buy_signal"
    mock_requests_post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Negative sentiment blocks BUY — RSI=30, macd_hist=0.8, Negative
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_negative_sentiment_blocks_buy(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 4: Negative sentiment → HOLD, Groq NOT called."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Negative", 0.85)

    indicators_df = _make_indicators_df("TCS", rsi=30.0, macd_hist=0.8)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert result.symbols_processed == 1
    assert len(result.hold_signals) == 1
    signal = result.hold_signals[0]
    assert signal.signal_type == "HOLD"
    assert signal.skip_reason == "negative_sentiment"
    mock_requests_post.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Groq low confidence downgrades BUY — confidence=0.45
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_groq_low_confidence_downgrades_buy(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 5: Groq returns confidence=0.45 (< 0.6) → HOLD with groq_low_confidence."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Neutral", 0.5)

    indicators_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    mock_requests_post.return_value = _groq_response(confidence=0.45)

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert len(result.hold_signals) == 1
    signal = result.hold_signals[0]
    assert signal.signal_type == "HOLD"
    assert signal.skip_reason == "groq_low_confidence"
    assert signal.groq_confidence == pytest.approx(0.45, abs=0.01)


# ---------------------------------------------------------------------------
# Test 6: Both LLMs fail — rule-based BUY preserved
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_both_llms_fail_rule_based_buy_preserved(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 6: Groq raises RequestException + Gemini raises Exception → BUY, groq_confidence=-1.0."""
    import requests as req_lib

    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.8)

    indicators_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    # Groq raises RequestException
    mock_requests_post.side_effect = req_lib.RequestException("Connection error")

    # Gemini raises Exception
    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_instance.models.generate_content.side_effect = Exception("Gemini error")

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert len(result.buy_signals) == 1
    signal = result.buy_signals[0]
    assert signal.signal_type == "BUY"
    assert signal.groq_confidence == LLM_UNAVAILABLE_SENTINEL
    assert signal.skip_reason is None


# ---------------------------------------------------------------------------
# Test 7: Late start — now_ist=08:55 → late_start=True, symbols_processed=0
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_late_start_returns_empty_result(
    mock_settings,
    mock_resolve_db,
    mock_log,
    mock_datetime,
    temp_db,
):
    """Test 7: Run starts after 08:50 IST → late_start=True, no DB writes."""
    mock_settings.groq_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    # 08:55 IST — past the 08:50 deadline
    late_time = datetime.datetime(2026, 4, 5, 8, 55, 0, tzinfo=IST)
    mock_datetime.datetime.now.return_value = late_time
    mock_datetime.date = datetime.date
    mock_datetime.timedelta = datetime.timedelta
    mock_datetime.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    assert result.late_start is True
    assert result.symbols_processed == 0
    assert result.buy_signals == []
    assert result.hold_signals == []

    # No rows written to signals table
    conn = sqlite3.connect(temp_db)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()
    if tables is not None:
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert count == 0
    conn.close()


# ---------------------------------------------------------------------------
# Test 8: Research missing → neutral defaults used, symbol still processed
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_research_missing_uses_neutral_defaults(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 8: No research_reports row → symbol processed with Neutral/0.3 defaults."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    # Ensure research_reports table exists but do NOT insert a row for TCS
    _ensure_research_table(temp_db)
    # Give it technical HOLD (RSI too high) so Groq is never called
    indicators_df = _make_indicators_df("TCS", rsi=55.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    # Symbol must be processed (not skipped)
    assert result.symbols_processed == 1

    # Verify research_missing logged
    log_calls = [
        c for c in mock_log.call_args_list
        if "research_missing_for_symbol" in str(c)
    ]
    assert len(log_calls) >= 1


# ---------------------------------------------------------------------------
# Test 9: OHLCV fails for one symbol — WIPRO HOLD, others processed
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_ohlcv_fails_for_one_symbol(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 9: fetch_ohlcv returns DF without WIPRO → WIPRO HOLD skip_reason=ohlcv_fetch_failed."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.8)
    _insert_research(temp_db, "WIPRO", "Positive", 0.7)

    # OHLCV only has TCS data — WIPRO is absent (simulates per-symbol failure)
    ohlcv_df = _make_ohlcv_df(["TCS"])

    # Indicators only for TCS
    indicators_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = ohlcv_df
    mock_add_indicators.return_value = indicators_df

    mock_requests_post.return_value = _groq_response(confidence=0.80)

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS", "WIPRO"])

    assert result.symbols_processed == 2

    wipro_hold = [s for s in result.hold_signals if s.symbol == "WIPRO"]
    assert len(wipro_hold) == 1
    assert wipro_hold[0].skip_reason == "ohlcv_fetch_failed"

    conn = sqlite3.connect(temp_db)
    wipro_row = conn.execute(
        "SELECT signal_type, skip_reason FROM signals WHERE symbol='WIPRO'",
    ).fetchone()
    conn.close()
    assert wipro_row is not None
    assert wipro_row[0] == "HOLD"
    assert wipro_row[1] == "ohlcv_fetch_failed"


# ---------------------------------------------------------------------------
# Test 10: Full audit trail — all symbols written regardless of signal_type
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_full_audit_trail_all_symbols_written(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 10: 3 symbols; 1 BUY + 2 HOLD → all 3 rows in signals table."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = "test-groq-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    symbols = ["TCS", "INFY", "WIPRO"]
    _insert_research(temp_db, "TCS", "Positive", 0.8)   # BUY: RSI<40, macd>0, Positive
    _insert_research(temp_db, "INFY", "Negative", 0.9)  # HOLD: Negative sentiment (RSI/MACD would fire)
    _insert_research(temp_db, "WIPRO", "Positive", 0.7) # HOLD: RSI too high

    tcs_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=0.5)
    infy_df = _make_indicators_df("INFY", rsi=30.0, macd_hist=0.8)   # would BUY but Negative blocks
    wipro_df = _make_indicators_df("WIPRO", rsi=60.0, macd_hist=0.5)  # RSI too high → HOLD

    combined_indicators = pd.concat([tcs_df, infy_df, wipro_df], ignore_index=True)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(symbols)
    mock_add_indicators.return_value = combined_indicators

    mock_requests_post.return_value = _groq_response(confidence=0.80)

    result = run_signal_agent(run_date=RUN_DATE, symbols=symbols)

    assert result.symbols_processed == 3
    assert len(result.buy_signals) == 1
    assert len(result.hold_signals) == 2

    conn = sqlite3.connect(temp_db)
    rows = conn.execute(
        "SELECT symbol, signal_type, skip_reason FROM signals ORDER BY symbol"
    ).fetchall()
    conn.close()

    assert len(rows) == 3
    symbols_in_db = {r[0] for r in rows}
    assert symbols_in_db == {"TCS", "INFY", "WIPRO"}

    tcs_row = next(r for r in rows if r[0] == "TCS")
    infy_row = next(r for r in rows if r[0] == "INFY")
    wipro_row = next(r for r in rows if r[0] == "WIPRO")

    assert tcs_row[1] == "BUY"
    assert tcs_row[2] is None

    assert infy_row[1] == "HOLD"
    assert infy_row[2] == "negative_sentiment"

    assert wipro_row[1] == "HOLD"
    assert wipro_row[2] == "no_technical_buy_signal"


# ---------------------------------------------------------------------------
# Test 11: Bollinger positions — BELOW, ABOVE, MIDDLE
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_bollinger_positions_all_three_cases(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 11: Bollinger position computed correctly for all three cases."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = None  # No Groq key to keep it simple
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    symbols = ["BELOW_SYM", "ABOVE_SYM", "MID_SYM"]
    for sym in symbols:
        _insert_research(temp_db, sym, "Positive", 0.7)

    # All three have RSI too high → HOLD (no Groq call needed), but Bollinger computed
    # close=85 < bb_lower=90 → BELOW
    below_df = _make_indicators_df("BELOW_SYM", rsi=55.0, macd_hist=0.5, close=85.0, bb_upper=110.0, bb_lower=90.0)
    # close=115 > bb_upper=110 → ABOVE
    above_df = _make_indicators_df("ABOVE_SYM", rsi=55.0, macd_hist=0.5, close=115.0, bb_upper=110.0, bb_lower=90.0)
    # close=100 between bands → MIDDLE
    mid_df = _make_indicators_df("MID_SYM", rsi=55.0, macd_hist=0.5, close=100.0, bb_upper=110.0, bb_lower=90.0)

    combined = pd.concat([below_df, above_df, mid_df], ignore_index=True)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(symbols)
    mock_add_indicators.return_value = combined

    result = run_signal_agent(run_date=RUN_DATE, symbols=symbols)

    signals_by_sym = {s.symbol: s for s in result.buy_signals + result.hold_signals}

    assert "BELOW_SYM" in signals_by_sym
    assert "ABOVE_SYM" in signals_by_sym
    assert "MID_SYM" in signals_by_sym

    assert signals_by_sym["BELOW_SYM"].bollinger_position == "BELOW"
    assert signals_by_sym["ABOVE_SYM"].bollinger_position == "ABOVE"
    assert signals_by_sym["MID_SYM"].bollinger_position == "MIDDLE"


# ---------------------------------------------------------------------------
# Test 12: OHLCV lookback window — start_date = run_date - timedelta(60)
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_ohlcv_lookback_window(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 12: fetch_ohlcv called with start_date=run_date-60 and end_date=run_date."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = None
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.7)

    indicators_df = _make_indicators_df("TCS", rsi=55.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    expected_start = RUN_DATE - datetime.timedelta(days=OHLCV_LOOKBACK_DAYS)
    mock_fetch_ohlcv.assert_called_once_with(
        symbols=["TCS"],
        start_date=expected_start,
        end_date=RUN_DATE,
        cache_expiry_hours=0,
    )


# ---------------------------------------------------------------------------
# Test 13: symbols override bypasses screener_results
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent._read_screener_results")
@patch("src.agents.signal_agent.settings")
def test_symbols_override_bypasses_screener(
    mock_settings,
    mock_read_screener,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 13: symbols override → _read_screener_results NOT called."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = None
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.7)
    _insert_research(temp_db, "INFY", "Positive", 0.7)

    combined_indicators = pd.concat([
        _make_indicators_df("TCS", rsi=55.0, macd_hist=0.5),
        _make_indicators_df("INFY", rsi=55.0, macd_hist=0.5),
    ], ignore_index=True)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS", "INFY"])
    mock_add_indicators.return_value = combined_indicators

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS", "INFY"])

    mock_read_screener.assert_not_called()
    assert result.symbols_processed == 2


# ---------------------------------------------------------------------------
# Test 14: MAX_SYMBOLS cap — 7 symbols provided, only 5 processed
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_max_symbols_cap(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 14: 7 symbols provided → only first 5 processed (MAX_SYMBOLS=5)."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = None
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    seven_syms = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    expected_five = seven_syms[:5]

    for sym in expected_five:
        _insert_research(temp_db, sym, "Positive", 0.7)

    indicators_parts = [_make_indicators_df(s, rsi=55.0, macd_hist=0.5) for s in expected_five]
    combined = pd.concat(indicators_parts, ignore_index=True)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(expected_five)
    mock_add_indicators.return_value = combined

    result = run_signal_agent(run_date=RUN_DATE, symbols=seven_syms)

    assert result.symbols_processed == MAX_SYMBOLS

    conn = sqlite3.connect(temp_db)
    count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    all_syms_in_db = {r[0] for r in conn.execute("SELECT symbol FROM signals").fetchall()}
    conn.close()

    assert count == MAX_SYMBOLS
    assert "S6" not in all_syms_in_db
    assert "S7" not in all_syms_in_db


# ---------------------------------------------------------------------------
# Test 15: Groq API key missing — rule-based decisions, groq_confidence=-1.0
# ---------------------------------------------------------------------------


@patch("src.agents.signal_agent.datetime")
@patch("src.agents.signal_agent.requests.post")
@patch("src.agents.signal_agent.genai.Client")
@patch("src.agents.signal_agent.log_agent_action")
@patch("src.agents.signal_agent.add_indicators")
@patch("src.agents.signal_agent.fetch_ohlcv")
@patch("src.agents.signal_agent._resolve_db_path")
@patch("src.agents.signal_agent.settings")
def test_groq_api_key_missing_rule_based_decisions(
    mock_settings,
    mock_resolve_db,
    mock_fetch_ohlcv,
    mock_add_indicators,
    mock_log,
    mock_genai_cls,
    mock_requests_post,
    mock_datetime,
    temp_db,
):
    """Test 15: settings.groq_api_key=None → rule-based BUY, groq_confidence=-1.0."""
    _mock_safe_datetime(mock_datetime)

    mock_settings.groq_api_key = None  # Missing!
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    _insert_research(temp_db, "TCS", "Positive", 0.8)

    indicators_df = _make_indicators_df("TCS", rsi=35.0, macd_hist=0.5)
    mock_fetch_ohlcv.return_value = _make_ohlcv_df(["TCS"])
    mock_add_indicators.return_value = indicators_df

    result = run_signal_agent(run_date=RUN_DATE, symbols=["TCS"])

    mock_requests_post.assert_not_called()

    assert len(result.buy_signals) == 1
    signal = result.buy_signals[0]
    assert signal.signal_type == "BUY"
    assert signal.groq_confidence == LLM_UNAVAILABLE_SENTINEL
    assert signal.skip_reason is None

    log_calls = [
        c for c in mock_log.call_args_list
        if "groq_api_key_missing" in str(c)
    ]
    assert len(log_calls) >= 1
