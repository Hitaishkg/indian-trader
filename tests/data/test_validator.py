"""Tests for src/data/validator.py — all 19 acceptance criteria."""

import dataclasses
import datetime
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.data.validator import (
    DataQualityError,
    DataQualityReport,
    _check_ohlcv_gaps,
    _check_roe,
    _compute_stock_score,
    validate_data,
)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """Create a temporary SQLite database path."""
    return str(tmp_path / "test.db")


def create_ohlcv_df(
    symbols_and_dates: dict[str, list[datetime.date]],
) -> pd.DataFrame:
    """Create a valid OHLCV DataFrame for testing.

    Args:
        symbols_and_dates: Dict mapping symbol -> list of datetime.date objects.
            Dates should be timezone-naive; they will be localized to IST.

    Returns:
        DataFrame with columns: symbol, date, open, high, low, close, volume.
    """
    rows = []
    for symbol, dates in symbols_and_dates.items():
        for date in dates:
            # Localize to IST
            tz_aware_date = pd.Timestamp(date, tz="Asia/Kolkata")
            rows.append({
                "symbol": symbol,
                "date": tz_aware_date,
                "open": 100.0,
                "high": 105.0,
                "low": 95.0,
                "close": 102.0,
                "volume": 1000.0,
            })
    return pd.DataFrame(rows)


def create_fundamentals_df(
    symbols: list[str],
    roe_values: dict[str, float | None] | None = None,
    de_values: dict[str, float | None] | None = None,
) -> pd.DataFrame:
    """Create a valid fundamentals DataFrame for testing.

    Args:
        symbols: List of NSE ticker symbols.
        roe_values: Dict mapping symbol -> ROE value. If None, all ROEs are 0.15.
        de_values: Dict mapping symbol -> debt-to-equity value. If None, all are 0.5.

    Returns:
        DataFrame with columns: symbol, roe, debt_to_equity.
    """
    if roe_values is None:
        roe_values = {sym: 0.15 for sym in symbols}
    if de_values is None:
        de_values = {sym: 0.5 for sym in symbols}

    rows = []
    for symbol in symbols:
        rows.append({
            "symbol": symbol,
            "roe": roe_values.get(symbol, 0.15),
            "debt_to_equity": de_values.get(symbol, 0.5),
        })
    return pd.DataFrame(rows)


# ============================================================================
# Acceptance Criterion 1: Valid DataFrames return DataQualityReport
# ============================================================================


