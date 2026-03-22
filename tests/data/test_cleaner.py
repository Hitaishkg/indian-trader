"""Tests for src/data/cleaner.py — covering all 29 acceptance criteria."""

import dataclasses
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.data.cleaner import clean_ohlcv, CleaningReport
from src.data.validator import _validate_ohlcv_df


# ============================================================================
# Helper function to build OHLCV DataFrames for testing
# ============================================================================


IST = ZoneInfo("Asia/Kolkata")


def make_ohlcv(symbols_data: dict) -> pd.DataFrame:
    """Build a test OHLCV DataFrame from a symbols_data dict.

    Args:
        symbols_data: {symbol: [(date_str, open, high, low, close, volume), ...]}

    Returns:
        DataFrame with columns [symbol, date, open, high, low, close, volume]
        sorted by (symbol, date) ascending.
    """
    rows = []
    for symbol, entries in symbols_data.items():
        for date_str, o, h, l, c, v in entries:
            rows.append({
                "symbol": symbol,
                "date": pd.Timestamp(date_str, tz=IST),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            })
    df = pd.DataFrame(rows)
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


# ============================================================================
# Criterion 1: Missing column raises ValueError
# ============================================================================


def test_criterion_1_missing_column_raises_valueerror() -> None:
    """Criterion 1: Missing column (e.g., close) raises ValueError."""
    df = pd.DataFrame({
        "symbol": ["RELIANCE"],
        "date": pd.date_range("2024-01-01", periods=1, tz=IST),
        "open": [100.0],
        "high": [105.0],
        "low": [98.0],
        # Missing "close"
        "volume": [1000000.0],
    })

    with pytest.raises(ValueError) as exc_info:
        clean_ohlcv(df)

    assert "close" in str(exc_info.value).lower()


# ============================================================================
# Criterion 2: Multiple missing columns listed in error
# ============================================================================


def test_criterion_2_multiple_missing_columns() -> None:
    """Criterion 2: Multiple missing columns (open, volume) listed in error."""
    df = pd.DataFrame({
        "symbol": ["RELIANCE"],
        "date": pd.date_range("2024-01-01", periods=1, tz=IST),
        # Missing "open" and "volume"
        "high": [105.0],
        "low": [98.0],
        "close": [103.0],
    })

    with pytest.raises(ValueError) as exc_info:
        clean_ohlcv(df)

    error_msg = str(exc_info.value).lower()
    assert "open" in error_msg
    assert "volume" in error_msg


# ============================================================================
# Criterion 3: Timezone-naive dates raise ValueError
# ============================================================================


def test_criterion_3_timezone_naive_dates() -> None:
    """Criterion 3: Timezone-naive dates raise ValueError."""
    df = pd.DataFrame({
        "symbol": ["RELIANCE"],
        "date": pd.date_range("2024-01-01", periods=1),  # No timezone
        "open": [100.0],
        "high": [105.0],
        "low": [98.0],
        "close": [103.0],
        "volume": [1000000.0],
    })

    with pytest.raises(ValueError) as exc_info:
        clean_ohlcv(df)

    assert "timezone" in str(exc_info.value).lower()


# ============================================================================
# Criterion 4: Valid schema passes without error
# ============================================================================


def test_criterion_4_valid_schema_passes() -> None:
    """Criterion 4: Valid schema passes without error."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert len(cleaned_df) == 1


# ============================================================================
# Criterion 5: Negative close price is flagged
# ============================================================================


def test_criterion_5_negative_close_flagged() -> None:
    """Criterion 5: Negative close price is flagged."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, -5.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert ("RELIANCE", "2024-01-01") in report.negative_price_flags


# ============================================================================
# Criterion 6: Zero open price is flagged
# ============================================================================


def test_criterion_6_zero_open_flagged() -> None:
    """Criterion 6: Zero open price is flagged."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 0.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert ("RELIANCE", "2024-01-01") in report.negative_price_flags


# ============================================================================
# Criterion 7: Negative price rows are NOT dropped
# ============================================================================


def test_criterion_7_negative_rows_not_dropped() -> None:
    """Criterion 7: Rows with negative prices are NOT dropped."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, -5.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert len(cleaned_df) == 1
    assert cleaned_df.iloc[0]["close"] == -5.0


# ============================================================================
# Criterion 8: Row where high < low is flagged
# ============================================================================


def test_criterion_8_consistency_high_less_than_low() -> None:
    """Criterion 8: Row where high < low is flagged."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 100.0, 200.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert ("RELIANCE", "2024-01-01") in report.consistency_flags


# ============================================================================
# Criterion 9: Consistency-flagged rows are NOT dropped
# ============================================================================


def test_criterion_9_consistency_rows_not_dropped() -> None:
    """Criterion 9: Rows with high < low are NOT dropped."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 100.0, 200.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert len(cleaned_df) == 1


# ============================================================================
# Criterion 10: Forward-fill of close works within a symbol group
# ============================================================================


