"""Tests for src/indicators/technical.py — Technical Indicator Calculations.

Test coverage for:
- Happy path: all indicators computed for sufficient data
- Per-symbol isolation: indicators computed independently per symbol
- Minimum lookback logic: symbols with < 26 rows receive NaN for all indicators
- ATR Wilder smoothing: verification against manual calculation
- Error handling: missing columns, timezone-naive dates, empty DataFrames
- Edge cases: single-row symbols, NaN prices, extra columns
- Logging: insufficient lookback events logged correctly
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from src.indicators.technical import (
    add_indicators,
    compute_atr_series,
    MINIMUM_LOOKBACK,
    ATR_PERIOD,
    RSI_PERIOD,
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL_PERIOD,
    BB_LENGTH,
    BB_STD,
)


# ---------------------------------------------------------------------------
# Test Fixtures — Synthetic OHLCV DataFrame Builder
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_ohlcv_builder():
    """Factory fixture to build realistic synthetic OHLCV DataFrames.

    Returns a callable that accepts:
    - symbol (str): ticker symbol
    - n_rows (int): number of rows to generate
    - base_price (float): starting price in INR
    - tz (str): timezone string, default "Asia/Kolkata"

    Generates realistic OHLCV with:
    - Timezone-aware dates (daily trading days from 2024-01-01)
    - Close prices as random walk
    - High = close * 1.01, Low = close * 0.99
    - Open = previous close
    - Volume = random int between 1M and 10M shares
    """
    np.random.seed(42)

    def builder(
        symbol: str,
        n_rows: int,
        base_price: float = 100.0,
        tz: str = "Asia/Kolkata",
    ) -> pd.DataFrame:
        """Build a synthetic OHLCV DataFrame for one symbol."""
        # Generate dates: daily trading days from 2024-01-01
        date_range = pd.date_range(
            start="2024-01-01", periods=n_rows, freq="B", tz=tz
        )

        # Generate close prices as random walk
        returns = np.random.normal(loc=0.0005, scale=0.015, size=n_rows)
        close_prices = base_price * np.exp(np.cumsum(returns))

        # Generate OHLCV
        data = {
            "symbol": [symbol] * n_rows,
            "date": date_range,
            "close": close_prices,
            "high": close_prices * 1.01,
            "low": close_prices * 0.99,
            "open": np.concatenate(([base_price], close_prices[:-1])),
            "volume": np.random.randint(1_000_000, 10_000_000, size=n_rows),
        }
        return pd.DataFrame(data)

    return builder


@pytest.fixture
def sample_ohlcv_50rows(synthetic_ohlcv_builder):
    """Single symbol with 50 rows — sufficient for all indicators."""
    return synthetic_ohlcv_builder(symbol="RELIANCE", n_rows=50, base_price=2500.0)


@pytest.fixture
def sample_ohlcv_25rows(synthetic_ohlcv_builder):
    """Single symbol with exactly 25 rows — below MINIMUM_LOOKBACK."""
    return synthetic_ohlcv_builder(symbol="HDFC", n_rows=25, base_price=1600.0)


@pytest.fixture
def sample_ohlcv_26rows(synthetic_ohlcv_builder):
    """Single symbol with exactly 26 rows — at MINIMUM_LOOKBACK."""
    return synthetic_ohlcv_builder(symbol="INFY", n_rows=26, base_price=1200.0)


@pytest.fixture
def sample_ohlcv_1row(synthetic_ohlcv_builder):
    """Single symbol with 1 row — minimal edge case."""
    return synthetic_ohlcv_builder(symbol="TCS", n_rows=1, base_price=3000.0)


@pytest.fixture
def multi_symbol_combined(synthetic_ohlcv_builder):
    """Combined DataFrame: Symbol A (50 rows), Symbol B (50 rows), Symbol C (50 rows)."""
    a = synthetic_ohlcv_builder(symbol="RELIANCE", n_rows=50, base_price=2500.0)
    b = synthetic_ohlcv_builder(symbol="HDFC", n_rows=50, base_price=1600.0)
    c = synthetic_ohlcv_builder(symbol="INFY", n_rows=50, base_price=1200.0)
    return pd.concat([a, b, c], ignore_index=True)


# ---------------------------------------------------------------------------
# Test Scenarios — Map to Spec Section 11
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Tests 1-3: Core functionality — happy path scenarios."""

    def test_happy_path_columns_present(self, multi_symbol_combined):
        """Test 1: All 8 indicator columns present in output (50 rows × 3 symbols)."""
        result = add_indicators(multi_symbol_combined)

        expected_indicators = [
            "rsi",
            "macd",
            "macd_signal",
            "macd_hist",
            "bb_upper",
            "bb_mid",
            "bb_lower",
            "atr",
        ]
        for col in expected_indicators:
            assert col in result.columns, f"Missing indicator column: {col}"

    def test_happy_path_original_columns_unchanged(self, multi_symbol_combined):
        """Test 2: Original 7 columns unchanged after add_indicators()."""
        result = add_indicators(multi_symbol_combined)

        original_cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
        for col in original_cols:
            pd.testing.assert_series_equal(
                result[col],
                multi_symbol_combined[col],
                check_names=True,
                check_dtype=True,
            )

    def test_happy_path_row_count_preserved(self, multi_symbol_combined):
        """Test 3: Output row count equals input row count."""
        result = add_indicators(multi_symbol_combined)
        assert len(result) == len(multi_symbol_combined)

    def test_happy_path_no_nan_in_tail(self, sample_ohlcv_50rows):
        """Bonus: Tail rows (after lookback) should not be NaN."""
        result = add_indicators(sample_ohlcv_50rows)

        # The last 10 rows should have valid indicators (no NaN).
        tail = result.iloc[-10:, :]
        indicators = ["rsi", "atr", "macd", "bb_upper"]
        for col in indicators:
            assert not tail[col].isna().any(), f"Tail rows have NaN in {col}"


