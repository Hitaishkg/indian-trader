"""Tests for src/strategy/quality_filter.py"""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.strategy.quality_filter import (
    apply_quality_filter,
    FilterReport,
    ROE_THRESHOLD,
    DE_THRESHOLD,
    VOLUME_VALUE_THRESHOLD,
    PRICE_THRESHOLD,
    PROXIMITY_THRESHOLD,
    MIN_UNIVERSE_SIZE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def build_fundamentals():
    """Factory to build fundamentals_df with defaults and overrides."""

    def _build(symbols: list[str], overrides: dict | None = None) -> pd.DataFrame:
        """Build fundamentals DataFrame.

        Args:
            symbols: List of symbol strings.
            overrides: Dict mapping symbol to dict of field overrides.
                      E.g. {"RELIANCE": {"roe": 0.10, "debt_to_equity": 1.5}}

        Returns:
            pd.DataFrame with columns: symbol, roe, debt_to_equity,
            eps_positive_4q, data_quality
        """
        if overrides is None:
            overrides = {}

        rows = []
        for symbol in symbols:
            row = {
                "symbol": symbol,
                "roe": 0.20,
                "debt_to_equity": 0.5,
                "eps_positive_4q": True,
                "data_quality": "ok",
            }
            if symbol in overrides:
                row.update(overrides[symbol])
            rows.append(row)

        return pd.DataFrame(rows)

    return _build


@pytest.fixture
def build_ohlcv():
    """Factory to build ohlcv_df with randomized close/volume."""

    def _build(
        symbols: list[str], n_rows: int = 260, base_price: float = 500.0
    ) -> pd.DataFrame:
        """Build OHLCV DataFrame.

        Args:
            symbols: List of symbol strings.
            n_rows: Number of trading days per symbol.
            base_price: Starting close price.

        Returns:
            pd.DataFrame with columns: symbol, date, open, high, low, close, volume
            DatetimeIndex uses Asia/Kolkata timezone.
        """
        np.random.seed(42)

        rows = []
        tz = ZoneInfo("Asia/Kolkata")
        end_date = datetime(2026, 3, 25, tzinfo=tz)

        for symbol in symbols:
            price = base_price
            for i in range(n_rows):
                date = end_date - timedelta(days=n_rows - i - 1)
                # Random walk for price
                price = price + np.random.normal(0, 5)
                price = max(price, 50.1)  # Never below ₹50
                close = price

                row = {
                    "symbol": symbol,
                    "date": date,
                    "open": close + np.random.uniform(-2, 2),
                    "high": close + np.random.uniform(0, 3),
                    "low": close - np.random.uniform(0, 3),
                    "close": close,
                    "volume": int(np.random.uniform(100000, 500000)),
                }
                rows.append(row)

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df

    return _build


# ---------------------------------------------------------------------------
# Test: Happy Path
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_happy_path_all_pass(mock_log, build_fundamentals, build_ohlcv):
    """All 5 symbols pass — output has 5 rows, all passed_hard_filters=True."""
    fund_df = build_fundamentals(["RELIANCE", "TCS", "INFY", "HDFC", "ICICI"])
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "INFY", "HDFC", "ICICI"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert len(filtered_df) == 5
    assert all(filtered_df["passed_hard_filters"] == True)
    assert report.passed_count == 5
    assert report.thin_universe == False


@patch("src.strategy.quality_filter.log_agent_action")
def test_output_columns_complete(mock_log, build_fundamentals, build_ohlcv):
    """Verify all 9 output columns are present."""
    fund_df = build_fundamentals(["RELIANCE", "TCS", "INFY"])
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "INFY"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    expected_cols = [
        "symbol",
        "roe",
        "debt_to_equity",
        "avg_daily_value",
        "latest_price",
        "high_52w",
        "pct_from_52w_high",
        "within_30pct_of_52w_high",
        "passed_hard_filters",
    ]
    assert list(filtered_df.columns) == expected_cols


# ---------------------------------------------------------------------------
# Test: Individual Filter Failures
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_roe_below_threshold(mock_log, build_fundamentals, build_ohlcv):
    """ROE=0.14 (below 0.15) — stock excluded."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "LOW_ROE", "HDFC", "ICICI"],
        {"LOW_ROE": {"roe": 0.14}},
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "LOW_ROE", "HDFC", "ICICI"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "LOW_ROE" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["roe"] >= 1


@patch("src.strategy.quality_filter.log_agent_action")
def test_de_above_threshold(mock_log, build_fundamentals, build_ohlcv):
    """D/E=1.1 (above 1.0) — stock excluded."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "HIGH_DE", "HDFC", "ICICI"],
        {"HIGH_DE": {"debt_to_equity": 1.1}},
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "HIGH_DE", "HDFC", "ICICI"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "HIGH_DE" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["debt_to_equity"] >= 1


@patch("src.strategy.quality_filter.log_agent_action")
def test_eps_negative(mock_log, build_fundamentals, build_ohlcv):
    """eps_positive_4q=False — stock excluded."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "NEG_EPS", "HDFC", "ICICI"],
        {"NEG_EPS": {"eps_positive_4q": False}},
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "NEG_EPS", "HDFC", "ICICI"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "NEG_EPS" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["eps"] >= 1


@patch("src.strategy.quality_filter.log_agent_action")
def test_volume_too_low(mock_log, build_fundamentals, build_ohlcv):
    """Very low volume (< 20 crore) — stock excluded."""
    fund_df = build_fundamentals(["RELIANCE", "TCS", "LOW_VOL", "HDFC", "ICICI"])
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "HDFC", "ICICI"], base_price=500.0)

    # Add LOW_VOL with very low volume
    low_vol_rows = []
    tz = ZoneInfo("Asia/Kolkata")
    end_date = datetime(2026, 3, 25, tzinfo=tz)
    for i in range(260):
        date = end_date - timedelta(days=260 - i - 1)
        low_vol_rows.append(
            {
                "symbol": "LOW_VOL",
                "date": date,
                "open": 500.0,
                "high": 502.0,
                "low": 498.0,
                "close": 500.0,
                "volume": 1,  # Essentially 0 value
            }
        )
    low_vol_df = pd.DataFrame(low_vol_rows)
    low_vol_df["date"] = pd.to_datetime(low_vol_df["date"])
    ohlcv_df = pd.concat([ohlcv_df, low_vol_df], ignore_index=True)

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "LOW_VOL" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["volume"] >= 1


@patch("src.strategy.quality_filter.log_agent_action")
def test_price_too_low(mock_log, build_fundamentals, build_ohlcv):
    """latest_price=45.0 (below 50) — stock excluded."""
    fund_df = build_fundamentals(["RELIANCE", "TCS", "LOW_PRICE", "HDFC", "ICICI"])
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "HDFC", "ICICI"], base_price=500.0)

    # Add LOW_PRICE with close=45
    low_price_rows = []
    tz = ZoneInfo("Asia/Kolkata")
    end_date = datetime(2026, 3, 25, tzinfo=tz)
    for i in range(260):
        date = end_date - timedelta(days=260 - i - 1)
        low_price_rows.append(
            {
                "symbol": "LOW_PRICE",
                "date": date,
                "open": 45.0,
                "high": 46.0,
                "low": 44.0,
                "close": 45.0,
                "volume": 100000,
            }
        )
    low_price_df = pd.DataFrame(low_price_rows)
    low_price_df["date"] = pd.to_datetime(low_price_df["date"])
    ohlcv_df = pd.concat([ohlcv_df, low_price_df], ignore_index=True)

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "LOW_PRICE" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["price"] >= 1


# ---------------------------------------------------------------------------
# Test: Soft Filter (52-week high proximity)
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_soft_filter_not_eliminating(mock_log, build_fundamentals, build_ohlcv):
    """within_30pct_of_52w_high=False does NOT eliminate stock."""
    fund_df = build_fundamentals(["RELIANCE", "TCS", "INFY"])
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "INFY"], base_price=500.0)

    # Force INFY to have pct_from_52w_high = 0.35 (outside 30%)
    # By setting most recent close to 35% below the high
    infy_mask = ohlcv_df["symbol"] == "INFY"
    infy_rows = ohlcv_df[infy_mask].copy()
    max_close = infy_rows["close"].max()
    # Set most recent close to 65% of high (35% down from high)
    recent_close = max_close * 0.65
    ohlcv_df.loc[infy_mask & (ohlcv_df.index == infy_rows.index[-1]), "close"] = (
        recent_close
    )

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    infy_row = filtered_df[filtered_df["symbol"] == "INFY"]
    assert not infy_row.empty
    assert infy_row["within_30pct_of_52w_high"].values[0] == False


# ---------------------------------------------------------------------------
# Test: Thin Universe
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_thin_universe_fewer_than_3(
    mock_log, build_fundamentals, build_ohlcv
):
    """Only 2 stocks pass — returns empty DF + thin_universe=True."""
    # 5 total, only 2 pass
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "LOW_ROE1", "LOW_ROE2", "LOW_ROE3"],
        {
            "LOW_ROE1": {"roe": 0.10},
            "LOW_ROE2": {"roe": 0.10},
            "LOW_ROE3": {"roe": 0.10},
        },
    )
    ohlcv_df = build_ohlcv(
        ["RELIANCE", "TCS", "LOW_ROE1", "LOW_ROE2", "LOW_ROE3"]
    )

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert len(filtered_df) == 0
    assert report.thin_universe == True
    assert report.passed_count == 2


@patch("src.strategy.quality_filter.log_agent_action")
def test_thin_universe_zero_pass(mock_log, build_fundamentals, build_ohlcv):
    """No stocks pass — thin_universe=True, empty DF."""
    fund_df = build_fundamentals(
        ["LOW1", "LOW2", "LOW3"],
        {
            "LOW1": {"roe": 0.10},
            "LOW2": {"roe": 0.10},
            "LOW3": {"roe": 0.10},
        },
    )
    ohlcv_df = build_ohlcv(["LOW1", "LOW2", "LOW3"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert len(filtered_df) == 0
    assert report.thin_universe == True
    assert report.passed_count == 0


# ---------------------------------------------------------------------------
# Test: Stale / Failed Fundamentals
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_stale_fundamentals_auto_fail(
    mock_log, build_fundamentals, build_ohlcv
):
    """data_quality='fundamentals_stale' — all filters failed."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "GOOD1", "STALE1", "STALE2", "STALE3"],
        {
            "STALE1": {"data_quality": "fundamentals_stale"},
            "STALE2": {"data_quality": "fundamentals_stale"},
            "STALE3": {"data_quality": "fundamentals_stale"},
        },
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "GOOD1", "STALE1", "STALE2", "STALE3"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    # 3 pass (RELIANCE, TCS, GOOD1), 3 stale
    assert len(filtered_df) == 3
    assert "STALE1" not in filtered_df["symbol"].values
    # All 5 failure keys should have count >= 3 (3 stale stocks)
    for key in ["roe", "debt_to_equity", "eps", "volume", "price"]:
        assert report.filter_failure_counts[key] >= 3


@patch("src.strategy.quality_filter.log_agent_action")
def test_failed_fundamentals_auto_fail(mock_log, build_fundamentals, build_ohlcv):
    """data_quality='failed' — all filters failed."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "GOOD1", "FAIL1", "FAIL2", "FAIL3"],
        {
            "FAIL1": {"data_quality": "failed"},
            "FAIL2": {"data_quality": "failed"},
            "FAIL3": {"data_quality": "failed"},
        },
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "GOOD1", "FAIL1", "FAIL2", "FAIL3"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert len(filtered_df) == 3
    assert "FAIL1" not in filtered_df["symbol"].values


# ---------------------------------------------------------------------------
# Test: OHLCV Missing
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_ohlcv_missing_symbol(mock_log, build_fundamentals, build_ohlcv):
    """Symbol in fundamentals but absent from ohlcv_df — excluded."""
    fund_df = build_fundamentals(["RELIANCE", "TCS", "MISSING"])
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "MISSING" not in filtered_df["symbol"].values
    # MISSING should fail volume and price filters
    assert report.filter_failure_counts["volume"] >= 1
    assert report.filter_failure_counts["price"] >= 1


# ---------------------------------------------------------------------------
# Test: Filter Failure Counts Accuracy
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_filter_failure_counts_accurate(
    mock_log, build_fundamentals, build_ohlcv
):
    """3 symbols each failing a different filter."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "LOW_ROE", "HIGH_DE", "NEG_EPS"],
        {
            "LOW_ROE": {"roe": 0.10},
            "HIGH_DE": {"debt_to_equity": 1.5},
            "NEG_EPS": {"eps_positive_4q": False},
        },
    )
    ohlcv_df = build_ohlcv(
        ["RELIANCE", "TCS", "LOW_ROE", "HIGH_DE", "NEG_EPS"]
    )

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert report.filter_failure_counts["roe"] == 1
    assert report.filter_failure_counts["debt_to_equity"] == 1
    assert report.filter_failure_counts["eps"] == 1


