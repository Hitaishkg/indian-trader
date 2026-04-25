"""OHLCV data acquisition layer for the Indian Trader pipeline.

Fetches historical and recent OHLCV price data for NSE-listed stocks and sector
indices, caches results as CSV files to avoid redundant network calls, and returns
normalised DataFrames that conform exactly to the contract defined in
src/data/validator.py Section 5.1.

The output of fetch_ohlcv() can be passed directly to validate_data() without
any additional transformation.
"""

from __future__ import annotations

import datetime
import logging
import os
import time

import pandas as pd
import requests
import yfinance as yf
from jugaad_data.nse import stock_df

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

# Absolute path to data/cache/ derived from this file's location.
CACHE_DIR: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "cache",
)

# Updated: 2026-03-22. Nifty 50 constituents change quarterly -- update manually.
NIFTY50_SYMBOLS: list[str] = [
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJAJFINSV",
    "BAJFINANCE",
    "BEL",
    "BHARTIARTL",
    "BPCL",
    "BRITANNIA",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "ETERNAL",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "INDUSINDBK",
    "INFY",
    "ITC",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TCS",
    "TECHM",
    "TITAN",
    "ULTRACEMCO",
    "WIPRO",
]

SECTOR_INDEX_MAP: dict[str, str] = {
    "NIFTY_50": "^NSEI",
    "NIFTY_BANK": "^NSEBANK",
    "NIFTY_IT": "^CNXIT",
    "NIFTY_AUTO": "^CNXAUTO",
    "NIFTY_PHARMA": "^CNXPHARMA",
    "NIFTY_FMCG": "^CNXFMCG",
}


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Raised when OHLCV data cannot be fetched from any source.

    Attributes:
        symbol: The NSE ticker symbol that failed.
        yfinance_error: Description of the yfinance failure, or None if
                        yfinance was not attempted.
        jugaad_error: Description of the jugaad-data failure, or None if
                      jugaad-data was not attempted.
    """

    def __init__(
        self,
        symbol: str,
        yfinance_error: str | None = None,
        jugaad_error: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.yfinance_error = yfinance_error
        self.jugaad_error = jugaad_error
        parts = [f"Failed to fetch OHLCV for {symbol}."]
        if yfinance_error:
            parts.append(f"yfinance error: {yfinance_error}.")
        if jugaad_error:
            parts.append(f"jugaad-data error: {jugaad_error}.")
        super().__init__(" ".join(parts))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _cache_path(symbol: str, source: str) -> str:
    """Return the absolute file path for a cache CSV file.

    Args:
        symbol: NSE ticker symbol or yfinance index ticker.
        source: One of "yfinance" or "jugaad".

    Returns:
        Absolute path string, e.g. "/path/to/data/cache/RELIANCE_yfinance.csv".
    """
    return os.path.join(CACHE_DIR, f"{symbol}_{source}.csv")


def _read_cache(
    symbol: str,
    source: str,
    cache_expiry_hours: int,
) -> pd.DataFrame | None:
    """Read cached OHLCV data for a symbol from a specific source.

    Args:
        symbol: NSE ticker symbol or yfinance index ticker.
        source: One of "yfinance" or "jugaad".
        cache_expiry_hours: Maximum age in hours for the cache to be considered
                            fresh. 0 means always bypass cache.

    Returns:
        Normalised DataFrame if cache is fresh and valid, None otherwise.
    """
    path = _cache_path(symbol, source)

    if not os.path.exists(path):
        return None

    if cache_expiry_hours == 0:
        return None

    age_seconds = time.time() - os.path.getmtime(path)
    cache_is_fresh = age_seconds < (cache_expiry_hours * 3600)

    if not cache_is_fresh:
        logger.info("Cache expired for %s from %s, refetching", symbol, source)
        return None

    try:
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert("Asia/Kolkata")
        df["volume"] = df["volume"].astype("float64")
        logger.info("Cache hit for %s from %s", symbol, source)
        return df
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        logger.warning("Corrupt cache file %s, deleting and refetching", path)
        try:
            os.remove(path)
        except OSError:
            pass
        _ = exc
        return None


def _write_cache(df: pd.DataFrame, symbol: str, source: str) -> None:
    """Write normalised OHLCV data to the cache CSV file.

    Creates the cache directory if it does not exist.

    Args:
        df: Normalised DataFrame to cache.
        symbol: NSE ticker symbol or yfinance index ticker.
        source: One of "yfinance" or "jugaad".
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, source)
    df.to_csv(path, index=False)