class TestPerSymbolIsolation:
    """Test 4: Verify per-symbol isolation — indicators computed independently."""

    def test_per_symbol_isolation(self, synthetic_ohlcv_builder):
        """Test 4: RSI values for Symbol A are independent of Symbol B's prices.

        Compute add_indicators() on combined DF and compare Symbol A's RSI
        against RSI computed on Symbol A alone. Must match within 1e-10.
        """
        # Build two symbols with very different price ranges
        a = synthetic_ohlcv_builder(symbol="RELIANCE", n_rows=50, base_price=100.0)
        b = synthetic_ohlcv_builder(symbol="HDFC", n_rows=50, base_price=2000.0)
        combined = pd.concat([a, b], ignore_index=True)

        # Compute on combined DF
        result_combined = add_indicators(combined)

        # Compute on Symbol A alone
        result_a_alone = add_indicators(a)

        # Compare Symbol A's RSI values (skip leading NaN)
        rsi_combined = result_combined[result_combined["symbol"] == "RELIANCE"]["rsi"]
        rsi_alone = result_a_alone["rsi"]

        # Reset indices for comparison
        rsi_combined.reset_index(drop=True, inplace=True)
        rsi_alone.reset_index(drop=True, inplace=True)

        # Must match within tolerance
        np.testing.assert_allclose(
            rsi_combined.dropna(),
            rsi_alone.dropna(),
            rtol=1e-10,
            atol=1e-10,
            err_msg="Symbol A RSI values differ when computed on combined vs alone",
        )


class TestInputImmutability:
    """Test 5: Input DataFrame is never mutated."""

    def test_input_immutability(self, multi_symbol_combined):
        """Test 5: Original DataFrame unchanged after add_indicators() call."""
        original_copy = multi_symbol_combined.copy()
        add_indicators(multi_symbol_combined)

        # Check that multi_symbol_combined is identical to the copy
        pd.testing.assert_frame_equal(
            multi_symbol_combined,
            original_copy,
            check_dtype=True,
            check_names=True,
        )


