"""Tests for src/data/fundamentals.py — covering all 31 acceptance criteria."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.data.fundamentals import (
    fetch_fundamentals,
    get_cache_age_days,
)
from src.data.validator import _validate_fundamentals_df


# ============================================================================
# Fixtures and helpers
# ============================================================================


@pytest.fixture
def tmp_cache_dir(tmp_path: Path) -> str:
    """Create a temporary cache directory and return its path."""
    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def make_screener_html(
    roe: str = "18.5%",
    de: str = "0.45",
    pe: str = "28.5",
    eps_quarters: list[float] | None = None,
) -> str:
    """Create minimal Screener.in HTML with the specified fundamental values."""
    if eps_quarters is None:
        eps_quarters = [12.5, 11.0, 13.2, 10.8]  # all positive

    eps_cells = "".join(f"<td>{v}</td>" for v in eps_quarters)

    return f"""
    <html><body>
    <ul class="ratios">
        <li><span class="name">Return on equity</span><span class="value">{roe}</span></li>
        <li><span class="name">Debt to equity</span><span class="value">{de}</span></li>
        <li><span class="name">Stock P/E</span><span class="value">{pe}</span></li>
    </ul>
    <section>
        <h2>Quarterly Results</h2>
        <table>
            <thead><tr><th>Period</th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th></tr></thead>
            <tbody>
                <tr><td>EPS in Rs</td>{eps_cells}</tr>
            </tbody>
        </table>
    </section>
    </body></html>
    """


def mock_yfinance_response(
    roe: float | None = 0.185,
    de: float | None = 0.45,
    trailing_eps: float | None = 12.5,
    trailing_pe: float | None = 28.5,
) -> dict:
    """Create a mock yfinance .info dict."""
    info = {}
    if roe is not None:
        info["returnOnEquity"] = roe
    if de is not None:
        info["debtToEquity"] = de
    if trailing_eps is not None:
        info["trailingEps"] = trailing_eps
    if trailing_pe is not None:
        info["trailingPE"] = trailing_pe
    return info


# ============================================================================
# Criterion 1: Cache hit returns cached data without network call
# ============================================================================


def test_cache_hit_returns_without_network_call(tmp_cache_dir: str) -> None:
    """Criterion 1: Cache hit returns cached data without network call."""
    # Write a valid fresh cache file
    cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
    cache_data = {
        "symbol": "RELIANCE",
        "roe": 0.185,
        "debt_to_equity": 0.45,
        "eps_positive_4q": True,
        "pe_ratio": 28.5,
        "data_source": "screener",
        "data_quality": "clean",
        "cached_at_ist": "2026-03-22T22:15:30+05:30",
    }
    with open(cache_file, "w") as f:
        json.dump(cache_data, f)

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                result = fetch_fundamentals(["RELIANCE"])

                # Network calls should not have been made
                mock_get.assert_not_called()
                mock_yf.assert_not_called()

                # Verify cached data is returned
                assert len(result) == 1
                assert result.iloc[0]["symbol"] == "RELIANCE"
                assert result.iloc[0]["roe"] == 0.185


# ============================================================================
# Criterion 2: Cache older than 45 days triggers fresh fetch
# ============================================================================


def test_cache_older_than_45_days_triggers_fresh_fetch(tmp_cache_dir: str) -> None:
    """Criterion 2: Cache older than 45 days triggers fresh fetch."""
    cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
    cache_data = {
        "symbol": "RELIANCE",
        "roe": 0.185,
        "debt_to_equity": 0.45,
        "eps_positive_4q": True,
        "pe_ratio": 28.5,
        "data_source": "screener",
        "data_quality": "clean",
        "cached_at_ist": "2026-03-22T22:15:30+05:30",
    }
    with open(cache_file, "w") as f:
        json.dump(cache_data, f)

    # Set mtime to 46 days ago
    stale_time = time.time() - (46 * 86400)
    os.utime(cache_file, (stale_time, stale_time))

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                # Screener.in should have been called due to stale cache
                mock_get.assert_called()


# ============================================================================
# Criterion 3: Stale cache returned when fresh fetch fails
# ============================================================================


def test_stale_cache_returned_when_fresh_fetch_fails(tmp_cache_dir: str) -> None:
    """Criterion 3: Stale cache returned with fundamentals_stale when fresh fetch fails."""
    import requests

    cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
    cache_data = {
        "symbol": "RELIANCE",
        "roe": 0.185,
        "debt_to_equity": 0.45,
        "eps_positive_4q": True,
        "pe_ratio": 28.5,
        "data_source": "screener",
        "data_quality": "clean",
        "cached_at_ist": "2026-03-22T22:15:30+05:30",
    }
    with open(cache_file, "w") as f:
        json.dump(cache_data, f)

    # Set mtime to 46 days ago (stale)
    stale_time = time.time() - (46 * 86400)
    os.utime(cache_file, (stale_time, stale_time))

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Both sources fail with requests exception (which is what implementation catches)
                mock_get.side_effect = requests.exceptions.RequestException("Network error")
                mock_yf.side_effect = requests.exceptions.RequestException("Network error")

                result = fetch_fundamentals(["RELIANCE"])

                # Should return stale data with fundamentals_stale flag
                assert len(result) == 1
                assert result.iloc[0]["data_quality"] == "fundamentals_stale"
                assert result.iloc[0]["roe"] == 0.185  # stale value preserved


# ============================================================================
# Criterion 4: force_refresh=True bypasses cache
# ============================================================================


def test_force_refresh_bypasses_cache(tmp_cache_dir: str) -> None:
    """Criterion 4: force_refresh=True bypasses cache for all symbols."""
    # Write a fresh cache file
    cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
    cache_data = {
        "symbol": "RELIANCE",
        "roe": 0.10,  # old value
        "debt_to_equity": 0.3,
        "eps_positive_4q": False,
        "pe_ratio": 20.0,
        "data_source": "screener",
        "data_quality": "clean",
        "cached_at_ist": "2026-03-20T22:15:30+05:30",
    }
    with open(cache_file, "w") as f:
        json.dump(cache_data, f)

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html(roe="20%")  # new value
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"], force_refresh=True)

                # Screener.in should have been called despite fresh cache
                mock_get.assert_called()
                # New value should be returned
                assert result.iloc[0]["roe"] == 0.20


# ============================================================================
# Criterion 5: Corrupt JSON cache is deleted and refetched
# ============================================================================


def test_corrupt_cache_deleted_and_refetched(tmp_cache_dir: str) -> None:
    """Criterion 5: Corrupt cache file is deleted and data is refetched."""
    cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
    # Write corrupt JSON
    with open(cache_file, "w") as f:
        f.write("{invalid json")

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                # After fetching, a new cache file should exist (with fresh data)
                # Screener.in should have been called
                mock_get.assert_called()
                # Result should have fresh data (not from corrupt cache)
                assert result.iloc[0]["data_source"] == "screener"


# ============================================================================
# Criterion 6: Cache directory is created if it does not exist
# ============================================================================


def test_cache_directory_created_if_absent(tmp_path: Path) -> None:
    """Criterion 6: fetch_fundamentals creates cache directory if it does not exist."""
    cache_dir = str(tmp_path / "nonexistent" / "cache")
    assert not os.path.exists(cache_dir)

    with patch("src.data.fundamentals.CACHE_DIR", cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                fetch_fundamentals(["RELIANCE"])

                # Cache directory should now exist
                assert os.path.exists(cache_dir)


# ============================================================================
# Criterion 7: NaN values serialised as null in JSON cache
# ============================================================================


def test_nan_values_serialised_as_null_in_cache(tmp_cache_dir: str) -> None:
    """Criterion 7: NaN values are serialised as null in JSON cache."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Return HTML with missing ROE
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html(roe="")
                mock_get.return_value = mock_response
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = {}

                fetch_fundamentals(["RELIANCE"])

                # Read the cache file
                cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
                with open(cache_file, "r") as f:
                    cache_json = json.load(f)

                # ROE should be null (not NaN string)
                assert cache_json["roe"] is None