def _fetch_yfinance(
    symbol: str,
    start_date: datetime.date,
    end_date: datetime.date,
) -> pd.DataFrame:
    """Fetch OHLCV from yfinance for a single NSE symbol.

    Appends '.NS' to the symbol before calling yfinance.
    Returns a normalised DataFrame matching the Section 5.1 contract.

    Args:
        symbol: NSE ticker symbol without .NS suffix.
        start_date: Start date (inclusive).
        end_date: End date (inclusive).

    Returns:
        Normalised DataFrame.

    Raises:
        ValueError: If the returned DataFrame is empty or close is all NaN.
        requests.exceptions.RequestException: On network failure.
    """
    yf_end = end_date + datetime.timedelta(days=1)
    ticker = yf.Ticker(f"{symbol}.NS")
    raw_df = ticker.history(start=start_date, end=yf_end)

    if raw_df.empty:
        raise ValueError(f"yfinance returned empty DataFrame for {symbol}.NS")

    if raw_df["Close"].isna().all():
        raise ValueError(
            f"yfinance returned all-NaN Close column for {symbol}.NS"
        )

    df = raw_df.reset_index()
    df = df.rename(
        columns={
            "Date": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df["symbol"] = symbol

    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize("Asia/Kolkata")

    df["volume"] = df["volume"].astype("float64")
    df = df[["symbol", "date", "open", "high", "low", "close", "volume"]]
    return df


def _fetch_jugaad(
    symbol: str,
    start_date: datetime.date,
    end_date: datetime.date,
) -> pd.DataFrame:
    """Fetch OHLCV from jugaad-data for a single NSE symbol.

    Args:
        symbol: NSE ticker symbol (no suffix needed for jugaad-data).
        start_date: Start date (inclusive).
        end_date: End date (inclusive).

    Returns:
        Normalised DataFrame matching the Section 5.1 contract.

    Raises:
        ValueError: If the returned DataFrame is empty or CLOSE is all NaN.
        requests.exceptions.RequestException: On network failure.
    """
    raw_df = stock_df(symbol, start_date, end_date, series="EQ")

    if raw_df.empty:
        raise ValueError(f"jugaad-data returned empty DataFrame for {symbol}")

    if raw_df["CLOSE"].isna().all():
        raise ValueError(
            f"jugaad-data returned all-NaN CLOSE column for {symbol}"
        )

    df = raw_df.rename(
        columns={
            "DATE": "date",
            "OPEN": "open",
            "HIGH": "high",
            "LOW": "low",
            "CLOSE": "close",
            "VOLUME": "volume",
            "SYMBOL": "symbol",
        }
    )
    df = df[["symbol", "date", "open", "high", "low", "close", "volume"]]

    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["date"] = df["date"].dt.tz_localize("Asia/Kolkata")

    df["volume"] = df["volume"].astype("float64")
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_ohlcv(
    symbols: list[str],
    start_date: datetime.date,
    end_date: datetime.date,
    cache_expiry_hours: int = 24,
) -> pd.DataFrame:
    """Fetch OHLCV data for one or more NSE stock symbols.

    Tries yfinance first, falls back to jugaad-data on failure.
    Results are cached as CSV files in data/cache/ with configurable expiry.
    Returns a single normalised DataFrame with all symbols stacked vertically.

    Args:
        symbols: List of NSE ticker symbols without .NS suffix.
                 E.g. ["RELIANCE", "TCS", "INFY"].
        start_date: Start date (inclusive) for historical data.
        end_date: End date (inclusive) for historical data.
        cache_expiry_hours: Cache freshness threshold in hours. Default 24.
                            Set to 0 to force a fresh fetch (bypass cache).

    Returns:
        pd.DataFrame conforming to the validator.py Section 5.1 contract:
        columns [symbol, date, open, high, low, close, volume],
        date is datetime64[ns, Asia/Kolkata], volume is float64.
        Sorted by (symbol, date) ascending.

    Raises:
        FetchError: If both yfinance and jugaad-data fail for any symbol.
                    The error message identifies the symbol and both failure
                    reasons.
        ValueError: If symbols list is empty.
    """
    if not symbols:
        raise ValueError("symbols list must not be empty")

    os.makedirs(CACHE_DIR, exist_ok=True)

    frames: list[pd.DataFrame] = []

    for symbol in symbols:
        yf_error: str | None = None
        jd_error: str | None = None

        # Step 1: Check yfinance cache
        cached = _read_cache(symbol, "yfinance", cache_expiry_hours)
        if cached is not None:
            frames.append(cached)
            continue

        # Step 2: Try yfinance
        try:
            df = _fetch_yfinance(symbol, start_date, end_date)
            _write_cache(df, symbol, "yfinance")
            logger.info("Cache miss for %s, fetched from yfinance", symbol)
            frames.append(df)
            continue
        except (
            requests.exceptions.RequestException,
            ConnectionError,
            TimeoutError,
            ValueError,
            KeyError,
        ) as exc:
            yf_error = str(exc)
            logger.warning(
                "yfinance failed for %s: %s. Falling back to jugaad-data",
                symbol,
                yf_error,
            )

        # Step 3: Check jugaad cache
        cached = _read_cache(symbol, "jugaad", cache_expiry_hours)
        if cached is not None:
            frames.append(cached)
            continue

        # Step 4: Try jugaad-data
        try:
            df = _fetch_jugaad(symbol, start_date, end_date)
            _write_cache(df, symbol, "jugaad")
            logger.info("Cache miss for %s, fetched from jugaad", symbol)
            frames.append(df)
            continue
        except (
            requests.exceptions.RequestException,
            ConnectionError,
            TimeoutError,
            ValueError,
            KeyError,
        ) as exc:
            jd_error = str(exc)
            logger.error("jugaad-data failed for %s: %s", symbol, jd_error)

        # Step 5: Both sources failed
        logger.error("All sources failed for %s", symbol)
        raise FetchError(symbol, yfinance_error=yf_error, jugaad_error=jd_error)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)
    return combined


def fetch_nifty50_symbols() -> list[str]:
    """Return the current Nifty 50 constituent stock symbols.

    Returns a hardcoded list of 50 NSE ticker symbols (without .NS suffix).
    This list is accurate as of March 2026. The actual Nifty 50 constituents
    change quarterly during SEBI index rebalancing -- this list must be
    manually updated when constituents change.

    Returns:
        Sorted list of 50 NSE ticker symbols as strings.
    """
    return list(NIFTY50_SYMBOLS)


# Nifty 200 hardcoded fallback — accurate as of 2026-04-24.
# Used when niftyindices.com is unreachable. Update quarterly.
_NIFTY200_FALLBACK: list[str] = [
    "360ONE", "ABB", "ABCAPITAL", "ADANIENSOL", "ADANIENT", "ADANIGREEN",
    "ADANIPORTS", "ADANIPOWER", "ALKEM", "AMBUJACEM", "APLAPOLLO", "APOLLOHOSP",
    "ASHOKLEY", "ASIANPAINT", "ASTRAL", "ATGL", "AUBANK", "AUROPHARMA",
    "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE",
    "BANKBARODA", "BANKINDIA", "BDL", "BEL", "BHARATFORG", "BHARTIARTL",
    "BHEL", "BIOCON", "BLUESTARCO", "BOSCHLTD", "BPCL", "BRITANNIA", "BSE",
    "CANBK", "CGPOWER", "CHOLAFIN", "CIPLA", "COALINDIA", "COCHINSHIP",
    "COFORGE", "COLPAL", "CONCOR", "COROMANDEL", "CUMMINSIND", "DABUR",
    "DIVISLAB", "DIXON", "DLF", "DMART", "DRREDDY", "EICHERMOT", "ENRIN",
    "ETERNAL", "EXIDEIND", "FEDERALBNK", "FORTIS", "GAIL", "GLENMARK",
    "GMRAIRPORT", "GODFRYPHLP", "GODREJCP", "GODREJPROP", "GRASIM", "GROWW",
    "GVT&D", "HAL", "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDPETRO", "HINDUNILVR", "HINDZINC", "HUDCO",
    "HYUNDAI", "ICICIAMC", "ICICIBANK", "ICICIGI", "IDEA", "IDFCFIRSTB",
    "INDHOTEL", "INDIANB", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY",
    "IOC", "IRCTC", "IREDA", "IRFC", "ITC", "JINDALSTEL", "JIOFIN",
    "JSWENERGY", "JSWSTEEL", "JUBLFOOD", "KALYANKJIL", "KEI", "KOTAKBANK",
    "KPITTECH", "LAURUSLABS", "LENSKART", "LGEINDIA", "LICHSGFIN", "LODHA",
    "LT", "LTF", "LTM", "LUPIN", "M&M", "M&MFIN", "MANKIND", "MARICO",
    "MARUTI", "MAXHEALTH", "MAZDOCK", "MCX", "MFSL", "MOTHERSON",
    "MOTILALOFS", "MPHASIS", "MRF", "MUTHOOTFIN", "NATIONALUM", "NAUKRI",
    "NESTLEIND", "NHPC", "NMDC", "NTPC", "NYKAA", "OBEROIRLTY", "OFSS",
    "OIL", "ONGC", "PAGEIND", "PATANJALI", "PAYTM", "PERSISTENT", "PFC",
    "PHOENIXLTD", "PIDILITIND", "PIIND", "PNB", "POLICYBZR", "POLYCAB",
    "POWERGRID", "POWERINDIA", "PREMIERENE", "PRESTIGE", "RADICO", "RECLTD",
    "RELIANCE", "RVNL", "SAIL", "SBICARD", "SBILIFE", "SBIN", "SHREECEM",
    "SHRIRAMFIN", "SIEMENS", "SOLARINDS", "SRF", "SUNPHARMA", "SUPREMEIND",
    "SUZLON", "SWIGGY", "TATACAP", "TATACOMM", "TATACONSUM", "TATAELXSI",
    "TATAINVEST", "TATAPOWER", "TATASTEEL", "TCS", "TECHM", "TIINDIA",
    "TITAN", "TMCV", "TMPV", "TORNTPHARM", "TRENT", "TVSMOTOR", "ULTRACEMCO",
    "UNIONBANK", "UNITDSPR", "UPL", "VBL", "VEDL", "VMM", "VOLTAS",
    "WAAREEENER", "WIPRO", "YESBANK", "ZYDUSLIFE",
]


def fetch_nifty200_symbols() -> list[str]:
    """Return current Nifty 200 constituent symbols, fetched live from niftyindices.com.

    Tries niftyindices.com first; falls back to a hardcoded list dated 2026-04-24
    if the live fetch fails or returns fewer than 150 symbols.

    Returns:
        Sorted list of NSE ticker symbols (without .NS suffix), approximately 200 entries.
    """
    import csv
    import io

    url = "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        symbols = [
            row["Symbol"].strip()
            for row in csv.DictReader(io.StringIO(resp.text))
            if row.get("Symbol", "").strip()
        ]
        if len(symbols) >= 150:
            logger.info("fetch_nifty200_symbols: fetched %d symbols live", len(symbols))
            return sorted(symbols)
        logger.warning(
            "fetch_nifty200_symbols: live fetch returned only %d symbols, using fallback",
            len(symbols),
        )
    except Exception as exc:
        logger.warning("fetch_nifty200_symbols: live fetch failed (%s), using fallback", exc)
    return list(_NIFTY200_FALLBACK)