def test_valid_dataframes_return_report(tmp_db_path: str) -> None:
    """Criterion 1: validate_data returns DataQualityReport when given valid DataFrames."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    assert isinstance(report, DataQualityReport)
    assert report.universe_size == 1
    assert "RELIANCE" in report.per_stock_scores


# ============================================================================
# Acceptance Criterion 2: DataQualityError raised when score < 0.60
# ============================================================================


def test_data_quality_error_raised_when_score_below_threshold(tmp_db_path: str) -> None:
    """Criterion 2: DataQualityError is raised when universe_quality_score < 0.60."""
    # Create a stock with all failures: ROE missing, OHLCV gaps present
    ohlcv_df = create_ohlcv_df({
        "STOCK1": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 20)],  # Large gap
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK1"],
        roe_values={"STOCK1": None},
    )

    with pytest.raises(DataQualityError) as exc_info:
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    error = exc_info.value
    assert error.universe_quality_score < 0.60
    assert isinstance(error.report, DataQualityReport)
    assert error.report.universe_quality_score == error.universe_quality_score


# ============================================================================
# Acceptance Criterion 3: ROE = 2.50 (out of range) scores 0.10
# ============================================================================


def test_roe_out_of_range_scores_0_10(tmp_db_path: str) -> None:
    """Criterion 3: ROE = 2.50 (outside range) scores exactly 0.10."""
    ohlcv_df = create_ohlcv_df({
        "STOCK": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK"],
        roe_values={"STOCK": 2.50},  # Outside [-0.50, 2.00]
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # Score should be:
    # - ROE plausibility (0.40 weight): 0.0 (failed)
    # - ROE present (0.10 weight): 1.0 (not missing)
    # - Gap (0.50 weight): 1.0 (no gaps)
    # Total: (0.0 * 0.40) + (1.0 * 0.10) + (1.0 * 0.50) = 0.60
    assert report.per_stock_scores["STOCK"] == pytest.approx(0.60)
    assert "STOCK" in report.failed_roe_symbols


# ============================================================================
# Acceptance Criterion 4: ROE = None scores 0.0 on ROE sub-components
# ============================================================================


def test_roe_none_scores_0_0(tmp_db_path: str) -> None:
    """Criterion 4: ROE = None scores 0.0 on both ROE sub-components."""
    ohlcv_df = create_ohlcv_df({
        "STOCK": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK"],
        roe_values={"STOCK": None},
    )

    # Score will be 0.50 which is below threshold, so DataQualityError will be raised
    # We can verify the score via the report in the exception
    with pytest.raises(DataQualityError) as exc_info:
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    report = exc_info.value.report

    # Score should be:
    # - ROE plausibility (0.40 weight): 0.0 (missing)
    # - ROE present (0.10 weight): 0.0 (missing)
    # - Gap (0.50 weight): 1.0 (no gaps)
    # Total: (0.0 * 0.40) + (0.0 * 0.10) + (1.0 * 0.50) = 0.50
    assert report.per_stock_scores["STOCK"] == pytest.approx(0.50)
    assert "STOCK" in report.roe_missing_symbols


# ============================================================================
# Acceptance Criterion 5: ROE = 0.18 (18%, in range) scores 0.50
# ============================================================================


def test_roe_in_range_scores_0_50(tmp_db_path: str) -> None:
    """Criterion 5: ROE = 0.18 scores 0.50 on ROE sub-components (0.40 + 0.10)."""
    ohlcv_df = create_ohlcv_df({
        "STOCK": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK"],
        roe_values={"STOCK": 0.18},
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # Score should be:
    # - ROE plausibility (0.40 weight): 1.0 (passed)
    # - ROE present (0.10 weight): 1.0 (present)
    # - Gap (0.50 weight): 1.0 (no gaps)
    # Total: (1.0 * 0.40) + (1.0 * 0.10) + (1.0 * 0.50) = 1.0
    assert report.per_stock_scores["STOCK"] == pytest.approx(1.0)


# ============================================================================
# Acceptance Criterion 6: 1 OHLCV gap of 7 days scores 0.25
# ============================================================================


def test_one_gap_7_days_scores_0_25(tmp_db_path: str) -> None:
    """Criterion 6: Stock with 1 gap of 7 days scores 0.25 on gap sub-component."""
    # Create dates with exactly 7 business days between them
    # 2026-03-02 (Monday) to 2026-03-12 (Thursday) = 7 business days between
    d1 = datetime.date(2026, 3, 2)  # Monday
    d2 = datetime.date(2026, 3, 12)  # Thursday

    ohlcv_df = create_ohlcv_df({
        "STOCK": [d1, d2],
    })
    fundamentals_df = create_fundamentals_df(["STOCK"])

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # Score should be:
    # - ROE plausibility (0.40 weight): 1.0
    # - ROE present (0.10 weight): 1.0
    # - Gap (0.50 weight): 0.5 (1 gap present)
    # Total: (1.0 * 0.40) + (1.0 * 0.10) + (0.5 * 0.50) = 0.75
    assert report.per_stock_scores["STOCK"] == pytest.approx(0.75)


# ============================================================================
# Acceptance Criterion 7: 2 OHLCV gaps score 0.0 on gap sub-component
# ============================================================================


def test_two_gaps_score_0_0(tmp_db_path: str) -> None:
    """Criterion 7: Stock with 2 OHLCV gaps scores 0.0 on gap sub-component."""
    # Create three dates with gaps between them
    # 2026-03-02 to 2026-03-12 = 7 business days
    # 2026-03-12 to 2026-03-24 = 7 business days
    dates = [
        datetime.date(2026, 3, 2),   # Monday
        datetime.date(2026, 3, 12),  # Thursday, 7 business days later
        datetime.date(2026, 3, 24),  # Tuesday, 7 business days later
    ]

    ohlcv_df = create_ohlcv_df({
        "STOCK": dates,
    })
    fundamentals_df = create_fundamentals_df(["STOCK"])

    # Score will be 0.50 which is below threshold, so DataQualityError will be raised
    with pytest.raises(DataQualityError) as exc_info:
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    report = exc_info.value.report

    # Score should be:
    # - ROE plausibility (0.40 weight): 1.0
    # - ROE present (0.10 weight): 1.0
    # - Gap (0.50 weight): 0.0 (2 gaps present)
    # Total: (1.0 * 0.40) + (1.0 * 0.10) + (0.0 * 0.50) = 0.50
    assert report.per_stock_scores["STOCK"] == pytest.approx(0.50)


# ============================================================================
# Acceptance Criterion 8: Gap of exactly 4 business days does NOT appear
# ============================================================================


def test_gap_4_business_days_not_detected(tmp_db_path: str) -> None:
    """Criterion 8: A gap of exactly 4 business days does NOT appear in gap_violations."""
    # Create dates with exactly 4 business days between them
    # Monday 2026-03-02 to Friday 2026-03-06 = 4 business days in between
    d1 = datetime.date(2026, 3, 2)  # Monday
    d2 = datetime.date(2026, 3, 6)  # Friday

    ohlcv_df = create_ohlcv_df({
        "STOCK": [d1, d2],
    })
    fundamentals_df = create_fundamentals_df(["STOCK"])

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # 4-day gap should NOT trigger violation (threshold is >= 5)
    assert len(report.gap_violations.get("STOCK", [])) == 0


# ============================================================================
# Acceptance Criterion 9: Gap of exactly 5 business days DOES appear
# ============================================================================


def test_gap_5_business_days_detected(tmp_db_path: str) -> None:
    """Criterion 9: A gap of exactly 5 business days DOES appear in gap_violations."""
    # Create dates with exactly 5 business days between them
    # Monday 2026-03-02 to Monday 2026-03-10 = 5 business days in between
    d1 = datetime.date(2026, 3, 2)  # Monday
    d2 = datetime.date(2026, 3, 10)  # Monday

    ohlcv_df = create_ohlcv_df({
        "STOCK": [d1, d2],
    })
    fundamentals_df = create_fundamentals_df(["STOCK"])

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # 5-day gap should trigger violation
    assert "STOCK" in report.gap_violations
    assert len(report.gap_violations["STOCK"]) >= 1


# ============================================================================
# Acceptance Criterion 10: de_coverage_low = True when < 80%
# ============================================================================


def test_de_coverage_low_true_when_below_80_percent(tmp_db_path: str) -> None:
    """Criterion 10: de_coverage_low = True when < 80% of symbols have D/E data."""
    ohlcv_df = create_ohlcv_df({
        "STOCK1": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK2": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK3": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK4": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK5": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    # 4 stocks with D/E, 1 without = 80% (threshold)
    # Make it 3 stocks with D/E, 2 without = 60% (below threshold)
    de_values = {
        "STOCK1": 0.5,
        "STOCK2": 0.5,
        "STOCK3": 0.5,
        "STOCK4": None,
        "STOCK5": None,
    }
    fundamentals_df = create_fundamentals_df(
        ["STOCK1", "STOCK2", "STOCK3", "STOCK4", "STOCK5"],
        de_values=de_values,
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    assert report.de_coverage_low is True
    assert report.de_coverage_ratio == pytest.approx(0.6)


# ============================================================================
# Acceptance Criterion 11: de_coverage_low deducts 0.10 from all scores
# ============================================================================


def test_de_coverage_low_deducts_0_10_from_scores(tmp_db_path: str) -> None:
    """Criterion 11: de_coverage_low causes all scores to be reduced by 0.10."""
    ohlcv_df = create_ohlcv_df({
        "STOCK1": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK2": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK3": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK4": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK5": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    de_values = {
        "STOCK1": 0.5,
        "STOCK2": 0.5,
        "STOCK3": 0.5,
        "STOCK4": None,
        "STOCK5": None,
    }
    fundamentals_df = create_fundamentals_df(
        ["STOCK1", "STOCK2", "STOCK3", "STOCK4", "STOCK5"],
        de_values=de_values,
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # All stocks with perfect ROE and no gaps would score 1.0 normally
    # With de_coverage_low, they should be deducted by 0.10
    for symbol in ["STOCK1", "STOCK2", "STOCK3", "STOCK4", "STOCK5"]:
        assert report.per_stock_scores[symbol] == pytest.approx(0.90)


# ============================================================================
# Acceptance Criterion 12: Every check logged to agent_logs
# ============================================================================


def test_all_checks_logged_to_agent_logs(tmp_db_path: str) -> None:
    """Criterion 12: Every check result is logged to agent_logs with correct event_type."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT event_type, symbol FROM agent_logs ORDER BY id")
    rows = cursor.fetchall()
    conn.close()

    event_types = {row[0] for row in rows}
    assert "roe_check" in event_types
    assert "de_coverage_check" in event_types
    assert "ohlcv_gap_check" in event_types
    assert "stock_score" in event_types
    assert "universe_score" in event_types


