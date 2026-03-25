"""Nifty 50 200-day SMA regime filter.

Determines the current market regime by comparing the Nifty 50 close price
to its 200-day simple moving average. Adjusts position sizing for new trades
and signals stop-loss tightening for open positions.

Three regimes:
- ABOVE_200DMA: close >= 200 DMA → position_size_multiplier = 1.0
- BELOW_200DMA: close < 200 DMA, < 10 consecutive days → multiplier = 0.5
- BELOW_200DMA_10DAYS: close < 200 DMA, >= 10 consecutive days → multiplier = 0.0
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SMA_PERIOD: int = 200
BELOW_DMA_BLOCK_DAYS: int = 10
POSITION_SIZE_ABOVE: float = 1.0
POSITION_SIZE_BELOW: float = 0.5
POSITION_SIZE_BLOCKED: float = 0.0
AGENT_NAME: str = "regime"

_RANKED_REQUIRED: list[str] = [
    "symbol",
    "momentum_score",
    "twelve_month_return",
    "one_month_return",
    "rank",
    "pct_from_52w_high",
    "within_30pct_of_52w_high",
]
_NIFTY_REQUIRED: list[str] = ["date", "close"]


# ---------------------------------------------------------------------------
# RegimeResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeResult:
    """Result of regime filter computation.

    Attributes:
        regime: One of "ABOVE_200DMA", "BELOW_200DMA", "BELOW_200DMA_10DAYS".
        nifty_close: Latest Nifty 50 closing price (INR).
        sma_200: Current 200-day SMA value of Nifty 50 close.
        consecutive_days_below: Number of consecutive trading days (from most
            recent) that Nifty 50 close has been below the rolling 200-day SMA.
        position_size_multiplier: 1.0 (above), 0.5 (below < 10 days),
            0.0 (below >= 10 days).
        tighten_stops: True when Nifty is below 200 DMA (any duration).
            Means open positions should use 1x ATR stop instead of 2x ATR.
        stop_tighten_symbols: List of symbol strings from open_positions
            that need their stop-loss tightened. Empty if tighten_stops
            is False or no open positions provided.
        computed_at_ist: ISO 8601 IST timestamp.
    """

    regime: str
    nifty_close: float
    sma_200: float
    consecutive_days_below: int
    position_size_multiplier: float
    tighten_stops: bool
    stop_tighten_symbols: list[str]
    computed_at_ist: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_regime_filter(
    ranked_df: pd.DataFrame,
    nifty_ohlcv_df: pd.DataFrame,
    open_positions: list[dict[str, object]] | None = None,
) -> tuple[pd.DataFrame, RegimeResult]:
    """Apply Nifty 50 200 DMA regime filter to momentum-ranked candidates.

    Computes the 200-day SMA of Nifty 50 close prices, determines the
    current regime, adjusts position sizing for new trades, and signals
    stop-loss tightening for open positions.

    Args:
        ranked_df: Output of compute_momentum(). One row per momentum-
            ranked candidate. Required columns: symbol, momentum_score,
            twelve_month_return, one_month_return, rank, pct_from_52w_high,
            within_30pct_of_52w_high.
        nifty_ohlcv_df: Nifty 50 index OHLCV data. Required columns:
            date, close. Must have >= 200 rows to compute 200-day SMA.
            Rows will be sorted by date ascending if not already sorted.
        open_positions: List of position dicts from PaperTrader.get_positions().
            Each dict must have at minimum a "symbol" key. If None or empty,
            stop_tighten_symbols will be [].

    Returns:
        Tuple of (filtered_df, regime_result).

        filtered_df: ranked_df with an added column "position_size_multiplier"
            (float). When regime is BELOW_200DMA_10DAYS, returns an empty
            DataFrame with the same columns (no candidates pass through).
            When regime is BELOW_200DMA, multiplier = 0.5. When ABOVE_200DMA,
            multiplier = 1.0. Original rank order is preserved.

        regime_result: RegimeResult frozen dataclass with full regime details.

    Raises:
        ValueError: If nifty_ohlcv_df has fewer than SMA_PERIOD (200) rows.
        ValueError: If nifty_ohlcv_df is missing required columns ("date", "close").
        ValueError: If ranked_df is missing required columns.
    """
    _validate_nifty(nifty_ohlcv_df)
    _validate_ranked(ranked_df)

    nifty_sorted = nifty_ohlcv_df.sort_values("date").reset_index(drop=True)

    sma_200 = compute_200dma(nifty_sorted)
    consecutive_days_below = count_consecutive_days_below_200dma(nifty_sorted)
    nifty_close = float(nifty_sorted.iloc[-1]["close"])

    # Determine regime
    if nifty_close >= sma_200:
        regime = "ABOVE_200DMA"
        position_size_multiplier = POSITION_SIZE_ABOVE
        tighten_stops = False
    elif consecutive_days_below >= BELOW_DMA_BLOCK_DAYS:
        regime = "BELOW_200DMA_10DAYS"
        position_size_multiplier = POSITION_SIZE_BLOCKED
        tighten_stops = True
    else:
        regime = "BELOW_200DMA"
        position_size_multiplier = POSITION_SIZE_BELOW
        tighten_stops = True

    # Build stop_tighten_symbols
    stop_tighten_symbols: list[str] = []
    if tighten_stops and open_positions:
        stop_tighten_symbols = [str(p["symbol"]) for p in open_positions]

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"regime_computed: {regime}, close={nifty_close:.2f}, "
            f"sma_200={sma_200:.2f}, days_below={consecutive_days_below}"
        ),
        result="ok",
    )

    # Build filtered_df
    output_columns = _RANKED_REQUIRED + ["position_size_multiplier"]
    if regime == "BELOW_200DMA_10DAYS":
        filtered_df = pd.DataFrame(columns=output_columns)
        log_agent_action(
            agent_name=AGENT_NAME,
            action="candidates_blocked: 0 candidates (BELOW_200DMA_10DAYS)",
            result="blocked",
        )
    else:
        filtered_df = ranked_df.copy()
        filtered_df["position_size_multiplier"] = position_size_multiplier
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"candidates_filtered: {len(filtered_df)} candidates, multiplier={position_size_multiplier}",
            result="ok",
        )

    if tighten_stops:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"stop_tighten: {len(stop_tighten_symbols)} symbols need stop-loss tightening",
            result="ok",
        )

    regime_result = RegimeResult(
        regime=regime,
        nifty_close=nifty_close,
        sma_200=sma_200,
        consecutive_days_below=consecutive_days_below,
        position_size_multiplier=position_size_multiplier,
        tighten_stops=tighten_stops,
        stop_tighten_symbols=stop_tighten_symbols,
        computed_at_ist=_ist_now(),
    )

    return filtered_df, regime_result


def compute_200dma(nifty_ohlcv_df: pd.DataFrame) -> float:
    """Compute the 200-day simple moving average of Nifty 50 close prices.

    Args:
        nifty_ohlcv_df: Nifty 50 OHLCV data with columns "date" and "close".
            Must have >= 200 rows.

    Returns:
        The 200-day SMA as a float (last value of the rolling mean).

    Raises:
        ValueError: If nifty_ohlcv_df has fewer than 200 rows.
        ValueError: If "close" column is missing.
    """
    if "close" not in nifty_ohlcv_df.columns:
        raise ValueError("nifty_ohlcv_df missing required columns: ['close']")
    if len(nifty_ohlcv_df) < SMA_PERIOD:
        raise ValueError(
            f"nifty_ohlcv_df must have >= {SMA_PERIOD} rows, got {len(nifty_ohlcv_df)}"
        )
    sma_series = nifty_ohlcv_df["close"].rolling(window=SMA_PERIOD).mean()
    return float(sma_series.iloc[-1])


def count_consecutive_days_below_200dma(nifty_ohlcv_df: pd.DataFrame) -> int:
    """Count consecutive trading days the Nifty 50 close has been below the rolling 200-day SMA.

    Counts backwards from the most recent row. Stops counting when a day
    is found where close >= rolling 200-day SMA, or when the rolling SMA
    is NaN (first 199 rows).

    Args:
        nifty_ohlcv_df: Nifty 50 OHLCV data with columns "date" and "close".
            Must have >= 200 rows.

    Returns:
        Number of consecutive trading days (from most recent) that the close
        was strictly below the rolling 200-day SMA. Returns 0 if the latest
        close is >= the 200-day SMA.

    Raises:
        ValueError: If nifty_ohlcv_df has fewer than 200 rows.
        ValueError: If "close" column is missing.
    """
    if "close" not in nifty_ohlcv_df.columns:
        raise ValueError("nifty_ohlcv_df missing required columns: ['close']")
    if len(nifty_ohlcv_df) < SMA_PERIOD:
        raise ValueError(
            f"nifty_ohlcv_df must have >= {SMA_PERIOD} rows, got {len(nifty_ohlcv_df)}"
        )

    sorted_df = nifty_ohlcv_df.sort_values("date").reset_index(drop=True)
    sma_series = sorted_df["close"].rolling(window=SMA_PERIOD).mean()

    count = 0
    for i in range(len(sorted_df) - 1, -1, -1):
        sma_val = sma_series.iloc[i]
        if pd.isna(sma_val):
            break
        close_val = float(sorted_df.iloc[i]["close"])
        if close_val < float(sma_val):
            count += 1
        else:
            break

    return count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_nifty(nifty_ohlcv_df: pd.DataFrame) -> None:
    """Validate nifty_ohlcv_df inputs; raise ValueError on violation."""
    missing = [c for c in _NIFTY_REQUIRED if c not in nifty_ohlcv_df.columns]
    if missing:
        raise ValueError(f"nifty_ohlcv_df missing required columns: {missing}")
    if len(nifty_ohlcv_df) < SMA_PERIOD:
        raise ValueError(
            f"nifty_ohlcv_df must have >= {SMA_PERIOD} rows, got {len(nifty_ohlcv_df)}"
        )


def _validate_ranked(ranked_df: pd.DataFrame) -> None:
    """Validate ranked_df inputs; raise ValueError on violation."""
    missing = [c for c in _RANKED_REQUIRED if c not in ranked_df.columns]
    if missing:
        raise ValueError(f"ranked_df missing required columns: {missing}")


def _ist_now() -> str:
    """Return current IST time as ISO 8601 string."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
