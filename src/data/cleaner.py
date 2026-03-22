"""OHLCV data cleaning layer for the Indian Trader pipeline.

Sits between fetcher.py and validator.py. Receives a raw OHLCV DataFrame
(normalised to the validator.py Section 5.1 contract) and returns a cleaned
copy alongside a CleaningReport that audits every repair and flag applied.

The cleaner performs best-effort data repair: forward-filling missing prices,
removing duplicate dates, and flagging anomalies (negative prices, OHLCV
inconsistencies, sub-floor close prices). It never drops anomalous rows — it
flags them and leaves them for the validator to assess downstream.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from src.config.settings import settings

# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, settings.log_level))

if not logging.root.handlers:
    _handler = logging.StreamHandler()
    _handler.setLevel(logging.DEBUG)
    logging.root.addHandler(_handler)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PRICE_FLOOR: float = 1.0
"""Default minimum close price for data sanity flagging. Overridable via the
price_floor parameter on clean_ohlcv(). This is NOT the strategy's price
filter (>= 50 INR) -- that lives in src/strategy/quality_filter.py."""

AGENT_NAME: str = "cleaner"
"""Agent name for future integration with src/utils/logger.py (Phase 1, step 6)."""

_REQUIRED_COLUMNS: list[str] = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
]

_IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleaningReport:
    """Result of a cleaning pass over an OHLCV DataFrame.

    All flag lists contain (symbol, date_str) tuples where date_str
    is an ISO 8601 date string (YYYY-MM-DD).

    Attributes:
        symbols_processed: Symbols present in the input DataFrame.
        rows_input: Row count of the input DataFrame before cleaning.
        rows_output: Row count after cleaning (equals rows_input minus duplicates_removed).
        duplicates_removed: Count of rows removed due to duplicate dates within a symbol.
        missing_close_filled: Count of NaN close values that were forward-filled.
        missing_ohlv_filled: Count of open/high/low/volume NaN values filled.
        negative_price_flags: List of (symbol, date_str) for rows with any price <= 0.
        consistency_flags: List of (symbol, date_str) for rows where high < low.
        price_floor_flags: List of (symbol, date_str) for rows where close < price_floor.
        cleaned_at_ist: IST timestamp (ISO 8601 with timezone) when clean_ohlcv was called.
    """

    symbols_processed: list[str]
    rows_input: int
    rows_output: int
    duplicates_removed: int
    missing_close_filled: int
    missing_ohlv_filled: int
    negative_price_flags: list[tuple[str, str]]
    consistency_flags: list[tuple[str, str]]
    price_floor_flags: list[tuple[str, str]]
    cleaned_at_ist: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_schema(df: pd.DataFrame) -> None:
    """Check that df has all required columns and timezone-aware dates.

    Mirrors the logic of validator.py's _validate_ohlcv_df but is implemented
    independently to avoid coupling to a private function.

    Args:
        df: The OHLCV DataFrame to validate structurally.

    Raises:
        ValueError: If required columns are missing or dates are not timezone-aware.
    """
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"DataFrame is missing required columns: {missing}. "
            f"Required: {_REQUIRED_COLUMNS}"
        )

    date_col = df["date"]
    if hasattr(date_col, "dt"):
        tz = date_col.dt.tz
    else:
        tz = None

    if tz is None:
        raise ValueError(
            "DataFrame 'date' column must be timezone-aware "
            "(expected Asia/Kolkata). Got timezone-naive dates."
        )


def _flag_negative_prices(df: pd.DataFrame) -> list[tuple[str, str]]:
    """Detect rows where any of open/high/low/close is <= 0.

    Args:
        df: The OHLCV DataFrame (schema already validated).

    Returns:
        List of (symbol, date_str) tuples for each anomalous row.
    """
    flags: list[tuple[str, str]] = []
    mask = (
        (df["open"] <= 0)
        | (df["high"] <= 0)
        | (df["low"] <= 0)
        | (df["close"] <= 0)
    )
    for _, row in df[mask].iterrows():
        symbol: str = str(row["symbol"])
        date_str: str = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
        logger.warning("Negative/zero price detected: %s on %s", symbol, date_str)
        flags.append((symbol, date_str))
    return flags


def _flag_consistency(df: pd.DataFrame) -> list[tuple[str, str]]:
    """Detect rows where high < low (data corruption indicator).

    Args:
        df: The OHLCV DataFrame (schema already validated).

    Returns:
        List of (symbol, date_str) tuples for each inconsistent row.
    """
    flags: list[tuple[str, str]] = []
    mask = df["high"] < df["low"]
    for _, row in df[mask].iterrows():
        symbol: str = str(row["symbol"])
        date_str: str = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
        logger.warning("OHLCV inconsistency (high < low): %s on %s", symbol, date_str)
        flags.append((symbol, date_str))
    return flags


def _fill_missing_values(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, int, int]:
    """Forward-fill close, set open/high/low from close, fill volume with 0.

    Operations are scoped per symbol group to prevent cross-symbol bleeding.

    Args:
        df: The OHLCV DataFrame (schema already validated).

    Returns:
        Tuple of (filled_df, missing_close_filled, missing_ohlv_filled).
    """
    # Count NaN closes before forward-fill
    close_nan_before = df["close"].isna().sum()

    # Forward-fill close within each symbol group
    df["close"] = df.groupby("symbol", group_keys=False)["close"].transform(
        lambda s: s.ffill()
    )

    close_nan_after = df["close"].isna().sum()
    missing_close_filled: int = int(close_nan_before - close_nan_after)

    # For rows where open/high/low are NaN, fill all three from the (now-filled) close
    missing_ohlv_filled: int = 0

    ohlv_nan_before_open = df["open"].isna().sum()
    ohlv_nan_before_high = df["high"].isna().sum()
    ohlv_nan_before_low = df["low"].isna().sum()

    # Identify rows where open/high/low are NaN (use the same mask for all three)
    ohlv_nan_mask = df["open"].isna() | df["high"].isna() | df["low"].isna()

    if ohlv_nan_mask.any():
        # For each row where any of open/high/low is NaN, set all three to close
        df.loc[ohlv_nan_mask, "open"] = df.loc[ohlv_nan_mask, "close"]
        df.loc[ohlv_nan_mask, "high"] = df.loc[ohlv_nan_mask, "close"]
        df.loc[ohlv_nan_mask, "low"] = df.loc[ohlv_nan_mask, "close"]

    ohlv_nan_after_open = df["open"].isna().sum()
    ohlv_nan_after_high = df["high"].isna().sum()
    ohlv_nan_after_low = df["low"].isna().sum()

    missing_ohlv_filled += int(ohlv_nan_before_open - ohlv_nan_after_open)
    missing_ohlv_filled += int(ohlv_nan_before_high - ohlv_nan_after_high)
    missing_ohlv_filled += int(ohlv_nan_before_low - ohlv_nan_after_low)

    # Fill remaining volume NaN with 0.0
    vol_nan_before = df["volume"].isna().sum()
    df["volume"] = df["volume"].fillna(0.0)
    vol_nan_after = df["volume"].isna().sum()
    missing_ohlv_filled += int(vol_nan_before - vol_nan_after)

    n_symbols = df["symbol"].nunique()
    logger.info(
        "Filled %d close values, %d open/high/low/volume values across %d symbols",
        missing_close_filled,
        missing_ohlv_filled,
        n_symbols,
    )

    return df, missing_close_filled, missing_ohlv_filled


def _remove_duplicates(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Remove duplicate dates within each symbol, keeping last occurrence.

    Args:
        df: The OHLCV DataFrame.

    Returns:
        Tuple of (deduped_df, duplicates_removed_count).
    """
    rows_before = len(df)

    # Identify duplicates before dropping so we can log them
    dup_mask = df.duplicated(subset=["symbol", "date"], keep="last")
    for _, row in df[dup_mask].iterrows():
        symbol: str = str(row["symbol"])
        date_str: str = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
        logger.warning("Duplicate date removed: %s on %s", symbol, date_str)

    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.reset_index(drop=True)

    rows_after = len(df)
    duplicates_removed: int = rows_before - rows_after
    return df, duplicates_removed


