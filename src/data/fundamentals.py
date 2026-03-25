"""Fundamental data acquisition layer for the Indian Trader pipeline.

Scrapes ROE, debt-to-equity, quarterly EPS, and P/E ratio from Screener.in
for NSE-listed stocks. Caches results as JSON files in data/cache/ with a
45-day expiry. Falls back to yfinance after 3 consecutive Screener.in
failures per symbol. Cross-validates P/E between sources when both are
available.

Returns a normalised DataFrame that is a superset of the src/data/validator.py
Section 5.2 contract. Consumed by validator.py, quality_filter.py, and main.py.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from src.config.settings import settings


# ---------------------------------------------------------------------------
# Module-level exception
# ---------------------------------------------------------------------------


class FundamentalsError(Exception):
    """Raised when fundamentals data fetch or DB operation fails."""

    pass

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

CACHE_DIR: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "cache",
)

CACHE_EXPIRY_SECONDS: int = 45 * 86400  # 45 days

SCREENER_BASE_URL: str = "https://www.screener.in/company"

SCREENER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SCREENER_TIMEOUT: int = 15  # seconds

MAX_STRIKES: int = 3  # 3-strike fallback threshold

PE_CROSS_VALIDATION_THRESHOLD: float = 0.20  # 20% deviation triggers stale_data flag

DE_NORMALISATION_THRESHOLD: float = 10.0  # yfinance D/E values above this are divided by 100

AGENT_NAME: str = "fundamentals"  # for future agent_logs integration

_IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Historical fundamentals constants
# ---------------------------------------------------------------------------

# Fiscal year safe-publish cutoff: Indian FY results reliably available from July
FISCAL_YEAR_SAFE_MONTH: int = 7  # July — FY results safely published by this month

# Historical data range for backtest
HISTORICAL_START_YEAR: int = 2010
HISTORICAL_END_YEAR: int = 2023

# Nifty 50 constituents by symbol: maps symbol -> list of calendar years present in index
# Compiled from NSE semi-annual reconstitution records (2010-2023).
# Represents the "stable core" — stocks present >= 80% of the 14-year backtest period,
# plus early-era stocks included to ensure adequate universe size pre-2015.
NIFTY_CONSTITUENTS_BY_SYMBOL: dict[str, list[int]] = {
    "RELIANCE": list(range(2010, 2024)),
    "TCS": list(range(2010, 2024)),
    "HDFCBANK": list(range(2010, 2024)),
    "INFY": list(range(2010, 2024)),
    "ICICIBANK": list(range(2010, 2024)),
    "HINDUNILVR": list(range(2010, 2024)),
    "ITC": list(range(2010, 2024)),
    "SBIN": list(range(2010, 2024)),
    "BHARTIARTL": list(range(2010, 2024)),
    "KOTAKBANK": list(range(2011, 2024)),
    "LT": list(range(2010, 2024)),
    "AXISBANK": list(range(2010, 2024)),
    "ASIANPAINT": list(range(2012, 2024)),
    "MARUTI": list(range(2010, 2024)),
    "HCLTECH": list(range(2010, 2024)),
    "SUNPHARMA": list(range(2010, 2024)),
    "TITAN": list(range(2012, 2024)),
    "BAJFINANCE": list(range(2014, 2024)),
    "WIPRO": list(range(2010, 2024)),
    "ULTRACEMCO": list(range(2010, 2024)),
    "NESTLEIND": list(range(2013, 2024)),
    "TATAMOTORS": list(range(2010, 2024)),
    "POWERGRID": list(range(2010, 2024)),
    "NTPC": list(range(2010, 2024)),
    "M&M": list(range(2010, 2024)),
    "TATASTEEL": list(range(2010, 2024)),
    "TECHM": list(range(2013, 2024)),
    "ONGC": list(range(2010, 2024)),
    "HDFCLIFE": list(range(2019, 2024)),
    "BAJAJFINSV": list(range(2015, 2024)),
    "JSWSTEEL": list(range(2010, 2024)),
    "INDUSINDBK": list(range(2013, 2024)),
    "GRASIM": list(range(2010, 2024)),
    "CIPLA": list(range(2010, 2024)),
    "DRREDDY": list(range(2010, 2024)),
    "BPCL": list(range(2010, 2024)),
    "COALINDIA": list(range(2011, 2024)),
    "HEROMOTOCO": list(range(2010, 2024)),
    "EICHERMOT": list(range(2016, 2024)),
    "DIVISLAB": list(range(2020, 2024)),
    "BRITANNIA": list(range(2016, 2024)),
    "HINDALCO": list(range(2010, 2024)),
    "ADANIPORTS": list(range(2013, 2024)),
    "TATACONSUM": list(range(2020, 2024)),
    "SBILIFE": list(range(2020, 2024)),
    "APOLLOHOSP": list(range(2021, 2024)),
    "UPL": list(range(2014, 2024)),
    "SAIL": list(range(2010, 2015)),
    "VEDL": list(range(2013, 2018)),
    "DLF": list(range(2010, 2014)),
    "JINDALSTEL": list(range(2010, 2015)),
    "BANKBARODA": list(range(2010, 2016)),
    "PNB": list(range(2010, 2014)),
    "BHEL": list(range(2010, 2016)),
    "ACC": list(range(2010, 2014)),
    "AMBUJACEM": list(range(2010, 2014)),
    "LUPIN": list(range(2014, 2020)),
    "YESBANK": list(range(2014, 2020)),
    "ZEEL": list(range(2014, 2020)),
    "GAIL": list(range(2010, 2024)),
    "IOC": list(range(2012, 2021)),
}


# ---------------------------------------------------------------------------
# Internal dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FundamentalsCache:
    """Internal cache schema for fundamentals JSON files.

    All fields map directly to the output DataFrame columns plus
    cached_at_ist for cache provenance tracking. This dataclass is
    serialised to JSON on cache write and deserialised on cache read.
    """

    symbol: str
    roe: float | None           # None when NaN (JSON null)
    debt_to_equity: float | None
    eps_positive_4q: bool
    pe_ratio: float | None
    data_source: str            # "screener" | "yfinance_fallback" | "failed"
    data_quality: str           # "clean" | "degraded" | "stale_data" | "fundamentals_stale" | "failed"
    cached_at_ist: str          # ISO 8601 IST timestamp


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _now_ist() -> str:
    """Return the current IST timestamp as an ISO 8601 string with timezone.

    Returns:
        ISO 8601 string with Asia/Kolkata offset, e.g. "2026-03-22T22:15:30+05:30".
    """
    import datetime
    return datetime.datetime.now(_IST).isoformat(timespec="seconds")


def _cache_path(symbol: str) -> str:
    """Return the absolute file path for a symbol's fundamentals cache JSON.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        Absolute path string, e.g. "/path/to/data/cache/RELIANCE_fundamentals.json".
    """
    return os.path.join(CACHE_DIR, f"{symbol}_fundamentals.json")


def _nan_to_none(v: float | None) -> float | None:
    """Convert NaN float to None for JSON serialisation compatibility.

    Args:
        v: Float value or None.

    Returns:
        None if v is None or NaN, else v unchanged.
    """
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return v


def _read_cache(symbol: str) -> _FundamentalsCache | None:
    """Read cached fundamentals for a symbol.

    Returns None if the cache file does not exist, is expired (> 45 days),
    or is corrupt (invalid JSON). Corrupt files are deleted.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        _FundamentalsCache if cache is fresh and valid, None otherwise.
    """
    path = _cache_path(symbol)

    if not os.path.exists(path):
        return None

    age_seconds = time.time() - os.path.getmtime(path)
    if age_seconds >= CACHE_EXPIRY_SECONDS:
        age_days = age_seconds / 86400
        logger.warning(
            "Fundamentals cache expired for %s (%.1f days old)", symbol, age_days
        )
        return None

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        cache = _FundamentalsCache(
            symbol=data["symbol"],
            roe=data.get("roe"),
            debt_to_equity=data.get("debt_to_equity"),
            eps_positive_4q=data.get("eps_positive_4q", False),
            pe_ratio=data.get("pe_ratio"),
            data_source=data["data_source"],
            data_quality=data["data_quality"],
            cached_at_ist=data["cached_at_ist"],
        )
        return cache
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.warning(
            "Corrupt cache file for %s, deleting and refetching: %s", symbol, exc
        )
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _read_stale_cache(symbol: str) -> _FundamentalsCache | None:
    """Read cached fundamentals regardless of expiry, for graceful degradation.

    Used when a fresh fetch fails entirely. Overrides data_quality to
    "fundamentals_stale" so downstream filters can reject the stale data.

    Args:
        symbol: NSE ticker symbol.

    Returns:
        _FundamentalsCache with data_quality="fundamentals_stale" if a cache
        file exists (even expired), None if no file exists or the file is corrupt.
    """
    path = _cache_path(symbol)

    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        cache = _FundamentalsCache(
            symbol=data["symbol"],
            roe=data.get("roe"),
            debt_to_equity=data.get("debt_to_equity"),
            eps_positive_4q=data.get("eps_positive_4q", False),
            pe_ratio=data.get("pe_ratio"),
            data_source=data["data_source"],
            data_quality="fundamentals_stale",
            cached_at_ist=data["cached_at_ist"],
        )
        return cache
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.warning(
            "Corrupt cache file for %s (stale read attempt): %s", symbol, exc
        )
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _write_cache(cache: _FundamentalsCache) -> None:
    """Write a _FundamentalsCache to its JSON cache file.

    Creates CACHE_DIR if necessary. Converts NaN to None before serialisation
    because json.dumps does not handle float('nan') correctly. OSError on
    write is logged as WARNING but not raised — data is still returned even
    if cache write fails.

    Args:
        cache: The fundamentals cache object to serialise.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(cache.symbol)

    cache_dict = {
        "symbol": cache.symbol,
        "roe": _nan_to_none(cache.roe),
        "debt_to_equity": _nan_to_none(cache.debt_to_equity),
        "eps_positive_4q": cache.eps_positive_4q,
        "pe_ratio": _nan_to_none(cache.pe_ratio),
        "data_source": cache.data_source,
        "data_quality": cache.data_quality,
        "cached_at_ist": cache.cached_at_ist,
    }

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cache_dict, indent=2))
    except OSError as exc:
        logger.warning("Failed to write cache file for %s: %s", cache.symbol, exc)


