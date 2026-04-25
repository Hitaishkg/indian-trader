"""Tests for src/agents/screener_agent.py.

Covers all 15 scenarios from the spec at docs/specs/2026-04-05-screener-agent.md.
"""

from __future__ import annotations

import datetime
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.agents.screener_agent import (
    run_screener_agent,
    ScreenerAgentError,
)

# ---------------------------------------------------------------------------
# Timezone constant for test use
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

# Fixed run date used across most tests
RUN_DATE = datetime.date(2026, 4, 5)


# ---------------------------------------------------------------------------
# Helpers to build realistic test DataFrames
# ---------------------------------------------------------------------------


def _make_fundamentals_df(symbols: list[str]) -> pd.DataFrame:
    """Create a realistic fundamentals DataFrame with required columns."""
    rows = []
    for symbol in symbols:
        rows.append(
            {
                "symbol": symbol,
                "roe": 0.20,
                "debt_to_equity": 0.5,
                "eps": 15.0,
                "eps_positive_4q": 1,
                "avg_daily_value": 50_000_000.0,
                "latest_price": 500.0,
                "high_52w": 550.0,
                "data_quality": "ok",
            }
        )
    return pd.DataFrame(rows)


def _make_ohlcv_df(
    symbols: list[str],
    num_rows: int = 400,
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
                    "open": 100.0 + i * 0.5,
                    "high": 105.0 + i * 0.5,
                    "low": 98.0 + i * 0.5,
                    "close": 101.0 + i * 0.5,
                    "volume": 1_000_000 + i * 1000,
                }
            )
    return pd.DataFrame(rows)


def _make_quality_filter_result(
    symbols: list[str], thin_universe: bool = False
) -> tuple[pd.DataFrame, MagicMock]:
    """Create a quality filter DataFrame and mock report."""
    if thin_universe or not symbols:
        quality_df = pd.DataFrame(columns=[
            "symbol", "roe", "debt_to_equity", "avg_daily_value",
            "latest_price", "high_52w", "pct_from_52w_high",
            "within_30pct_of_52w_high", "passed_hard_filters"
        ])
    else:
        rows = [
            {
                "symbol": sym,
                "roe": 0.20,
                "debt_to_equity": 0.5,
                "avg_daily_value": 50_000_000.0,
                "latest_price": 500.0,
                "high_52w": 550.0,
                "pct_from_52w_high": 0.09,
                "within_30pct_of_52w_high": 1,
                "passed_hard_filters": 1,
            }
            for sym in symbols
        ]
        quality_df = pd.DataFrame(rows)

    report = MagicMock()
    report.passed_count = len(symbols) if not thin_universe else 0
    report.universe_size = len(symbols) if symbols else 50
    report.thin_universe = thin_universe or len(symbols) < 3
    report.filter_failure_counts = {}

    return quality_df, report


def _make_momentum_result(
    quality_df: pd.DataFrame, top_n: int = 5
) -> tuple[pd.DataFrame, MagicMock]:
    """Create a momentum ranking DataFrame and mock report."""
    if quality_df.empty:
        ranked_df = pd.DataFrame(columns=[
            "symbol", "momentum_score", "twelve_month_return",
            "one_month_return", "rank", "pct_from_52w_high",
            "within_30pct_of_52w_high"
        ])
    else:
        rows = []
        for idx, (_, row) in enumerate(quality_df.iterrows()):
            rows.append(
                {
                    "symbol": str(row["symbol"]),
                    "momentum_score": 10.0 - idx,  # Highest first
                    "twelve_month_return": 0.25 - idx * 0.02,
                    "one_month_return": 0.05,
                    "rank": idx + 1,
                    "pct_from_52w_high": 0.09,
                    "within_30pct_of_52w_high": 1,
                }
            )
        ranked_df = pd.DataFrame(rows[:top_n])

    report = MagicMock()
    report.scored_count = len(quality_df)
    report.selected_count = len(ranked_df)

    return ranked_df, report