def _flag_price_floor(
    df: pd.DataFrame,
    price_floor: float,
) -> list[tuple[str, str]]:
    """Detect rows where close < price_floor.

    Args:
        df: The OHLCV DataFrame.
        price_floor: Minimum acceptable close price.

    Returns:
        List of (symbol, date_str) tuples for each flagged row.
    """
    flags: list[tuple[str, str]] = []
    mask = df["close"] < price_floor
    for _, row in df[mask].iterrows():
        symbol: str = str(row["symbol"])
        date_str: str = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
        close: float = float(row["close"])
        logger.warning(
            "Price below floor (%s): %s on %s, close=%s",
            price_floor,
            symbol,
            date_str,
            close,
        )
        flags.append((symbol, date_str))
    return flags


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def clean_ohlcv(
    df: pd.DataFrame,
    price_floor: float = 1.0,
) -> tuple[pd.DataFrame, CleaningReport]:
    """Clean an OHLCV DataFrame by handling missing values, removing duplicates, and flagging anomalies.

    Receives a normalised OHLCV DataFrame (matching the validator.py Section 5.1
    contract) and returns a cleaned copy alongside a CleaningReport detailing
    every repair and flag applied. The cleaner never drops anomalous rows -- it
    flags them and leaves them for the validator to assess. If no cleaning is
    needed, the DataFrame is returned unchanged.

    Args:
        df: Normalised OHLCV DataFrame with columns [symbol, date, open, high,
            low, close, volume]. The date column must be timezone-aware
            (Asia/Kolkata). Produced by src.data.fetcher.fetch_ohlcv().
        price_floor: Minimum acceptable close price. Rows with close below
                     this value are flagged in the CleaningReport but not
                     removed. Default 1.0 (INR). This is a data sanity check,
                     not a strategy filter.

    Returns:
        A tuple of (cleaned_df, cleaning_report) where:
        - cleaned_df has the same schema as the input (same columns, same
          dtypes, same sort order). Only values may differ.
        - cleaning_report is a frozen CleaningReport with full audit trail.

    Raises:
        ValueError: If required columns are missing or dates are timezone-naive.
    """
    # Capture timestamp at function entry
    cleaned_at_ist: str = datetime.datetime.now(_IST).isoformat(timespec="seconds")

    # Record input dimensions
    rows_input: int = len(df)
    symbols_processed: list[str] = sorted(df["symbol"].unique().tolist())

    # Operate on a copy — never mutate the caller's DataFrame
    working: pd.DataFrame = df.copy()

    # --- 5a. Schema validation ---
    _validate_schema(working)

    # --- 5b. Negative price detection ---
    negative_price_flags = _flag_negative_prices(working)

    # --- 5c. OHLCV consistency check ---
    consistency_flags = _flag_consistency(working)

    # --- 5d. Missing value handling ---
    working, missing_close_filled, missing_ohlv_filled = _fill_missing_values(working)

    # --- 5e. Duplicate date removal ---
    working, duplicates_removed = _remove_duplicates(working)

    # --- 5f. Price floor check ---
    price_floor_flags = _flag_price_floor(working, price_floor)

    # --- Build summary ---
    rows_output: int = len(working)
    total_flags: int = (
        len(negative_price_flags) + len(consistency_flags) + len(price_floor_flags)
    )

    logger.info(
        "Cleaning complete: %d rows in, %d rows out, %d duplicates removed, %d anomalies flagged",
        rows_input,
        rows_output,
        duplicates_removed,
        total_flags,
    )

    report = CleaningReport(
        symbols_processed=symbols_processed,
        rows_input=rows_input,
        rows_output=rows_output,
        duplicates_removed=duplicates_removed,
        missing_close_filled=missing_close_filled,
        missing_ohlv_filled=missing_ohlv_filled,
        negative_price_flags=negative_price_flags,
        consistency_flags=consistency_flags,
        price_floor_flags=price_floor_flags,
        cleaned_at_ist=cleaned_at_ist,
    )

    return working, report