def _cache_to_row(cache: _FundamentalsCache, cache_age_days: float) -> dict[str, object]:
    """Convert a _FundamentalsCache to a DataFrame row dict.

    Args:
        cache: The fundamentals cache object.
        cache_age_days: Age of the cache in days at time of fetch.

    Returns:
        Dict suitable for constructing a single-row DataFrame.
    """
    return {
        "symbol": cache.symbol,
        "roe": float("nan") if cache.roe is None else cache.roe,
        "debt_to_equity": float("nan") if cache.debt_to_equity is None else cache.debt_to_equity,
        "eps_positive_4q": cache.eps_positive_4q,
        "pe_ratio": float("nan") if cache.pe_ratio is None else cache.pe_ratio,
        "data_source": cache.data_source,
        "data_quality": cache.data_quality,
        "cache_age_days": cache_age_days,
        "fetched_at_ist": cache.cached_at_ist,
    }


def _scrape_screener(symbol: str) -> _FundamentalsCache | None:
    """Scrape fundamental data for a single symbol from Screener.in.

    Tries the consolidated URL first, then the standalone URL. Returns None
    only if BOTH URLs fail with HTTP errors or exceptions. Returning partial
    data (some NaN fields) counts as success — it is NOT a failure.

    Args:
        symbol: NSE ticker symbol without .NS suffix.

    Returns:
        _FundamentalsCache on success (even with partial data), None on complete failure.
    """
    urls = [
        f"{SCREENER_BASE_URL}/{symbol}/consolidated/",
        f"{SCREENER_BASE_URL}/{symbol}/",
    ]

    html_text: str | None = None

    for url in urls:
        try:
            response = requests.get(
                url,
                headers=SCREENER_HEADERS,
                timeout=SCREENER_TIMEOUT,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "Screener.in request failed for %s at %s: %s", symbol, url, exc
            )
            continue

        if response.status_code == 200:
            html_text = response.text
            break
        else:
            logger.warning(
                "Screener.in returned HTTP %d for %s at %s",
                response.status_code,
                symbol,
                url,
            )
            continue

    if html_text is None:
        return None

    soup = BeautifulSoup(html_text, "html.parser")

    roe: float | None = None
    debt_to_equity: float | None = None
    eps_positive_4q: bool = False
    pe_ratio: float | None = None

    # --- ROE ---
    roe_elem = soup.find(
        lambda tag: tag.name in ("li", "td", "span")
        and tag.string
        and ("Return on equity" in tag.string or tag.string.strip() == "ROE")
    )
    if roe_elem is not None:
        try:
            value_span = roe_elem.find_next("span", class_="number")
            if value_span is None:
                value_span = roe_elem.find_next(
                    lambda t: t.name in ("span", "td") and t.string and t.string.strip()
                )
            if value_span is not None and value_span.string:
                raw_roe = value_span.string.strip().replace(",", "").replace("%", "")
                roe = float(raw_roe) / 100.0
        except (AttributeError, ValueError):
            logger.warning("Could not extract roe from Screener.in for %s", symbol)
    else:
        # Alternative: search for "Return on Equity" in ratio sections
        for tag in soup.find_all("li"):
            text = tag.get_text(separator=" ", strip=True)
            if "Return on equity" in text or "Return on Equity" in text:
                try:
                    spans = tag.find_all("span")
                    for sp in spans:
                        raw = sp.get_text(strip=True).replace(",", "").replace("%", "")
                        if raw:
                            roe = float(raw) / 100.0
                            break
                except (AttributeError, ValueError):
                    logger.warning(
                        "Could not extract roe from Screener.in for %s", symbol
                    )
                break

    if roe is None:
        logger.warning("Could not extract roe from Screener.in for %s", symbol)

    # --- D/E (computed from Balance Sheet: Borrowings / (Equity Capital + Reserves)) ---
    # Screener.in does not display D/E as a named ratio. It must be derived from
    # the annual Balance Sheet table using the most recent year's column (last td).
    equity_capital: float | None = None
    reserves: float | None = None
    borrowings: float | None = None

    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if not (heading and "Balance Sheet" in heading.get_text()):
            continue
        table = section.find("table")
        if table is None:
            break
        for row in table.find_all("tr"):
            label_td = row.find("td", class_="text")
            if label_td is None:
                continue
            label = label_td.get_text(strip=True).rstrip("+")
            all_tds = row.find_all("td")
            if len(all_tds) < 2:
                continue
            latest_raw = all_tds[-1].get_text(strip=True).replace(",", "")
            try:
                val = float(latest_raw) if latest_raw else None
            except ValueError:
                val = None
            if label == "Equity Capital":
                equity_capital = val
            elif label == "Reserves":
                reserves = val
            elif label in ("Borrowings", "Borrowing"):
                borrowings = val
        break  # Balance Sheet section found and parsed

    if equity_capital is not None and reserves is not None and borrowings is not None:
        equity_total = equity_capital + reserves
        if equity_total > 0:
            debt_to_equity = borrowings / equity_total
        else:
            logger.warning(
                "Balance Sheet equity is zero or negative for %s — cannot compute D/E",
                symbol,
            )
    else:
        logger.warning(
            "Could not extract debt_to_equity from Screener.in for %s"
            " (Equity Capital=%s Reserves=%s Borrowings=%s)",
            symbol, equity_capital, reserves, borrowings,
        )

    # --- P/E ---
    pe_elem = soup.find(
        lambda tag: tag.name in ("li", "td", "span")
        and tag.string
        and (
            "Stock P/E" in tag.string
            or "Price to Earning" in tag.string
        )
    )
    if pe_elem is not None:
        try:
            value_span = pe_elem.find_next("span", class_="number")
            if value_span is None:
                value_span = pe_elem.find_next(
                    lambda t: t.name in ("span", "td") and t.string and t.string.strip()
                )
            if value_span is not None and value_span.string:
                raw_pe = value_span.string.strip().replace(",", "")
                pe_ratio = float(raw_pe)
        except (AttributeError, ValueError):
            logger.warning(
                "Could not extract pe_ratio from Screener.in for %s", symbol
            )
    else:
        for tag in soup.find_all("li"):
            text = tag.get_text(separator=" ", strip=True)
            if "Stock P/E" in text or "Price to Earning" in text:
                try:
                    spans = tag.find_all("span")
                    for sp in spans:
                        raw = sp.get_text(strip=True).replace(",", "")
                        if raw:
                            pe_ratio = float(raw)
                            break
                except (AttributeError, ValueError):
                    logger.warning(
                        "Could not extract pe_ratio from Screener.in for %s", symbol
                    )
                break

    if pe_ratio is None:
        logger.warning("Could not extract pe_ratio from Screener.in for %s", symbol)

    # --- Quarterly EPS ---
    quarterly_table = None
    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if heading and "Quarterly" in heading.get_text():
            quarterly_table = section.find("table")
            break

    if quarterly_table is not None:
        try:
            eps_row = None
            for row in quarterly_table.find_all("tr"):
                first_cell = row.find(["td", "th"])
                if first_cell:
                    cell_text = first_cell.get_text(strip=True)
                    if cell_text in ("EPS", "EPS in Rs"):
                        eps_row = row
                        break

            if eps_row is not None:
                cells = eps_row.find_all("td")
                # Skip the label cell (index 0), take data cells
                data_cells = cells[1:] if len(cells) > 1 else []
                last_4 = data_cells[-4:] if len(data_cells) >= 4 else []
                if len(last_4) == 4:
                    eps_values: list[float] = []
                    for cell in last_4:
                        raw = cell.get_text(strip=True).replace(",", "")
                        eps_values.append(float(raw))
                    eps_positive_4q = all(v > 0 for v in eps_values)
                else:
                    # Fewer than 4 quarters available — conservative: treat as fail
                    eps_positive_4q = False
            else:
                logger.warning(
                    "Could not extract eps_positive_4q from Screener.in for %s", symbol
                )
        except (AttributeError, ValueError):
            logger.warning(
                "Could not extract eps_positive_4q from Screener.in for %s", symbol
            )
    else:
        logger.warning(
            "Could not extract eps_positive_4q from Screener.in for %s", symbol
        )

    logger.info("Fetched fundamentals from Screener.in for %s", symbol)

    return _FundamentalsCache(
        symbol=symbol,
        roe=roe,
        debt_to_equity=debt_to_equity,
        eps_positive_4q=eps_positive_4q,
        pe_ratio=pe_ratio,
        data_source="screener",
        data_quality="clean",
        cached_at_ist=_now_ist(),
    )


