"""Data quality validator for the Indian Trader pipeline.

This module is the first gating dependency in Phase 1. It validates OHLCV and
fundamentals DataFrames for corruption, coverage gaps, and time-series holes
before any strategy logic runs. Every trade decision logged to agent_logs must
carry a data_quality_score produced by this module.
"""

from __future__ import annotations

import datetime
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

# ---------------------------------------------------------------------------
# Module-level constants (domain rules — not configurable via env vars)
# ---------------------------------------------------------------------------

ROE_MIN: float = -0.50
ROE_MAX: float = 2.00
DE_COVERAGE_THRESHOLD: float = 0.80
UNIVERSE_QUALITY_THRESHOLD: float = 0.60
MAX_OHLCV_GAP_DAYS: int = 5
AGENT_NAME: str = "validator"

_IST = ZoneInfo("Asia/Kolkata")

# Required columns for each DataFrame input
_OHLCV_REQUIRED_COLUMNS: list[str] = [
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
]
_FUNDAMENTALS_REQUIRED_COLUMNS: list[str] = ["symbol", "roe", "debt_to_equity"]


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class DataQualityError(Exception):
    """Raised when universe_quality_score drops below 0.6.

    Attributes:
        universe_quality_score: The score that triggered this error.
        report: The full DataQualityReport for inspection by the caller.
    """

    def __init__(
        self,
        universe_quality_score: float,
        report: "DataQualityReport",
    ) -> None:
        super().__init__(
            f"Universe data quality score {universe_quality_score:.4f} is below "
            f"the required threshold of {UNIVERSE_QUALITY_THRESHOLD}"
        )
        self.universe_quality_score = universe_quality_score
        self.report = report


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataQualityReport:
    """Result of a full data quality validation run.

    Attributes:
        per_stock_scores: Mapping from NSE symbol to data_quality_score (0.0-1.0).
        universe_quality_score: Weighted aggregate score across all checked stocks (0.0-1.0).
        failed_roe_symbols: List of symbols where ROE was outside [-0.50, 2.00].
        roe_missing_symbols: List of symbols where ROE was NULL/NaN.
        de_coverage_ratio: Fraction of universe with non-null debt_to_equity (0.0-1.0).
        de_coverage_low: True if de_coverage_ratio < 0.80.
        gap_violations: Mapping from symbol to list of (gap_start_date, gap_length_days)
                        tuples for every OHLCV gap longer than 5 consecutive trading days.
        checked_at_ist: ISO 8601 timestamp (IST) when this report was generated.
        universe_size: Total number of symbols passed into the validator.
    """

    per_stock_scores: dict[str, float]
    universe_quality_score: float
    failed_roe_symbols: list[str]
    roe_missing_symbols: list[str]
    de_coverage_ratio: float
    de_coverage_low: bool
    gap_violations: dict[str, list[tuple[str, int]]]
    checked_at_ist: str
    universe_size: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_ist() -> datetime.datetime:
    """Return the current datetime in IST."""
    return datetime.datetime.now(_IST)


def _ist_isoformat() -> str:
    """Return current IST time as ISO 8601 string with second precision."""
    return _now_ist().isoformat(timespec="seconds")


def _check_roe(
    symbol: str,
    roe: float | None,
) -> tuple[bool, str]:
    """Validate a single stock's ROE value.

    Returns (passed: bool, detail: str).
    passed is True if roe is not None/NaN AND -0.50 <= roe <= 2.00.
    detail is a human-readable string explaining the result.

    Args:
        symbol: NSE ticker symbol (used for context in detail string).
        roe: ROE value as a decimal, or None if unavailable.

    Returns:
        Tuple of (passed, detail_string).
    """
    if roe is None or (isinstance(roe, float) and math.isnan(roe)):
        return False, f"{symbol}: ROE is null/NaN — cannot validate plausibility"

    if ROE_MIN <= roe <= ROE_MAX:
        return (
            True,
            f"{symbol}: ROE={roe:.4f} is within plausible range [{ROE_MIN}, {ROE_MAX}]",
        )

    return (
        False,
        f"{symbol}: ROE={roe:.4f} is outside plausible range [{ROE_MIN}, {ROE_MAX}]",
    )