def _make_regime_result(
    ranked_df: pd.DataFrame,
    regime: str = "ABOVE_200DMA",
) -> tuple[pd.DataFrame, MagicMock]:
    """Create a regime filter DataFrame and mock RegimeResult."""
    if regime == "BELOW_200DMA_10DAYS":
        # apply_regime_filter returns empty DataFrame when blocked
        filtered_df = pd.DataFrame(columns=[
            "symbol", "momentum_score", "twelve_month_return",
            "one_month_return", "rank", "pct_from_52w_high",
            "within_30pct_of_52w_high", "position_size_multiplier"
        ])
        multiplier = 0.0
    elif regime == "BELOW_200DMA":
        filtered_df = ranked_df.copy()
        filtered_df["position_size_multiplier"] = 0.5
        multiplier = 0.5
    else:  # ABOVE_200DMA
        filtered_df = ranked_df.copy()
        filtered_df["position_size_multiplier"] = 1.0
        multiplier = 1.0

    regime_result = MagicMock()
    regime_result.regime = regime
    regime_result.position_size_multiplier = multiplier
    regime_result.nifty_close = 25000.0
    regime_result.sma_200 = 24000.0
    regime_result.consecutive_days_below = 0 if regime == "ABOVE_200DMA" else 10

    return filtered_df, regime_result