def _fetch_yfinance_fundamentals(symbol: str) -> _FundamentalsCache | None:
    """Fetch fundamental data for a single symbol from yfinance as fallback.

    Uses yf.Ticker("{symbol}.NS").info. Normalises D/E (divides by 100 if > 10).
    eps_positive_4q is derived from trailingEps > 0 (trailing annual, not per-quarter).
    Returns None only on total failure (empty dict, exception, network error).

    Args:
        symbol: NSE ticker symbol without .NS suffix.

    Returns:
        _FundamentalsCache on success (even partial), None on total failure.
    """
    logger.warning("Using yfinance fallback for %s fundamentals", symbol)

    try:
        info: dict = yf.Ticker(f"{symbol}.NS").info
    except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
        logger.error(
            "yfinance .info fetch failed for %s: %s", symbol, exc
        )
        return None

    if not info:
        logger.error("yfinance returned empty info dict for %s", symbol)
        return None

    # --- ROE ---
    roe: float | None = None
    try:
        raw_roe = info.get("returnOnEquity")
        if raw_roe is not None:
            roe = float(raw_roe)
    except (KeyError, ValueError):
        pass

    # --- D/E ---
    debt_to_equity: float | None = None
    try:
        raw_de = info.get("debtToEquity")
        if raw_de is not None:
            de_val = float(raw_de)
            if de_val > DE_NORMALISATION_THRESHOLD:
                logger.debug(
                    "Normalised yfinance D/E for %s: %s -> %s",
                    symbol,
                    de_val,
                    de_val / 100.0,
                )
                de_val = de_val / 100.0
            debt_to_equity = de_val
    except (KeyError, ValueError):
        pass

    # --- EPS (trailing annual approximation) ---
    eps_positive_4q: bool = False
    try:
        trailing_eps = info.get("trailingEps")
        if trailing_eps is not None:
            eps_positive_4q = float(trailing_eps) > 0
    except (KeyError, ValueError):
        pass

    # --- P/E ---
    pe_ratio: float | None = None
    try:
        raw_pe = info.get("trailingPE")
        if raw_pe is not None:
            pe_ratio = float(raw_pe)
    except (KeyError, ValueError):
        pass

    logger.info("Fetched fundamentals from yfinance for %s", symbol)

    return _FundamentalsCache(
        symbol=symbol,
        roe=roe,
        debt_to_equity=debt_to_equity,
        eps_positive_4q=eps_positive_4q,
        pe_ratio=pe_ratio,
        data_source="yfinance_fallback",
        data_quality="degraded",
        cached_at_ist=_now_ist(),
    )


