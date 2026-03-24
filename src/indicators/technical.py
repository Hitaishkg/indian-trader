"""Technical indicator calculations for the Indian Trader pipeline.

Pure calculation module — computes RSI, MACD, Bollinger Bands, and ATR on
cleaned OHLCV data using pandas-ta. Accepts the output of clean_ohlcv() and
returns a new DataFrame with 8 indicator columns appended.

Contains zero strategy logic: no buy/sell signals, no thresholds, no
filtering. Indicators are computed per symbol independently so that values
never bleed across symbol boundaries. No database writes. No network calls.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta  # type: ignore[import-untyped]

from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MINIMUM_LOOKBACK: int = 26
"""Minimum rows required per symbol to compute any indicator. Equals MACD slow
period — the largest minimum data requirement among all four indicators."""

RSI_PERIOD: int = 14
"""Default RSI lookback period."""

MACD_FAST: int = 12
"""Default MACD fast EMA period."""

MACD_SLOW: int = 26
"""Default MACD slow EMA period."""

MACD_SIGNAL_PERIOD: int = 9
"""Default MACD signal line smoothing period."""

BB_LENGTH: int = 20
"""Default Bollinger Bands lookback length."""

BB_STD: float = 2.0
"""Default Bollinger Bands standard deviation multiplier."""

ATR_PERIOD: int = 14
"""Default ATR lookback period (Wilder smoothing)."""

AGENT_NAME: str = "technical"
"""Agent name for log_agent_action() calls."""

_REQUIRED_COLUMNS: list[str] = ["symbol", "date", "open", "high", "low", "close", "volume"]
"""Columns that must exist in the input DataFrame."""

_ATR_REQUIRED_COLUMNS: list[str] = ["high", "low", "close"]
"""Columns required for standalone ATR computation."""

# Column name mapping from pandas-ta internal names to project names.
_INDICATOR_COLUMNS: list[str] = [
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "atr",
]


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------


def _compute_for_symbol(
    group: pd.DataFrame,
    symbol: str,
    rsi_period: int,
    macd_fast: int,
    macd_slow: int,
    macd_signal_period: int,
    bb_length: int,
    bb_std: float,
    atr_period: int,
) -> pd.DataFrame:
    """Compute all indicators for a single symbol group.

    This private helper is called by add_indicators() via a for-loop over
    groupby("symbol") groups. It receives only one symbol's rows plus the
    symbol name explicitly (avoids relying on the groupby key column being
    present in the group, which changed in pandas 3.0).

    When the group has fewer than MINIMUM_LOOKBACK rows all 8 indicator columns
    are set to NaN and a WARNING is logged. Any pandas-ta failure is caught per
    indicator so one indicator's error cannot skip others.

    Args:
        group: Single-symbol slice of the cleaned OHLCV DataFrame (copy).
        symbol: NSE ticker symbol name for this group.
        rsi_period: RSI lookback period.
        macd_fast: MACD fast EMA period.
        macd_slow: MACD slow EMA period.
        macd_signal_period: MACD signal line smoothing period.
        bb_length: Bollinger Bands lookback length.
        bb_std: Bollinger Bands standard deviation multiplier.
        atr_period: ATR lookback period.

    Returns:
        The same rows with indicator columns appended. Never raises.
    """
    group = group.copy()

    # Pre-fill all indicator columns with NaN so the schema is always complete.
    for col in _INDICATOR_COLUMNS:
        group[col] = float("nan")

    # Short-circuit if insufficient rows.
    if len(group) < MINIMUM_LOOKBACK:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"insufficient_lookback: {len(group)} rows < {MINIMUM_LOOKBACK}"
            ),
            level="WARNING",
            symbol=symbol,
            result="skipped",
        )
        return group

    try:
        # --- RSI ---
        try:
            rsi_series = ta.rsi(close=group["close"], length=rsi_period)
            if rsi_series is not None:
                group["rsi"] = rsi_series.values
            else:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="pandas_ta returned None for rsi",
                    level="WARNING",
                    symbol=symbol,
                    result="error",
                )
        except TypeError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"pandas_ta returned None for rsi: TypeError {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )

        # --- MACD ---
        try:
            macd_df = ta.macd(
                close=group["close"],
                fast=macd_fast,
                slow=macd_slow,
                signal=macd_signal_period,
            )
            if macd_df is not None:
                macd_col = f"MACD_{macd_fast}_{macd_slow}_{macd_signal_period}"
                macd_hist_col = f"MACDh_{macd_fast}_{macd_slow}_{macd_signal_period}"
                macd_sig_col = f"MACDs_{macd_fast}_{macd_slow}_{macd_signal_period}"
                if macd_col in macd_df.columns:
                    group["macd"] = macd_df[macd_col].values
                if macd_hist_col in macd_df.columns:
                    group["macd_hist"] = macd_df[macd_hist_col].values
                if macd_sig_col in macd_df.columns:
                    group["macd_signal"] = macd_df[macd_sig_col].values
            else:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="pandas_ta returned None for macd",
                    level="WARNING",
                    symbol=symbol,
                    result="error",
                )
        except TypeError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"pandas_ta returned None for macd: TypeError {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )

        # --- Bollinger Bands ---
        try:
            bb_df = ta.bbands(close=group["close"], length=bb_length, std=bb_std)  # type: ignore[arg-type]
            if bb_df is not None:
                # pandas-ta column names vary by version (e.g. BBL_20_2.0_2.0 vs
                # BBL_20_2.0), so use prefix matching rather than exact names.
                prefix = f"{bb_length}_"
                bb_lower_col = next(
                    (c for c in bb_df.columns if c.startswith(f"BBL_{prefix}")), None
                )
                bb_mid_col = next(
                    (c for c in bb_df.columns if c.startswith(f"BBM_{prefix}")), None
                )
                bb_upper_col = next(
                    (c for c in bb_df.columns if c.startswith(f"BBU_{prefix}")), None
                )
                if bb_lower_col is not None:
                    group["bb_lower"] = bb_df[bb_lower_col].values
                if bb_mid_col is not None:
                    group["bb_mid"] = bb_df[bb_mid_col].values
                if bb_upper_col is not None:
                    group["bb_upper"] = bb_df[bb_upper_col].values
            else:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="pandas_ta returned None for bbands",
                    level="WARNING",
                    symbol=symbol,
                    result="error",
                )
        except TypeError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"pandas_ta returned None for bbands: TypeError {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )

        # --- ATR (Wilder smoothing via RMA — pandas-ta default) ---
        try:
            atr_series = ta.atr(
                high=group["high"],
                low=group["low"],
                close=group["close"],
                length=atr_period,
            )
            if atr_series is not None:
                group["atr"] = atr_series.values
            else:
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action="pandas_ta returned None for atr",
                    level="WARNING",
                    symbol=symbol,
                    result="error",
                )
        except TypeError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"pandas_ta returned None for atr: TypeError {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )

    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"indicator computation failed for symbol: {exc}",
            level="ERROR",
            symbol=symbol,
            result="error",
        )
        # Reset all indicators to NaN for this symbol on unexpected failure.
        for col in _INDICATOR_COLUMNS:
            group[col] = float("nan")

    return group


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_indicators(
    df: pd.DataFrame,
    rsi_period: int = RSI_PERIOD,
    macd_fast: int = MACD_FAST,
    macd_slow: int = MACD_SLOW,
    macd_signal: int = MACD_SIGNAL_PERIOD,
    bb_length: int = BB_LENGTH,
    bb_std: float = BB_STD,
    atr_period: int = ATR_PERIOD,
) -> pd.DataFrame:
    """Compute technical indicators per symbol and append columns to the DataFrame.

    Processes each symbol group independently to prevent cross-symbol data
    bleeding. Symbols with fewer than MINIMUM_LOOKBACK rows receive NaN for
    all indicator columns rather than partial or incorrect values.

    Args:
        df: Cleaned OHLCV DataFrame from clean_ohlcv(). Must contain columns:
            symbol, date, open, high, low, close, volume. The date column must
            be timezone-aware (Asia/Kolkata) and the DataFrame must be sorted
            by (symbol, date) ascending.
        rsi_period: RSI lookback period. Default 14.
        macd_fast: MACD fast EMA period. Default 12.
        macd_slow: MACD slow EMA period. Default 26.
        macd_signal: MACD signal line period. Default 9.
        bb_length: Bollinger Bands lookback length. Default 20.
        bb_std: Bollinger Bands standard deviation multiplier. Default 2.0.
        atr_period: ATR lookback period. Default 14.

    Returns:
        A new DataFrame with the same rows and original columns, plus 8 new
        indicator columns: rsi, macd, macd_signal, macd_hist, bb_upper,
        bb_mid, bb_lower, atr. Rows where indicators cannot be computed
        (insufficient lookback) contain NaN in those columns.

    Raises:
        ValueError: If required columns are missing or date is timezone-naive.
        ValueError: If the DataFrame is empty.
    """
    # --- Input validation ---
    if len(df) == 0:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="validation_failed: DataFrame is empty",
            level="ERROR",
            result="error",
        )
        raise ValueError("DataFrame is empty, cannot compute indicators")

    missing_cols = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"validation_failed: missing columns {missing_cols}",
            level="ERROR",
            result="error",
        )
        raise ValueError(f"DataFrame missing required columns: {missing_cols}")

    date_dtype = df["date"].dtype
    if not hasattr(date_dtype, "tz") or date_dtype.tz is None:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="validation_failed: date column is timezone-naive",
            level="ERROR",
            result="error",
        )
        raise ValueError("date column must be timezone-aware (Asia/Kolkata)")

    # Work on a copy so the caller's DataFrame is never mutated.
    working_df = df.copy()

    n_symbols = working_df["symbol"].nunique()
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"computing indicators for {n_symbols} symbols",
        level="INFO",
    )

    # Apply per-symbol calculation via explicit for-loop to avoid pandas 3.0
    # groupby.apply() dropping the groupby key column from the group DataFrame.
    processed_groups: list[pd.DataFrame] = []
    for symbol_name, group in working_df.groupby("symbol"):
        processed_groups.append(
            _compute_for_symbol(
                group,
                symbol=str(symbol_name),
                rsi_period=rsi_period,
                macd_fast=macd_fast,
                macd_slow=macd_slow,
                macd_signal_period=macd_signal,
                bb_length=bb_length,
                bb_std=bb_std,
                atr_period=atr_period,
            )
        )
    # sort_index restores the original row order since groupby sorts groups
    # alphabetically, which would otherwise reorder rows.
    result = pd.concat(processed_groups).sort_index()

    # Count skipped vs ok symbols based on whether all indicators are NaN.
    n_skipped = 0
    n_ok = 0
    for sym, grp in result.groupby("symbol"):
        if grp["rsi"].isna().all() and grp["atr"].isna().all():
            n_skipped += 1
        else:
            n_ok += 1

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"indicators computed: {n_ok} symbols ok, {n_skipped} symbols skipped"
        ),
        level="INFO",
        result="ok",
    )

    return result  # type: ignore[return-value]


def compute_atr_series(
    df: pd.DataFrame,
    period: int = ATR_PERIOD,
) -> pd.Series:
    """Compute ATR series for a single symbol's OHLCV data using Wilder smoothing.

    Exposed as a standalone function because other modules (risk_agent,
    execution_agent, main.py) need ATR for a single symbol without computing
    all indicators. Uses pandas-ta internally.

    Args:
        df: OHLCV DataFrame for ONE symbol. Must contain columns: high, low,
            close. Must be sorted by date ascending.
        period: ATR lookback period. Default 14.

    Returns:
        pd.Series of ATR values aligned to the input index. Leading rows
        within the lookback window contain NaN.

    Raises:
        ValueError: If required columns (high, low, close) are missing.
    """
    missing_cols = [c for c in _ATR_REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(
            f"DataFrame missing required columns for ATR: {missing_cols}"
        )

    atr_series = ta.atr(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        length=period,
    )

    if atr_series is None:
        # Return a Series of NaN aligned to the input index.
        return pd.Series(float("nan"), index=df.index, dtype="float64")

    # Realign to the input index to handle any index mismatches from pandas-ta.
    return atr_series.reindex(df.index)
