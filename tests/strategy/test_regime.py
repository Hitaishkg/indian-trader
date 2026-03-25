"""Tests for src/strategy/regime.py"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from unittest.mock import patch

from src.strategy.regime import (
    RegimeResult,
    SMA_PERIOD,
    BELOW_DMA_BLOCK_DAYS,
    apply_regime_filter,
    compute_200dma,
    count_consecutive_days_below_200dma,
)

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_nifty(n_rows: int = 250, base_close: float = 20000.0, trend: float = 10.0) -> pd.DataFrame:
    """Generate Nifty 50 OHLCV with n_rows rows, linearly trending close prices."""
    start = datetime(2023, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n_rows, freq="B", tz=IST)
    closes = [base_close + i * trend for i in range(n_rows)]
    return pd.DataFrame({"date": dates, "close": closes})


def _make_nifty_below_for_n_days(n_below: int, total_rows: int = 260) -> pd.DataFrame:
    """
    Build a Nifty DataFrame where the last n_below rows have close below their rolling 200 SMA.

    Strategy: use a high flat price for the first (total_rows - n_below) rows,
    then drop sharply so the rolling SMA (window=200) is well above the close.
    """
    start = datetime(2020, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=total_rows, freq="B", tz=IST)

    # First portion: flat at 20000 → rolling SMA will converge to 20000
    split = total_rows - n_below
    closes = [20000.0] * split + [10000.0] * n_below  # sharp drop

    return pd.DataFrame({"date": dates, "close": closes})


def _make_ranked_df(symbols: list[str] | None = None) -> pd.DataFrame:
    """Build a minimal ranked_df with required columns."""
    if symbols is None:
        symbols = ["A", "B", "C"]
    n = len(symbols)
    return pd.DataFrame(
        {
            "symbol": symbols,
            "momentum_score": [0.30 - i * 0.05 for i in range(n)],
            "twelve_month_return": [0.40 - i * 0.05 for i in range(n)],
            "one_month_return": [0.10] * n,
            "rank": list(range(1, n + 1)),
            "pct_from_52w_high": [0.10] * n,
            "within_30pct_of_52w_high": [True] * n,
        }
    )


# ---------------------------------------------------------------------------
# Regime determination tests
# ---------------------------------------------------------------------------


def test_above_200dma():
    """Nifty above 200 DMA → regime ABOVE_200DMA, multiplier=1.0, tighten_stops=False."""
    # Rising trend: latest close always above its 200 SMA
    nifty = _make_nifty(n_rows=250, base_close=20000.0, trend=10.0)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        filtered_df, result = apply_regime_filter(ranked, nifty)

    assert result.regime == "ABOVE_200DMA"
    assert result.position_size_multiplier == 1.0
    assert result.tighten_stops is False
    assert result.stop_tighten_symbols == []
    assert len(filtered_df) == 3
    assert "position_size_multiplier" in filtered_df.columns
    assert all(filtered_df["position_size_multiplier"] == 1.0)


def test_below_200dma_under_10_days():
    """Below 200 DMA for 5 days → BELOW_200DMA, multiplier=0.5, tighten_stops=True."""
    nifty = _make_nifty_below_for_n_days(n_below=5)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        filtered_df, result = apply_regime_filter(ranked, nifty)

    assert result.regime == "BELOW_200DMA"
    assert result.position_size_multiplier == 0.5
    assert result.tighten_stops is True
    assert len(filtered_df) == 3
    assert all(filtered_df["position_size_multiplier"] == 0.5)


def test_below_200dma_10_plus_days():
    """Below 200 DMA for 12 days → BELOW_200DMA_10DAYS, multiplier=0.0, empty candidates."""
    nifty = _make_nifty_below_for_n_days(n_below=12)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        filtered_df, result = apply_regime_filter(ranked, nifty)

    assert result.regime == "BELOW_200DMA_10DAYS"
    assert result.position_size_multiplier == 0.0
    assert result.tighten_stops is True
    assert len(filtered_df) == 0
    assert "position_size_multiplier" in filtered_df.columns


def test_exactly_9_days_below():
    """Exactly 9 consecutive days below → BELOW_200DMA (not blocked)."""
    nifty = _make_nifty_below_for_n_days(n_below=9)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty)

    assert result.regime == "BELOW_200DMA"
    assert result.position_size_multiplier == 0.5
    assert result.consecutive_days_below == 9


def test_exactly_10_days_below():
    """Exactly 10 consecutive days below → BELOW_200DMA_10DAYS (blocked)."""
    nifty = _make_nifty_below_for_n_days(n_below=10)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty)

    assert result.regime == "BELOW_200DMA_10DAYS"
    assert result.position_size_multiplier == 0.0
    assert result.consecutive_days_below == 10


def test_nifty_exactly_on_200dma():
    """close == sma_200 exactly → ABOVE_200DMA (uses >= comparison)."""
    # Build a flat series: all closes equal → rolling mean == close
    n = 250
    start = datetime(2023, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)
    nifty = pd.DataFrame({"date": dates, "close": [20000.0] * n})
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty)

    assert result.regime == "ABOVE_200DMA"
    assert result.tighten_stops is False


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_compute_200dma_correct_value():
    """200 DMA = mean of last 200 close values in a known flat sequence."""
    n = 250
    start = datetime(2023, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)
    closes = list(range(1, n + 1))  # 1, 2, ..., 250
    nifty = pd.DataFrame({"date": dates, "close": [float(c) for c in closes]})

    sma = compute_200dma(nifty)

    # 200 DMA at end = mean of rows 51..250 (1-indexed) = mean of 51..250
    expected = sum(range(51, 251)) / 200.0
    assert abs(sma - expected) < 1e-6


def test_count_consecutive_days_below_correct():
    """Known sequence ending with 7 days below → count=7."""
    nifty = _make_nifty_below_for_n_days(n_below=7)
    count = count_consecutive_days_below_200dma(nifty)
    assert count == 7


def test_count_consecutive_days_when_above():
    """Latest close above 200 SMA → count=0."""
    nifty = _make_nifty(n_rows=250, base_close=20000.0, trend=10.0)
    count = count_consecutive_days_below_200dma(nifty)
    assert count == 0


# ---------------------------------------------------------------------------
# Stop tightening tests
# ---------------------------------------------------------------------------


def test_stop_tighten_symbols_populated():
    """Below 200 DMA with 2 open positions → stop_tighten_symbols has both symbols."""
    nifty = _make_nifty_below_for_n_days(n_below=5)
    ranked = _make_ranked_df()
    open_positions = [
        {"symbol": "HDFC", "stop_loss": 1500.0, "entry_price": 1600.0},
        {"symbol": "INFY", "stop_loss": 1400.0, "entry_price": 1500.0},
    ]

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty, open_positions=open_positions)

    assert result.tighten_stops is True
    assert set(result.stop_tighten_symbols) == {"HDFC", "INFY"}


def test_stop_tighten_symbols_empty_when_above():
    """Above 200 DMA with open positions → stop_tighten_symbols is []."""
    nifty = _make_nifty(n_rows=250, base_close=20000.0, trend=10.0)
    ranked = _make_ranked_df()
    open_positions = [{"symbol": "HDFC", "stop_loss": 1500.0, "entry_price": 1600.0}]

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty, open_positions=open_positions)

    assert result.tighten_stops is False
    assert result.stop_tighten_symbols == []


def test_stop_tighten_symbols_empty_when_no_positions():
    """Below 200 DMA with open_positions=None → stop_tighten_symbols is []."""
    nifty = _make_nifty_below_for_n_days(n_below=5)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty, open_positions=None)

    assert result.tighten_stops is True
    assert result.stop_tighten_symbols == []


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


def test_valueerror_fewer_than_200_rows():
    """Pass 150 rows of nifty data → ValueError mentioning row count."""
    nifty = _make_nifty(n_rows=150)
    ranked = _make_ranked_df()

    with pytest.raises(ValueError, match="must have >= 200 rows"):
        apply_regime_filter(ranked, nifty)


def test_valueerror_missing_close_column():
    """nifty_ohlcv_df without 'close' column → ValueError."""
    nifty = _make_nifty(n_rows=250).drop(columns=["close"])
    ranked = _make_ranked_df()

    with pytest.raises(ValueError, match="missing required columns"):
        apply_regime_filter(ranked, nifty)


def test_valueerror_missing_date_column():
    """nifty_ohlcv_df without 'date' column → ValueError."""
    nifty = _make_nifty(n_rows=250).drop(columns=["date"])
    ranked = _make_ranked_df()

    with pytest.raises(ValueError, match="missing required columns"):
        apply_regime_filter(ranked, nifty)


def test_valueerror_missing_columns_ranked():
    """ranked_df without 'symbol' column → ValueError."""
    nifty = _make_nifty(n_rows=250)
    ranked = _make_ranked_df().drop(columns=["symbol"])

    with pytest.raises(ValueError, match="ranked_df missing required columns"):
        apply_regime_filter(ranked, nifty)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


def test_empty_ranked_df():
    """Empty ranked_df (0 rows, correct columns) → valid RegimeResult + empty filtered_df."""
    nifty = _make_nifty(n_rows=250, base_close=20000.0, trend=10.0)
    ranked = pd.DataFrame(
        columns=[
            "symbol", "momentum_score", "twelve_month_return", "one_month_return",
            "rank", "pct_from_52w_high", "within_30pct_of_52w_high",
        ]
    )

    with patch("src.strategy.regime.log_agent_action"):
        filtered_df, result = apply_regime_filter(ranked, nifty)

    assert len(filtered_df) == 0
    assert "position_size_multiplier" in filtered_df.columns
    assert result.regime == "ABOVE_200DMA"
    assert isinstance(result.nifty_close, float)
    assert isinstance(result.sma_200, float)


def test_regime_result_frozen():
    """Attempting to mutate RegimeResult attribute raises FrozenInstanceError."""
    result = RegimeResult(
        regime="ABOVE_200DMA",
        nifty_close=20000.0,
        sma_200=19000.0,
        consecutive_days_below=0,
        position_size_multiplier=1.0,
        tighten_stops=False,
        stop_tighten_symbols=[],
        computed_at_ist="2026-03-25T10:00:00+05:30",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.regime = "BELOW_200DMA"  # type: ignore[misc]


def test_computed_at_ist_valid_timestamp():
    """computed_at_ist is a valid IST timestamp with +05:30 offset."""
    nifty = _make_nifty(n_rows=250, base_close=20000.0, trend=10.0)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        _, result = apply_regime_filter(ranked, nifty)

    ts = datetime.fromisoformat(result.computed_at_ist)
    assert ts.utcoffset() is not None
    offset_hours = ts.utcoffset().total_seconds() / 3600  # type: ignore[union-attr]
    assert offset_hours == 5.5


def test_output_columns_include_position_size_multiplier():
    """Output filtered_df has all ranked_df columns plus position_size_multiplier."""
    nifty = _make_nifty(n_rows=250, base_close=20000.0, trend=10.0)
    ranked = _make_ranked_df()

    with patch("src.strategy.regime.log_agent_action"):
        filtered_df, _ = apply_regime_filter(ranked, nifty)

    expected_cols = [
        "symbol", "momentum_score", "twelve_month_return", "one_month_return",
        "rank", "pct_from_52w_high", "within_30pct_of_52w_high",
        "position_size_multiplier",
    ]
    for col in expected_cols:
        assert col in filtered_df.columns


def test_compute_200dma_raises_insufficient_rows():
    """compute_200dma with < 200 rows → ValueError."""
    nifty = _make_nifty(n_rows=100)
    with pytest.raises(ValueError, match="must have >= 200 rows"):
        compute_200dma(nifty)


def test_count_consecutive_raises_insufficient_rows():
    """count_consecutive_days_below_200dma with < 200 rows → ValueError."""
    nifty = _make_nifty(n_rows=100)
    with pytest.raises(ValueError, match="must have >= 200 rows"):
        count_consecutive_days_below_200dma(nifty)