def _cross_validate_pe(symbol: str, screener_pe: float) -> str:
    """Cross-validate P/E ratio between Screener.in and yfinance.

    Fetches yfinance trailingPE and computes deviation. Returns "stale_data"
    if deviation exceeds 20%, "clean" otherwise. Also returns "clean" if
    yfinance P/E is unavailable (cannot penalise for missing data).

    Args:
        symbol: NSE ticker symbol without .NS suffix.
        screener_pe: P/E ratio obtained from Screener.in.

    Returns:
        "clean" or "stale_data".
    """
    try:
        info: dict = yf.Ticker(f"{symbol}.NS").info
        raw_yf_pe = info.get("trailingPE")
    except (requests.exceptions.RequestException, KeyError, ValueError):
        logger.info(
            "Cross-validation skipped for %s: yfinance P/E unavailable", symbol
        )
        return "clean"

    if raw_yf_pe is None:
        logger.info(
            "Cross-validation skipped for %s: yfinance P/E unavailable", symbol
        )
        return "clean"

    try:
        yf_pe = float(raw_yf_pe)
    except ValueError:
        logger.info(
            "Cross-validation skipped for %s: yfinance P/E unavailable", symbol
        )
        return "clean"

    if math.isnan(yf_pe):
        logger.info(
            "Cross-validation skipped for %s: yfinance P/E unavailable", symbol
        )
        return "clean"

    deviation = abs(screener_pe - yf_pe) / max(screener_pe, yf_pe)

    if deviation > PE_CROSS_VALIDATION_THRESHOLD:
        logger.warning(
            "P/E cross-validation failed for %s: Screener=%.1f, yfinance=%.1f"
            " -- flagged as stale_data",
            symbol,
            screener_pe,
            yf_pe,
        )
        return "stale_data"

    logger.debug(
        "P/E cross-validation passed for %s: Screener=%.1f, yfinance=%.1f",
        symbol,
        screener_pe,
        yf_pe,
    )
    return "clean"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_fundamentals(
    symbols: list[str],
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Fetch fundamental data for NSE stocks from Screener.in with yfinance fallback.

    Scrapes ROE, D/E, quarterly EPS, and P/E from Screener.in for each symbol.
    Caches results as JSON in data/cache/ with 45-day expiry. Falls back to
    yfinance after 3 consecutive Screener.in failures per symbol. Cross-validates
    P/E between sources when both are available.

    Processing per symbol:
    1. Check cache unless force_refresh=True. Return cached row if fresh.
    2. Attempt Screener.in up to MAX_STRIKES times (3-strike rule).
    3. On 3-strike failure: fall back to yfinance.
    4. On successful Screener.in fetch: run P/E cross-validation.
    5. Write to cache on any successful fetch.
    6. On total failure: include row with NaN values and data_source="failed".

    Symbols that completely fail both sources are included in the output with
    NaN values, not silently dropped.

    Args:
        symbols: List of NSE ticker symbols without .NS suffix.
                 E.g. ["RELIANCE", "TCS", "INFY"].
        force_refresh: If True, bypass cache for all symbols and fetch fresh data.

    Returns:
        pd.DataFrame with one row per symbol. Columns: symbol, roe, debt_to_equity,
        eps_positive_4q, pe_ratio, data_source, data_quality, cache_age_days,
        fetched_at_ist. Sorted by symbol ascending. RangeIndex.

    Raises:
        ValueError: If symbols list is empty.
    """
    if not symbols:
        raise ValueError("symbols list must not be empty")

    os.makedirs(CACHE_DIR, exist_ok=True)

    rows: list[dict[str, object]] = []

    for symbol in symbols:
        # --- Step 1: Cache check ---
        if not force_refresh:
            cached = _read_cache(symbol)
            if cached is not None:
                path = _cache_path(symbol)
                age_days = (time.time() - os.path.getmtime(path)) / 86400
                logger.info(
                    "Cache hit for %s fundamentals (%.1f days old)", symbol, age_days
                )
                rows.append(_cache_to_row(cached, age_days))
                continue

        # --- Step 2: Screener.in with 3-strike retry ---
        strike_count = 0
        screener_result: _FundamentalsCache | None = None

        while strike_count < MAX_STRIKES:
            logger.info(
                "Cache miss for %s fundamentals, fetching from screener", symbol
            )
            time.sleep(random.uniform(2.0, 5.0))

            result = _scrape_screener(symbol)
            if result is not None:
                screener_result = result
                break

            strike_count += 1
            logger.warning(
                "Screener.in failed for %s (strike %d/3): fetch returned None",
                symbol,
                strike_count,
            )

        if screener_result is not None:
            # Cross-validate P/E if available
            quality = "clean"
            if screener_result.pe_ratio is not None and not math.isnan(
                screener_result.pe_ratio
            ):
                quality = _cross_validate_pe(symbol, screener_result.pe_ratio)

            # Rebuild cache with potentially updated data_quality
            final_cache = _FundamentalsCache(
                symbol=screener_result.symbol,
                roe=screener_result.roe,
                debt_to_equity=screener_result.debt_to_equity,
                eps_positive_4q=screener_result.eps_positive_4q,
                pe_ratio=screener_result.pe_ratio,
                data_source=screener_result.data_source,
                data_quality=quality,
                cached_at_ist=screener_result.cached_at_ist,
            )
            _write_cache(final_cache)
            rows.append(_cache_to_row(final_cache, 0.0))
            continue

        # --- Step 3: 3-strike limit reached — yfinance fallback ---
        logger.warning(
            "Screener.in failed 3 consecutive times for %s, falling back to yfinance",
            symbol,
        )

        yf_result = _fetch_yfinance_fundamentals(symbol)

        if yf_result is not None:
            _write_cache(yf_result)
            rows.append(_cache_to_row(yf_result, 0.0))
            continue

        # --- Step 4: Both sources failed — try stale cache for graceful degradation ---
        logger.error("Both Screener.in and yfinance failed for %s", symbol)

        stale = _read_stale_cache(symbol)
        if stale is not None:
            path = _cache_path(symbol)
            age_days = (time.time() - os.path.getmtime(path)) / 86400
            rows.append(_cache_to_row(stale, age_days))
            continue

        # --- Step 5: Total failure — include failed row ---
        rows.append(
            {
                "symbol": symbol,
                "roe": float("nan"),
                "debt_to_equity": float("nan"),
                "eps_positive_4q": False,
                "pe_ratio": float("nan"),
                "data_source": "failed",
                "data_quality": "failed",
                "cache_age_days": float("nan"),
                "fetched_at_ist": _now_ist(),
            }
        )

    df = pd.DataFrame(rows)

    # Enforce dtypes
    df["roe"] = df["roe"].astype("float64")
    df["debt_to_equity"] = df["debt_to_equity"].astype("float64")
    df["pe_ratio"] = df["pe_ratio"].astype("float64")
    df["cache_age_days"] = df["cache_age_days"].astype("float64")
    df["eps_positive_4q"] = df["eps_positive_4q"].astype(bool)

    df = df.sort_values("symbol").reset_index(drop=True)

    return df


def get_cache_age_days(symbol: str) -> float | None:
    """Return the age of cached fundamentals data for a symbol in days.

    Args:
        symbol: NSE ticker symbol without .NS suffix.

    Returns:
        Age in days as float, or None if no cache file exists for this symbol.
    """
    cache_path = _cache_path(symbol)
    if not os.path.exists(cache_path):
        return None
    return (time.time() - os.path.getmtime(cache_path)) / 86400


# ---------------------------------------------------------------------------
# Historical fundamentals — SQLite-backed functions (Phase 2 additions)
# ---------------------------------------------------------------------------


def _init_historical_tables(db_path: str) -> sqlite3.Connection:
    """Create historical fundamentals tables and return an open WAL connection.

    Creates fundamentals_history and nifty_constituents tables if they do not
    already exist. Applies WAL pragmas matching the paper_trader.py pattern.
    The caller is responsible for closing the connection (use a finally block).

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        Open sqlite3.Connection with WAL mode applied.

    Raises:
        FundamentalsError: If the SQLite connection or table creation fails.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA cache_size=-64000;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS fundamentals_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT NOT NULL,
                fiscal_year    INTEGER NOT NULL,
                roe            REAL,
                debt_to_equity REAL,
                eps_positive   INTEGER,
                data_source    TEXT NOT NULL,
                data_quality   TEXT NOT NULL,
                fetched_at_ist TEXT NOT NULL,
                UNIQUE(symbol, fiscal_year)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS nifty_constituents (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol   TEXT NOT NULL,
                year     INTEGER NOT NULL,
                in_index INTEGER NOT NULL,
                UNIQUE(symbol, year)
            )
        """)

        conn.commit()
        return conn

    except sqlite3.Error as exc:
        raise FundamentalsError(
            f"Failed to initialise historical tables at {db_path}: {exc}"
        ) from exc


def _populate_nifty_constituents(conn: sqlite3.Connection) -> None:
    """Populate nifty_constituents table from the hardcoded NIFTY_CONSTITUENTS_BY_SYMBOL dict.

    Inserts one row per (symbol, year) combination for all years 2010-2023.
    in_index=1 if the symbol was in the Nifty 50 that year, 0 otherwise.
    Uses INSERT OR IGNORE so calling this function twice is safe (idempotent).

    Args:
        conn: Open sqlite3.Connection with nifty_constituents table already created.

    Raises:
        FundamentalsError: If any SQLite write fails.
    """
    rows: list[tuple[str, int, int]] = []
    for symbol, years_in_index in NIFTY_CONSTITUENTS_BY_SYMBOL.items():
        years_set = set(years_in_index)
        for year in range(HISTORICAL_START_YEAR, HISTORICAL_END_YEAR + 1):
            in_index = 1 if year in years_set else 0
            rows.append((symbol, year, in_index))

    try:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO nifty_constituents (symbol, year, in_index) VALUES (?, ?, ?)",
                rows,
            )
    except sqlite3.Error as exc:
        raise FundamentalsError(
            f"Failed to populate nifty_constituents: {exc}"
        ) from exc


def fetch_historical_fundamentals(
    symbols: list[str],
    force_refresh: bool = False,
) -> None:
    """Fetch and store historical annual fundamentals from Screener.in for each symbol.

    Scrapes annual ROE, D/E, and EPS from Screener.in's Key Ratios, Balance Sheet,
    and Profit & Loss sections. Stores results to the fundamentals_history SQLite
    table. Returns None — callers query the table via get_fundamentals_for_date().

    Staleness rule: skips symbols where ALL rows in fundamentals_history are
    fetched within the last 45 days (unless force_refresh=True). Fetches when
    any rows are stale, missing expected years, or force_refresh=True.

    3-strike fallback: after 3 Screener.in failures for a symbol, falls back to
    yfinance for the current year only. Prior years receive NULL-valued placeholder
    rows with data_source='yfinance_fallback' and data_quality='failed', so
    get_fundamentals_for_date() returns 'missing' quality rather than raising.

    EPS approximation: eps_positive is 1 if annual EPS > 0, NOT the quarterly
    4-consecutive check used in live trading. This is documented explicitly
    in the fundamentals_history schema and carried through to eps_positive_4q
    in get_fundamentals_for_date() output. See spec Section 7.

    Args:
        symbols: List of NSE ticker symbols without .NS suffix.
        force_refresh: If True, re-fetches all symbols regardless of cache age.

    Raises:
        ValueError: If symbols list is empty.
        FundamentalsError: If SQLite operations fail.
    """
    if not symbols:
        raise ValueError("symbols list must not be empty")

    db_path = settings.database_url.replace("sqlite:///", "")
    conn = _init_historical_tables(db_path)

    try:
        now_ist = _now_ist()
        cutoff_days = 45

        for symbol in symbols:
            # ----------------------------------------------------------------
            # STEP 1: Staleness check
            # ----------------------------------------------------------------
            if not force_refresh:
                try:
                    rows_db = conn.execute(
                        "SELECT fetched_at_ist FROM fundamentals_history WHERE symbol = ?",
                        (symbol,),
                    ).fetchall()
                except sqlite3.Error as exc:
                    raise FundamentalsError(
                        f"DB read failed for staleness check on {symbol}: {exc}"
                    ) from exc

                if rows_db:
                    # Check if ALL rows are within 45 days
                    all_fresh = True
                    for (fetched_at_str,) in rows_db:
                        try:
                            fetched_dt = datetime.datetime.fromisoformat(fetched_at_str)
                            age_days = (
                                datetime.datetime.now(fetched_dt.tzinfo) - fetched_dt
                            ).total_seconds() / 86400
                            if age_days >= cutoff_days:
                                all_fresh = False
                                break
                        except (ValueError, TypeError):
                            all_fresh = False
                            break

                    if all_fresh:
                        logger.info(
                            "Historical fundamentals cache hit for %s — skipping fetch",
                            symbol,
                        )
                        continue

            # ----------------------------------------------------------------
            # STEP 2: Screener.in fetch with 3-strike retry
            # ----------------------------------------------------------------
            time.sleep(random.uniform(2, 5))

            strike_count = 0
            parsed_data: dict[int, dict[str, float | int | None]] | None = None

            while strike_count < MAX_STRIKES:
                try:
                    parsed_data = _scrape_screener_historical(symbol)
                    if parsed_data is not None:
                        break
                    strike_count += 1
                    logger.warning(
                        "Screener.in historical parse failed for %s (strike %d/%d)",
                        symbol, strike_count, MAX_STRIKES,
                    )
                    if strike_count < MAX_STRIKES:
                        time.sleep(random.uniform(2, 5))
                except requests.exceptions.RequestException as exc:
                    strike_count += 1
                    logger.warning(
                        "Screener.in request error for %s (strike %d/%d): %s",
                        symbol, strike_count, MAX_STRIKES, exc,
                    )
                    if strike_count < MAX_STRIKES:
                        time.sleep(random.uniform(2, 5))

            # ----------------------------------------------------------------
            # STEP 3: Store Screener.in results
            # ----------------------------------------------------------------
            if parsed_data is not None and parsed_data:
                try:
                    with conn:
                        for fiscal_year, fields in parsed_data.items():
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO fundamentals_history
                                    (symbol, fiscal_year, roe, debt_to_equity,
                                     eps_positive, data_source, data_quality, fetched_at_ist)
                                VALUES (?, ?, ?, ?, ?, 'screener', 'clean', ?)
                                """,
                                (
                                    symbol,
                                    fiscal_year,
                                    fields.get("roe"),
                                    fields.get("debt_to_equity"),
                                    fields.get("eps_positive"),
                                    now_ist,
                                ),
                            )
                    logger.info(
                        "Stored %d years of historical fundamentals for %s from Screener.in",
                        len(parsed_data), symbol,
                    )
                    continue
                except sqlite3.Error as exc:
                    raise FundamentalsError(
                        f"DB write failed for historical fundamentals of {symbol}: {exc}"
                    ) from exc

            # ----------------------------------------------------------------
            # STEP 4: 3-strike limit reached — yfinance fallback
            # ----------------------------------------------------------------
            logger.warning(
                "Screener.in failed %d times for %s — falling back to yfinance",
                MAX_STRIKES, symbol,
            )

            current_year = datetime.date.today().year
            yf_roe: float | None = None
            yf_de: float | None = None
            yf_eps_positive: int | None = None
            yf_source = "yfinance_fallback"

            try:
                info: dict = yf.Ticker(f"{symbol}.NS").info
                if info:
                    raw_roe = info.get("returnOnEquity")
                    if raw_roe is not None:
                        yf_roe = float(raw_roe)

                    raw_de = info.get("debtToEquity")
                    if raw_de is not None:
                        de_val = float(raw_de)
                        if de_val > DE_NORMALISATION_THRESHOLD:
                            de_val = de_val / 100.0
                        yf_de = de_val

                    trailing_eps = info.get("trailingEps")
                    if trailing_eps is not None:
                        yf_eps_positive = 1 if float(trailing_eps) > 0 else 0
            except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
                logger.error("yfinance fallback failed for %s: %s", symbol, exc)
                yf_source = "failed"

            try:
                with conn:
                    # Current year: store whatever yfinance returned (degraded quality)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO fundamentals_history
                            (symbol, fiscal_year, roe, debt_to_equity,
                             eps_positive, data_source, data_quality, fetched_at_ist)
                        VALUES (?, ?, ?, ?, ?, ?, 'degraded', ?)
                        """,
                        (symbol, current_year, yf_roe, yf_de, yf_eps_positive,
                         yf_source, now_ist),
                    )

                    # All prior years with no DB data: insert NULL placeholder rows
                    existing_years = {
                        row[0] for row in conn.execute(
                            "SELECT fiscal_year FROM fundamentals_history WHERE symbol = ?",
                            (symbol,),
                        ).fetchall()
                    }
                    for year in range(HISTORICAL_START_YEAR, current_year):
                        if year not in existing_years:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO fundamentals_history
                                    (symbol, fiscal_year, roe, debt_to_equity,
                                     eps_positive, data_source, data_quality, fetched_at_ist)
                                VALUES (?, ?, NULL, NULL, NULL, 'yfinance_fallback', 'failed', ?)
                                """,
                                (symbol, year, now_ist),
                            )
            except sqlite3.Error as exc:
                raise FundamentalsError(
                    f"DB write failed for yfinance fallback data of {symbol}: {exc}"
                ) from exc

            logger.warning(
                "Stored yfinance_fallback fundamentals for %s (current year only; "
                "prior years marked as failed)",
                symbol,
            )

    finally:
        conn.close()