# ---------------------------------------------------------------------------
# Test: Input Validation
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_empty_fundamentals_raises(mock_log, build_fundamentals, build_ohlcv):
    """Empty fundamentals_df raises ValueError."""
    fund_df = pd.DataFrame()
    ohlcv_df = build_ohlcv(["RELIANCE"])

    with pytest.raises(ValueError, match="fundamentals_df must not be empty"):
        apply_quality_filter(fund_df, ohlcv_df)


@patch("src.strategy.quality_filter.log_agent_action")
def test_empty_ohlcv_raises(mock_log, build_fundamentals, build_ohlcv):
    """Empty ohlcv_df raises ValueError."""
    fund_df = build_fundamentals(["RELIANCE"])
    ohlcv_df = pd.DataFrame()

    with pytest.raises(ValueError, match="ohlcv_df must not be empty"):
        apply_quality_filter(fund_df, ohlcv_df)


@patch("src.strategy.quality_filter.log_agent_action")
def test_missing_fundamentals_column_raises(mock_log, build_fundamentals, build_ohlcv):
    """Missing required column in fundamentals_df raises ValueError."""
    fund_df = build_fundamentals(["RELIANCE"])
    fund_df = fund_df.drop(columns=["roe"])
    ohlcv_df = build_ohlcv(["RELIANCE"])

    with pytest.raises(ValueError, match="fundamentals_df missing required columns"):
        apply_quality_filter(fund_df, ohlcv_df)