def test_criterion_10_ffill_close_within_symbol() -> None:
    """Criterion 10: Forward-fill of close works within a symbol group."""
    df = make_ohlcv({
        "A": [
            ("2024-01-01", 100.0, 105.0, 98.0, 100.0, 1000000.0),
            ("2024-01-02", 100.0, 105.0, 98.0, float('nan'), 1000000.0),
            ("2024-01-03", 100.0, 105.0, 98.0, 110.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert cleaned_df.iloc[1]["close"] == 100.0
    assert report.missing_close_filled == 1


# ============================================================================
# Criterion 11: Forward-fill does NOT bleed across symbols
# ============================================================================


def test_criterion_11_ffill_no_cross_symbol_bleed() -> None:
    """Criterion 11: Forward-fill does NOT bleed across symbols."""
    df = make_ohlcv({
        "A": [
            ("2024-01-01", 100.0, 105.0, 98.0, 100.0, 1000000.0),
            ("2024-01-02", 100.0, 105.0, 98.0, float('nan'), 1000000.0),
        ],
        "B": [
            ("2024-01-01", 200.0, 205.0, 198.0, float('nan'), 2000000.0),
            ("2024-01-02", 200.0, 205.0, 198.0, 210.0, 2000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)

    # A day2 should be filled with A day1 close
    a_day2 = cleaned_df[(cleaned_df["symbol"] == "A") & (cleaned_df["date"] == pd.Timestamp("2024-01-02", tz=IST))]
    assert a_day2.iloc[0]["close"] == 100.0

    # B day1 should remain NaN (no prior data to fill from)
    b_day1 = cleaned_df[(cleaned_df["symbol"] == "B") & (cleaned_df["date"] == pd.Timestamp("2024-01-01", tz=IST))]
    assert pd.isna(b_day1.iloc[0]["close"])


# ============================================================================
# Criterion 12: Missing open/high/low filled with close value
# ============================================================================


def test_criterion_12_missing_ohlv_filled_from_close() -> None:
    """Criterion 12: Missing open/high/low filled from close value."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", float('nan'), float('nan'), float('nan'), 100.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert cleaned_df.iloc[0]["open"] == 100.0
    assert cleaned_df.iloc[0]["high"] == 100.0
    assert cleaned_df.iloc[0]["low"] == 100.0
    assert report.missing_ohlv_filled >= 3


# ============================================================================
# Criterion 13: Missing volume filled with 0
# ============================================================================


def test_criterion_13_missing_volume_filled_with_zero() -> None:
    """Criterion 13: Missing volume filled with 0."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, float('nan')),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert cleaned_df.iloc[0]["volume"] == 0.0


# ============================================================================
# Criterion 14: Duplicate dates within symbol: last occurrence kept
# ============================================================================


def test_criterion_14_duplicate_dates_last_kept() -> None:
    """Criterion 14: Duplicate dates within symbol — last occurrence kept."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 100.0, 1000000.0),
            ("2024-01-01", 100.0, 105.0, 98.0, 110.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert len(cleaned_df) == 1
    assert cleaned_df.iloc[0]["close"] == 110.0


# ============================================================================
# Criterion 15: Duplicate count recorded in CleaningReport
# ============================================================================


def test_criterion_15_duplicate_count_recorded() -> None:
    """Criterion 15: Duplicate count recorded in CleaningReport."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 100.0, 1000000.0),
            ("2024-01-01", 100.0, 105.0, 98.0, 110.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert report.duplicates_removed == 1


# ============================================================================
# Criterion 16: Duplicates across different symbols are NOT removed
# ============================================================================


def test_criterion_16_duplicates_cross_symbol_not_removed() -> None:
    """Criterion 16: Same date across different symbols is normal — both kept."""
    df = make_ohlcv({
        "A": [
            ("2024-01-01", 100.0, 105.0, 98.0, 100.0, 1000000.0),
        ],
        "B": [
            ("2024-01-01", 200.0, 205.0, 198.0, 210.0, 2000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert len(cleaned_df) == 2
    assert report.duplicates_removed == 0


# ============================================================================
# Criterion 17: Close below price floor is flagged
# ============================================================================


def test_criterion_17_close_below_floor_flagged() -> None:
    """Criterion 17: Close below price floor is flagged."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 0.5, 1.0, 0.4, 0.5, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert ("RELIANCE", "2024-01-01") in report.price_floor_flags


# ============================================================================
# Criterion 18: Custom price floor is respected
# ============================================================================


def test_criterion_18_custom_price_floor() -> None:
    """Criterion 18: Custom price_floor parameter is respected."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 30.0, 35.0, 28.0, 30.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df, price_floor=50.0)
    assert ("RELIANCE", "2024-01-01") in report.price_floor_flags


# ============================================================================
# Criterion 19: Output columns identical to input columns
# ============================================================================


def test_criterion_19_output_columns_identical() -> None:
    """Criterion 19: Output columns identical to input columns."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert list(cleaned_df.columns) == list(df.columns)


# ============================================================================
# Criterion 20: Output dtypes identical to input dtypes
# ============================================================================


def test_criterion_20_output_dtypes_identical() -> None:
    """Criterion 20: Output dtypes identical to input dtypes."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    for col in df.columns:
        assert cleaned_df[col].dtype == df[col].dtype, f"dtype mismatch for {col}"


# ============================================================================
# Criterion 21: Output sort order preserved (symbol, date) ascending
# ============================================================================


def test_criterion_21_output_sort_order_preserved() -> None:
    """Criterion 21: Output is sorted by (symbol, date) ascending."""
    df = make_ohlcv({
        "B": [
            ("2024-01-03", 100.0, 105.0, 98.0, 103.0, 1000000.0),
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ],
        "A": [
            ("2024-01-02", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)

    # Check that symbols are in order (A before B)
    symbols = cleaned_df["symbol"].unique().tolist()
    assert symbols == sorted(symbols)

    # Check that dates are in order within each symbol
    for symbol in symbols:
        symbol_data = cleaned_df[cleaned_df["symbol"] == symbol]
        dates = symbol_data["date"].tolist()
        assert dates == sorted(dates)


# ============================================================================
# Criterion 22: rows_output == rows_input - duplicates_removed
# ============================================================================


def test_criterion_22_rows_output_arithmetic() -> None:
    """Criterion 22: rows_output == rows_input - duplicates_removed."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 100.0, 1000000.0),
            ("2024-01-01", 100.0, 105.0, 98.0, 110.0, 1000000.0),
            ("2024-01-02", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    expected_output = report.rows_input - report.duplicates_removed
    assert report.rows_output == expected_output


# ============================================================================
# Criterion 23: CleaningReport is frozen
# ============================================================================


def test_criterion_23_cleaning_report_frozen() -> None:
    """Criterion 23: CleaningReport is frozen (cannot mutate after construction)."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)

    with pytest.raises(dataclasses.FrozenInstanceError):
        report.rows_input = 999  # type: ignore


# ============================================================================
# Criterion 24: cleaned_at_ist contains IST timezone offset
# ============================================================================


def test_criterion_24_cleaned_at_ist_timezone() -> None:
    """Criterion 24: cleaned_at_ist field contains IST timezone (+05:30)."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    assert "+05:30" in report.cleaned_at_ist


# ============================================================================
# Criterion 25: symbols_processed contains all input symbols
# ============================================================================


def test_criterion_25_symbols_processed() -> None:
    """Criterion 25: symbols_processed contains all input symbols."""
    df = make_ohlcv({
        "A": [("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0)],
        "B": [("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0)],
        "C": [("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0)],
    })

    cleaned_df, report = clean_ohlcv(df)
    assert len(report.symbols_processed) == 3
    assert set(report.symbols_processed) == {"A", "B", "C"}


# ============================================================================
# Criterion 26: Input DataFrame is not mutated
# ============================================================================


def test_criterion_26_input_not_mutated() -> None:
    """Criterion 26: Input DataFrame is not mutated by clean_ohlcv."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, float('nan'), 1000000.0),
        ]
    })

    df_copy = df.copy()
    cleaned_df, report = clean_ohlcv(df)

    pd.testing.assert_frame_equal(df, df_copy)


# ============================================================================
# Criterion 27: mypy passes with --ignore-missing-imports
# ============================================================================


def test_criterion_27_mypy_passes() -> None:
    """Criterion 27: mypy passes on src/data/cleaner.py."""
    import subprocess
    result = subprocess.run(
        ["python", "-m", "mypy", "src/data/cleaner.py", "--ignore-missing-imports"],
        cwd="/home/hitaish/projects/indian-trader",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"mypy failed:\n{result.stdout}\n{result.stderr}"


# ============================================================================
# Criterion 28: ruff check passes
# ============================================================================


def test_criterion_28_ruff_check_passes() -> None:
    """Criterion 28: ruff check passes on src/data/cleaner.py."""
    import subprocess
    result = subprocess.run(
        ["python", "-m", "ruff", "check", "src/data/cleaner.py"],
        cwd="/home/hitaish/projects/indian-trader",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"ruff check failed:\n{result.stdout}\n{result.stderr}"


# ============================================================================
# Criterion 29: No bare except clauses
# ============================================================================


def test_criterion_29_no_bare_except() -> None:
    """Criterion 29: No bare except: clauses in the source file."""
    src_path = Path(__file__).parent.parent.parent / "src" / "data" / "cleaner.py"
    with open(src_path, "r") as f:
        content = f.read()

    lines = content.split("\n")
    bare_except_lines = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped == "except:" or (stripped.startswith("except:") and not stripped.startswith("except Exception")):
            bare_except_lines.append(i)

    assert len(bare_except_lines) == 0, f"Bare except clauses found on lines: {bare_except_lines}"


# ============================================================================
# Additional integration test: Output passes validator contract
# ============================================================================


def test_output_passes_validator_contract() -> None:
    """Output from clean_ohlcv passes validator._validate_ohlcv_df check."""
    df = make_ohlcv({
        "RELIANCE": [
            ("2024-01-01", 100.0, 105.0, 98.0, 103.0, 1000000.0),
            ("2024-01-02", 101.0, 106.0, 99.0, 104.0, 1100000.0),
        ]
    })

    cleaned_df, report = clean_ohlcv(df)
    # Should not raise
    _validate_ohlcv_df(cleaned_df)
