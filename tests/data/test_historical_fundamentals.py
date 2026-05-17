"""Tests for historical fundamentals support in src/data/fundamentals.py

Tests cover:
- Table initialization (_init_historical_tables)
- Historical fundamentals fetching and storage (fetch_historical_fundamentals)
- Point-in-time fiscal year selection (get_fundamentals_for_date)
- Nifty universe retrieval (get_nifty_universe_for_year)
- Nifty constituents population (_populate_nifty_constituents)
"""

from __future__ import annotations

import datetime
import tempfile
from unittest import mock

import pytest

from src.data.fundamentals import (
    _init_historical_tables,
    _populate_nifty_constituents,
    fetch_historical_fundamentals,
    get_fundamentals_for_date,
    get_nifty_universe_for_year,
)


@pytest.fixture
def tmp_db() -> str:
    """Create a temporary SQLite database file for testing.

    Yields the database path and cleans up after the test.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup happens automatically after test


@pytest.fixture
def mock_settings(tmp_db, monkeypatch):
    """Mock the settings module to use a temporary database."""
    mock_settings_obj = mock.Mock()
    mock_settings_obj.database_url = f"sqlite:///{tmp_db}"
    mock_settings_obj.log_level = "INFO"
    monkeypatch.setattr("src.data.fundamentals.settings", mock_settings_obj)
    return mock_settings_obj


# =============================================================================
# Table Initialization Tests (1-2)
# =============================================================================


def test_fundamentals_history_table_created(tmp_db):
    """Test that _init_historical_tables creates the fundamentals_history table."""
    conn = _init_historical_tables(tmp_db)
    try:
        # Query sqlite_master to check if table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fundamentals_history'"
        )
        assert cursor.fetchone() is not None, "fundamentals_history table not created"
    finally:
        conn.close()


def test_nifty_constituents_table_created(tmp_db):
    """Test that _init_historical_tables creates the nifty_constituents table."""
    conn = _init_historical_tables(tmp_db)
    try:
        # Query sqlite_master to check if table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nifty_constituents'"
        )
        assert cursor.fetchone() is not None, "nifty_constituents table not created"
    finally:
        conn.close()


# =============================================================================
# Fiscal Year Selection Tests (6-10, 13)
# =============================================================================


def test_get_date_fiscal_year_jan(tmp_db, mock_settings):
    """Test fiscal year selection for January (month <= 6 -> year-1)."""
    as_of_date = datetime.date(2015, 1, 15)
    conn = _init_historical_tables(tmp_db)
    try:
        # Insert test data for FY2014
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('RELIANCE', 2014, 0.20, 0.5, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('RELIANCE', 2015, 0.22, 0.55, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        # Query for January 2015 — should get FY2014
        df = get_fundamentals_for_date(["RELIANCE"], as_of_date)
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["roe"] == 0.20  # FY2014 value, not FY2015
    finally:
        pass


def test_get_date_fiscal_year_jun(tmp_db, mock_settings):
    """Test fiscal year selection for June (month <= 6 -> year-1)."""
    as_of_date = datetime.date(2015, 6, 30)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('TCS', 2014, 0.18, 0.4, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('TCS', 2015, 0.20, 0.45, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["TCS"], as_of_date)
        assert len(df) == 1
        assert df.iloc[0]["roe"] == 0.18  # FY2014, not FY2015
    finally:
        pass


def test_get_date_fiscal_year_jul(tmp_db, mock_settings):
    """Test fiscal year selection for July (month >= 7 -> year)."""
    as_of_date = datetime.date(2015, 7, 1)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('INFY', 2014, 0.16, 0.35, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('INFY', 2015, 0.19, 0.42, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["INFY"], as_of_date)
        assert len(df) == 1
        assert df.iloc[0]["roe"] == 0.19  # FY2015 (July is >= 7)
    finally:
        pass


def test_get_date_fiscal_year_apr(tmp_db, mock_settings):
    """Test April boundary (month <= 6 -> year-1, NOT year, preventing lookahead bias)."""
    as_of_date = datetime.date(2015, 4, 1)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('HDFCBANK', 2014, 0.21, 0.3, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('HDFCBANK', 2015, 0.23, 0.32, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["HDFCBANK"], as_of_date)
        assert len(df) == 1
        assert df.iloc[0]["roe"] == 0.21  # FY2014, not FY2015 (April <= 6)
    finally:
        pass


def test_get_date_fiscal_year_oct(tmp_db, mock_settings):
    """Test October boundary (month >= 7 -> year)."""
    as_of_date = datetime.date(2014, 10, 15)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('ICICIBANK', 2013, 0.15, 0.45, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('ICICIBANK', 2014, 0.17, 0.48, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["ICICIBANK"], as_of_date)
        assert len(df) == 1
        assert df.iloc[0]["roe"] == 0.17  # FY2014 (October >= 7)
    finally:
        pass


def test_get_date_no_future_leak(tmp_db, mock_settings):
    """Test that May 2015 query returns FY2014, not FY2015 (no lookahead bias)."""
    as_of_date = datetime.date(2015, 5, 1)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('SBIN', 2014, 0.14, 0.6, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('SBIN', 2015, 0.16, 0.62, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["SBIN"], as_of_date)
        assert len(df) == 1
        # May is month 5, which is <= 6, so fiscal_year = 2015 - 1 = 2014
        assert df.iloc[0]["roe"] == 0.14
    finally:
        pass


# =============================================================================
# Missing Data Tests (11)
# =============================================================================


def test_get_date_missing_row(tmp_db, mock_settings):
    """Test that missing DB row returns data_quality='missing' with NaN financials."""
    as_of_date = datetime.date(2015, 7, 1)
    conn = _init_historical_tables(tmp_db)
    try:
        # Don't insert any data for this symbol
        conn.close()

        df = get_fundamentals_for_date(["NONEXISTENT"], as_of_date)
        assert len(df) == 1
        assert df.iloc[0]["data_quality"] == "missing"
        assert df.iloc[0]["data_source"] == "missing"
        import math
        assert math.isnan(df.iloc[0]["roe"])
        assert math.isnan(df.iloc[0]["debt_to_equity"])
        # eps_positive_4q is bool dtype, check for False value
        assert not df.iloc[0]["eps_positive_4q"]
    finally:
        pass


# =============================================================================
# Schema Compatibility Tests (12, 18)
# =============================================================================


def test_get_date_schema_compat(tmp_db, mock_settings):
    """Test that output columns match fetch_fundamentals() output (minus pe_ratio, cache_age_days)."""
    as_of_date = datetime.date(2015, 7, 1)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('TCS', 2015, 0.25, 0.4, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["TCS"], as_of_date)
        expected_cols = {
            "symbol",
            "roe",
            "debt_to_equity",
            "eps_positive_4q",
            "data_source",
            "data_quality",
            "fetched_at_ist",
        }
        assert set(df.columns) == expected_cols
    finally:
        pass


def test_eps_approximation_column_name(tmp_db, mock_settings):
    """Verify output column is named 'eps_positive_4q' (not 'eps_positive_annual')."""
    as_of_date = datetime.date(2020, 7, 1)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('RELIANCE', 2020, 0.15, 0.35, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        df = get_fundamentals_for_date(["RELIANCE"], as_of_date)
        assert "eps_positive_4q" in df.columns
        assert "eps_positive_annual" not in df.columns
        assert "eps_positive" not in df.columns
    finally:
        pass


# =============================================================================
# Nifty Universe Tests (14-15)
# =============================================================================


def test_nifty_universe_2015(tmp_db, mock_settings):
    """Test get_nifty_universe_for_year(2015) returns non-empty list with known members."""
    result = get_nifty_universe_for_year(2015)
    assert isinstance(result, list)
    assert len(result) > 0
    # Verify some known members that were in Nifty 50 in 2015
    assert "RELIANCE" in result
    assert "TCS" in result
    assert "INFY" in result
    assert "HDFCBANK" in result


def test_nifty_universe_2020(tmp_db, mock_settings):
    """Test get_nifty_universe_for_year(2020) returns non-empty list with known members."""
    result = get_nifty_universe_for_year(2020)
    assert isinstance(result, list)
    assert len(result) > 0
    # Verify some known members that were in Nifty 50 in 2020
    assert "RELIANCE" in result
    assert "HDFCLIFE" in result  # Added 2019
    assert "TATACONSUM" in result  # Added 2020


# =============================================================================
# Nifty Constituents Population Tests (16)
# =============================================================================


def test_populate_idempotent(tmp_db, mock_settings):
    """Test that calling _populate_nifty_constituents twice produces no duplicates."""
    conn = _init_historical_tables(tmp_db)
    try:
        _populate_nifty_constituents(conn)
        count_1 = conn.execute(
            "SELECT COUNT(*) FROM nifty_constituents"
        ).fetchone()[0]

        _populate_nifty_constituents(conn)
        count_2 = conn.execute(
            "SELECT COUNT(*) FROM nifty_constituents"
        ).fetchone()[0]

        assert count_1 == count_2
        # Verify structure
        assert count_1 > 0
    finally:
        conn.close()


# =============================================================================
# Fetch Historical Fundamentals Tests (3-5, 17)
# =============================================================================


def test_fetch_historical_stores_rows(tmp_db, mock_settings):
    """Test that fetch_historical_fundamentals stores rows from Screener.in."""
    mock_parsed_data = {
        2020: {"roe": 0.18, "debt_to_equity": 0.4, "eps_positive": 1},
        2019: {"roe": 0.16, "debt_to_equity": 0.38, "eps_positive": 1},
    }

    with mock.patch(
        "src.data.fundamentals._scrape_screener_historical",
        return_value=mock_parsed_data,
    ), mock.patch("src.data.fundamentals.time.sleep"):
        fetch_historical_fundamentals(["RELIANCE"])

    conn = _init_historical_tables(tmp_db)
    try:
        rows = conn.execute(
            "SELECT symbol, fiscal_year, roe FROM fundamentals_history"
            " WHERE symbol='RELIANCE' ORDER BY fiscal_year DESC"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1] == 2020  # fiscal_year
        assert rows[0][2] == 0.18  # roe
    finally:
        conn.close()


def test_fetch_historical_cache_hit(tmp_db, mock_settings):
    """Test that fresh cached data (< 45 days) skips HTTP when force_refresh=False."""
    from zoneinfo import ZoneInfo
    fresh_ts = (
        datetime.datetime.now(ZoneInfo("Asia/Kolkata")) - datetime.timedelta(days=10)
    ).isoformat()
    conn = _init_historical_tables(tmp_db)
    try:
        # Insert fresh data (10 days ago — well within the 45-day window)
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('TCS', 2020, 0.22, 0.35, 1, 'screener', 'clean', ?)
            """,
            (fresh_ts,),
        )
        conn.commit()
        conn.close()

        # Mock should not be called
        with mock.patch(
            "src.data.fundamentals._scrape_screener_historical"
        ) as mock_scrape:
            fetch_historical_fundamentals(["TCS"], force_refresh=False)
            mock_scrape.assert_not_called()
    finally:
        pass