def _check_ohlcv_gaps(
    symbol: str,
    dates: pd.Series,
    trading_calendar: list[datetime.date] | None,
) -> list[tuple[str, int]]:
    """Detect gaps longer than 5 consecutive trading days in a sorted date series.

    Args:
        symbol: NSE ticker symbol (used only for logging context).
        dates: Sorted Series of datetime64 values for this symbol's OHLCV rows.
        trading_calendar: See validate_data docstring.

    Returns:
        List of (gap_start_date_str, gap_length_days) tuples.
        Returns empty list if no gaps exceed 5 consecutive trading days.
    """
    if len(dates) < 2:
        return []

    # Convert to date objects for comparison
    date_objects: list[datetime.date] = [
        pd.Timestamp(d).date() for d in dates
    ]

    gaps: list[tuple[str, int]] = []

    for i in range(len(date_objects) - 1):
        d1 = date_objects[i]
        d2 = date_objects[i + 1]

        if trading_calendar is not None:
            # Count calendar dates strictly between d1 and d2
            gap_count = sum(1 for d in trading_calendar if d1 < d < d2)
        else:
            # Use pandas business day range to approximate trading days
            bdate_range = pd.bdate_range(start=d1, end=d2, inclusive="neither")
            gap_count = len(bdate_range)

        if gap_count >= MAX_OHLCV_GAP_DAYS:
            gap_start = d1 + datetime.timedelta(days=1)
            gaps.append((gap_start.isoformat(), gap_count))

    return gaps


def _compute_stock_score(
    roe_passed: bool,
    roe_missing: bool,
    gap_violations: list[tuple[str, int]],
) -> float:
    """Compute data_quality_score for a single stock.

    Scoring formula:
      - ROE plausibility (weight 0.40): 1.0 if roe_passed, else 0.0
      - ROE present (weight 0.10): 1.0 if not roe_missing, else 0.0
      - OHLCV gap (weight 0.50): 1.0 for 0 gaps, 0.5 for 1 gap, 0.0 for 2+ gaps

    Returns a float in [0.0, 1.0].

    Args:
        roe_passed: True if ROE is present and within [-0.50, 2.00].
        roe_missing: True if ROE is null/NaN.
        gap_violations: List of gap tuples from _check_ohlcv_gaps.

    Returns:
        Clamped score in [0.0, 1.0].
    """
    roe_plausibility_score = 1.0 if roe_passed else 0.0
    roe_present_score = 0.0 if roe_missing else 1.0

    gap_count = len(gap_violations)
    if gap_count == 0:
        gap_score = 1.0
    elif gap_count == 1:
        gap_score = 0.5
    else:
        gap_score = 0.0

    raw_score = (
        (roe_plausibility_score * 0.40)
        + (roe_present_score * 0.10)
        + (gap_score * 0.50)
    )
    return max(0.0, min(1.0, raw_score))


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open SQLite connection and apply required pragmas.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        An open sqlite3.Connection with WAL mode and other pragmas applied.

    Raises:
        sqlite3.OperationalError: If the database cannot be opened.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA cache_size=-64000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _ensure_agent_logs_table(conn: sqlite3.Connection) -> None:
    """Create agent_logs table if it does not exist.

    Args:
        conn: Open SQLite connection.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_logs (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name         TEXT    NOT NULL,
            event_type         TEXT    NOT NULL,
            symbol             TEXT,
            detail             TEXT,
            data_quality_score REAL,
            timestamp_ist      TEXT    NOT NULL
        );
        """
    )
    conn.commit()