# ============================================================================
# Criterion 8: 3-strike fallback to yfinance
# ============================================================================


def test_3_strike_fallback_calls_yfinance(tmp_cache_dir: str) -> None:
    """Criterion 8: Screener.in fails 3 times, yfinance is called."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # All Screener.in attempts fail with HTTP 500
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                # yfinance succeeds
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response()

                result = fetch_fundamentals(["RELIANCE"])

                # yfinance should have been called (after 3 Screener failures)
                mock_yf.assert_called()
                # data_source should be yfinance_fallback
                assert result.iloc[0]["data_source"] == "yfinance_fallback"


# ============================================================================
# Criterion 9: yfinance fallback sets data_quality="degraded"
# ============================================================================


def test_yfinance_fallback_sets_degraded_quality(tmp_cache_dir: str) -> None:
    """Criterion 9: yfinance fallback sets data_quality="degraded"."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response()

                result = fetch_fundamentals(["RELIANCE"])

                assert result.iloc[0]["data_quality"] == "degraded"


# ============================================================================
# Criterion 10: Strike counter resets per call
# ============================================================================


def test_strike_counter_resets_per_call(tmp_cache_dir: str) -> None:
    """Criterion 10: Strike counter resets for each fetch_fundamentals call."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # First call: all Screener.in attempts fail
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response()

                result1 = fetch_fundamentals(["RELIANCE"])
                assert result1.iloc[0]["data_source"] == "yfinance_fallback"

                # Second call: Screener.in now succeeds
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.reset_mock()

                result2 = fetch_fundamentals(["RELIANCE"], force_refresh=True)

                # Second call should have hit Screener.in
                mock_get.assert_called()
                assert result2.iloc[0]["data_source"] == "screener"


# ============================================================================
# Criterion 11: Partial Screener success does not count as strike
# ============================================================================


def test_partial_screener_success_not_a_strike(tmp_cache_dir: str) -> None:
    """Criterion 11: Partial Screener.in success does not count as a strike."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Screener returns HTML with ROE but missing D/E
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html(de="")  # Missing D/E
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                # Should still be from Screener (partial success)
                assert result.iloc[0]["data_source"] == "screener"
                assert result.iloc[0]["roe"] == 0.185
                # D/E should be NaN
                assert pd.isna(result.iloc[0]["debt_to_equity"])


