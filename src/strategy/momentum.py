"""12-1 momentum factor computation and top-N candidate selection.

Formula: momentum_score = 12-month total return - 1-month total return

Where:
  12-month return = (close[today] - close[252 days ago]) / close[252 days ago]
  1-month return  = (close[today] - close[21 days ago]) / close[21 days ago]

Symbols with fewer than 252 trading days of history are excluded.
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

TWELVE_MONTH_LOOKBACK: int = 252     # Trading days in 12 months
ONE_MONTH_LOOKBACK: int = 21         # Trading days in 1 month
DEFAULT_TOP_N: int = 5
TIEBREAKER_THRESHOLD: float = 0.02   # 2% relative difference threshold
AGENT_NAME: str = "momentum"

# Output column order
_OUTPUT_COLUMNS: list[str] = [
    "symbol",
    "momentum_score",
    "twelve_month_return",
    "one_month_return",
    "rank",
    "pct_from_52w_high",
    "within_30pct_of_52w_high",
]

_QUALITY_REQUIRED: list[str] = ["symbol", "pct_from_52w_high", "within_30pct_of_52w_high"]
_OHLCV_REQUIRED: list[str] = ["symbol", "date", "close"]


# ---------------------------------------------------------------------------
# MomentumReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentumReport:
    """Summary of momentum computation results.

    Attributes:
        scored_count: Number of symbols for which a valid momentum score
            was computed (had >= 252 rows of history).
        selected_count: Number of symbols in the final top-N output.
        insufficient_history_count: Number of symbols excluded for having
            < 252 rows of OHLCV history.
        tiebreaker_applied_count: Number of times the 2% tiebreaker rule
            was applied during ranking.
        computed_at_ist: ISO 8601 IST timestamp when computation was performed.
    """

    scored_count: int
    selected_count: int
    insufficient_history_count: int
    tiebreaker_applied_count: int
    computed_at_ist: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_momentum(
    quality_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    top_n: int = DEFAULT_TOP_N,
) -> tuple[pd.DataFrame, MomentumReport]:
    """Compute 12-1 momentum scores and select top N candidates.

    Formula: momentum_score = 12-month total return - 1-month total return

    Where:
    - 12-month return = (close[today] - close[252 days ago]) / close[252 days ago]
    - 1-month return = (close[today] - close[21 days ago]) / close[21 days ago]

    Symbols with fewer than 252 trading days of history are excluded from
    ranking and logged as insufficient_history.

    Args:
        quality_df: Output of apply_quality_filter(). One row per quality-
            filtered symbol. Required columns: symbol, pct_from_52w_high,
            within_30pct_of_52w_high.
        ohlcv_df: Full OHLCV history. Multi-symbol DataFrame. Required
            columns: symbol, date, close. Date must be timezone-aware
            (Asia/Kolkata).
        top_n: Number of top candidates to return. Default 5.

    Returns:
        Tuple of (ranked_df, report).
        ranked_df: DataFrame with one row per selected candidate, sorted by
            rank ascending (rank 1 = highest momentum). Columns: symbol,
            momentum_score, twelve_month_return, one_month_return, rank,
            pct_from_52w_high, within_30pct_of_52w_high.
        report: MomentumReport frozen dataclass with computation metadata.

    Raises:
        ValueError: If quality_df is empty.
        ValueError: If ohlcv_df is empty.
        ValueError: If required columns missing from quality_df.
        ValueError: If required columns missing from ohlcv_df.
        ValueError: If top_n < 1.
    """
    _validate_inputs(quality_df, ohlcv_df, top_n)

    # Build per-symbol OHLCV lookup
    ohlcv_by_symbol: dict[str, pd.DataFrame] = {
        str(sym): grp.sort_values("date").reset_index(drop=True)
        for sym, grp in ohlcv_df.groupby("symbol")
    }

    # Build quality lookup for pct_from_52w_high and within_30pct_of_52w_high
    quality_lookup: dict[str, dict[str, object]] = {
        str(row["symbol"]): {
            "pct_from_52w_high": row["pct_from_52w_high"],
            "within_30pct_of_52w_high": row["within_30pct_of_52w_high"],
        }
        for _, row in quality_df.iterrows()
    }

    # Score each symbol
    scores: list[dict[str, object]] = []
    insufficient_history_count: int = 0

    for symbol in quality_df["symbol"].astype(str):
        rows = ohlcv_by_symbol.get(symbol, pd.DataFrame())

        if len(rows) < TWELVE_MONTH_LOOKBACK:
            insufficient_history_count += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"insufficient_history: {len(rows)} rows (need {TWELVE_MONTH_LOOKBACK})",
                symbol=symbol,
                result="insufficient_history",
            )
            continue

        close_today = float(rows.iloc[-1]["close"])
        close_12m_ago = float(rows.iloc[-TWELVE_MONTH_LOOKBACK]["close"])
        close_1m_ago = float(rows.iloc[-ONE_MONTH_LOOKBACK]["close"])

        if close_12m_ago == 0.0:
            insufficient_history_count += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action="zero_close_12m: close at 252-day lookback is 0.0",
                symbol=symbol,
                result="zero_close_12m",
            )
            continue

        if close_1m_ago == 0.0:
            insufficient_history_count += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action="zero_close_1m: close at 21-day lookback is 0.0",
                symbol=symbol,
                result="zero_close_1m",
            )
            continue

        twelve_month_return = (close_today - close_12m_ago) / close_12m_ago
        one_month_return = (close_today - close_1m_ago) / close_1m_ago
        momentum_score = twelve_month_return - one_month_return

        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"momentum_score={momentum_score:.4f} "
                f"(12m={twelve_month_return:.4f}, 1m={one_month_return:.4f})"
            ),
            symbol=symbol,
            result="scored",
        )

        quality_info = quality_lookup.get(symbol, {})
        scores.append(
            {
                "symbol": symbol,
                "momentum_score": momentum_score,
                "twelve_month_return": twelve_month_return,
                "one_month_return": one_month_return,
                "pct_from_52w_high": quality_info.get("pct_from_52w_high", float("nan")),
                "within_30pct_of_52w_high": quality_info.get("within_30pct_of_52w_high", False),
            }
        )

    # If no valid scores, return empty DataFrame with correct schema
    if not scores:
        empty_df = pd.DataFrame(columns=_OUTPUT_COLUMNS)
        report = MomentumReport(
            scored_count=0,
            selected_count=0,
            insufficient_history_count=insufficient_history_count,
            tiebreaker_applied_count=0,
            computed_at_ist=_ist_now(),
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"momentum complete: 0 scored, 0 selected, "
                f"{insufficient_history_count} insufficient history, 0 tiebreakers"
            ),
            result="ok",
        )
        return empty_df, report

    scored_count = len(scores)

    # Sort by momentum_score descending
    scores.sort(key=lambda x: float(x["momentum_score"]), reverse=True)  # type: ignore[arg-type]

    # Apply tiebreaker (single adjacent-pair pass)
    tiebreaker_applied_count = _apply_tiebreaker(scores)

    # Take top_n (or all if fewer)
    selected = scores[:top_n]

    # Assign ranks
    ranked_rows: list[dict[str, object]] = []
    for rank_idx, entry in enumerate(selected, start=1):
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"selected: rank={rank_idx}, momentum_score={float(entry['momentum_score']):.4f}",  # type: ignore[arg-type]
            symbol=str(entry["symbol"]),
            result="selected",
        )
        ranked_rows.append(
            {
                "symbol": entry["symbol"],
                "momentum_score": entry["momentum_score"],
                "twelve_month_return": entry["twelve_month_return"],
                "one_month_return": entry["one_month_return"],
                "rank": rank_idx,
                "pct_from_52w_high": entry["pct_from_52w_high"],
                "within_30pct_of_52w_high": entry["within_30pct_of_52w_high"],
            }
        )

    ranked_df = pd.DataFrame(ranked_rows, columns=_OUTPUT_COLUMNS).reset_index(drop=True)
    # Ensure rank column is int64
    ranked_df["rank"] = ranked_df["rank"].astype("int64")

    selected_count = len(ranked_df)

    report = MomentumReport(
        scored_count=scored_count,
        selected_count=selected_count,
        insufficient_history_count=insufficient_history_count,
        tiebreaker_applied_count=tiebreaker_applied_count,
        computed_at_ist=_ist_now(),
    )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"momentum complete: {scored_count} scored, {selected_count} selected, "
            f"{insufficient_history_count} insufficient history, "
            f"{tiebreaker_applied_count} tiebreakers"
        ),
        result="ok",
    )

    return ranked_df, report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_inputs(
    quality_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    top_n: int,
) -> None:
    """Validate all inputs; raise ValueError on any violation."""
    if quality_df.empty:
        raise ValueError("quality_df must not be empty")
    if ohlcv_df.empty:
        raise ValueError("ohlcv_df must not be empty")

    missing_quality = [c for c in _QUALITY_REQUIRED if c not in quality_df.columns]
    if missing_quality:
        raise ValueError(f"quality_df missing required columns: {missing_quality}")

    missing_ohlcv = [c for c in _OHLCV_REQUIRED if c not in ohlcv_df.columns]
    if missing_ohlcv:
        raise ValueError(f"ohlcv_df missing required columns: {missing_ohlcv}")

    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")


def _apply_tiebreaker(scores: list[dict[str, object]]) -> int:
    """Single adjacent-pair pass tiebreaker. Mutates scores in place.

    For each adjacent pair (i, i+1): if relative difference in momentum_score
    is < 2%, the symbol with lower pct_from_52w_high (closer to 52w high)
    takes the higher rank. Returns the number of swaps made.
    """
    tiebreaker_count = 0
    for i in range(len(scores) - 1):
        score_a = float(scores[i]["momentum_score"])  # type: ignore[arg-type]
        score_b = float(scores[i + 1]["momentum_score"])  # type: ignore[arg-type]

        if score_b != 0.0:
            rel_diff = abs(score_a - score_b) / abs(score_b)
        elif score_a != 0.0:
            rel_diff = abs(score_a)
        else:
            rel_diff = 0.0

        if rel_diff < TIEBREAKER_THRESHOLD:
            pct_a = float(scores[i]["pct_from_52w_high"])  # type: ignore[arg-type]
            pct_b = float(scores[i + 1]["pct_from_52w_high"])  # type: ignore[arg-type]

            # Lower pct_from_52w_high = closer to 52w high = wins tiebreaker
            if pct_b < pct_a:
                # Swap: b should be ranked higher (position i)
                scores[i], scores[i + 1] = scores[i + 1], scores[i]
                winner = str(scores[i]["symbol"])
                loser = str(scores[i + 1]["symbol"])
                winner_pct = float(scores[i]["pct_from_52w_high"])  # type: ignore[arg-type]
                loser_pct = float(scores[i + 1]["pct_from_52w_high"])  # type: ignore[arg-type]
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=(
                        f"tiebreaker: {winner} (pct_from_52w_high={winner_pct:.4f}) beats "
                        f"{loser} (pct_from_52w_high={loser_pct:.4f}), scores within 2%"
                    ),
                    result="tiebreaker_applied",
                )
                tiebreaker_count += 1

    return tiebreaker_count


def _ist_now() -> str:
    """Return current IST time as ISO 8601 string."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
