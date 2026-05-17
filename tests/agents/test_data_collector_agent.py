"""Tests for src/agents/data_collector_agent.py.

Tests orchestration logic, sanity checks, alert behavior, and DB interaction.
fetch_historical_fundamentals and fetch_nifty200_symbols are always mocked —
no network calls are made.
"""
from __future__ import annotations

import datetime
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.agents.data_collector_agent import (
    AGENT_NAME,
    COVERAGE_ALERT_THRESHOLD,
    DataCollectorError,
    DataCollectorResult,
    _check_roe_plausibility,
    _count_fresh_symbols,
    run_data_collector_agent,
)
from src.data.fetcher import FetchError
from src.data.fundamentals import FundamentalsError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Temporary SQLite database path for test isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield str(db_path)


def _make_fundamentals_history_table(db_path: str) -> None:
    """Create a minimal fundamentals_history table for testing."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals_history (
            symbol TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            roe REAL,
            debt_to_equity REAL,
            eps_positive INTEGER,
            data_source TEXT DEFAULT 'screener.in',
            data_quality TEXT DEFAULT 'ok',
            fetched_at_ist TEXT NOT NULL,
            PRIMARY KEY (symbol, fiscal_year)
        )
    """)
    conn.commit()
    conn.close()


def _insert_fresh_row(db_path: str, symbol: str, roe: float = 0.15, data_quality: str = "ok") -> None:
    """Insert a row with fetched_at_ist set to now (fresh)."""
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO fundamentals_history "
        "(symbol, fiscal_year, roe, fetched_at_ist, data_quality) VALUES (?, 2025, ?, ?, ?)",
        (symbol, roe, now, data_quality),
    )
    conn.commit()
    conn.close()


def _insert_stale_row(db_path: str, symbol: str) -> None:
    """Insert a row with fetched_at_ist 50 days ago (stale)."""
    from zoneinfo import ZoneInfo
    stale = (
        datetime.datetime.now(ZoneInfo("Asia/Kolkata")) - datetime.timedelta(days=50)
    ).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO fundamentals_history "
        "(symbol, fiscal_year, roe, fetched_at_ist, data_quality) VALUES (?, 2024, 0.10, ?, 'ok')",
        (symbol, stale),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: Symbol fetch failure raises DataCollectorError with phase='symbol_fetch'
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_symbol_fetch_failure_raises_error(mock_settings, mock_fetch_symbols, mock_log, temp_db):
    """FetchError from fetch_nifty200_symbols → DataCollectorError phase='symbol_fetch'."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_fetch_symbols.side_effect = FetchError("nifty200", yfinance_error="timeout")

    with pytest.raises(DataCollectorError) as exc_info:
        run_data_collector_agent()

    assert exc_info.value.phase == "symbol_fetch"
    assert "Nifty 200" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 2: FundamentalsError raises DataCollectorError with phase='fundamentals_fetch'
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_fundamentals_db_failure_raises_error(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_log, temp_db
):
    """FundamentalsError from fetch_historical_fundamentals → DataCollectorError phase='fundamentals_fetch'."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_fetch_symbols.return_value = ["TCS", "INFY"]
    mock_fetch_hist.side_effect = FundamentalsError("DB write failed")

    with pytest.raises(DataCollectorError) as exc_info:
        run_data_collector_agent()

    assert exc_info.value.phase == "fundamentals_fetch"


