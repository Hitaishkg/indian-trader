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

import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

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

    # --- D/E ---
    de_elem = soup.find(
        lambda tag: tag.name in ("li", "td", "span")
        and tag.string
        and (
            "Debt to equity" in tag.string
            or "Debt / Equity" in tag.string
        )
    )
    if de_elem is not None:
        try:
            value_span = de_elem.find_next("span", class_="number")
            if value_span is None:
                value_span = de_elem.find_next(
                    lambda t: t.name in ("span", "td") and t.string and t.string.strip()
                )
            if value_span is not None and value_span.string:
                raw_de = value_span.string.strip().replace(",", "")
                debt_to_equity = float(raw_de)
        except (AttributeError, ValueError):
            logger.warning(
                "Could not extract debt_to_equity from Screener.in for %s", symbol
            )
    else:
        for tag in soup.find_all("li"):
            text = tag.get_text(separator=" ", strip=True)
            if "Debt to equity" in text or "Debt / Equity" in text:
                try:
                    spans = tag.find_all("span")
                    for sp in spans:
                        raw = sp.get_text(strip=True).replace(",", "")
                        if raw:
                            debt_to_equity = float(raw)
                            break
                except (AttributeError, ValueError):
                    logger.warning(
                        "Could not extract debt_to_equity from Screener.in for %s",
                        symbol,
                    )
                break

    if debt_to_equity is None:
        logger.warning(
            "Could not extract debt_to_equity from Screener.in for %s", symbol
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