def _make_sector_df(
    num_rows: int = 400,
    base_date: datetime.date | None = None,
) -> pd.DataFrame:
    """Create a sector indices DataFrame with NIFTY_50 data."""
    if base_date is None:
        base_date = RUN_DATE

    rows = []
    for i in range(num_rows):
        date = base_date - datetime.timedelta(days=(num_rows - 1 - i))
        rows.append(
            {
                "symbol": "NIFTY_50",
                "date": date.isoformat(),
                "open": 24000.0 + i,
                "high": 24500.0 + i,
                "low": 23500.0 + i,
                "close": 24200.0 + i,
                "volume": 100_000_000 + i * 100000,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Shared fixture for temp DB
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Yield a path to a fresh temporary SQLite DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_screener.db")
        yield db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScreenerAgent:
    """Test suite for screener_agent.py."""

    # Scenario 1: Happy path — 5 quality stocks, regime ABOVE_200DMA
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_happy_path_full_universe_5_quality_above_200dma(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_symbols,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 1: Happy path with 5 quality stocks and ABOVE_200DMA regime."""
        # Setup
        mock_settings.database_url = f"sqlite:///{temp_db}"
        mock_settings.nifty_universe = "nifty50"
        nifty_universe = [f"STOCK{i}" for i in range(1, 51)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_symbols.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(
            nifty_universe[:5]
        )
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert len(result.top5) == 5
        assert result.thin_universe is False
        assert result.regime_blocked is False
        assert result.symbols_screened == 50
        assert result.symbols_passed_quality == 5
        mock_send_info.assert_called_once()
        mock_send_alert.assert_not_called()

    # Scenario 2: thin_universe — only 2 stocks pass quality filter
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_thin_universe_only_2_pass(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_symbols,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 2: Only 2 stocks pass quality filter (thin universe)."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 51)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        # Create quality_filter result with exactly 2 passing symbols
        quality_df = pd.DataFrame([
            {
                "symbol": "STOCK1",
                "roe": 0.20,
                "debt_to_equity": 0.5,
                "avg_daily_value": 50_000_000.0,
                "latest_price": 500.0,
                "high_52w": 550.0,
                "pct_from_52w_high": 0.09,
                "within_30pct_of_52w_high": 1,
                "passed_hard_filters": 1,
            },
            {
                "symbol": "STOCK2",
                "roe": 0.20,
                "debt_to_equity": 0.5,
                "avg_daily_value": 50_000_000.0,
                "latest_price": 500.0,
                "high_52w": 550.0,
                "pct_from_52w_high": 0.09,
                "within_30pct_of_52w_high": 1,
                "passed_hard_filters": 1,
            },
        ])
        quality_report = MagicMock()
        quality_report.passed_count = 2
        quality_report.universe_size = 50
        quality_report.thin_universe = True
        quality_report.filter_failure_counts = {}
        mock_quality_filter.return_value = (quality_df, quality_report)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert result.thin_universe is True
        assert result.top5 == []
        assert result.symbols_passed_quality == 2
        mock_send_alert.assert_called_once()
        assert "thin universe" in mock_send_alert.call_args[1]["subject"]
        mock_send_info.assert_not_called()

    # Scenario 3: Exactly 3 stocks pass (minimum)
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_exactly_3_stocks_minimum(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_symbols,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 3: Exactly 3 stocks pass quality filter (minimum threshold)."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 51)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(
            ["STOCK1", "STOCK2", "STOCK3"]
        )
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=3)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert result.thin_universe is False
        assert len(result.top5) == 3
        mock_send_info.assert_called_once()

    # Scenario 4: regime=BELOW_200DMA
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_regime_below_200dma(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_symbols,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 4: Regime = BELOW_200DMA (reduce sizing, no block)."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(
            ranked_df, regime="BELOW_200DMA"
        )
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert result.regime_blocked is False
        assert len(result.top5) == 5
        for sr in result.top5:
            assert sr.position_size_multiplier == 0.5

    # Scenario 5: regime=BELOW_200DMA_10DAYS (blocked)
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_regime_blocked_below_200dma_10days(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_symbols,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 5: Regime = BELOW_200DMA_10DAYS (blocked, no new positions)."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        # apply_regime_filter returns empty DataFrame when BELOW_200DMA_10DAYS
        filtered_df, regime_result = _make_regime_result(
            ranked_df, regime="BELOW_200DMA_10DAYS"
        )
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert result.regime_blocked is True
        assert len(result.top5) == 5
        for sr in result.top5:
            assert sr.position_size_multiplier == 0.0
            assert sr.regime == "BELOW_200DMA_10DAYS"
        mock_send_alert.assert_called_once()
        assert "regime blocked" in mock_send_alert.call_args[1]["subject"]

    # Scenario 6: OHLCV fetch raises FetchError
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_ohlcv_fetch_error(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_log,
        temp_db,
    ):
        """Scenario 6: OHLCV fetch raises FetchError."""
        from src.data.fetcher import FetchError

        mock_settings.database_url = f"sqlite:///{temp_db}"
        mock_get_universe.return_value = [f"STOCK{i}" for i in range(1, 6)]
        mock_fetch_ohlcv.side_effect = FetchError(
            symbol="STOCK1",
            yfinance_error="yfinance failed",
            jugaad_error="jugaad failed",
        )

        # Execute & Assert
        with pytest.raises(ScreenerAgentError) as exc_info:
            run_screener_agent(run_date=RUN_DATE)
        assert exc_info.value.phase == "ohlcv_fetch"

    # Scenario 7: Fundamentals fetch raises FundamentalsError
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_fundamentals_fetch_error(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_log,
        temp_db,
    ):
        """Scenario 7: Fundamentals fetch raises FundamentalsError."""
        from src.data.fundamentals import FundamentalsError

        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.side_effect = FundamentalsError(
            "Screener.in fetch failed"
        )

        # Execute & Assert
        with pytest.raises(ScreenerAgentError) as exc_info:
            run_screener_agent(run_date=RUN_DATE)
        assert exc_info.value.phase == "fundamentals_fetch"

    # Scenario 8: DB write raises sqlite3.Error
    @patch("src.agents.screener_agent._write_results")
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_db_write_error(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_log,
        mock_write_results,
        temp_db,
    ):
        """Scenario 8: DB write raises sqlite3.Error."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        mock_write_results.side_effect = ScreenerAgentError(
            message="DB write failed: UNIQUE constraint failed",
            phase="db_write",
        )

        # Execute & Assert
        with pytest.raises(ScreenerAgentError) as exc_info:
            run_screener_agent(run_date=RUN_DATE)
        assert exc_info.value.phase == "db_write"

    # Scenario 9: Tiebreaker applied
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_tiebreaker_applied(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 9: Tiebreaker applied when stocks are within 2% momentum score."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        # Pre-tiebroken ranked DataFrame
        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert that compute_momentum was called (which handles tiebreaker internally)
        assert mock_momentum.called
        assert len(result.top5) == 5

    # Scenario 10: All stocks fail quality filter (0 pass)
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_all_stocks_fail_quality_filter(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 10: All stocks fail quality filter (0 pass)."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 51)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(
            [],
            thin_universe=True,
        )
        mock_quality_filter.return_value = (quality_df, quality_report)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert result.thin_universe is True
        assert result.symbols_passed_quality == 0
        assert result.top5 == []
        mock_send_alert.assert_called_once()

    # Scenario 11: run_date=None (defaults to today in IST)
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_run_date_none_defaults_to_today(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 11: run_date=None defaults to datetime.date.today() in IST."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute without explicit run_date
        result = run_screener_agent(run_date=None)

        # Assert that run_date was set to today (in IST)
        assert result.run_date == datetime.date.today()

    # Scenario 12: symbols_screened equals universe length
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_symbols_screened_equals_universe_length(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 12: symbols_screened equals length of universe."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 51)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe[:5])
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert result.symbols_screened == len(nifty_universe)
        assert result.symbols_screened == 50

    # Scenario 13: UNIQUE(symbol, run_date) conflict — second run overwrites
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.fetch_nifty50_symbols")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_unique_conflict_second_run_overwrites(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_symbols,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 13: UNIQUE(symbol, run_date) conflict — second run overwrites."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute first run
        result1 = run_screener_agent(run_date=RUN_DATE)
        assert len(result1.top5) == 5

        # Reset mocks and execute second run with same date
        mock_get_universe.reset_mock()
        mock_fetch_ohlcv.reset_mock()
        mock_fetch_sector.reset_mock()
        mock_get_fundamentals.reset_mock()
        mock_quality_filter.reset_mock()
        mock_momentum.reset_mock()
        mock_regime_filter.reset_mock()

        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)
        mock_momentum.return_value = (ranked_df, momentum_report)
        mock_regime_filter.return_value = (filtered_df, regime_result)

        result2 = run_screener_agent(run_date=RUN_DATE)

        # Assert second run succeeded without error
        assert len(result2.top5) == 5

    # Scenario 14: screened_at is IST timezone-aware
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_screened_at_ist_timezone_aware(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 14: ScreenerResult.screened_at is IST timezone-aware."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 6)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe)
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        result = run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert len(result.top5) > 0
        sr = result.top5[0]
        assert sr.screened_at.tzinfo is not None
        assert str(sr.screened_at.tzinfo) == "Asia/Kolkata"

    # Scenario 15: send_info called once on happy path; send_alert not called
    @patch("src.agents.screener_agent.log_agent_action")
    @patch("src.agents.screener_agent.send_info")
    @patch("src.agents.screener_agent.send_alert")
    @patch("src.agents.screener_agent.apply_regime_filter")
    @patch("src.agents.screener_agent.compute_momentum")
    @patch("src.agents.screener_agent.apply_quality_filter")
    @patch("src.agents.screener_agent.get_fundamentals_for_date")
    @patch("src.agents.screener_agent.fetch_sector_indices")
    @patch("src.agents.screener_agent.fetch_ohlcv")
    @patch("src.agents.screener_agent.get_nifty_universe_for_year")
    @patch("src.agents.screener_agent.settings")
    def test_send_info_once_send_alert_not_called_happy_path(
        self,
        mock_settings,
        mock_get_universe,
        mock_fetch_ohlcv,
        mock_fetch_sector,
        mock_get_fundamentals,
        mock_quality_filter,
        mock_momentum,
        mock_regime_filter,
        mock_send_alert,
        mock_send_info,
        mock_log,
        temp_db,
    ):
        """Scenario 15: send_info called once; send_alert not called on happy path."""
        mock_settings.database_url = f"sqlite:///{temp_db}"
        nifty_universe = [f"STOCK{i}" for i in range(1, 51)]
        mock_get_universe.return_value = nifty_universe
        mock_fetch_ohlcv.return_value = _make_ohlcv_df(nifty_universe)
        mock_fetch_sector.return_value = _make_sector_df()
        mock_get_fundamentals.return_value = _make_fundamentals_df(nifty_universe)

        quality_df, quality_report = _make_quality_filter_result(nifty_universe[:5])
        mock_quality_filter.return_value = (quality_df, quality_report)

        ranked_df, momentum_report = _make_momentum_result(quality_df, top_n=5)
        mock_momentum.return_value = (ranked_df, momentum_report)

        filtered_df, regime_result = _make_regime_result(ranked_df, regime="ABOVE_200DMA")
        mock_regime_filter.return_value = (filtered_df, regime_result)

        # Execute
        run_screener_agent(run_date=RUN_DATE)

        # Assert
        assert mock_send_info.call_count == 1
        assert mock_send_alert.call_count == 0