# ---------------------------------------------------------------------------
# Test 3: Successful run returns DataCollectorResult with correct structure
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_successful_run_returns_result(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """Successful run returns DataCollectorResult with correct types and run_date."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_fetch_symbols.return_value = ["TCS", "INFY", "RELIANCE"]
    mock_fetch_hist.return_value = None

    _make_fundamentals_history_table(temp_db)
    _insert_fresh_row(temp_db, "TCS")
    _insert_fresh_row(temp_db, "INFY")
    _insert_fresh_row(temp_db, "RELIANCE")

    run_date = datetime.date(2026, 5, 17)
    result = run_data_collector_agent(run_date=run_date)

    assert isinstance(result, DataCollectorResult)
    assert result.run_date == run_date
    assert result.symbols_attempted == 3
    assert 0.0 <= result.coverage_pct <= 1.0


# ---------------------------------------------------------------------------
# Test 4: Coverage below threshold triggers alert, sanity_passed=False
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_low_coverage_sends_alert_and_sanity_false(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """When fewer than 80% of symbols have fresh data, alert sent and sanity_passed=False."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    # 10 symbols, only 1 will have fresh data (10% coverage)
    symbols = [f"SYM{i}" for i in range(10)]
    mock_fetch_symbols.return_value = symbols
    mock_fetch_hist.return_value = None

    _make_fundamentals_history_table(temp_db)
    _insert_fresh_row(temp_db, "SYM0")  # only 1 fresh

    result = run_data_collector_agent()

    assert result.sanity_passed is False
    assert result.coverage_pct < COVERAGE_ALERT_THRESHOLD
    mock_alert.assert_called_once()
    alert_msg = mock_alert.call_args.kwargs["message"]
    assert "Coverage low" in alert_msg


# ---------------------------------------------------------------------------
# Test 5: Coverage above threshold, no alert, sanity_passed=True
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_high_coverage_no_alert_sanity_passes(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """When ≥ 80% of symbols have fresh data and ROE is valid, no alert, sanity_passed=True."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    symbols = ["TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK"]
    mock_fetch_symbols.return_value = symbols
    mock_fetch_hist.return_value = None

    _make_fundamentals_history_table(temp_db)
    for sym in symbols:
        _insert_fresh_row(temp_db, sym, roe=0.20)

    result = run_data_collector_agent()

    assert result.sanity_passed is True
    assert result.coverage_pct == 1.0
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: ROE plausibility failure sends alert mentioning ROE
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_implausible_roe_sends_alert(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """ROE outside [-0.50, 2.00] triggers alert mentioning ROE plausibility."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    symbols = ["TCS"]
    mock_fetch_symbols.return_value = symbols
    mock_fetch_hist.return_value = None

    _make_fundamentals_history_table(temp_db)
    _insert_fresh_row(temp_db, "TCS", roe=5.0)  # implausible ROE

    result = run_data_collector_agent()

    assert result.sanity_passed is False
    mock_alert.assert_called_once()
    alert_msg = mock_alert.call_args.kwargs["message"]
    assert "ROE" in alert_msg or "plausibility" in alert_msg


# ---------------------------------------------------------------------------
# Test 7: fetch_historical_fundamentals called with the full symbol list
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_fetch_historical_fundamentals_called_with_all_symbols(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """fetch_historical_fundamentals is called with the exact symbol list from fetch_nifty200_symbols."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    symbols = ["TCS", "INFY", "WIPRO"]
    mock_fetch_symbols.return_value = symbols
    mock_fetch_hist.return_value = None

    _make_fundamentals_history_table(temp_db)
    for sym in symbols:
        _insert_fresh_row(temp_db, sym)

    run_data_collector_agent()

    mock_fetch_hist.assert_called_once_with(symbols)


# ---------------------------------------------------------------------------
# Test 8: run_date defaults to today IST
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_run_date_defaults_to_today(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """When run_date is None, result.run_date is today in IST."""
    from zoneinfo import ZoneInfo
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_fetch_symbols.return_value = ["TCS"]
    mock_fetch_hist.return_value = None

    _make_fundamentals_history_table(temp_db)
    _insert_fresh_row(temp_db, "TCS")

    result = run_data_collector_agent(run_date=None)

    today_ist = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    assert result.run_date == today_ist


# ---------------------------------------------------------------------------
# Test 9: _count_fresh_symbols returns 0 when table does not exist
# ---------------------------------------------------------------------------


def test_count_fresh_symbols_returns_zero_on_missing_table(temp_db):
    """_count_fresh_symbols returns 0 when fundamentals_history table doesn't exist."""
    result = _count_fresh_symbols(temp_db)
    assert result == 0


# ---------------------------------------------------------------------------
# Test 10: _count_fresh_symbols counts only fresh rows (not stale)
# ---------------------------------------------------------------------------


def test_count_fresh_symbols_excludes_stale(temp_db):
    """_count_fresh_symbols counts only symbols with at least one row fetched within 45 days."""
    _make_fundamentals_history_table(temp_db)
    _insert_fresh_row(temp_db, "TCS")
    _insert_stale_row(temp_db, "INFY")  # stale — should not be counted

    count = _count_fresh_symbols(temp_db)
    assert count == 1


# ---------------------------------------------------------------------------
# Test 11: _check_roe_plausibility returns True when no recent rows
# ---------------------------------------------------------------------------


def test_roe_plausibility_true_when_no_recent_rows(temp_db):
    """_check_roe_plausibility returns True when no rows were fetched in the last 24 hours."""
    _make_fundamentals_history_table(temp_db)
    _insert_stale_row(temp_db, "TCS")  # old row, not checked

    result = _check_roe_plausibility(temp_db)
    assert result is True


# ---------------------------------------------------------------------------
# Test 12: _check_roe_plausibility returns False for ROE > 2.00
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
def test_roe_plausibility_false_for_high_roe(mock_log, temp_db):
    """_check_roe_plausibility returns False when ROE > 2.00 is found in recent rows."""
    _make_fundamentals_history_table(temp_db)
    _insert_fresh_row(temp_db, "BADCO", roe=3.5)

    result = _check_roe_plausibility(temp_db)
    assert result is False


# ---------------------------------------------------------------------------
# Test 13: symbols_failed computed correctly when not all symbols get fresh data
# ---------------------------------------------------------------------------


@patch("src.agents.data_collector_agent.log_agent_action")
@patch("src.agents.data_collector_agent._safe_send_alert")
@patch("src.agents.data_collector_agent.fetch_historical_fundamentals")
@patch("src.agents.data_collector_agent.fetch_nifty200_symbols")
@patch("src.agents.data_collector_agent.settings")
def test_symbols_failed_computed_correctly(
    mock_settings, mock_fetch_symbols, mock_fetch_hist, mock_alert, mock_log, temp_db
):
    """symbols_failed = symbols_attempted - symbols_with_fresh_data_after_run."""
    mock_settings.database_url = f"sqlite:///{temp_db}"
    symbols = ["TCS", "INFY", "WIPRO", "HDFCBANK", "RELIANCE"]
    mock_fetch_symbols.return_value = symbols
    mock_fetch_hist.return_value = None  # simulates partial success

    _make_fundamentals_history_table(temp_db)
    # Only 3 of 5 end up with fresh data (simulating 2 symbol failures)
    _insert_fresh_row(temp_db, "TCS")
    _insert_fresh_row(temp_db, "INFY")
    _insert_fresh_row(temp_db, "WIPRO")

    result = run_data_collector_agent()

    assert result.symbols_attempted == 5
    assert result.symbols_failed == 2
    assert result.coverage_pct == pytest.approx(3 / 5)