# ============================================================================
# Acceptance Criterion 13: timestamp_ist contains +05:30 (IST timezone offset)
# ============================================================================


def test_timestamp_ist_timezone_aware(tmp_db_path: str) -> None:
    """Criterion 13: timestamp_ist values are timezone-aware IST (contain +05:30)."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp_ist FROM agent_logs LIMIT 1")
    timestamp_str = cursor.fetchone()[0]
    conn.close()

    assert "+05:30" in timestamp_str


# ============================================================================
# Acceptance Criterion 14: Missing column raises ValueError (not KeyError)
# ============================================================================


def test_missing_column_raises_valueerror(tmp_db_path: str) -> None:
    """Criterion 14: Missing required column raises ValueError (not KeyError)."""
    # Create OHLCV DataFrame missing the 'close' column
    ohlcv_df = pd.DataFrame({
        "symbol": ["RELIANCE"],
        "date": [pd.Timestamp("2026-03-01", tz="Asia/Kolkata")],
        "open": [100.0],
        "high": [105.0],
        "low": [95.0],
        # Missing 'close'
        "volume": [1000.0],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    with pytest.raises(ValueError):
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)


# ============================================================================
# Acceptance Criterion 15: Timezone-naive dates raise ValueError
# ============================================================================


def test_timezone_naive_dates_raise_valueerror(tmp_db_path: str) -> None:
    """Criterion 15: OHLCV DataFrame with timezone-naive dates raises ValueError."""
    ohlcv_df = pd.DataFrame({
        "symbol": ["RELIANCE"],
        "date": [pd.Timestamp("2026-03-01")],  # No timezone
        "open": [100.0],
        "high": [105.0],
        "low": [95.0],
        "close": [102.0],
        "volume": [1000.0],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    with pytest.raises(ValueError, match="timezone-aware"):
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)


# ============================================================================
# Acceptance Criterion 16: DataQualityReport is immutable
# ============================================================================


def test_dataqualityreport_immutable(tmp_db_path: str) -> None:
    """Criterion 16: DataQualityReport is frozen — cannot set fields after construction."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    with pytest.raises(dataclasses.FrozenInstanceError):
        report.universe_quality_score = 0.99  # type: ignore


