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

## src/data/fundamentals.py

**Purpose:** Fundamental data acquisition layer — scrapes ROE, D/E, quarterly EPS, and P/E from Screener.in for NSE stocks, caches as JSON in data/cache/ with 45-day expiry, falls back to yfinance after 3 consecutive Screener.in failures, and cross-validates P/E between sources.

**Public API:**
- `fetch_fundamentals(symbols: list[str], force_refresh: bool = False) -> pd.DataFrame` — returns one row per symbol with all fundamental fields; never raises on individual symbol failure (failed symbols included with NaN values)
- `get_cache_age_days(symbol: str) -> float | None` — returns cache age in days or None if no cache exists

**Reads from:** Screener.in (primary), yfinance API (fallback), data/cache/{SYMBOL}_fundamentals.json (cache)

**Writes to:** data/cache/{SYMBOL}_fundamentals.json (JSON cache, 45-day expiry, gitignored)

**Called by:** main.py (Phase 1), src/strategy/quality_filter.py (Phase 2), Screener Agent (Phase 4)

**Calls:** requests (Screener.in HTTP), beautifulsoup4 (HTML parsing), yfinance (fallback + cross-validation), src.config.settings (log_level)

**Key constants / thresholds relevant to debugging:**
- `CACHE_EXPIRY_SECONDS = 45 * 86400` — 45-day cache for quarterly fundamentals
- `MAX_STRIKES = 3` — consecutive Screener.in failures before yfinance fallback
- `PE_CROSS_VALIDATION_THRESHOLD = 0.20` — 20% P/E deviation triggers stale_data flag
- `DE_NORMALISATION_THRESHOLD = 10.0` — yfinance D/E values above this are ÷100 (percentage form)
- `data_quality` values: "clean" / "degraded" (yfinance fallback) / "stale_data" (P/E mismatch) / "fundamentals_stale" (>45 days old) / "failed"
- Stale cache (>45 days): returned with data_quality="fundamentals_stale" when fresh fetch fails — pipeline degrades gracefully rather than crashing
- Cross-validation: only runs on Screener.in data, not on yfinance fallback (would be self-referential)
- eps_positive_4q: from yfinance fallback uses trailingEps > 0 (approximation — cannot detect single negative quarter within positive trailing year)
- Screener.in scraping: 2–5 second random delay before each request; consolidated URL tried first, standalone fallback

## src/utils/logger.py

**Purpose:** Configures Python's logging framework to emit structured log records to both stderr (console) and the SQLite `agent_logs` table simultaneously.

**Public API:**
- `setup_logging(db_path: str | None = None) -> None` — configures root logger with StreamHandler + SQLiteHandler; idempotent
- `get_logger(name: str) -> logging.Logger` — thin wrapper around `logging.getLogger(name)`
- `log_agent_action(agent_name, action, level, symbol, result, data_quality_score) -> None` — direct structured write to agent_logs, bypassing LogRecord pipeline
- `SQLiteHandler(db_path: str)` — thread-safe logging.Handler subclass; public method `write_row(...)` for direct inserts

**Reads from:** `settings.database_url`, `settings.log_level` (via `src.config.settings`)

**Writes to:** `agent_logs` table in the SQLite database

**Called by:** `main.py` (calls `setup_logging()` at startup); all modules that use `logging.getLogger(__name__)` flow through it automatically after setup

**Calls:** `src.config.settings` only

**Key constants/thresholds:** `_VALID_LEVELS = frozenset({"DEBUG","INFO","WARNING","ERROR","CRITICAL"})`. WAL mode pragmas applied on first DB connection. SQLiteHandler is thread-safe via `threading.Lock`.

## src/utils/notifier.py

**Purpose:** Dual-channel notification delivery — Telegram Bot API and Gmail API. Routes ALERT and CHECKPOINT to both channels; INFO to Telegram only. Phase-gated: skips channels gracefully when credentials are absent.

**Public API:**
- `send_alert(subject: str, message: str) -> dict[str, bool]` — sends to both Telegram and Gmail; logs CRITICAL if both fail
- `send_checkpoint(subject: str, message: str) -> dict[str, bool]` — sends to both channels; for human trade approval requests
- `send_info(message: str) -> dict[str, bool]` — Telegram only; gmail key is always False
- `NotificationType` enum: ALERT, CHECKPOINT, INFO

**Reads from:** `settings.telegram_bot_token`, `settings.telegram_chat_id`, `settings.gmail_credentials` (via `src.config.settings`); `token.json` at project root (OAuth token)

**Writes to:** `agent_logs` table (via `log_agent_action()`); `token.json` at project root (on first Gmail OAuth authorization)

**Called by:** All trading agents that need to notify the user (Execution Agent, Monitor Agent, Watchlist Builder, Reporter Agent — Phase 3/4). `main.py` in dry-run mode.

**Calls:** `src.config.settings`, `src.utils.logger.log_agent_action()`, Telegram Bot API (`api.telegram.org`), Gmail API (`googleapis.com`)

**Key constants/thresholds:** `_TELEGRAM_TIMEOUT = 10s`, `_TELEGRAM_MAX_LENGTH = 4096` chars (truncated). Gmail OAuth scopes: `gmail.send` only. Google library imports are lazy (inside `_build_gmail_service` only). `_gmail_service_cache` module-level — service object cached after first successful auth.

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

## src/indicators/technical.py

