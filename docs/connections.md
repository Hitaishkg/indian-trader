# Module Connections Reference

> Maintained by the Docs Agent. Updated automatically after every build.
> Every section is replaced (not appended) when a module is updated.
> For system overview and debugging guide, see `docs/SYSTEM.md`.

---

<!-- Docs Agent: insert new module sections below this line, alphabetically by path -->

## src/config/settings.py

**Purpose:** Single source of truth for all configuration — loads `.env` at import time, validates every required variable, and exposes a frozen `Settings` dataclass singleton used by every other module.

**Public API:**
- `class ConfigurationError(Exception)` — raised at startup if any required variable is missing or invalid; carries `errors: list[str]` with all problems found
- `class Settings` — frozen dataclass with typed fields for all 16 config variables; secrets masked in `__repr__`/`__str__`
- `load_settings(env_path: str | None = None) -> Settings` — loads `.env`, validates, coerces types, returns Settings; accepts explicit path for testing
- `settings: Settings` — module-level singleton; import via `from src.config.settings import settings`

**Reads from:** `.env` file (via python-dotenv) and `os.environ`

**Writes to:** nothing — read-only configuration loader

**Called by:** every module that needs configuration (imports `settings` singleton)

**Calls:** `python-dotenv` (dotenv.load_dotenv), `os.environ`, Python stdlib (`dataclasses`, `os`)

**Key constants / thresholds relevant to debugging:**
- Always-required variables (absent = startup failure): `PAPER_TRADING`, `MAX_TRADE_AMOUNT`, `DATABASE_URL`, `LOG_LEVEL`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `GITHUB_PAT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional with safety default: `LIVE_TRADING` → `False` (never accidentally goes live)
- Phase-gated (absent → `None`, no error): `SHOONYA_USER`, `SHOONYA_PASSWORD`, `SHOONYA_TOTP_SECRET`, `FYERS_API_KEY`, `BRAVE_API_KEY`, `GMAIL_CREDENTIALS`
- `MAX_TRADE_AMOUNT` hard cap: must be positive int ≤ 10000
- `DATABASE_URL` must start with `sqlite:///`
- `LOG_LEVEL` must be one of `DEBUG`/`INFO`/`WARNING`/`ERROR`
- Safety interlock: `LIVE_TRADING=true` + `PAPER_TRADING=true` simultaneously → `ConfigurationError`
- Secret masking: 11 fields masked with `***` in repr; phase-gated None fields shown as `None`

## src/data/cleaner.py

**Purpose:** Best-effort OHLCV data repair layer — forward-fills missing prices, removes duplicate dates, and flags anomalies (negative prices, OHLCV inconsistencies, price floor violations). Sits between fetcher and validator in the pipeline.

**Public API:**
- `clean_ohlcv(df: pd.DataFrame, price_floor: float = 1.0) -> tuple[pd.DataFrame, CleaningReport]` — cleans a normalised OHLCV DataFrame; returns cleaned copy and audit report
- `class CleaningReport` — frozen dataclass with fields: symbols_processed, rows_input, rows_output, duplicates_removed, missing_close_filled, missing_ohlv_filled, negative_price_flags, consistency_flags, price_floor_flags, cleaned_at_ist

**Reads from:** pd.DataFrame passed by caller (no external sources)

**Writes to:** nothing — returns cleaned DataFrame and CleaningReport; logging to stdout only

**Called by:** main.py (Phase 1), Data Collector Agent (Phase 4)

**Calls:** src.config.settings (log_level), pandas, Python stdlib logging/dataclasses/zoneinfo

**Key constants / thresholds relevant to debugging:**
- `PRICE_FLOOR = 1.0` — data sanity threshold (NOT the strategy's ₹50 filter — that's quality_filter.py)
- `AGENT_NAME = "cleaner"` — for future logger.py integration
- Cleaning order: schema validation → negative price flags → consistency flags → missing value fill → duplicate removal → price floor flags
- Forward-fill is scoped per symbol group — never bleeds across symbol boundaries
- Anomalous rows are NEVER dropped — only flagged in CleaningReport
- Duplicate dates: last occurrence kept (`keep="last"`), count in `duplicates_removed`
- Input DataFrame is never mutated — operates on `df.copy()`

## src/data/fetcher.py