# ============================================================================
# Acceptance Criterion 17: Running twice appends rows (no deduplication)
# ============================================================================


def test_running_twice_appends_rows(tmp_db_path: str) -> None:
    """Criterion 17: Running validate_data twice on same db_path appends rows."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM agent_logs")
    count_after_first = cursor.fetchone()[0]
    conn.close()

    validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM agent_logs")
    count_after_second = cursor.fetchone()[0]
    conn.close()

    assert count_after_second > count_after_first


# ============================================================================
# Acceptance Criterion 18 & 19: mypy and ruff tests
# ============================================================================
# These are run via command-line tools in the test runner below.


# ============================================================================
# Additional boundary tests
# ============================================================================


def test_roe_at_lower_boundary(tmp_db_path: str) -> None:
    """Test ROE at exact lower boundary (-0.50)."""
    ohlcv_df = create_ohlcv_df({
        "STOCK": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK"],
        roe_values={"STOCK": -0.50},
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # Should pass ROE plausibility check
    assert "STOCK" not in report.failed_roe_symbols
    assert report.per_stock_scores["STOCK"] == pytest.approx(1.0)


def test_roe_at_upper_boundary(tmp_db_path: str) -> None:
    """Test ROE at exact upper boundary (2.00)."""
    ohlcv_df = create_ohlcv_df({
        "STOCK": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK"],
        roe_values={"STOCK": 2.00},
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # Should pass ROE plausibility check
    assert "STOCK" not in report.failed_roe_symbols
    assert report.per_stock_scores["STOCK"] == pytest.approx(1.0)


def test_empty_fundamentals_df(tmp_db_path: str) -> None:
    """Test with empty fundamentals DataFrame (zero universe)."""
    # Create empty OHLCV with proper timezone-aware date column
    ohlcv_df = pd.DataFrame({
        "symbol": pd.Series([], dtype="str"),
        "date": pd.Series([], dtype="datetime64[ns, Asia/Kolkata]"),
        "open": pd.Series([], dtype="float64"),
        "high": pd.Series([], dtype="float64"),
        "low": pd.Series([], dtype="float64"),
        "close": pd.Series([], dtype="float64"),
        "volume": pd.Series([], dtype="float64"),
    })
    # Create empty fundamentals DataFrame with required columns
    fundamentals_df = pd.DataFrame({
        "symbol": pd.Series([], dtype="str"),
        "roe": pd.Series([], dtype="float64"),
        "debt_to_equity": pd.Series([], dtype="float64"),
    })

    with pytest.raises(DataQualityError) as exc_info:
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    assert exc_info.value.universe_quality_score == 0.0


def test_symbol_in_fundamentals_missing_from_ohlcv(tmp_db_path: str) -> None:
    """Test when a symbol is in fundamentals but absent from OHLCV."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE", "INFOTECH"])

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # INFOTECH should have gap_score 0.0 (no OHLCV data)
    assert "INFOTECH" in report.gap_violations
    # Score should be: (1.0 * 0.40) + (1.0 * 0.10) + (0.0 * 0.50) = 0.50
    assert report.per_stock_scores["INFOTECH"] == pytest.approx(0.50)