@patch("src.strategy.quality_filter.log_agent_action")
def test_missing_ohlcv_column_raises(mock_log, build_fundamentals, build_ohlcv):
    """Missing required column in ohlcv_df raises ValueError."""
    fund_df = build_fundamentals(["RELIANCE"])
    ohlcv_df = build_ohlcv(["RELIANCE"])
    ohlcv_df = ohlcv_df.drop(columns=["close"])

    with pytest.raises(ValueError, match="ohlcv_df missing required columns"):
        apply_quality_filter(fund_df, ohlcv_df)


# ---------------------------------------------------------------------------
# Test: FilterReport properties
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_filter_report_fields(mock_log, build_fundamentals, build_ohlcv):
    """FilterReport has correct fields and values."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "INFY", "LOW_ROE"],
        {"LOW_ROE": {"roe": 0.10}},
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "INFY", "LOW_ROE"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert report.universe_size == 4
    assert report.passed_count == 3
    assert report.failed_count == 1
    assert report.thin_universe == False
    assert isinstance(report.filter_failure_counts, dict)
    assert isinstance(report.filtered_at_ist, str)
    assert len(report.filtered_at_ist) > 0


@patch("src.strategy.quality_filter.log_agent_action")
def test_filter_report_frozen(mock_log, build_fundamentals, build_ohlcv):
    """FilterReport is frozen — cannot mutate."""
    fund_df = build_fundamentals(["RELIANCE"])
    ohlcv_df = build_ohlcv(["RELIANCE"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    with pytest.raises(Exception):  # FrozenInstanceError
        report.universe_size = 999


# ---------------------------------------------------------------------------
# Test: Percentage from 52-week high calculation
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_pct_from_52w_high_calculation(mock_log, build_fundamentals, build_ohlcv):
    """pct_from_52w_high = (high_52w - latest_price) / high_52w."""
    # Build OHLCV with controlled prices
    tz = ZoneInfo("Asia/Kolkata")
    end_date = datetime(2026, 3, 25, tzinfo=tz)

    rows = []
    for symbol in ["TEST", "DUMMY1", "DUMMY2"]:
        for i in range(260):
            date = end_date - timedelta(days=260 - i - 1)
            if symbol == "TEST":
                # Last row: close=900, all others: close=1000
                close = 1000.0 if i < 259 else 900.0
            else:
                # Dummy symbols: constant price
                close = 500.0
            # Volume must be high enough: close * volume > 20 crore
            volume = 300000
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": volume,
                }
            )

    ohlcv_df = pd.DataFrame(rows)
    ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])

    fund_df = build_fundamentals(["TEST", "DUMMY1", "DUMMY2"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert len(filtered_df) > 0, "Should pass all filters with 3+ symbols"
    test_row = filtered_df[filtered_df["symbol"] == "TEST"]
    assert len(test_row) > 0, "TEST should be in result"
    pct = test_row["pct_from_52w_high"].values[0]
    # Expected: (1000 - 900) / 1000 = 0.1
    assert abs(pct - 0.1) < 1e-10
    assert test_row["within_30pct_of_52w_high"].values[0] == True


# ---------------------------------------------------------------------------
# Test: Lookback days parameter
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_avg_daily_value_uses_lookback(mock_log, build_fundamentals, build_ohlcv):
    """With lookback_days=10 vs 260, avg_daily_value differs."""
    # Build 260 rows where early rows have low price and recent have high price
    tz = ZoneInfo("Asia/Kolkata")
    end_date = datetime(2026, 3, 25, tzinfo=tz)

    rows = []
    for symbol in ["TREND", "DUMMY1", "DUMMY2"]:
        for i in range(260):
            date = end_date - timedelta(days=260 - i - 1)
            if symbol == "TREND":
                # Linear increase from 200 to 800
                close = 200 + (600 * i / 259)
            else:
                # Dummy symbols: constant price
                close = 500.0
            # Volume must be high enough to meet threshold at lower prices
            # At close=200, volume needs > 20_000_000 / 200 = 100_000
            volume = 150000
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": volume,
                }
            )

    ohlcv_df = pd.DataFrame(rows)
    ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])
    fund_df = build_fundamentals(["TREND", "DUMMY1", "DUMMY2"])

    filtered_260, report_260 = apply_quality_filter(fund_df, ohlcv_df, lookback_days=260)
    filtered_10, report_10 = apply_quality_filter(fund_df, ohlcv_df, lookback_days=10)

    assert len(filtered_260) > 0, "Should pass with lookback_days=260"
    assert len(filtered_10) > 0, "Should pass with lookback_days=10"

    val_260 = filtered_260[filtered_260["symbol"] == "TREND"]["avg_daily_value"].values[0]
    val_10 = filtered_10[filtered_10["symbol"] == "TREND"]["avg_daily_value"].values[0]

    # lookback_days=10 uses only recent (high price) rows, so avg should be much higher
    assert val_10 > val_260


# ---------------------------------------------------------------------------
# Test: All filters evaluated (no short-circuit)
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_all_filters_evaluated_no_short_circuit(
    mock_log, build_fundamentals, build_ohlcv
):
    """Symbol failing multiple filters — each failure counted."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "MULTI_FAIL"],
        {
            "MULTI_FAIL": {
                "roe": 0.10,  # Fails ROE
                "debt_to_equity": 1.5,  # Fails D/E
                "eps_positive_4q": False,  # Fails EPS
            }
        },
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "MULTI_FAIL"], base_price=100.0)

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "MULTI_FAIL" not in filtered_df["symbol"].values
    # All three failures should be counted
    assert report.filter_failure_counts["roe"] >= 1
    assert report.filter_failure_counts["debt_to_equity"] >= 1
    assert report.filter_failure_counts["eps"] >= 1


