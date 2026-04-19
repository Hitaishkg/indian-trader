"""Quality filter for the Indian Trader stock selection pipeline.

Implements the five hard quality filters that every Nifty 50 stock must pass
before entering the momentum ranking pipeline. This is the first gate in the
three-step stock selection process:

  quality filter -> momentum rank -> regime filter

Stocks that fail any single hard filter are eliminated. A soft tiebreaker
score (52-week high proximity) is computed but never used to eliminate stocks;
it is passed downstream to momentum.py for tiebreaking only.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

ROE_THRESHOLD: float = 0.12  # Relaxed from 0.15 per sensitivity backtest 2026-04-19
# Evidence: Sharpe 0.544→0.851, DD 12.58%→9.07%, PF 1.203→1.415
DE_THRESHOLD: float = 1.0
VOLUME_VALUE_THRESHOLD: float = 20_000_000.0  # 20 crore INR
PRICE_THRESHOLD: float = 50.0
PROXIMITY_THRESHOLD: float = 0.30
DEFAULT_LOOKBACK_DAYS: int = 252
MIN_UNIVERSE_SIZE: int = 3
AGENT_NAME: str = "quality_filter"

FUNDAMENTALS_REQUIRED_COLS: list[str] = [
    "symbol",
    "roe",
    "debt_to_equity",
    "eps_positive_4q",
    "data_quality",
]
OHLCV_REQUIRED_COLS: list[str] = ["symbol", "date", "close", "volume"]

_IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

# Output schema columns (used to build empty DataFrame when thin_universe)
_OUTPUT_COLS: list[str] = [
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


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterReport:
    """Summary of quality filtering results.

    Attributes:
        universe_size: Total number of symbols evaluated.
        passed_count: Number of symbols that passed all 5 hard filters.
        failed_count: Number of symbols that failed at least one hard filter.
        thin_universe: True if passed_count < 3 (minimum universe rule).
        filter_failure_counts: Dict mapping filter name to number of stocks
            that failed it. Keys: "roe", "debt_to_equity", "eps", "volume",
            "price". A stock may appear in multiple failure counts.
        filtered_at_ist: ISO 8601 IST timestamp when filtering was performed.
    """

    universe_size: int
    passed_count: int
    failed_count: int
    thin_universe: bool
    filter_failure_counts: dict[str, int]
    filtered_at_ist: str


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ist_now() -> str:
    """Return the current time as an ISO 8601 IST timestamp string."""
    return datetime.datetime.now(_IST).isoformat(timespec="seconds")


def _compute_ohlcv_metrics(
    symbol: str,
    ohlcv_df: pd.DataFrame,
    lookback_days: int,
) -> dict[str, float | bool]:
    """Compute OHLCV-derived metrics for a single symbol.

    Args:
        symbol: NSE ticker symbol.
        ohlcv_df: Full multi-symbol OHLCV DataFrame.
        lookback_days: Number of most-recent trading days to use.

    Returns:
        Dict with keys avg_daily_value, latest_price, high_52w,
        pct_from_52w_high, within_30pct_of_52w_high, and ohlcv_missing (bool).
    """
    sym_df = ohlcv_df[ohlcv_df["symbol"] == symbol]

    if sym_df.empty:
        return {
            "avg_daily_value": 0.0,
            "latest_price": 0.0,
            "high_52w": 0.0,
            "pct_from_52w_high": 1.0,
            "within_30pct_of_52w_high": False,
            "ohlcv_missing": True,
        }

    sym_df = sym_df.sort_values("date", ascending=False).head(lookback_days)

    close = sym_df["close"].astype(float)
    volume = sym_df["volume"].astype(float)

    daily_value = close * volume
    avg_daily_value: float = float(daily_value.mean())
    latest_price: float = float(close.iloc[0])
    high_52w: float = float(close.max())

    if high_52w == 0.0:
        pct_from_52w_high = 1.0
    else:
        pct_from_52w_high = (high_52w - latest_price) / high_52w

    within_30pct_of_52w_high: bool = pct_from_52w_high <= PROXIMITY_THRESHOLD

    return {
        "avg_daily_value": avg_daily_value,
        "latest_price": latest_price,
        "high_52w": high_52w,
        "pct_from_52w_high": pct_from_52w_high,
        "within_30pct_of_52w_high": within_30pct_of_52w_high,
        "ohlcv_missing": False,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_quality_filter(
    fundamentals_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[pd.DataFrame, FilterReport]:
    """Apply all five hard quality filters to the stock universe.

    Filters:
    1. ROE > 15% (0.15 as decimal)
    2. Debt-to-equity < 1.0
    3. EPS positive for last 4 consecutive quarters (eps_positive_4q == True)
    4. Average daily traded value > 20,00,00,000 INR (20 crore)
    5. Latest close price > 50 INR

    Also computes the soft tiebreaker: percentage distance from 52-week high.
    The soft tiebreaker is never used to eliminate stocks; it is passed
    downstream to momentum.py for tiebreaking only.

    Args:
        fundamentals_df: Output of fetch_fundamentals(). One row per symbol.
            Required columns: symbol, roe, debt_to_equity, eps_positive_4q,
            data_quality.
        ohlcv_df: Output of fetch_ohlcv() or clean_ohlcv(). Multi-symbol OHLCV.
            Required columns: symbol, date, close, volume.
        lookback_days: Number of trading days to use for volume averaging and
            52-week high calculation. Default 252 (one trading year).

    Returns:
        Tuple of (filtered_df, report).
        filtered_df: DataFrame with one row per symbol that passed ALL hard
            filters. Empty DataFrame (same schema, zero rows) if fewer than 3
            stocks pass. Columns: symbol, roe, debt_to_equity, avg_daily_value,
            latest_price, high_52w, pct_from_52w_high, within_30pct_of_52w_high,
            passed_hard_filters.
        report: FilterReport frozen dataclass with filtering metadata.

    Raises:
        ValueError: If fundamentals_df or ohlcv_df is empty.
        ValueError: If required columns are missing from either DataFrame.
    """
    # -----------------------------------------------------------------------
    # 1. Input validation
    # -----------------------------------------------------------------------
    if fundamentals_df.empty:
        raise ValueError("fundamentals_df must not be empty")

    missing_fund_cols = [
        c for c in FUNDAMENTALS_REQUIRED_COLS if c not in fundamentals_df.columns
    ]
    if missing_fund_cols:
        raise ValueError(
            f"fundamentals_df missing required columns: {missing_fund_cols}"
        )

    if ohlcv_df.empty:
        raise ValueError("ohlcv_df must not be empty")

    missing_ohlcv_cols = [
        c for c in OHLCV_REQUIRED_COLS if c not in ohlcv_df.columns
    ]
    if missing_ohlcv_cols:
        raise ValueError(
            f"ohlcv_df missing required columns: {missing_ohlcv_cols}"
        )

    # -----------------------------------------------------------------------
    # 2. Evaluate filters per symbol
    # -----------------------------------------------------------------------
    filter_failure_counts: dict[str, int] = {
        "roe": 0,
        "debt_to_equity": 0,
        "eps": 0,
        "volume": 0,
        "price": 0,
    }

    passing_rows: list[dict] = []
    filtered_at_ist = _ist_now()

    for _, row in fundamentals_df.iterrows():
        symbol: str = str(row["symbol"])
        data_quality: str = str(row.get("data_quality", ""))

        failures: list[str] = []

        # Check stale or failed fundamentals first (fails all filters)
        if data_quality == "fundamentals_stale":
            log_agent_action(
                agent_name=AGENT_NAME,
                action="fundamentals_stale: all hard filters auto-failed",
                level="WARNING",
                symbol=symbol,
                result="failed",
            )
            # Count as failing all 5 hard filters
            for key in filter_failure_counts:
                filter_failure_counts[key] += 1
            continue

        if data_quality == "failed":
            log_agent_action(
                agent_name=AGENT_NAME,
                action="fundamentals_failed: all hard filters auto-failed",
                level="WARNING",
                symbol=symbol,
                result="failed",
            )
            for key in filter_failure_counts:
                filter_failure_counts[key] += 1
            continue

        # Pre-compute OHLCV metrics
        metrics = _compute_ohlcv_metrics(symbol, ohlcv_df, lookback_days)

        if metrics["ohlcv_missing"]:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="ohlcv_missing: volume and price filters auto-failed",
                level="WARNING",
                symbol=symbol,
                result="failed",
            )

        # Evaluate ALL five hard filters (no short-circuit)

        # Filter 1: ROE
        roe = row["roe"]
        if pd.isna(roe):
            failures.append("roe")
            filter_failure_counts["roe"] += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action="roe_data_missing",
                level="WARNING",
                symbol=symbol,
                result="failed",
            )
        elif float(roe) <= ROE_THRESHOLD:
            failures.append("roe")
            filter_failure_counts["roe"] += 1

        # Filter 2: Debt-to-equity
        de = row["debt_to_equity"]
        if pd.isna(de):
            failures.append("debt_to_equity")
            filter_failure_counts["debt_to_equity"] += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action="de_data_missing",
                level="WARNING",
                symbol=symbol,
                result="failed",
            )
        elif float(de) >= DE_THRESHOLD:
            failures.append("debt_to_equity")
            filter_failure_counts["debt_to_equity"] += 1

        # Filter 3: EPS
        eps = row["eps_positive_4q"]
        if pd.isna(eps) if not isinstance(eps, bool) else False:
            failures.append("eps")
            filter_failure_counts["eps"] += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action="eps_data_missing",
                level="WARNING",
                symbol=symbol,
                result="failed",
            )
        elif not bool(eps):
            failures.append("eps")
            filter_failure_counts["eps"] += 1

        # Filter 4: Volume / avg daily traded value
        avg_daily_value: float = float(metrics["avg_daily_value"])
        if avg_daily_value <= VOLUME_VALUE_THRESHOLD:
            failures.append("volume")
            filter_failure_counts["volume"] += 1

        # Filter 5: Price
        latest_price: float = float(metrics["latest_price"])
        if latest_price <= PRICE_THRESHOLD:
            failures.append("price")
            filter_failure_counts["price"] += 1

        # Per-symbol log
        if failures:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"failed filters: {', '.join(failures)}",
                level="INFO",
                symbol=symbol,
                result="failed",
            )
        else:
            log_agent_action(
                agent_name=AGENT_NAME,
                action="passed all hard filters",
                level="INFO",
                symbol=symbol,
                result="passed",
            )
            passing_rows.append(
                {
                    "symbol": symbol,
                    "roe": float(roe),
                    "debt_to_equity": float(de),
                    "avg_daily_value": avg_daily_value,
                    "latest_price": latest_price,
                    "high_52w": float(metrics["high_52w"]),
                    "pct_from_52w_high": float(metrics["pct_from_52w_high"]),
                    "within_30pct_of_52w_high": bool(metrics["within_30pct_of_52w_high"]),
                    "passed_hard_filters": True,
                }
            )

    # -----------------------------------------------------------------------
    # 3. Build FilterReport
    # -----------------------------------------------------------------------
    universe_size: int = len(fundamentals_df)
    passed_count: int = len(passing_rows)
    failed_count: int = universe_size - passed_count
    thin_universe: bool = passed_count < MIN_UNIVERSE_SIZE

    report = FilterReport(
        universe_size=universe_size,
        passed_count=passed_count,
        failed_count=failed_count,
        thin_universe=thin_universe,
        filter_failure_counts=filter_failure_counts,
        filtered_at_ist=filtered_at_ist,
    )

    # -----------------------------------------------------------------------
    # 4. Thin universe check
    # -----------------------------------------------------------------------
    if thin_universe:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"thin_universe: only {passed_count} stocks passed, "
                f"minimum is {MIN_UNIVERSE_SIZE}, returning empty"
            ),
            level="WARNING",
            result="thin_universe",
        )
        empty_df = pd.DataFrame(columns=_OUTPUT_COLS)
        return empty_df, report

    # -----------------------------------------------------------------------
    # 5. Build and return output DataFrame
    # -----------------------------------------------------------------------
    filtered_df = (
        pd.DataFrame(passing_rows)
        .sort_values("symbol", ascending=True)
        .reset_index(drop=True)
    )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"filter complete: {passed_count}/{universe_size} passed, "
            f"thin_universe={thin_universe}"
        ),
        result="ok",
    )

    return filtered_df, report