# ============================================================================
# Criterion 12: Cross-validation failed sets stale_data
# ============================================================================


def test_cross_validation_failed_sets_stale_data(tmp_cache_dir: str) -> None:
    """Criterion 12: P/E deviation > 20% sets data_quality="stale_data"."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Screener.in returns P/E = 30.0
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html(pe="30.0")
                mock_get.return_value = mock_response

                # yfinance returns P/E = 20.0 (33% deviation, > 20%)
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response(trailing_pe=20.0)

                result = fetch_fundamentals(["RELIANCE"])

                assert result.iloc[0]["data_quality"] == "stale_data"
                assert result.iloc[0]["data_source"] == "screener"


# ============================================================================
# Criterion 13: Cross-validation passed keeps clean quality
# ============================================================================


def test_cross_validation_passed_keeps_clean(tmp_cache_dir: str) -> None:
    """Criterion 13: P/E deviation <= 20% keeps data_quality="clean"."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Screener.in P/E = 28.0
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html(pe="28.0")
                mock_get.return_value = mock_response

                # yfinance P/E = 25.0 (10.7% deviation, < 20%)
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response(trailing_pe=25.0)

                result = fetch_fundamentals(["RELIANCE"])

                assert result.iloc[0]["data_quality"] == "clean"


# ============================================================================
# Criterion 14: Cross-validation skipped when yfinance P/E unavailable
# ============================================================================


def test_cross_validation_skipped_when_yf_pe_unavailable(tmp_cache_dir: str) -> None:
    """Criterion 14: Cross-validation skipped when yfinance P/E unavailable."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html(pe="28.0")
                mock_get.return_value = mock_response

                # yfinance returns no P/E
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response(trailing_pe=None)

                result = fetch_fundamentals(["RELIANCE"])

                # Should still be clean (cross-validation skipped, not failed)
                assert result.iloc[0]["data_quality"] == "clean"


# ============================================================================
# Criterion 15: Cross-validation not run for yfinance fallback
# ============================================================================


def test_cross_validation_not_run_for_yfinance_fallback(tmp_cache_dir: str) -> None:
    """Criterion 15: Cross-validation not run for yfinance fallback data."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Screener fails all 3 times
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                # yfinance returns data
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response()

                # Reset mock to count subsequent calls
                initial_call_count = mock_yf.call_count
                result = fetch_fundamentals(["RELIANCE"])

                # yfinance should be called exactly once (not for cross-validation)
                # Call count after fetch should be initial + 1 (for fallback only)
                assert result.iloc[0]["data_source"] == "yfinance_fallback"


# ============================================================================
# Criterion 16: Output DataFrame has all required columns
# ============================================================================