def _scrape_screener_historical(
    symbol: str,
) -> dict[int, dict[str, float | int | None]] | None:
    """Scrape multi-year annual fundamentals from Screener.in for a single symbol.

    Parses the Key Ratios, Balance Sheet, and Profit & Loss tables to extract
    ROE, D/E, and EPS across all available fiscal years (up to 10-12 years).

    Tries the consolidated URL first, then standalone. Returns None only on
    HTTP/network failure or complete parse failure (no year columns found).
    Partial data (some fields NULL for some years) is returned as-is.

    Args:
        symbol: NSE ticker symbol without .NS suffix.

    Returns:
        Dict mapping fiscal_year (int) -> {"roe": float|None, "debt_to_equity":
        float|None, "eps_positive": int|None}, or None on failure.

    Raises:
        requests.exceptions.RequestException: Propagated for caller's 3-strike logic.
    """
    urls = [
        f"{SCREENER_BASE_URL}/{symbol}/consolidated/",
        f"{SCREENER_BASE_URL}/{symbol}/",
    ]

    html_text: str | None = None
    for url in urls:
        response = requests.get(
            url,
            headers=SCREENER_HEADERS,
            timeout=SCREENER_TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code == 200:
            html_text = response.text
            break
        logger.warning(
            "Screener.in returned HTTP %d for %s at %s",
            response.status_code, symbol, url,
        )

    if html_text is None:
        return None

    soup = BeautifulSoup(html_text, "html.parser")

    # ----------------------------------------------------------------
    # Extract year headers from Key Ratios section ("Mar YYYY")
    # ----------------------------------------------------------------
    fiscal_years: list[int] = []
    roe_by_year: dict[int, float | None] = {}
    de_by_year: dict[int, float | None] = {}
    eps_by_year: dict[int, int | None] = {}

    # --- ROE from Key Ratios section ---
    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if heading is None:
            continue
        heading_text = heading.get_text(strip=True)
        if "Ratio" not in heading_text:
            continue
        table = section.find("table")
        if table is None:
            break

        rows_in_table = table.find_all("tr")
        if not rows_in_table:
            break

        # First row: year headers — cells contain "Mar YYYY"
        header_cells = rows_in_table[0].find_all(["th", "td"])
        for cell in header_cells[1:]:  # skip first label cell
            raw = cell.get_text(strip=True)
            try:
                if "Mar" in raw or "Sep" in raw or len(raw) == 4:
                    year_str = raw.split()[-1]
                    fiscal_years.append(int(year_str))
            except (ValueError, IndexError):
                continue

        # Find ROE row
        for row in rows_in_table[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True)
            if "Return on equity" in label or "Return on Equity" in label or label.strip() == "ROE":
                for idx, cell in enumerate(cells[1:len(fiscal_years) + 1]):
                    raw_val = cell.get_text(strip=True).replace(",", "").replace("%", "").strip()
                    if raw_val and raw_val != "--":
                        try:
                            roe_by_year[fiscal_years[idx]] = float(raw_val) / 100.0
                        except ValueError:
                            roe_by_year[fiscal_years[idx]] = None
                    else:
                        roe_by_year[fiscal_years[idx]] = None
                break
        break

    # --- D/E from Balance Sheet section ---
    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if heading is None:
            continue
        if "Balance Sheet" not in heading.get_text():
            continue
        table = section.find("table")
        if table is None:
            break

        rows_in_table = table.find_all("tr")
        if not rows_in_table:
            break

        # Extract year headers from Balance Sheet (may differ from Ratios)
        bs_years: list[int] = []
        header_cells = rows_in_table[0].find_all(["th", "td"])
        for cell in header_cells[1:]:
            raw = cell.get_text(strip=True)
            try:
                if "Mar" in raw or "Sep" in raw or len(raw) == 4:
                    year_str = raw.split()[-1]
                    bs_years.append(int(year_str))
            except (ValueError, IndexError):
                continue

        # If no year headers found in Balance Sheet, fall back to Ratios years
        if not bs_years:
            bs_years = fiscal_years

        equity_capital_by_year: dict[int, float | None] = {}
        reserves_by_year: dict[int, float | None] = {}
        borrowings_by_year: dict[int, float | None] = {}

        for row in rows_in_table[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True).rstrip("+")
            for idx, cell in enumerate(cells[1:len(bs_years) + 1]):
                if idx >= len(bs_years):
                    break
                raw_val = cell.get_text(strip=True).replace(",", "").strip()
                if raw_val and raw_val != "--":
                    try:
                        val: float | None = float(raw_val)
                    except ValueError:
                        val = None
                else:
                    val = None

                if label == "Equity Capital":
                    equity_capital_by_year[bs_years[idx]] = val
                elif label == "Reserves":
                    reserves_by_year[bs_years[idx]] = val
                elif label in ("Borrowings", "Borrowing"):
                    borrowings_by_year[bs_years[idx]] = val

        for year in bs_years:
            ec = equity_capital_by_year.get(year)
            res = reserves_by_year.get(year)
            borr = borrowings_by_year.get(year)
            if ec is not None and res is not None and borr is not None:
                equity_total = ec + res
                if equity_total > 0:
                    de_by_year[year] = borr / equity_total
                else:
                    de_by_year[year] = None
            else:
                de_by_year[year] = None
        break

    # --- EPS from Profit & Loss section ---
    for section in soup.find_all("section"):
        heading = section.find(["h2", "h3"])
        if heading is None:
            continue
        heading_text = heading.get_text(strip=True)
        if "Profit" not in heading_text and "Loss" not in heading_text:
            continue
        # Skip Quarterly P&L — we need the annual one
        if "Quarterly" in heading_text:
            continue
        table = section.find("table")
        if table is None:
            break

        rows_in_table = table.find_all("tr")
        if not rows_in_table:
            break

        # Extract year headers from P&L (may have different coverage)
        pl_years: list[int] = []
        header_cells = rows_in_table[0].find_all(["th", "td"])
        for cell in header_cells[1:]:
            raw = cell.get_text(strip=True)
            try:
                if "Mar" in raw or "Sep" in raw or len(raw) == 4:
                    year_str = raw.split()[-1]
                    pl_years.append(int(year_str))
            except (ValueError, IndexError):
                continue

        if not pl_years:
            pl_years = fiscal_years

        for row in rows_in_table[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = cells[0].get_text(strip=True)
            if label in ("EPS in Rs", "EPS"):
                for idx, cell in enumerate(cells[1:len(pl_years) + 1]):
                    if idx >= len(pl_years):
                        break
                    raw_val = cell.get_text(strip=True).replace(",", "").strip()
                    if raw_val and raw_val != "--":
                        try:
                            eps_val = float(raw_val)
                            eps_by_year[pl_years[idx]] = 1 if eps_val > 0 else 0
                        except ValueError:
                            eps_by_year[pl_years[idx]] = None
                    else:
                        eps_by_year[pl_years[idx]] = None
                break
        break

    # ----------------------------------------------------------------
    # Merge all years into a single result dict
    # ----------------------------------------------------------------
    all_years = set(fiscal_years) | set(de_by_year.keys()) | set(eps_by_year.keys())

    if not all_years:
        logger.warning("No year columns found in Screener.in for %s", symbol)
        return None

    result: dict[int, dict[str, float | int | None]] = {}
    for year in all_years:
        result[year] = {
            "roe": roe_by_year.get(year),
            "debt_to_equity": de_by_year.get(year),
            "eps_positive": eps_by_year.get(year),
        }

    return result


def get_fundamentals_for_date(
    symbols: list[str],
    as_of_date: datetime.date,
) -> pd.DataFrame:
    """Return point-in-time fundamentals for symbols as of a given historical date.

    Selects the fiscal year corresponding to as_of_date using the Indian FY
    safe-publish rule (lookahead-bias-free):
      - month <= 6 (Jan–June): fiscal_year = as_of_date.year - 1
        (FY results not yet reliably published; use prior completed FY)
      - month >= 7 (Jul–Dec): fiscal_year = as_of_date.year
        (FY ending March of this year is safely published by July)

    Examples:
      2015-06-30 -> fiscal_year=2014  (June: FY2015 not yet published)
      2015-07-01 -> fiscal_year=2015  (July: FY2015 safely published)
      2015-04-01 -> fiscal_year=2014  (April: FY2015 just ended, not published)
      2014-10-15 -> fiscal_year=2014  (October: FY2014 published ✓)

    The output column eps_positive_4q carries annual EPS > 0 (not 4-quarter
    check) for historical data. This is an accepted approximation for backtesting.
    See spec Section 7 and fetch_historical_fundamentals() docstring.

    Output columns match fetch_fundamentals() output (minus pe_ratio and
    cache_age_days) for drop-in compatibility with quality_filter.py.

    Args:
        symbols: List of NSE ticker symbols without .NS suffix.
        as_of_date: The historical date for which to retrieve fundamentals.

    Returns:
        pd.DataFrame with one row per symbol. Columns: symbol, roe,
        debt_to_equity, eps_positive_4q, data_source, data_quality,
        fetched_at_ist. Symbols with no DB row return NaN financials
        and data_quality="missing". Sorted by symbol ascending.

    Raises:
        ValueError: If symbols list is empty or as_of_date is not datetime.date.
        FundamentalsError: If SQLite operations fail.
    """
    if not symbols:
        raise ValueError("symbols list must not be empty")
    if not isinstance(as_of_date, datetime.date):
        raise ValueError(
            f"as_of_date must be a datetime.date instance, got {type(as_of_date)}"
        )

    # Determine safe fiscal year (no lookahead bias)
    if as_of_date.month <= 6:
        fiscal_year = as_of_date.year - 1
    else:
        fiscal_year = as_of_date.year

    db_path = settings.database_url.replace("sqlite:///", "")
    conn = _init_historical_tables(db_path)

    rows: list[dict[str, object]] = []

    try:
        for symbol in symbols:
            try:
                row_db = conn.execute(
                    """
                    SELECT roe, debt_to_equity, eps_positive,
                           data_source, data_quality, fetched_at_ist
                    FROM fundamentals_history
                    WHERE symbol = ? AND fiscal_year = ?
                    """,
                    (symbol, fiscal_year),
                ).fetchone()
            except sqlite3.Error as exc:
                raise FundamentalsError(
                    f"DB read failed for {symbol} fiscal_year={fiscal_year}: {exc}"
                ) from exc

            if row_db is not None:
                roe_val, de_val, eps_val, data_source, data_quality, fetched_at_ist = row_db
                rows.append({
                    "symbol": symbol,
                    "roe": float("nan") if roe_val is None else float(roe_val),
                    "debt_to_equity": float("nan") if de_val is None else float(de_val),
                    "eps_positive_4q": bool(eps_val) if eps_val is not None else False,
                    "data_source": data_source,
                    "data_quality": data_quality,
                    "fetched_at_ist": fetched_at_ist,
                })
            else:
                rows.append({
                    "symbol": symbol,
                    "roe": float("nan"),
                    "debt_to_equity": float("nan"),
                    "eps_positive_4q": False,
                    "data_source": "missing",
                    "data_quality": "missing",
                    "fetched_at_ist": _now_ist(),
                })
    finally:
        conn.close()

    df = pd.DataFrame(rows)

    # Enforce dtypes
    df["roe"] = df["roe"].astype("float64")
    df["debt_to_equity"] = df["debt_to_equity"].astype("float64")
    df["eps_positive_4q"] = df["eps_positive_4q"].astype(bool)

    df = df.sort_values("symbol").reset_index(drop=True)

    return df


def get_nifty_universe_for_year(year: int) -> list[str]:
    """Return NSE ticker symbols that were in the Nifty 50 for the given calendar year.

    Lazily initialises the nifty_constituents table on first call.
    Returns an empty list if the year is outside 2010-2023 — no error raised.

    Args:
        year: Calendar year (e.g. 2015). Returns empty list if outside 2010-2023.

    Returns:
        List of NSE ticker symbols sorted alphabetically. Empty list if year
        is out of range or no constituents found.

    Raises:
        FundamentalsError: If SQLite operations fail.
    """
    db_path = settings.database_url.replace("sqlite:///", "")
    conn = _init_historical_tables(db_path)

    try:
        try:
            count_row = conn.execute(
                "SELECT COUNT(*) FROM nifty_constituents"
            ).fetchone()
            count = count_row[0] if count_row else 0
        except sqlite3.Error as exc:
            raise FundamentalsError(
                f"DB read failed checking nifty_constituents count: {exc}"
            ) from exc

        if count == 0:
            _populate_nifty_constituents(conn)

        try:
            rows_db = conn.execute(
                """
                SELECT symbol FROM nifty_constituents
                WHERE year = ? AND in_index = 1
                ORDER BY symbol
                """,
                (year,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise FundamentalsError(
                f"DB read failed for nifty_constituents year={year}: {exc}"
            ) from exc

        return [row[0] for row in rows_db]

    finally:
        conn.close()
