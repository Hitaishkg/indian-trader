"""Tests for src/strategy/momentum.py"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from unittest.mock import patch

from src.strategy.momentum import (
    MomentumReport,
    TIEBREAKER_THRESHOLD,
    TWELVE_MONTH_LOOKBACK,
    ONE_MONTH_LOOKBACK,
    DEFAULT_TOP_N,
    compute_momentum,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")


def _make_ohlcv(symbol: str, n_rows: int = 300, base_close: float = 100.0) -> pd.DataFrame:
    """Generate synthetic OHLCV for a single symbol with n_rows trading days."""
    start = datetime(2023, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n_rows, freq="B", tz=IST)
    closes = [base_close + i * 0.1 for i in range(n_rows)]
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n_rows,
        }
    )


def _make_quality_df(symbols: list[str], pct_from_52w_high: list[float] | None = None) -> pd.DataFrame:
    """Build a minimal quality_df with required columns."""
    if pct_from_52w_high is None:
        pct_from_52w_high = [0.10] * len(symbols)
    within = [p <= 0.30 for p in pct_from_52w_high]
    return pd.DataFrame(
        {
            "symbol": symbols,
            "roe": [0.20] * len(symbols),
            "debt_to_equity": [0.5] * len(symbols),
            "avg_daily_value": [25_000_000.0] * len(symbols),
            "latest_price": [100.0] * len(symbols),
            "high_52w": [110.0] * len(symbols),
            "pct_from_52w_high": pct_from_52w_high,
            "within_30pct_of_52w_high": within,
            "passed_hard_filters": [True] * len(symbols),
        }
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_five_stocks_all_sufficient_history():
    """5 stocks all with >= 252 rows: all scored, output has 5 rows."""
    symbols = ["A", "B", "C", "D", "E"]
    ohlcv = pd.concat([_make_ohlcv(s, 300) for s in symbols])
    quality = _make_quality_df(symbols)

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert len(ranked_df) == 5
    assert report.scored_count == 5
    assert report.selected_count == 5
    assert report.insufficient_history_count == 0
    assert set(ranked_df["symbol"]) == set(symbols)


def test_top_n_limits_output():
    """top_n=3 with 5 scored stocks → output has 3 rows."""
    symbols = ["A", "B", "C", "D", "E"]
    ohlcv = pd.concat([_make_ohlcv(s, 300) for s in symbols])
    quality = _make_quality_df(symbols)

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=3)

    assert len(ranked_df) == 3
    assert report.selected_count == 3
    assert list(ranked_df["rank"]) == [1, 2, 3]


def test_momentum_score_computed_correctly():
    """Exact formula check: close_today=120, close_252d_ago=100, close_21d_ago=115."""
    # Build 300-row OHLCV with specific close values
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)
    closes = [90.0] * n
    closes[-252] = 100.0   # close 252 days ago
    closes[-21] = 115.0    # close 21 days ago
    closes[-1] = 120.0     # close today

    ohlcv = pd.DataFrame(
        {
            "symbol": "STOCK",
            "date": dates,
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )
    quality = _make_quality_df(["STOCK"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=1)

    assert len(ranked_df) == 1
    expected_12m = (120.0 - 100.0) / 100.0   # 0.20
    expected_1m = (120.0 - 115.0) / 115.0    # ~0.043478
    expected_score = expected_12m - expected_1m

    assert abs(ranked_df.iloc[0]["twelve_month_return"] - expected_12m) < 1e-10
    assert abs(ranked_df.iloc[0]["one_month_return"] - expected_1m) < 1e-10
    assert abs(ranked_df.iloc[0]["momentum_score"] - expected_score) < 1e-10


def test_ranking_order_correct():
    """Stock A score=0.30, stock B score=0.20 → A rank 1, B rank 2."""
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _build_stock(symbol: str, score_offset: float) -> pd.DataFrame:
        closes = [100.0] * n
        closes[-1] = 100.0 + score_offset * 100
        closes[-252] = 100.0
        closes[-21] = 100.0  # 1m return = 0
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    ohlcv = pd.concat([_build_stock("A", 0.30), _build_stock("B", 0.20)])
    quality = _make_quality_df(["A", "B"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, _ = compute_momentum(quality, ohlcv, top_n=2)

    assert ranked_df.iloc[0]["symbol"] == "A"
    assert ranked_df.iloc[0]["rank"] == 1
    assert ranked_df.iloc[1]["symbol"] == "B"
    assert ranked_df.iloc[1]["rank"] == 2


def test_output_columns_match_spec():
    """Output DataFrame has exactly the 7 expected columns."""
    symbols = ["A", "B"]
    ohlcv = pd.concat([_make_ohlcv(s, 300) for s in symbols])
    quality = _make_quality_df(symbols)

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, _ = compute_momentum(quality, ohlcv)

    expected_cols = [
        "symbol",
        "momentum_score",
        "twelve_month_return",
        "one_month_return",
        "rank",
        "pct_from_52w_high",
        "within_30pct_of_52w_high",
    ]
    assert list(ranked_df.columns) == expected_cols


# ---------------------------------------------------------------------------
# Tiebreaker
# ---------------------------------------------------------------------------


def test_tiebreaker_invoked_within_2pct():
    """Scores within 2%: stock with lower pct_from_52w_high wins."""
    # score_a=1.00, score_b=0.99 → rel_diff=0.01 < 0.02
    # A has pct=0.15, B has pct=0.05 → B wins (closer to 52w high)
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _stock(symbol: str, momentum: float) -> pd.DataFrame:
        closes = [100.0] * n
        closes[-1] = 100.0 * (1 + momentum)
        closes[-252] = 100.0
        closes[-21] = 100.0
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    ohlcv = pd.concat([_stock("A", 1.00), _stock("B", 0.99)])
    # A pct=0.15, B pct=0.05 → B should win (lower pct)
    quality = _make_quality_df(["A", "B"], pct_from_52w_high=[0.15, 0.05])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=2)

    # B wins tiebreaker → B rank 1
    assert ranked_df.iloc[0]["symbol"] == "B"
    assert report.tiebreaker_applied_count == 1


def test_tiebreaker_not_invoked_outside_2pct():
    """Scores outside 2%: original order preserved."""
    # Set close[-21] = close[-1] so 1m_return=0, momentum_score = 12m_return
    # A: 12m=1.00, score=1.00; B: 12m=0.95, score=0.95
    # rel_diff = |1.00 - 0.95| / 0.95 ≈ 0.0526 >= 0.02 → no tiebreaker
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _stock(symbol: str, twelve_m_return: float) -> pd.DataFrame:
        close_today = 100.0 * (1 + twelve_m_return)
        closes = [close_today] * n
        closes[-252] = 100.0    # 12m lookback anchor
        # close[-21] stays at close_today → 1m_return=0 → score = twelve_m_return
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    ohlcv = pd.concat([_stock("A", 1.00), _stock("B", 0.95)])
    quality = _make_quality_df(["A", "B"], pct_from_52w_high=[0.20, 0.05])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=2)

    assert ranked_df.iloc[0]["symbol"] == "A"
    assert report.tiebreaker_applied_count == 0


def test_tiebreaker_equal_pct_preserves_order():
    """Within 2% but equal pct_from_52w_high → original order preserved."""
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _stock(symbol: str, momentum: float) -> pd.DataFrame:
        closes = [100.0] * n
        closes[-1] = 100.0 * (1 + momentum)
        closes[-252] = 100.0
        closes[-21] = 100.0
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    # Both within 2%, same pct → no swap
    ohlcv = pd.concat([_stock("A", 1.00), _stock("B", 0.99)])
    quality = _make_quality_df(["A", "B"], pct_from_52w_high=[0.10, 0.10])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=2)

    assert ranked_df.iloc[0]["symbol"] == "A"
    assert report.tiebreaker_applied_count == 0


def test_tiebreaker_count_in_report():
    """2 tiebreakers applied → report.tiebreaker_applied_count == 2."""
    # Arrange 3 stocks with score pairs within 2%: A-B and B-C
    # A=1.00, B=0.99, C=0.98 — each pair rel_diff < 0.02
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _stock(symbol: str, momentum: float) -> pd.DataFrame:
        closes = [100.0] * n
        closes[-1] = 100.0 * (1 + momentum)
        closes[-252] = 100.0
        closes[-21] = 100.0
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    ohlcv = pd.concat([_stock("A", 1.00), _stock("B", 0.99), _stock("C", 0.98)])
    # B pct=0.05 < A pct=0.15 → B beats A (swap 1)
    # C pct=0.03 < A pct=0.15 (now at pos 1) → C beats A (swap 2)
    quality = _make_quality_df(["A", "B", "C"], pct_from_52w_high=[0.15, 0.05, 0.03])

    with patch("src.strategy.momentum.log_agent_action"):
        _, report = compute_momentum(quality, ohlcv, top_n=3)

    assert report.tiebreaker_applied_count == 2


# ---------------------------------------------------------------------------
# Insufficient history
# ---------------------------------------------------------------------------


def test_symbol_with_fewer_than_252_rows_excluded():
    """Symbol with 200 rows → excluded, insufficient_history_count=1."""
    ohlcv = pd.concat([_make_ohlcv("A", 300), _make_ohlcv("B", 200)])
    quality = _make_quality_df(["A", "B"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert "B" not in ranked_df["symbol"].values
    assert report.insufficient_history_count == 1
    assert report.scored_count == 1


def test_symbol_not_in_ohlcv_treated_as_insufficient():
    """Symbol in quality_df but absent from ohlcv → counted as insufficient_history."""
    ohlcv = _make_ohlcv("A", 300)
    quality = _make_quality_df(["A", "MISSING"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert "MISSING" not in ranked_df["symbol"].values
    assert report.insufficient_history_count == 1


def test_all_symbols_insufficient_history():
    """All < 252 rows → empty output, scored_count=0, selected_count=0."""
    ohlcv = pd.concat([_make_ohlcv("A", 100), _make_ohlcv("B", 150)])
    quality = _make_quality_df(["A", "B"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert len(ranked_df) == 0
    assert report.scored_count == 0
    assert report.selected_count == 0
    assert report.insufficient_history_count == 2


def test_mix_sufficient_and_insufficient():
    """3 of 5 have enough history → scored_count=3, output has min(top_n, 3) rows."""
    ohlcv = pd.concat(
        [
            _make_ohlcv("A", 300),
            _make_ohlcv("B", 300),
            _make_ohlcv("C", 300),
            _make_ohlcv("D", 150),
            _make_ohlcv("E", 100),
        ]
    )
    quality = _make_quality_df(["A", "B", "C", "D", "E"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert report.scored_count == 3
    assert report.insufficient_history_count == 2
    assert len(ranked_df) == 3


# ---------------------------------------------------------------------------
# Division by zero
# ---------------------------------------------------------------------------


def test_zero_close_12m_excludes_symbol():
    """close at 252-day lookback == 0.0 → symbol excluded, logged as zero_close_12m."""
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)
    closes = [100.0] * n
    closes[-252] = 0.0  # zero at 12m lookback

    ohlcv = pd.DataFrame(
        {
            "symbol": "ZERO12M",
            "date": dates,
            "close": closes,
            "open": closes,
            "high": closes,
            "low": closes,
            "volume": [1_000_000] * n,
        }
    )
    quality = _make_quality_df(["ZERO12M"])

    logged_results: list[str] = []
    def capture(*args, **kwargs):  # type: ignore
        if kwargs.get("result"):
            logged_results.append(kwargs["result"])

    with patch("src.strategy.momentum.log_agent_action", side_effect=capture):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert len(ranked_df) == 0
    assert "zero_close_12m" in logged_results


def test_zero_close_1m_excludes_symbol():
    """close at 21-day lookback == 0.0 → symbol excluded, logged as zero_close_1m."""
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)
    closes = [100.0] * n
    closes[-21] = 0.0  # zero at 1m lookback

    ohlcv = pd.DataFrame(
        {
            "symbol": "ZERO1M",
            "date": dates,
            "close": closes,
            "open": closes,
            "high": closes,
            "low": closes,
            "volume": [1_000_000] * n,
        }
    )
    quality = _make_quality_df(["ZERO1M"])

    logged_results: list[str] = []
    def capture(*args, **kwargs):  # type: ignore
        if kwargs.get("result"):
            logged_results.append(kwargs["result"])

    with patch("src.strategy.momentum.log_agent_action", side_effect=capture):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert len(ranked_df) == 0
    assert "zero_close_1m" in logged_results


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_quality_df_raises():
    """Empty quality_df → ValueError."""
    quality = pd.DataFrame(columns=["symbol", "pct_from_52w_high", "within_30pct_of_52w_high"])
    ohlcv = _make_ohlcv("A", 300)
    with pytest.raises(ValueError, match="quality_df must not be empty"):
        compute_momentum(quality, ohlcv)


def test_empty_ohlcv_df_raises():
    """Empty ohlcv_df → ValueError."""
    quality = _make_quality_df(["A"])
    ohlcv = pd.DataFrame(columns=["symbol", "date", "close"])
    with pytest.raises(ValueError, match="ohlcv_df must not be empty"):
        compute_momentum(quality, ohlcv)


def test_missing_quality_column_raises():
    """Missing required column in quality_df → ValueError naming the column."""
    quality = _make_quality_df(["A"]).drop(columns=["pct_from_52w_high"])
    ohlcv = _make_ohlcv("A", 300)
    with pytest.raises(ValueError, match="quality_df missing required columns"):
        compute_momentum(quality, ohlcv)


def test_missing_ohlcv_column_raises():
    """Missing required column in ohlcv_df → ValueError naming the column."""
    quality = _make_quality_df(["A"])
    ohlcv = _make_ohlcv("A", 300).drop(columns=["close"])
    with pytest.raises(ValueError, match="ohlcv_df missing required columns"):
        compute_momentum(quality, ohlcv)


def test_top_n_zero_raises():
    """top_n=0 → ValueError."""
    quality = _make_quality_df(["A"])
    ohlcv = _make_ohlcv("A", 300)
    with pytest.raises(ValueError, match="top_n must be >= 1"):
        compute_momentum(quality, ohlcv, top_n=0)


def test_top_n_negative_raises():
    """top_n=-1 → ValueError."""
    quality = _make_quality_df(["A"])
    ohlcv = _make_ohlcv("A", 300)
    with pytest.raises(ValueError, match="top_n must be >= 1"):
        compute_momentum(quality, ohlcv, top_n=-1)


# ---------------------------------------------------------------------------
# MomentumReport
# ---------------------------------------------------------------------------


def test_momentum_report_is_frozen():
    """Attempting to mutate MomentumReport attribute raises FrozenInstanceError."""
    report = MomentumReport(
        scored_count=1,
        selected_count=1,
        insufficient_history_count=0,
        tiebreaker_applied_count=0,
        computed_at_ist="2026-03-25T10:00:00+05:30",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.scored_count = 99  # type: ignore[misc]


def test_computed_at_ist_is_valid_ist_timestamp():
    """computed_at_ist is parseable ISO 8601 with +05:30 offset."""
    quality = _make_quality_df(["A"])
    ohlcv = _make_ohlcv("A", 300)

    with patch("src.strategy.momentum.log_agent_action"):
        _, report = compute_momentum(quality, ohlcv)

    ts = datetime.fromisoformat(report.computed_at_ist)
    assert ts.utcoffset() is not None
    offset_hours = ts.utcoffset().total_seconds() / 3600  # type: ignore[union-attr]
    assert offset_hours == 5.5


def test_report_counts_sum_correctly():
    """scored_count + insufficient_history_count == total symbols in quality_df."""
    ohlcv = pd.concat([_make_ohlcv("A", 300), _make_ohlcv("B", 100), _make_ohlcv("C", 300)])
    quality = _make_quality_df(["A", "B", "C"])

    with patch("src.strategy.momentum.log_agent_action"):
        _, report = compute_momentum(quality, ohlcv)

    assert report.scored_count + report.insufficient_history_count == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_exactly_252_rows_gets_valid_score():
    """Symbol with exactly 252 rows → valid score (boundary condition)."""
    ohlcv = _make_ohlcv("A", 252)
    quality = _make_quality_df(["A"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv)

    assert len(ranked_df) == 1
    assert report.scored_count == 1
    assert report.insufficient_history_count == 0


def test_fewer_scored_than_top_n():
    """Only 2 symbols scored, top_n=5 → output has 2 rows, selected_count=2."""
    ohlcv = pd.concat([_make_ohlcv("A", 300), _make_ohlcv("B", 300)])
    quality = _make_quality_df(["A", "B"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=5)

    assert len(ranked_df) == 2
    assert report.selected_count == 2


def test_negative_momentum_scores_ranked_correctly():
    """Stocks with negative momentum scores still ranked: less negative = higher rank."""
    # Set close[-21] = close[-1] so 1m_return=0, momentum_score = 12m_return (negative)
    # A declined 10% → score=-0.10; B declined 20% → score=-0.20 → A rank 1
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _declining_stock(symbol: str, decline: float) -> pd.DataFrame:
        close_today = 100.0 * (1 - decline)
        closes = [close_today] * n
        closes[-252] = 100.0    # 12m lookback anchor
        # close[-21] stays at close_today → 1m_return=0 → score = -decline
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    ohlcv = pd.concat([_declining_stock("A", 0.10), _declining_stock("B", 0.20)])
    quality = _make_quality_df(["A", "B"])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, _ = compute_momentum(quality, ohlcv, top_n=2)

    assert ranked_df.iloc[0]["symbol"] == "A"
    assert ranked_df.iloc[0]["momentum_score"] > ranked_df.iloc[1]["momentum_score"]


def test_tiebreaker_with_score_b_zero():
    """score_a=small, score_b=0.0 → rel_diff=abs(score_a) → tiebreaker applied if < 0.02."""
    n = 300
    start = datetime(2022, 1, 1, tzinfo=IST)
    dates = pd.date_range(start=start, periods=n, freq="B", tz=IST)

    def _stock_with_exact_momentum(symbol: str, close_today: float) -> pd.DataFrame:
        closes = [100.0] * n
        closes[-1] = close_today
        closes[-252] = 100.0   # 12m lookback
        closes[-21] = close_today  # 1m return = 0 → momentum = 12m return
        return pd.DataFrame(
            {
                "symbol": symbol,
                "date": dates,
                "close": closes,
                "open": closes,
                "high": closes,
                "low": closes,
                "volume": [1_000_000] * n,
            }
        )

    # A: 12m=+1%, 1m=0 → momentum=0.01
    # B: 12m=0%, 1m=0 → momentum=0.0
    # rel_diff = abs(0.01) / abs(0.0) → uses abs(score_a) = 0.01 < 0.02 → tiebreaker
    ohlcv = pd.concat([
        _stock_with_exact_momentum("A", 101.0),   # momentum=0.01
        _stock_with_exact_momentum("B", 100.0),   # momentum=0.0
    ])
    quality = _make_quality_df(["A", "B"], pct_from_52w_high=[0.20, 0.05])

    with patch("src.strategy.momentum.log_agent_action"):
        ranked_df, report = compute_momentum(quality, ohlcv, top_n=2)

    # B has lower pct (0.05) and rel_diff < 0.02 → B wins tiebreaker
    assert ranked_df.iloc[0]["symbol"] == "B"
    assert report.tiebreaker_applied_count == 1