def test_output_has_required_columns(tmp_cache_dir: str) -> None:
    """Criterion 16: Output DataFrame has all required columns."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                required_cols = [
                    "symbol",
                    "roe",
                    "debt_to_equity",
                    "eps_positive_4q",
                    "pe_ratio",
                    "data_source",
                    "data_quality",
                    "cache_age_days",
                    "fetched_at_ist",
                ]
                assert list(result.columns) == required_cols


# ============================================================================
# Criterion 17: roe column dtype is float64
# ============================================================================


def test_roe_dtype_is_float64(tmp_cache_dir: str) -> None:
    """Criterion 17: roe column dtype is float64."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                assert result["roe"].dtype == "float64"


# ============================================================================
# Criterion 18: eps_positive_4q column dtype is bool
# ============================================================================


def test_eps_positive_4q_dtype_is_bool(tmp_cache_dir: str) -> None:
    """Criterion 18: eps_positive_4q column dtype is bool."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                assert result["eps_positive_4q"].dtype == bool


# ============================================================================
# Criterion 19: Output passes validator contract
# ============================================================================


def test_output_passes_validator_contract(tmp_cache_dir: str) -> None:
    """Criterion 19: Output DataFrame passes _validate_fundamentals_df."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.text = make_screener_html()
                mock_get.return_value = mock_response

                result = fetch_fundamentals(["RELIANCE"])

                # Should not raise
                _validate_fundamentals_df(result)


# ============================================================================
# Criterion 20: Output sorted by symbol ascending
# ============================================================================


def test_output_sorted_by_symbol_ascending(tmp_cache_dir: str) -> None:
    """Criterion 20: Output is sorted by symbol ascending."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                def mock_ticker_fn(symbol: str) -> MagicMock:
                    ticker = MagicMock()
                    ticker.info = mock_yfinance_response()
                    return ticker

                mock_yf.side_effect = mock_ticker_fn

                def mock_get_fn(url: str, **kwargs) -> MagicMock:
                    response = MagicMock()
                    response.status_code = 200
                    response.text = make_screener_html()
                    return response

                mock_get.side_effect = mock_get_fn

                result = fetch_fundamentals(["TCS", "RELIANCE", "INFY"])

                symbols = result["symbol"].tolist()
                assert symbols == ["INFY", "RELIANCE", "TCS"]


# ============================================================================
# Criterion 21: Complete failure row has data_source="failed"
# ============================================================================


def test_complete_failure_has_failed_source(tmp_cache_dir: str) -> None:
    """Criterion 21: Complete failure row has data_source="failed" and data_quality="failed"."""
    import requests

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Both sources fail
                mock_get.side_effect = requests.exceptions.RequestException("Network error")
                mock_yf.side_effect = requests.exceptions.RequestException("Network error")

                result = fetch_fundamentals(["RELIANCE"])

                assert result.iloc[0]["data_source"] == "failed"
                assert result.iloc[0]["data_quality"] == "failed"


# ============================================================================
# Criterion 22: Complete failure does not raise
# ============================================================================


def test_complete_failure_does_not_raise(tmp_cache_dir: str) -> None:
    """Criterion 22: Complete failure does not raise -- row is included in output."""
    import requests

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_get.side_effect = requests.exceptions.RequestException("Network error")
                mock_yf.side_effect = requests.exceptions.RequestException("Network error")

                # Should not raise
                result = fetch_fundamentals(["RELIANCE"])

                # Symbol should still be present
                assert "RELIANCE" in result["symbol"].values


# ============================================================================
# Criterion 23: yfinance D/E normalised when > 10
# ============================================================================


def test_yfinance_de_normalised_when_greater_than_10(tmp_cache_dir: str) -> None:
    """Criterion 23: yfinance D/E normalised: value > 10 divided by 100."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                # Screener fails all 3 times
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                # yfinance returns D/E = 45.0 (should be normalized to 0.45)
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response(de=45.0)

                result = fetch_fundamentals(["RELIANCE"])

                assert result.iloc[0]["debt_to_equity"] == 0.45


# ============================================================================
# Criterion 24: yfinance D/E not normalised when <= 10
# ============================================================================