# ---------------------------------------------------------------------------
# Test: Logging
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_log_agent_action_called(mock_log, build_fundamentals, build_ohlcv):
    """log_agent_action is called with agent_name='quality_filter'."""
    fund_df = build_fundamentals(["RELIANCE"])
    ohlcv_df = build_ohlcv(["RELIANCE"])

    apply_quality_filter(fund_df, ohlcv_df)

    # Verify log_agent_action was called
    assert mock_log.called
    # Verify at least one call has agent_name="quality_filter"
    calls_with_agent = [
        call
        for call in mock_log.call_args_list
        if call.kwargs.get("agent_name") == "quality_filter"
    ]
    assert len(calls_with_agent) > 0


# ---------------------------------------------------------------------------
# Test: NaN handling
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_nan_roe_fails(mock_log, build_fundamentals, build_ohlcv):
    """ROE=NaN — fails ROE filter, logged as roe_data_missing."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "NAN_ROE"],
        {"NAN_ROE": {"roe": np.nan}},
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "NAN_ROE"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "NAN_ROE" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["roe"] >= 1


@patch("src.strategy.quality_filter.log_agent_action")
def test_nan_de_fails(mock_log, build_fundamentals, build_ohlcv):
    """D/E=NaN — fails D/E filter, logged as de_data_missing."""
    fund_df = build_fundamentals(
        ["RELIANCE", "TCS", "NAN_DE"],
        {"NAN_DE": {"debt_to_equity": np.nan}},
    )
    ohlcv_df = build_ohlcv(["RELIANCE", "TCS", "NAN_DE"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    assert "NAN_DE" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["debt_to_equity"] >= 1


# ---------------------------------------------------------------------------
# Test: Volume threshold boundary
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_volume_threshold_boundary(mock_log, build_fundamentals, build_ohlcv):
    """avg_daily_value exactly at threshold (20 crore) — fails (strictly >)."""
    tz = ZoneInfo("Asia/Kolkata")
    end_date = datetime(2026, 3, 25, tzinfo=tz)

    # Create OHLCV where close * volume always equals exactly 20 crore
    rows = []
    for i in range(260):
        date = end_date - timedelta(days=260 - i - 1)
        close = 500.0
        volume = int(20_000_000.0 / close)  # = 40000
        rows.append(
            {
                "symbol": "BOUNDARY",
                "date": date,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": volume,
            }
        )

    ohlcv_df = pd.DataFrame(rows)
    ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])

    fund_df = build_fundamentals(["BOUNDARY"])

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    # Should be excluded (threshold is strictly >)
    assert "BOUNDARY" not in filtered_df["symbol"].values
    assert report.filter_failure_counts["volume"] >= 1


# ---------------------------------------------------------------------------
# Test: Sorted output
# ---------------------------------------------------------------------------


@patch("src.strategy.quality_filter.log_agent_action")
def test_output_sorted_by_symbol(mock_log, build_fundamentals, build_ohlcv):
    """Output is sorted by symbol ascending."""
    symbols = ["ZZZZZ", "AAAAA", "MMMMM", "BBBBB", "CCCCC"]
    fund_df = build_fundamentals(symbols)
    ohlcv_df = build_ohlcv(symbols)

    filtered_df, report = apply_quality_filter(fund_df, ohlcv_df)

    output_symbols = filtered_df["symbol"].tolist()
    assert output_symbols == sorted(output_symbols)