class TestMinimumLookback:
    """Tests 6-8: Minimum lookback logic — symbols with < 26 rows."""

    def test_lookback_25_rows_all_nan(self, synthetic_ohlcv_builder):
        """Test 6: Symbol with 25 rows (< MINIMUM_LOOKBACK) has all NaN indicators."""
        df_25 = synthetic_ohlcv_builder(symbol="HDFC", n_rows=25, base_price=1600.0)
        df_50 = synthetic_ohlcv_builder(symbol="RELIANCE", n_rows=50, base_price=2500.0)
        combined = pd.concat([df_25, df_50], ignore_index=True)

        result = add_indicators(combined)

        # All 8 indicators for HDFC (25 rows) must be NaN
        hdfc_rows = result[result["symbol"] == "HDFC"]
        indicators = ["rsi", "macd", "macd_signal", "macd_hist", "bb_upper", "bb_mid", "bb_lower", "atr"]
        for col in indicators:
            assert hdfc_rows[col].isna().all(), f"HDFC {col} should be all NaN, but is not"

        # RELIANCE (50 rows) should have valid indicators in tail
        reliance_rows = result[result["symbol"] == "RELIANCE"]
        for col in indicators:
            assert not reliance_rows.tail(10)[col].isna().any(), f"RELIANCE tail {col} should have valid values"

    def test_lookback_26_rows_not_all_nan(self, synthetic_ohlcv_builder):
        """Test 7: Symbol with exactly 26 rows (= MINIMUM_LOOKBACK) computes indicators."""
        df_26 = synthetic_ohlcv_builder(symbol="INFY", n_rows=26, base_price=1200.0)
        result = add_indicators(df_26)

        # At least the tail rows should have some valid indicator values
        indicators = ["rsi", "atr", "macd"]
        has_valid = False
        for col in indicators:
            if not result[col].isna().all():
                has_valid = True
                break

        assert has_valid, "Symbol with 26 rows should have at least some valid indicator values"

    def test_all_symbols_below_minimum(self, synthetic_ohlcv_builder):
        """Test 8: All symbols < MINIMUM_LOOKBACK → all NaN everywhere, no crash."""
        df_10 = synthetic_ohlcv_builder(symbol="A", n_rows=10, base_price=100.0)
        df_15 = synthetic_ohlcv_builder(symbol="B", n_rows=15, base_price=200.0)
        combined = pd.concat([df_10, df_15], ignore_index=True)

        result = add_indicators(combined)

        # All indicators should be NaN for all rows
        indicators = ["rsi", "macd", "macd_signal", "macd_hist", "bb_upper", "bb_mid", "bb_lower", "atr"]
        for col in indicators:
            assert result[col].isna().all(), f"All symbols < minimum should have NaN in {col}"


class TestATRWilderSmoothing:
    """Tests 9-10: ATR Wilder smoothing verification."""

    def test_atr_wilder_smoothing(self, sample_ohlcv_50rows):
        """Test 9: compute_atr_series() output matches ta.atr() directly.

        pandas-ta's RMA (Wilder) initialises the first ATR value as the SMA of
        the first N true ranges, then applies Wilder smoothing. This differs
        from a pure ewm(com=N-1, adjust=False) which uses a different warm-up.
        We verify our wrapper calls ta.atr() correctly by comparing against
        ta.atr() called directly on the same data.
        """
        import pandas_ta as ta  # type: ignore[import-untyped]

        atr_from_module = compute_atr_series(sample_ohlcv_50rows, period=14)

        # Direct ta.atr() call — ground truth for Wilder RMA as implemented
        ta_atr = ta.atr(
            high=sample_ohlcv_50rows["high"],
            low=sample_ohlcv_50rows["low"],
            close=sample_ohlcv_50rows["close"],
            length=14,
        ).reindex(sample_ohlcv_50rows.index)

        valid_mask = ~(atr_from_module.isna() | ta_atr.isna())
        assert valid_mask.sum() > 0, "No valid (non-NaN) ATR values to compare"
        np.testing.assert_allclose(
            atr_from_module[valid_mask].values,
            ta_atr[valid_mask].values,
            rtol=1e-10,
            atol=1e-10,
            err_msg="compute_atr_series() output does not match ta.atr() directly",
        )

    def test_compute_atr_series_standalone(self, sample_ohlcv_50rows):
        """Test 10: compute_atr_series() returns Series with correct index and NaN structure."""
        result = compute_atr_series(sample_ohlcv_50rows)

        assert isinstance(result, pd.Series), "compute_atr_series should return pd.Series"
        assert len(result) == len(sample_ohlcv_50rows), "Result length != input length"

        # Index should match input
        pd.testing.assert_index_equal(result.index, sample_ohlcv_50rows.index)

        # Leading rows should be NaN, tail should have valid floats
        # For period=14, first 13 rows should be NaN
        assert result.iloc[:13].isna().all(), "Leading 13 rows should be NaN"
        assert not result.iloc[-10:].isna().any(), "Tail rows should have valid floats"