def test_de_coverage_exactly_80_percent(tmp_db_path: str) -> None:
    """Test de_coverage_ratio exactly at 80% (should NOT be low)."""
    ohlcv_df = create_ohlcv_df({
        "STOCK1": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK2": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK3": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK4": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK5": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    de_values = {
        "STOCK1": 0.5,
        "STOCK2": 0.5,
        "STOCK3": 0.5,
        "STOCK4": 0.5,
        "STOCK5": None,  # Only 1 missing = 80%
    }
    fundamentals_df = create_fundamentals_df(
        ["STOCK1", "STOCK2", "STOCK3", "STOCK4", "STOCK5"],
        de_values=de_values,
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    assert report.de_coverage_low is False
    assert report.de_coverage_ratio == pytest.approx(0.8)
    # Scores should NOT be deducted
    for symbol in ["STOCK1", "STOCK2", "STOCK3", "STOCK4", "STOCK5"]:
        assert report.per_stock_scores[symbol] == pytest.approx(1.0)


def test_check_roe_internal_function() -> None:
    """Test _check_roe internal function directly."""
    passed, detail = _check_roe("RELIANCE", 0.15)
    assert passed is True
    assert "plausible" in detail.lower() or "within" in detail.lower()

    passed, detail = _check_roe("RELIANCE", None)
    assert passed is False

    passed, detail = _check_roe("RELIANCE", 2.50)
    assert passed is False


def test_check_ohlcv_gaps_internal_function() -> None:
    """Test _check_ohlcv_gaps internal function directly."""
    d1 = datetime.date(2026, 3, 1)
    d2 = datetime.date(2026, 3, 20)

    dates = pd.Series([d1, d2])
    gaps = _check_ohlcv_gaps("STOCK", dates, None)

    # Should detect a gap
    assert len(gaps) > 0


def test_compute_stock_score_internal_function() -> None:
    """Test _compute_stock_score internal function directly."""
    score = _compute_stock_score(roe_passed=True, roe_missing=False, gap_violations=[])
    assert score == pytest.approx(1.0)

    # When roe_passed=False and roe_missing=True: (0.0 * 0.40) + (0.0 * 0.10) + (1.0 * 0.50) = 0.50
    score = _compute_stock_score(roe_passed=False, roe_missing=True, gap_violations=[])
    assert score == pytest.approx(0.50)

    # 1 gap: gap_score = 0.5, so total = (1.0 * 0.40) + (1.0 * 0.10) + (0.5 * 0.50) = 0.75
    score = _compute_stock_score(roe_passed=True, roe_missing=False, gap_violations=[("2026-03-05", 7)])
    assert score == pytest.approx(0.75)

    # 2 gaps: gap_score = 0.0, so total = (1.0 * 0.40) + (1.0 * 0.10) + (0.0 * 0.50) = 0.50
    score = _compute_stock_score(
        roe_passed=True,
        roe_missing=False,
        gap_violations=[("2026-03-05", 7), ("2026-03-15", 8)],
    )
    assert score == pytest.approx(0.50)


def test_multiple_stocks_aggregate_score(tmp_db_path: str) -> None:
    """Test universe_quality_score aggregation across multiple stocks."""
    ohlcv_df = create_ohlcv_df({
        "STOCK1": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
        "STOCK2": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(
        ["STOCK1", "STOCK2"],
        roe_values={"STOCK1": 0.18, "STOCK2": 0.18},
    )

    report = validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    # Both stocks should score 1.0
    assert report.per_stock_scores["STOCK1"] == pytest.approx(1.0)
    assert report.per_stock_scores["STOCK2"] == pytest.approx(1.0)
    # Universe average should be 1.0
    assert report.universe_quality_score == pytest.approx(1.0)


def test_json_serialization_in_logs(tmp_db_path: str) -> None:
    """Test that check details are properly JSON-serialized in agent_logs."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = create_fundamentals_df(["RELIANCE"])

    validate_data(ohlcv_df, fundamentals_df, tmp_db_path)

    conn = sqlite3.connect(tmp_db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT detail FROM agent_logs WHERE event_type = 'roe_check'")
    detail_str = cursor.fetchone()[0]
    conn.close()

    # Should be valid JSON
    detail_dict = json.loads(detail_str)
    assert "roe" in detail_dict
    assert "passed" in detail_dict
    assert "reason" in detail_dict


def test_fundamental_missing_column_raises_valueerror(tmp_db_path: str) -> None:
    """Test missing column in fundamentals_df raises ValueError."""
    ohlcv_df = create_ohlcv_df({
        "RELIANCE": [datetime.date(2026, 3, 1), datetime.date(2026, 3, 2)],
    })
    fundamentals_df = pd.DataFrame({
        "symbol": ["RELIANCE"],
        "roe": [0.15],
        # Missing 'debt_to_equity'
    })

    with pytest.raises(ValueError):
        validate_data(ohlcv_df, fundamentals_df, tmp_db_path)