**Purpose:** Pure calculation module computing RSI, MACD, Bollinger Bands, and ATR on cleaned OHLCV data using pandas-ta. Per-symbol isolation via explicit for-loop to avoid pandas 3.0 groupby.apply key column loss. Symbols with fewer than 26 rows (MINIMUM_LOOKBACK) return all-NaN indicators.

**Public API**
- `add_indicators(df: pd.DataFrame, rsi_period: int = 14, macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9, bb_length: int = 20, bb_std: float = 2.0, atr_period: int = 14) -> pd.DataFrame` — appends 8 indicator columns (rsi, macd, macd_signal, macd_hist, bb_upper, bb_mid, bb_lower, atr) to a copy of input DataFrame; per-symbol via explicit for-loop; symbols with < 26 rows get all NaN
- `compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series` — standalone Wilder-smoothed ATR for a single-symbol DataFrame; raises ValueError if high/low/close missing

**Reads from**
- Input DataFrame (memory): cleaned OHLCV from clean_ohlcv(); expected columns: symbol, date, open, high, low, close, volume

**Writes to**
- Nothing — pure in-memory calculation, no DB writes, no file writes, no network calls

**Called by**
- main.py: compute_atr_series() for position sizing calculations in Phase 1 dry-run
- signal_agent.py (Phase 3): add_indicators() for technical signal generation in morning pipeline

**Calls**
- pandas-ta: ta.rsi(), ta.macd(), ta.bbands(), ta.atr()

**Key constants / thresholds**
- `MINIMUM_LOOKBACK = 26` — symbols with fewer rows get all-NaN indicators (equals MACD slow period); prevents unreliable indicator values on thin data
- `RSI_PERIOD = 14`, `MACD_FAST = 12`, `MACD_SLOW = 26`, `MACD_SIGNAL_PERIOD = 9`
- `BB_LENGTH = 20`, `BB_STD = 2.0`, `ATR_PERIOD = 14`
- ATR uses Wilder smoothing via pandas-ta (RMA with ewm(com=period-1, adjust=False) and SMA initialisation for first N periods)
- Bollinger Bands column naming varies by pandas-ta version; fixed via prefix matching (BBL_, BBM_, BBU_ prefixes)

## src/execution/paper_trader.py

**Purpose:** Simulated CNC swing trade execution engine with SQLite-backed orders, positions, and trades tables; GTT stop-loss and take-profit simulation without any broker API calls.

**Public API:**
- `class PaperTrader` — execution simulator for paper trading phases; raises ValueError at construction if settings.live_trading is True
- `PaperTrader.__init__(db_path: str | None = None) -> None` — opens SQLite connection with WAL pragmas, creates orders/trades/positions tables, logs to agent_logs
- `PaperTrader.place_order(symbol: str, side: str, quantity: int, entry_price: float, stop_loss: float, take_profit: float) -> int` — writes PENDING order BEFORE execution, creates/closes positions, returns orders.id
- `PaperTrader.close_position(symbol: str, exit_price: float, exit_reason: str) -> int` — closes open position, writes completed trade, exit_reason in {STOP_LOSS, TAKE_PROFIT, MANUAL_EXIT, REGIME_TIGHTENED}, returns trades.id
- `PaperTrader.get_positions() -> list[dict[str, object]]` — returns all open positions from positions table as list of dicts
- `PaperTrader.get_pnl() -> dict[str, float]` — returns {realized_pnl, unrealized_pnl, total_pnl, trade_count, win_count, loss_count}
- `PaperTrader.check_gtts(current_prices: dict[str, float]) -> list[dict[str, object]]` — checks stop-losses and take-profits, triggers close_position on hit, returns list of triggered dicts
- `PaperTrader.update_stop_loss(symbol: str, new_stop_loss: float) -> None` — updates stop-loss in positions table (used by Monitor Agent for regime/LLM tightening)

**Reads from:** positions, orders, trades tables in SQLite (state persistence, reads only on close_position and GTT checks)

**Writes to:** orders, positions, trades tables in SQLite; agent_logs table (via log_agent_action)

**Called by:** main.py (Phase 1 dry-run), Execution Agent (Phase 4), Monitor Agent (Phase 4 GTT reconciliation)

**Calls:** src.config.settings (LIVE_TRADING gate, MAX_TRADE_AMOUNT), src.utils.logger.log_agent_action()

**Key constants / thresholds relevant to debugging:**
- `LIVE_TRADING=false` — gate enforced at construction; raises ValueError if true
- `MAX_TRADE_AMOUNT` — hard cap checked in place_order; order_value = entry_price × quantity must be ≤ max_trade_amount
- `_VALID_SIDES` frozenset: {"BUY", "SELL"}
- `_VALID_EXIT_REASONS` frozenset: {"STOP_LOSS", "TAKE_PROFIT", "MANUAL_EXIT", "REGIME_TIGHTENED"}
- All prices in INR as float; all quantities as int. No fractional shares.
- All timestamps IST (Asia/Kolkata) in ISO 8601 format with timezone offset
- BUY orders: stop_loss must be < entry_price, take_profit must be > entry_price
- SELL orders: stop_loss must be > entry_price, take_profit must be < entry_price
- GTT simulation: check_gtts() is called with current_prices dict; stops checked before take-profits (conservative)
- Every order written to orders table BEFORE simulating execution (not after)
- WAL mode pragmas applied at __init__ time (same as logger.py)

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