class TestErrorHandling:
    """Tests 10-14: Error handling — missing columns, timezone issues, empty DF."""

    def test_missing_column_raises(self, sample_ohlcv_50rows):
        """Test 11: Missing 'close' column raises ValueError."""
        df_no_close = sample_ohlcv_50rows.drop(columns=["close"])
        with pytest.raises(ValueError, match="missing required columns"):
            add_indicators(df_no_close)

    def test_timezone_naive_raises(self, sample_ohlcv_50rows):
        """Test 12: Timezone-naive date column raises ValueError."""
        df_naive = sample_ohlcv_50rows.copy()
        df_naive["date"] = df_naive["date"].dt.tz_localize(None)
        with pytest.raises(ValueError, match="timezone-aware"):
            add_indicators(df_naive)

    def test_empty_dataframe_raises(self):
        """Test 13: Empty DataFrame raises ValueError."""
        df_empty = pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume"]
        )
        df_empty["date"] = pd.to_datetime([], utc=True).tz_convert("Asia/Kolkata")
        with pytest.raises(ValueError, match="empty"):
            add_indicators(df_empty)

    def test_compute_atr_missing_column(self, sample_ohlcv_50rows):
        """Test 13: compute_atr_series() missing 'high' raises ValueError."""
        df_no_high = sample_ohlcv_50rows.drop(columns=["high"])
        with pytest.raises(ValueError, match="missing required columns"):
            compute_atr_series(df_no_high)

    def test_compute_atr_missing_low(self, sample_ohlcv_50rows):
        """Additional: compute_atr_series() missing 'low' raises ValueError."""
        df_no_low = sample_ohlcv_50rows.drop(columns=["low"])
        with pytest.raises(ValueError, match="missing required columns"):
            compute_atr_series(df_no_low)

    def test_compute_atr_missing_close(self, sample_ohlcv_50rows):
        """Additional: compute_atr_series() missing 'close' raises ValueError."""
        df_no_close = sample_ohlcv_50rows.drop(columns=["close"])
        with pytest.raises(ValueError, match="missing required columns"):
            compute_atr_series(df_no_close)


class TestEdgeCases:
    """Tests 14-16: Edge cases — single row, NaN prices, extra columns."""

    def test_single_row_symbol_no_crash(self, sample_ohlcv_1row):
        """Test 14: Single symbol with 1 row does not crash; all indicators NaN."""
        result = add_indicators(sample_ohlcv_1row)

        indicators = ["rsi", "macd", "macd_signal", "macd_hist", "bb_upper", "bb_mid", "bb_lower", "atr"]
        for col in indicators:
            assert result[col].isna().all(), f"Single-row symbol should have NaN in {col}"

    def test_nan_prices_no_crash(self, sample_ohlcv_50rows):
        """Test 15: Symbol with NaN in close column does not crash."""
        df_with_nan = sample_ohlcv_50rows.copy()
        df_with_nan.loc[10:15, "close"] = np.nan

        # Should not raise
        result = add_indicators(df_with_nan)

        # Result should have some NaN indicators (from pandas-ta handling NaN)
        assert result["rsi"].isna().any(), "Result should contain NaN indicators from NaN prices"

    def test_extra_columns_preserved(self, sample_ohlcv_50rows):
        """Test 16: DataFrame with extra column (e.g. 'flag') preserves that column."""
        df_with_extra = sample_ohlcv_50rows.copy()
        df_with_extra["flag"] = np.arange(len(df_with_extra))

        result = add_indicators(df_with_extra)

        assert "flag" in result.columns, "Extra column 'flag' should be preserved"
        pd.testing.assert_series_equal(
            result["flag"],
            df_with_extra["flag"],
            check_names=True,
            check_dtype=True,
        )


class TestLogging:
    """Test 17: Logging verification for insufficient lookback."""

    def test_insufficient_lookback_logging(self, synthetic_ohlcv_builder):
        """Test 17: Insufficient lookback triggers log_agent_action with result='skipped'."""
        df_25 = synthetic_ohlcv_builder(symbol="HDFC", n_rows=25, base_price=1600.0)

        with patch("src.indicators.technical.log_agent_action") as mock_log:
            add_indicators(df_25)

            # Check that log_agent_action was called with result="skipped"
            calls = mock_log.call_args_list
            skipped_calls = [
                call
                for call in calls
                if call[1].get("result") == "skipped" and call[1].get("symbol") == "HDFC"
            ]
            assert len(skipped_calls) > 0, "log_agent_action should be called with result='skipped' for HDFC"


class TestParameterVariations:
    """Additional tests: custom period parameters."""

    def test_custom_rsi_period(self, sample_ohlcv_50rows):
        """Test with custom RSI period."""
        result = add_indicators(sample_ohlcv_50rows, rsi_period=9)
        assert "rsi" in result.columns
        # RSI with shorter period should have fewer leading NaN
        assert not result.tail(10)["rsi"].isna().all()

    def test_custom_atr_period(self, sample_ohlcv_50rows):
        """Test with custom ATR period."""
        result = add_indicators(sample_ohlcv_50rows, atr_period=7)
        assert "atr" in result.columns
        # ATR with shorter period should have fewer leading NaN
        assert not result.tail(10)["atr"].isna().all()

    def test_custom_bb_parameters(self, sample_ohlcv_50rows):
        """Test with custom Bollinger Bands parameters."""
        result = add_indicators(sample_ohlcv_50rows, bb_length=10, bb_std=1.5)
        assert "bb_upper" in result.columns
        assert "bb_mid" in result.columns
        assert "bb_lower" in result.columns
        # Tail should have valid values
        assert not result.tail(10)["bb_upper"].isna().all()