def _log_event(
    conn: sqlite3.Connection,
    event_type: str,
    symbol: str | None,
    detail: object,
    data_quality_score: float | None,
) -> None:
    """Write a single row to agent_logs.

    Args:
        conn: Open SQLite connection.
        event_type: One of the defined event_type values.
        symbol: NSE ticker symbol or None for universe-level events.
        detail: Detail dict/str to JSON-serialise, or None.
        data_quality_score: Float 0.0-1.0 or None.
    """
    if detail is None:
        detail_str: str | None = None
    elif isinstance(detail, str):
        detail_str = detail
    else:
        try:
            detail_str = json.dumps(detail)
        except (TypeError, ValueError):
            detail_str = str(detail)

    try:
        conn.execute(
            """
            INSERT INTO agent_logs
                (agent_name, event_type, symbol, detail, data_quality_score, timestamp_ist)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                AGENT_NAME,
                event_type,
                symbol,
                detail_str,
                data_quality_score,
                _ist_isoformat(),
            ),
        )
        conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        print(
            f"[validator] Failed to write event_type={event_type} symbol={symbol} "
            f"to agent_logs",
            file=sys.stderr,
        )
        raise


def _validate_ohlcv_df(ohlcv_df: pd.DataFrame) -> None:
    """Check that ohlcv_df has all required columns and timezone-aware dates.

    Args:
        ohlcv_df: The OHLCV DataFrame to validate structurally.

    Raises:
        ValueError: If required columns are missing or dates are not timezone-aware.
    """
    missing = [c for c in _OHLCV_REQUIRED_COLUMNS if c not in ohlcv_df.columns]
    if missing:
        raise ValueError(
            f"ohlcv_df is missing required columns: {missing}. "
            f"Required: {_OHLCV_REQUIRED_COLUMNS}"
        )

    # Check that the date column is timezone-aware
    date_col = ohlcv_df["date"]
    if hasattr(date_col, "dt"):
        tz = date_col.dt.tz
    else:
        # Scalar or non-datetime — will fail later but raise clear error
        tz = None

    if tz is None:
        raise ValueError(
            "ohlcv_df 'date' column must be timezone-aware "
            "(expected Asia/Kolkata). Got timezone-naive dates."
        )


def _validate_fundamentals_df(fundamentals_df: pd.DataFrame) -> None:
    """Check that fundamentals_df has all required columns.

    Args:
        fundamentals_df: The fundamentals DataFrame to validate structurally.

    Raises:
        ValueError: If required columns are missing.
    """
    missing = [
        c for c in _FUNDAMENTALS_REQUIRED_COLUMNS if c not in fundamentals_df.columns
    ]
    if missing:
        raise ValueError(
            f"fundamentals_df is missing required columns: {missing}. "
            f"Required: {_FUNDAMENTALS_REQUIRED_COLUMNS}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_data(
    ohlcv_df: pd.DataFrame,
    fundamentals_df: pd.DataFrame,
    db_path: str,
    trading_calendar: list[datetime.date] | None = None,
) -> DataQualityReport:
    """Run all three data quality checks on the provided DataFrames and return a report.

    Validates ROE plausibility, debt-to-equity coverage, and OHLCV continuity
    for every symbol present in fundamentals_df. Logs every check result to the
    agent_logs table in the SQLite database at db_path. Raises DataQualityError
    if universe_quality_score < 0.6.

    Args:
        ohlcv_df: Normalised OHLCV DataFrame conforming to the contract in Section 5.1.
                  Must contain data for all symbols in fundamentals_df. Symbols present
                  in fundamentals_df but absent from ohlcv_df receive a gap_score of 0.0.
        fundamentals_df: Normalised fundamentals DataFrame conforming to Section 5.2.
                         Defines the universe. Every symbol in this DataFrame is checked.
        db_path: Absolute path to the SQLite database file. Created if it does not exist.
        trading_calendar: Optional explicit list of trading dates (date objects, IST) to use
                          when computing OHLCV gaps. When None, gaps are detected by comparing
                          the actual date sequence against pandas business-day frequency
                          (BDay), which approximates NSE trading days. The caller should
                          pass an actual NSE calendar when available for correctness.

    Returns:
        DataQualityReport populated with results from all checks.

    Raises:
        ValueError: If ohlcv_df or fundamentals_df are missing required columns,
                    or if ohlcv_df dates are not timezone-aware.
        DataQualityError: If universe_quality_score < 0.6 after all checks complete.
        sqlite3.OperationalError: If the database cannot be opened or written to.
    """
    # Capture timestamp at function entry as specified
    checked_at_ist = _now_ist().isoformat(timespec="seconds")

    # --- Structural validation of inputs ---
    _validate_ohlcv_df(ohlcv_df)
    _validate_fundamentals_df(fundamentals_df)

    # --- Open database and ensure schema ---
    conn = _open_db(db_path)
    try:
        _ensure_agent_logs_table(conn)
        return _run_validation(
            ohlcv_df=ohlcv_df,
            fundamentals_df=fundamentals_df,
            trading_calendar=trading_calendar,
            conn=conn,
            checked_at_ist=checked_at_ist,
        )
    finally:
        conn.close()


def _run_validation(
    ohlcv_df: pd.DataFrame,
    fundamentals_df: pd.DataFrame,
    trading_calendar: list[datetime.date] | None,
    conn: sqlite3.Connection,
    checked_at_ist: str,
) -> DataQualityReport:
    """Execute all validation checks and construct the DataQualityReport.

    Args:
        ohlcv_df: Validated OHLCV DataFrame.
        fundamentals_df: Validated fundamentals DataFrame.
        trading_calendar: Optional NSE trading calendar.
        conn: Open SQLite connection with agent_logs table available.
        checked_at_ist: ISO 8601 IST timestamp captured at validate_data entry.

    Returns:
        DataQualityReport with all fields populated.

    Raises:
        DataQualityError: If universe_quality_score < 0.6.
        sqlite3.OperationalError: If database writes fail.
    """
    universe_size = len(fundamentals_df)

    # Handle empty universe immediately
    if universe_size == 0:
        empty_report = DataQualityReport(
            per_stock_scores={},
            universe_quality_score=0.0,
            failed_roe_symbols=[],
            roe_missing_symbols=[],
            de_coverage_ratio=0.0,
            de_coverage_low=True,
            gap_violations={},
            checked_at_ist=checked_at_ist,
            universe_size=0,
        )
        _log_event(
            conn,
            "universe_score",
            None,
            {"universe_quality_score": 0.0, "reason": "empty universe"},
            0.0,
        )
        _log_event(
            conn,
            "data_quality_error",
            None,
            {
                "universe_quality_score": 0.0,
                "reason": "fundamentals_df is empty — zero symbols",
            },
            0.0,
        )
        raise DataQualityError(0.0, empty_report)

    # Pre-index OHLCV by symbol for fast lookup
    ohlcv_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol_val, group in ohlcv_df.groupby("symbol"):
        ohlcv_by_symbol[str(symbol_val)] = group.sort_values("date")

    # --- Phase 1: ROE checks (per stock) ---
    failed_roe_symbols: list[str] = []
    roe_missing_symbols: list[str] = []
    roe_passed_map: dict[str, bool] = {}
    roe_missing_map: dict[str, bool] = {}

    for _, row in fundamentals_df.iterrows():
        symbol: str = str(row["symbol"])
        roe_raw = row["roe"]

        # Normalise: treat pandas NA/NaN uniformly
        roe_value: float | None
        if pd.isna(roe_raw):
            roe_value = None
        else:
            roe_value = float(roe_raw)

        passed, detail_str = _check_roe(symbol, roe_value)

        roe_passed_map[symbol] = passed
        roe_missing_map[symbol] = roe_value is None

        if roe_value is None:
            roe_missing_symbols.append(symbol)
        elif not passed:
            failed_roe_symbols.append(symbol)

        roe_detail: dict[str, object] = {
            "roe": roe_value,
            "passed": passed,
            "reason": detail_str,
        }
        _log_event(conn, "roe_check", symbol, roe_detail, None)

    # --- Phase 2: D/E coverage check (universe-level) ---
    de_present_count = sum(
        1
        for _, row in fundamentals_df.iterrows()
        if not pd.isna(row["debt_to_equity"])
    )
    de_coverage_ratio = de_present_count / universe_size
    de_coverage_low = de_coverage_ratio < DE_COVERAGE_THRESHOLD

    de_check_detail: dict[str, object] = {
        "de_coverage_ratio": round(de_coverage_ratio, 6),
        "present_count": de_present_count,
        "total_count": universe_size,
    }
    _log_event(conn, "de_coverage_check", None, de_check_detail, None)

    if de_coverage_low:
        missing_count = universe_size - de_present_count
        _log_event(
            conn,
            "data_coverage_low",
            None,
            {
                "de_coverage_ratio": round(de_coverage_ratio, 6),
                "missing_count": missing_count,
                "total_count": universe_size,
            },
            None,
        )

    # --- Phase 3: OHLCV gap checks (per stock) ---
    gap_violations_map: dict[str, list[tuple[str, int]]] = {}

    for _, row in fundamentals_df.iterrows():
        symbol = str(row["symbol"])

        if symbol in ohlcv_by_symbol:
            symbol_dates = ohlcv_by_symbol[symbol]["date"]
            gaps = _check_ohlcv_gaps(symbol, symbol_dates, trading_calendar)
        else:
            # Symbol present in fundamentals but absent from OHLCV
            gaps = []

        gap_violations_map[symbol] = gaps

        gap_detail: dict[str, object] = {
            "gap_count": len(gaps),
            "gaps": [[g[0], g[1]] for g in gaps],
        }
        if symbol not in ohlcv_by_symbol:
            gap_detail["note"] = "symbol absent from ohlcv_df — gap_score set to 0.0"

        _log_event(conn, "ohlcv_gap_check", symbol, gap_detail, None)

    # --- Phase 4: Per-stock scores ---
    per_stock_scores_raw: dict[str, float] = {}

    for _, row in fundamentals_df.iterrows():
        symbol = str(row["symbol"])

        roe_passed = roe_passed_map[symbol]
        roe_missing = roe_missing_map[symbol]
        gaps = gap_violations_map[symbol]

        roe_plausibility_score = 1.0 if roe_passed else 0.0
        roe_present_score = 0.0 if roe_missing else 1.0

        # If symbol was absent from OHLCV, force gap_score = 0.0 per spec
        if symbol not in ohlcv_by_symbol:
            gap_score_log = 0.0
            raw_score = (
                (roe_plausibility_score * 0.40)
                + (roe_present_score * 0.10)
                + (gap_score_log * 0.50)
            )
            stock_score = max(0.0, min(1.0, raw_score))
        else:
            gap_count = len(gaps)
            if gap_count == 0:
                gap_score_log = 1.0
            elif gap_count == 1:
                gap_score_log = 0.5
            else:
                gap_score_log = 0.0
            stock_score = _compute_stock_score(roe_passed, roe_missing, gaps)

        per_stock_scores_raw[symbol] = stock_score

        stock_score_detail: dict[str, object] = {
            "roe_plausibility_score": roe_plausibility_score,
            "roe_present_score": roe_present_score,
            "gap_score": gap_score_log,
            "de_deduction": 0.0,  # Will be updated if de_coverage_low
        }
        _log_event(conn, "stock_score", symbol, stock_score_detail, stock_score)

    # --- Phase 5: Apply D/E deduction if coverage is low ---
    per_stock_scores: dict[str, float]
    if de_coverage_low:
        per_stock_scores = {
            sym: max(0.0, score - 0.10)
            for sym, score in per_stock_scores_raw.items()
        }
        # Re-log corrected stock_score rows with de_deduction = 0.10
        for sym, corrected_score in per_stock_scores.items():
            roe_passed = roe_passed_map[sym]
            roe_missing = roe_missing_map[sym]
            gaps = gap_violations_map[sym]

            if sym not in ohlcv_by_symbol:
                gap_score_log = 0.0
            else:
                gap_count = len(gaps)
                if gap_count == 0:
                    gap_score_log = 1.0
                elif gap_count == 1:
                    gap_score_log = 0.5
                else:
                    gap_score_log = 0.0

            roe_plausibility_score = 1.0 if roe_passed else 0.0
            roe_present_score = 0.0 if roe_missing else 1.0

            corrected_detail: dict[str, object] = {
                "roe_plausibility_score": roe_plausibility_score,
                "roe_present_score": roe_present_score,
                "gap_score": gap_score_log,
                "de_deduction": 0.10,
            }
            _log_event(conn, "stock_score", sym, corrected_detail, corrected_score)
    else:
        per_stock_scores = per_stock_scores_raw

    # --- Phase 6: Universe quality score ---
    universe_quality_score = sum(per_stock_scores.values()) / len(per_stock_scores)

    _log_event(
        conn,
        "universe_score",
        None,
        {
            "universe_quality_score": round(universe_quality_score, 6),
            "universe_size": universe_size,
            "de_coverage_ratio": round(de_coverage_ratio, 6),
            "de_coverage_low": de_coverage_low,
        },
        universe_quality_score,
    )

    # --- Build final report ---
    report = DataQualityReport(
        per_stock_scores=per_stock_scores,
        universe_quality_score=universe_quality_score,
        failed_roe_symbols=failed_roe_symbols,
        roe_missing_symbols=roe_missing_symbols,
        de_coverage_ratio=de_coverage_ratio,
        de_coverage_low=de_coverage_low,
        gap_violations=gap_violations_map,
        checked_at_ist=checked_at_ist,
        universe_size=universe_size,
    )

    # --- Raise DataQualityError if score below threshold (after report is built) ---
    if universe_quality_score < UNIVERSE_QUALITY_THRESHOLD:
        _log_event(
            conn,
            "data_quality_error",
            None,
            {
                "universe_quality_score": round(universe_quality_score, 6),
                "threshold": UNIVERSE_QUALITY_THRESHOLD,
            },
            universe_quality_score,
        )
        raise DataQualityError(universe_quality_score, report)

    return report