def test_fetch_historical_force_refresh(tmp_db, mock_settings):
    """Test that force_refresh=True triggers HTTP request even with fresh cache."""
    conn = _init_historical_tables(tmp_db)
    try:
        # Insert fresh data
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('INFY', 2020, 0.20, 0.40, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        mock_parsed_data = {
            2020: {"roe": 0.21, "debt_to_equity": 0.41, "eps_positive": 1},
        }

        with mock.patch(
            "src.data.fundamentals._scrape_screener_historical",
            return_value=mock_parsed_data,
        ), mock.patch("src.data.fundamentals.time.sleep"):
            fetch_historical_fundamentals(["INFY"], force_refresh=True)

        # Verify HTTP was called
        conn = _init_historical_tables(tmp_db)
        try:
            rows = conn.execute(
                "SELECT roe FROM fundamentals_history WHERE symbol='INFY'"
            ).fetchall()
            # Should have updated value
            assert rows[0][0] == 0.21
        finally:
            conn.close()
    finally:
        pass


def test_yfinance_fallback_after_3_strikes(tmp_db, mock_settings):
    """Test yfinance fallback after 3 Screener.in failures."""
    with mock.patch(
        "src.data.fundamentals._scrape_screener_historical", return_value=None
    ), mock.patch(
        "src.data.fundamentals.yf.Ticker"
    ) as mock_ticker, mock.patch(
        "src.data.fundamentals.time.sleep"
    ):
        # Mock yfinance response
        mock_info = {
            "returnOnEquity": 0.18,
            "debtToEquity": 0.40,
            "trailingEps": 50.0,
        }
        mock_ticker.return_value.info = mock_info

        fetch_historical_fundamentals(["HDFCBANK"])

        conn = _init_historical_tables(tmp_db)
        try:
            row = conn.execute(
                "SELECT data_source, data_quality FROM fundamentals_history"
                " WHERE symbol='HDFCBANK' AND fiscal_year=?"
                " ORDER BY fiscal_year DESC LIMIT 1",
                (datetime.date.today().year,),
            ).fetchone()
            assert row is not None
            # Current year should have degraded quality after yfinance fallback
            assert row[0] == "yfinance_fallback"
            assert row[1] == "degraded"
        finally:
            conn.close()


# =============================================================================
# Error Handling Tests
# =============================================================================


def test_get_fundamentals_empty_symbols(tmp_db, mock_settings):
    """Test that empty symbols list raises ValueError."""
    with pytest.raises(ValueError, match="symbols list must not be empty"):
        get_fundamentals_for_date([], datetime.date(2015, 7, 1))


def test_fetch_historical_empty_symbols(tmp_db, mock_settings):
    """Test that empty symbols list raises ValueError."""
    with pytest.raises(ValueError, match="symbols list must not be empty"):
        fetch_historical_fundamentals([])


def test_get_fundamentals_invalid_date_type(tmp_db, mock_settings):
    """Test that non-date as_of_date raises ValueError."""
    with pytest.raises(ValueError, match="as_of_date must be a datetime.date instance"):
        get_fundamentals_for_date(["RELIANCE"], "2015-07-01")


def test_get_fundamentals_invalid_date_object(tmp_db, mock_settings):
    """Test that datetime.datetime (not date) is accepted (it's a subclass of date)."""
    # Note: datetime.datetime is actually a subclass of datetime.date,
    # so isinstance(dt, datetime.date) returns True for datetime.datetime
    # This test verifies the actual behavior
    as_of_date = datetime.datetime(2015, 7, 1, 10, 0, 0)
    conn = _init_historical_tables(tmp_db)
    try:
        conn.execute(
            """
            INSERT INTO fundamentals_history
            (symbol, fiscal_year, roe, debt_to_equity, eps_positive,
             data_source, data_quality, fetched_at_ist)
            VALUES ('RELIANCE', 2015, 0.20, 0.4, 1, 'screener', 'clean',
                    '2026-03-25T10:00:00+05:30')
            """
        )
        conn.commit()
        conn.close()

        # Should not raise — datetime.datetime is a subclass of datetime.date
        df = get_fundamentals_for_date(["RELIANCE"], as_of_date)
        assert len(df) == 1
    finally:
        pass