**Purpose:** OHLCV data acquisition layer — fetches historical price data for NSE stocks and sector indices from yfinance (primary) with jugaad-data fallback, caches results to CSV in data/cache/.

**Public API:**
- `fetch_ohlcv(symbols: list[str], start_date: datetime.date, end_date: datetime.date, cache_expiry_hours: int = 24) -> pd.DataFrame` — fetches OHLCV for one or more NSE symbols; returns normalised DataFrame matching validator.py Section 5.1 contract
- `fetch_nifty50_symbols() -> list[str]` — returns hardcoded list of 50 current Nifty 50 constituents (updated 2026-03-22; update manually each quarter)
- `fetch_sector_indices(start_date: datetime.date, end_date: datetime.date, cache_expiry_hours: int = 24) -> pd.DataFrame` — fetches NIFTY_50, NIFTY_BANK, NIFTY_IT, NIFTY_AUTO, NIFTY_PHARMA, NIFTY_FMCG via yfinance only
- `class FetchError(Exception)` — raised when both yfinance and jugaad-data fail; carries `.symbol`, `.yfinance_error`, `.jugaad_error` attributes

**Reads from:** yfinance API (primary), jugaad-data NSE API (fallback), data/cache/ CSV files

**Writes to:** data/cache/{SYMBOL}_{source}.csv (CSV cache files, gitignored)

**Called by:** src/data/cleaner.py (Phase 1), Data Collector Agent at 22:00 IST (Phase 4), src/backtest/runner.py (Phase 2)

**Calls:** yfinance (yf.Ticker.history), jugaad_data.nse.stock_df, src.config.settings (log_level)

**Key constants / thresholds relevant to debugging:**
- `CACHE_DIR` — absolute path to data/cache/ derived from __file__
- `NIFTY50_SYMBOLS` — 50-element list; update manually each quarter when NSE rebalances
- `SECTOR_INDEX_MAP` — maps human-readable names to yfinance tickers (e.g. "NIFTY_IT" → "^CNXIT")
- yfinance symbols use ".NS" suffix internally; output DataFrame strips it (stores "RELIANCE" not "RELIANCE.NS")
- yfinance end date is exclusive — fetcher adds +1 day automatically
- jugaad-data fallback not available for sector indices (yfinance only for indices)
- Cache expiry default: 24 hours. Set cache_expiry_hours=0 to force fresh fetch.
- Corrupt cache files are deleted and treated as cache miss (not an error)

## src/data/validator.py

**Purpose:** Data quality gate — validates OHLCV and fundamentals DataFrames for corruption, coverage gaps, and time-series holes before any strategy logic runs.

**Public API:**
- `validate_data(ohlcv_df: pd.DataFrame, fundamentals_df: pd.DataFrame, db_path: str, trading_calendar: list[datetime.date] | None = None) -> DataQualityReport` — runs all three checks, logs to agent_logs, raises DataQualityError if universe score < 0.6
- `class DataQualityReport` — frozen dataclass with fields: per_stock_scores, universe_quality_score, failed_roe_symbols, roe_missing_symbols, de_coverage_ratio, de_coverage_low, gap_violations, checked_at_ist, universe_size
- `class DataQualityError(Exception)` — carries .universe_quality_score and .report attributes

**Reads from:** ohlcv_df and fundamentals_df DataFrames passed by caller (no direct DB or API reads)

**Writes to:** `agent_logs` table in SQLite (data/trading.db)

**Called by:** main.py (Phase 1), Data Collector Agent (Phase 4 onwards)

**Calls:** sqlite3 (stdlib), pandas, zoneinfo (stdlib)

**Key constants / thresholds relevant to debugging:**
- `ROE_MIN = -0.50`, `ROE_MAX = 2.00` — plausibility bounds (not strategy thresholds)
- `DE_COVERAGE_THRESHOLD = 0.80` — minimum fraction of universe with D/E data
- `UNIVERSE_QUALITY_THRESHOLD = 0.60` — halt threshold; pipeline stops if score drops below this
- `MAX_OHLCV_GAP_DAYS = 5` — gaps ≥ 5 consecutive trading days trigger a violation
- `AGENT_NAME = "validator"` — written to agent_logs.agent_name

**event_type values written to agent_logs:** `roe_check`, `de_coverage_check`, `data_coverage_low`, `ohlcv_gap_check`, `stock_score`, `universe_score`, `data_quality_error`