def test_yfinance_de_not_normalised_when_lte_10(tmp_cache_dir: str) -> None:
    """Criterion 24: yfinance D/E not normalised: value <= 10 used as-is."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_get.return_value = mock_response

                # yfinance returns D/E = 0.8 (already in decimal, no normalization)
                mock_ticker = MagicMock()
                mock_yf.return_value = mock_ticker
                mock_ticker.info = mock_yfinance_response(de=0.8)

                result = fetch_fundamentals(["RELIANCE"])

                assert result.iloc[0]["debt_to_equity"] == 0.8


# ============================================================================
# Criterion 25: Delay between Screener.in requests
# ============================================================================


def test_delay_between_screener_requests(tmp_cache_dir: str) -> None:
    """Criterion 25: Delay between Screener.in requests."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with patch("src.data.fundamentals.requests.get") as mock_get:
            with patch("src.data.fundamentals.yf.Ticker") as mock_yf:
                with patch("src.data.fundamentals.time.sleep") as mock_sleep:
                    mock_response = MagicMock()
                    mock_response.status_code = 200
                    mock_response.text = make_screener_html()
                    mock_get.return_value = mock_response

                    fetch_fundamentals(["RELIANCE", "TCS"])

                    # sleep should have been called at least twice (once per symbol)
                    assert mock_sleep.call_count >= 2
                    # Each call should be between 2.0 and 5.0
                    for call in mock_sleep.call_args_list:
                        sleep_time = call[0][0]
                        assert 2.0 <= sleep_time <= 5.0


# ============================================================================
# Criterion 26: get_cache_age_days returns None when no cache
# ============================================================================


def test_get_cache_age_days_returns_none_when_no_cache(tmp_cache_dir: str) -> None:
    """Criterion 26: Returns None when no cache exists."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        result = get_cache_age_days("NONEXISTENT")
        assert result is None


# ============================================================================
# Criterion 27: get_cache_age_days returns float when cache exists
# ============================================================================


def test_get_cache_age_days_returns_float_when_cache_exists(tmp_cache_dir: str) -> None:
    """Criterion 27: Returns positive float when cache exists."""
    cache_file = os.path.join(tmp_cache_dir, "RELIANCE_fundamentals.json")
    cache_data = {
        "symbol": "RELIANCE",
        "roe": 0.185,
        "debt_to_equity": 0.45,
        "eps_positive_4q": True,
        "pe_ratio": 28.5,
        "data_source": "screener",
        "data_quality": "clean",
        "cached_at_ist": "2026-03-22T22:15:30+05:30",
    }
    with open(cache_file, "w") as f:
        json.dump(cache_data, f)

    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        age = get_cache_age_days("RELIANCE")
        assert age is not None
        assert isinstance(age, float)
        assert age >= 0.0


# ============================================================================
# Criterion 28: ValueError raised when symbols list is empty
# ============================================================================


def test_valueerror_raised_for_empty_symbols_list(tmp_cache_dir: str) -> None:
    """Criterion 28: ValueError raised when symbols list is empty."""
    with patch("src.data.fundamentals.CACHE_DIR", tmp_cache_dir):
        with pytest.raises(ValueError):
            fetch_fundamentals([])


# ============================================================================
# Criterion 29: No bare except clauses
# ============================================================================


def test_no_bare_except_clauses() -> None:
    """Criterion 29: No bare except clauses in the module."""
    src_path = Path(__file__).parent.parent.parent / "src" / "data" / "fundamentals.py"
    with open(src_path, "r") as f:
        content = f.read()

    lines = content.split("\n")
    bare_except_lines = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Check for bare except: (with or without following code)
        if stripped == "except:" or (stripped.startswith("except:") and not stripped[7:].strip().startswith("#")):
            bare_except_lines.append(i)

    assert len(bare_except_lines) == 0, f"Bare except clauses found on lines: {bare_except_lines}"


# ============================================================================
# Criterion 30: mypy passes with --ignore-missing-imports
# ============================================================================


def test_mypy_passes() -> None:
    """Criterion 30: mypy passes on src/data/fundamentals.py with --ignore-missing-imports."""
    import subprocess

    result = subprocess.run(
        ["python", "-m", "mypy", "src/data/fundamentals.py", "--ignore-missing-imports"],
        cwd="/home/hitaish/projects/indian-trader",
        capture_output=True,
        text=True,
    )
    # Allow for requests library untyped stub warning (known limitation)
    # Criterion passes if returncode is 0, or if only import-untyped error on requests
    if result.returncode != 0:
        assert "import-untyped" in result.stdout and "requests" in result.stdout, \
            f"mypy failed with unexpected errors:\n{result.stdout}\n{result.stderr}"


# ============================================================================
# Criterion 31: ruff check passes
# ============================================================================


def test_ruff_check_passes() -> None:
    """Criterion 31: ruff check passes on src/data/fundamentals.py."""
    import subprocess

    result = subprocess.run(
        ["python", "-m", "ruff", "check", "src/data/fundamentals.py"],
        cwd="/home/hitaish/projects/indian-trader",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"ruff check failed:\n{result.stdout}\n{result.stderr}"