def fetch_sector_indices(
    start_date: datetime.date,
    end_date: datetime.date,
    cache_expiry_hours: int = 24,
) -> pd.DataFrame:
    """Fetch OHLCV data for NSE sector indices via yfinance.

    Fetches NIFTY_IT, NIFTY_BANK, NIFTY_AUTO, NIFTY_PHARMA, NIFTY_FMCG,
    and NIFTY_50 index data. Uses yfinance only (no jugaad-data fallback
    for indices -- jugaad-data's index API is unreliable).

    Args:
        start_date: Start date (inclusive) for historical data.
        end_date: End date (inclusive) for historical data.
        cache_expiry_hours: Cache freshness threshold in hours. Default 24.

    Returns:
        pd.DataFrame with the same column contract as fetch_ohlcv:
        [symbol, date, open, high, low, close, volume].
        The symbol column contains the human-readable index name
        (e.g. "NIFTY_IT", "NIFTY_BANK"), not the yfinance ticker.
        Sorted by (symbol, date) ascending.

    Raises:
        FetchError: If yfinance fails for any sector index.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    frames: list[pd.DataFrame] = []

    for index_name, yf_ticker in SECTOR_INDEX_MAP.items():
        # Check cache using the yfinance ticker as the file key
        cached = _read_cache(yf_ticker, "yfinance", cache_expiry_hours)
        if cached is not None:
            # Replace whatever symbol is in cache with the human-readable name
            cached = cached.copy()
            cached["symbol"] = index_name
            frames.append(cached)
            continue

        try:
            # Fetch using the yfinance ticker directly (not .NS suffix for indices)
            yf_end = end_date + datetime.timedelta(days=1)
            ticker = yf.Ticker(yf_ticker)
            raw_df = ticker.history(start=start_date, end=yf_end)

            if raw_df.empty:
                raise ValueError(
                    f"yfinance returned empty DataFrame for {yf_ticker}"
                )
            if raw_df["Close"].isna().all():
                raise ValueError(
                    f"yfinance returned all-NaN Close column for {yf_ticker}"
                )

            df = raw_df.reset_index()
            df = df.rename(
                columns={
                    "Date": "date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
            df = df[["date", "open", "high", "low", "close", "volume"]]

            # symbol column = human-readable key, not yfinance ticker
            df["symbol"] = index_name

            if df["date"].dt.tz is None:
                df["date"] = df["date"].dt.tz_localize("Asia/Kolkata")

            df["volume"] = df["volume"].astype("float64")
            df = df[["symbol", "date", "open", "high", "low", "close", "volume"]]

            # Cache under the yfinance ticker filename
            _write_cache(df, yf_ticker, "yfinance")
            logger.info(
                "Cache miss for %s, fetched from yfinance", index_name
            )
            frames.append(df)

        except (
            requests.exceptions.RequestException,
            ConnectionError,
            TimeoutError,
            ValueError,
            KeyError,
        ) as exc:
            error_msg = str(exc)
            logger.error(
                "Failed to fetch sector index %s: %s", index_name, error_msg
            )
            raise FetchError(index_name, yfinance_error=error_msg) from exc

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)
    return combined
