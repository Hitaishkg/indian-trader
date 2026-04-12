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

### Historical Additions (backtest support)

**Public API:**
- `class FundamentalsError(Exception)` — raised on DB or network failures during historical fetch
- `fetch_historical_fundamentals(symbols: list[str], force_refresh: bool = False) -> None` — scrapes Screener.in quarterly data (all available history), stores to fundamentals_history table in SQLite; no return value (side-effect write only)
- `get_fundamentals_for_date(symbols: list[str], as_of_date: datetime.date) -> pd.DataFrame` — point-in-time historical fundamentals; fiscal year lookup: month <= 6 uses prior FY results, month >= 7 uses current FY results
- `get_nifty_universe_for_year(year: int) -> list[str]` — returns list of Nifty 50 constituents that were in the index during that year (from nifty_constituents table)
- `NIFTY_CONSTITUENTS_BY_SYMBOL` dict — hardcoded 67-symbol Nifty 50 constituent list with fiscal year entry dates (covers 2010–2023 all known entries)

**Reads from:** Screener.in (quarterly history scraping), SQLite fundamentals_history table (point-in-time lookups)

**Writes to:** fundamentals_history table (all quarterly fundamentals for all symbols), nifty_constituents table (Nifty membership by fiscal year)

**Called by:** src/backtest/runner.py (Phase 2, step 5+)

**Calls:** requests (Screener.in HTTP), sqlite3 (stdlib), pandas (DataFrame construction), zoneinfo (IST timestamps)

**Key constants / thresholds relevant to debugging:**
- `NIFTY_CONSTITUENTS_BY_SYMBOL` — 67 symbols covering entry dates from 2010 to 2023; manually updated when NSE rebalances
- Fiscal year cutoff: month >= 7 → use current FY (FY=year), month <= 6 → use prior FY (FY=year-1). July chosen because BSE publishes FY results between May and July (not safe for point-in-time lookups before July)
- `eps_positive_4q` column: maps annual EPS > 0 to positive four-quarter EPS flag for schema compatibility with validator.py
- `FundamentalsError` is local exception (not imported from validator.py — incompatible signature with DataQualityError)
- Historical fetch may take 5–10 seconds per symbol due to Screener.in delays (2–5 second inter-request pause)

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

## src/strategy/regime.py

**Purpose:** Nifty 50 200-day SMA regime filter. Determines market regime, adjusts position sizing for new trades, and signals which open positions need stop-loss tightening. Final strategy filter before backtest integration.

**Public API:**
- `apply_regime_filter(ranked_df, nifty_ohlcv_df, open_positions=None) -> tuple[pd.DataFrame, RegimeResult]`
- `class RegimeResult` (frozen dataclass) — regime, nifty_close, sma_200, consecutive_days_below, position_size_multiplier, tighten_stops, stop_tighten_symbols, computed_at_ist
- `compute_200dma(nifty_ohlcv_df) -> float`
- `count_consecutive_days_below_200dma(nifty_ohlcv_df) -> int`

**Reads from**
- ranked_df (memory): from compute_momentum()
- nifty_ohlcv_df (memory): date + close columns — get via fetch_sector_indices() filtering NIFTY_50
- open_positions (memory): list of dicts from PaperTrader.get_positions()

**Writes to**
- Nothing — pure computation. Only agent_logs via log_agent_action().

**Called by**
- backtest/runner.py (Phase 2)
- screener_agent.py (Phase 3)
- main.py: dry-run pipeline

**Calls**
- src/utils/logger.py: log_agent_action()

**Key constants / thresholds**
- `SMA_PERIOD = 200` — 200-day SMA window
- `BELOW_DMA_BLOCK_DAYS = 10` — 10+ consecutive days below → no new positions
- Multipliers: ABOVE=1.0, BELOW=0.5, BLOCKED=0.0
- Boundary: close == SMA → ABOVE_200DMA (>= comparison)
- Does NOT call PaperTrader.update_stop_loss() — caller handles stop execution

## src/strategy/momentum.py

**Purpose:** Computes the 12-1 momentum factor (12-month total return minus 1-month total return) for quality-filtered symbols and selects the top N weekly candidates. Applies a 2% adjacent-pair tiebreaker using 52-week high proximity.

**Public API:**
- `compute_momentum(quality_df, ohlcv_df, top_n=5) -> tuple[pd.DataFrame, MomentumReport]` — scores all quality-filtered symbols; returns ranked_df (top N) and MomentumReport
- `class MomentumReport` (frozen dataclass) — scored_count, selected_count, insufficient_history_count, tiebreaker_applied_count, computed_at_ist

**Reads from**
- quality_df (memory): symbol, pct_from_52w_high, within_30pct_of_52w_high — from apply_quality_filter()
- ohlcv_df (memory): symbol, date, close — full OHLCV history from fetch_ohlcv()

**Writes to**
- Nothing — pure calculation. Only agent_logs via log_agent_action().

**Called by**
- screener_agent.py (Phase 3): weekly Monday run
- backtest/runner.py (Phase 2): historical backtest
- main.py: dry-run pipeline

**Calls**
- src/utils/logger.py: log_agent_action()

**Key constants / thresholds**
- `TWELVE_MONTH_LOOKBACK = 252` — trading days for 12-month return
- `ONE_MONTH_LOOKBACK = 21` — trading days for 1-month return
- `DEFAULT_TOP_N = 5` — number of candidates to select
- `TIEBREAKER_THRESHOLD = 0.02` — 2% relative diff triggers tiebreaker
- Symbols with <252 rows are excluded and logged as insufficient_history

## src/strategy/quality_filter.py

**Purpose:** Quality filter applying five hard filters to stock universe. All filters must pass; failure on any one disqualifies the stock entirely. Detects thin universes (fewer than 3 passing stocks) and returns empty DataFrame.

**Public API**
- `apply_quality_filter(fundamentals_df, ohlcv_df, lookback_days=252) -> tuple[pd.DataFrame, FilterReport]` — filters universe to stocks passing all 5 hard filters; returns passing stocks DataFrame + FilterReport
- `class FilterReport` (frozen dataclass) — universe_size, passed_count, failed_count, thin_universe (bool), filter_failure_counts (dict), filtered_at_ist (str)

**Reads from**
- fundamentals_df (memory): symbol, roe, debt_to_equity, eps_positive_4q, data_quality — from fetch_fundamentals()
- ohlcv_df (memory): symbol, date, close, volume — from fetch_ohlcv() / clean_ohlcv()

**Writes to**
- Nothing — pure calculation. Only agent_logs via log_agent_action().

**Called by**
- screener_agent.py (Phase 3): weekly Monday run
- backtest/runner.py (Phase 2): historical backtest
- main.py: dry-run pipeline

**Calls**
- src/utils/logger.py: log_agent_action()

**Key constants / thresholds**
- `ROE_THRESHOLD = 0.15` — ROE must exceed 15%
- `DE_THRESHOLD = 1.0` — Debt/equity must be below 1.0
- `VOLUME_VALUE_THRESHOLD = 20_000_000` — avg daily traded value > ₹20 crore
- `PRICE_THRESHOLD = 50.0` — latest close must exceed ₹50
- `PROXIMITY_THRESHOLD = 0.30` — soft: within 30% of 52-week high (never eliminates)
- `MIN_UNIVERSE_SIZE = 3` — fewer than 3 passing → thin_universe=True, return empty DataFrame

## src/backtest/runner.py

**Purpose:** backtesting.py wrapper that runs quality_filter → momentum → regime pipeline over 2010–2023 historical data; returns BacktestResult with metrics for gate evaluation; solves single-symbol limitation by using Nifty 50 index as dummy instrument with multi-stock portfolio tracking in _PortfolioTracker class.

**Public API**
- `run_backtest(start_date: datetime.date, end_date: datetime.date, initial_cash: float = 10_000.0) -> BacktestResult` — runs full three-step strategy over historical date range; returns BacktestResult with gates_passed=False; raises ValueError on invalid inputs, BacktestError on data/simulation failures
- `class BacktestResult` — frozen dataclass with fields: start_date, end_date, total_return_pct (float), annualized_return_pct (float), sharpe_ratio (float), max_drawdown_pct (float, positive), win_rate_pct (float), total_trades (int), profit_factor (float, inf if zero losses with wins, 0.0 if no wins), regime_changes (int), regime_blocked_weeks (int), raw_stats (dict), gates_passed (bool, always False from runner)
- `class BacktestError(Exception)` — raised on fatal backtest errors; attributes: message (str), phase (str: "data_fetch", "strategy_init", "simulation", "stats_extraction")

**Reads from:**
- fundamentals_history table (via get_fundamentals_for_date): quarterly ROE, D/E, EPS data for point-in-time lookups with no lookahead bias
- nifty_constituents table (via get_nifty_universe_for_year): Nifty 50 membership by calendar year (2010–2023)
- yfinance + jugaad-data (via fetch_ohlcv): historical OHLCV for all Nifty 50 constituents over backtest window

**Writes to:**
- Nothing — returns BacktestResult in memory only; does not write to database

**Called by:**
- src/backtest/validator.py (Phase 2, step 6): reads BacktestResult, checks all 5 backtest gates (Sharpe, drawdown, win rate, trade count, profit factor), sets gates_passed=True if all pass

**Calls:**
- fetch_ohlcv(), fetch_sector_indices() (src/data/fetcher.py): historical OHLCV for stocks and Nifty 50 index
- get_fundamentals_for_date(), get_nifty_universe_for_year() (src/data/fundamentals.py): point-in-time fundamentals and constituent universe
- apply_quality_filter() (src/strategy/quality_filter.py): filters universe by 5 hard criteria
- compute_momentum() (src/strategy/momentum.py): 12-1 momentum scores, top-5 selection
- apply_regime_filter() (src/strategy/regime.py): Nifty 50 200 DMA regime filter
- compute_atr_series() (src/indicators/technical.py): ATR for stop-loss calculation
- log_agent_action() (src/utils/logger.py): logs to agent_logs

**Key constants / thresholds:**
- `AGENT_NAME = "backtest_runner"` — for agent_logs
- `RISK_PER_TRADE = 0.01` — 1% of current balance per trade
- `MAX_POSITIONS = 2` — maximum 2 simultaneous open positions
- `MAX_POSITION_PCT = 0.40` — hard cap at 40% of total capital per position
- `MAX_TRADE_AMOUNT = 10_000.0` — never execute single trade > ₹10,000 notional
- `STOP_LOSS_ATR_NORMAL = 2.0` — normal regime: stop-loss at 2× ATR below entry
- `STOP_LOSS_ATR_TIGHT = 1.0` — tight regime: stop-loss at 1× ATR below entry
- `STOP_LOSS_MAX_PCT = 0.03` — hard cap: never tighten stop-loss more than 3% from entry
- `TAKE_PROFIT_RATIO = 2.0` — take-profit at 2× stop-loss distance (minimum 1:2 risk-reward)
- `ATR_PERIOD = 14` — Wilder-smoothed ATR for stop calculation
- `MIN_BACKTEST_START = date(2010, 1, 1)` — earliest allowed backtest start
- `MAX_BACKTEST_END = date(2023, 12, 31)` — latest allowed backtest end (historical data coverage)
- `LOOKBACK_CALENDAR_DAYS = 400` — warm-up period (no trades for first ~400 calendar days to build indicator history); prevents trading on insufficient data
- Weekly rebalance anchor: uses (iso_year, iso_week) tuple not iso_week alone — handles Diwali week and multi-day holiday blocks correctly
- Weekend bar guard: skips Saturday NSE non-trading sessions to prevent anomalous price data

## src/backtest/validator.py — Pure computation gate checker

**Purpose:** Evaluates BacktestResult against 5 required gates from risk.md. Only module that sets gates_passed=True. Returns ValidationResult with per-gate breakdown and final verdict. Contains zero business logic beyond gate checking.

**Public API**
- `validate_backtest(result: BacktestResult) -> ValidationResult` — evaluates result against all 5 gates; returns ValidationResult; raises ValueError if total_trades < 0 (corrupt input)
- `class GateResult(frozen)` — gate_name, threshold, actual_value, passed (bool)
- `class ValidationResult(frozen)` — all_gates_passed, gate_results (tuple of 5 GateResult), validated_result (BacktestResult with gates_passed=True if all pass, else unchanged)

**Reads from:** BacktestResult passed by caller (in-memory; no DB or external source reads)

**Writes to:** agent_logs table (via log_agent_action only; one entry on completion)

**Called by:** main.py / Phase 2 validation run (post-backtest)

**Calls:** src/utils/logger.log_agent_action()

**Key constants / thresholds**
- `SHARPE_THRESHOLD = 1.0` — gate: sharpe_ratio > 1.0
- `MAX_DRAWDOWN_THRESHOLD = 15.0` — gate: max_drawdown_pct < 15.0
- `WIN_RATE_THRESHOLD = 40.0` — gate: win_rate_pct > 40.0
- `MIN_TRADES_THRESHOLD = 100` — gate: total_trades >= 100 (only gate using >= not >)
- `PROFIT_FACTOR_THRESHOLD = 1.3` — gate: profit_factor > 1.3 (handles float('inf') naturally)
- `AGENT_NAME = "backtest_validator"` — for agent_logs

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

## src/agents/watchlist_agent.py

**Purpose**: Final watchlist builder — reads top 5 screener candidates + LLM research, applies combined decision rule (position_size_multiplier == 0.0 → SKIP; negative sentiment → SKIP; else PROCEED), computes partial pre-trade scorecard (max 20 pts watchlist level), writes all candidates (PROCEED and SKIP) to watchlist table for full audit trail, sends human approval checkpoint via Telegram + Gmail, handles timeout at 07:00 IST.

**Public API:**
- `class WatchlistAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: "db_read" / "db_write" / "notification" / "timeout_check")
- `class WatchlistCandidate` — frozen dataclass (intermediate, not persisted): symbol (str), rank (int, 1=highest), momentum_score (float), regime (str), position_size_multiplier (float), sentiment (str), confidence (float), earnings_transcript_unavailable (bool), combined_decision (str: "PROCEED" / "SKIP"), skip_reason (str | None), scorecard_score (int), scorecard_max (int)
- `class WatchlistEntry` — frozen dataclass (written to watchlist table): symbol, combined_decision, scorecard_score, scorecard_max, sentiment, confidence, rank, regime, position_size_multiplier, human_approved (int 0/1), approval_source (str | None), added_at (IST datetime), run_date (date)
- `class WatchlistAgentResult` — frozen dataclass: run_date (date), candidates_evaluated (int), proceed_count (int), skipped_count (int), approved_symbols (list[str]), human_responded (bool), completed_at (IST datetime)
- `run_watchlist_agent(run_date: datetime.date | None = None) -> WatchlistAgentResult` — reads screener_results + research_reports for run_date, applies combined decision rule, writes all to watchlist table, sends checkpoint notification, returns immediately (non-blocking); raises WatchlistAgentError on DB or notification failure
- `check_watchlist_timeout(run_date: datetime.date) -> None` — called by orchestrator at 07:00 IST; marks all pending approvals (human_approved=0, approval_source IS NULL) as approval_source='timeout_skip'; sends alert if any rows timed out; raises WatchlistAgentError(phase='timeout_check') on DB failure
- `record_human_approval(symbol: str, run_date: datetime.date, approved: bool) -> None` — records human decision from Telegram; sets human_approved=1 if approved, human_approved=0 if rejected; sets approval_source='human_explicit'; no-op with WARNING if symbol not found; never raises

**Reads from:**
- screener_results table: quality_passed=1 rows for run_date (all top-N candidates)
- research_reports table: sentiment, confidence, source_urls, earnings_transcript_unavailable per symbol; filters by completed_at IS NOT NULL for run_date (no race conditions); if multiple rows per symbol, uses most recent (ORDER BY completed_at DESC LIMIT 1)

**Writes to:**
- watchlist table: one row per evaluated candidate (both PROCEED and SKIP); UNIQUE constraint on (symbol, run_date); approval_source remains NULL until check_watchlist_timeout or record_human_approval called

**Called by:**
- orchestrator.py at 23:30 IST every Monday (Phase 3 evening pipeline)
- orchestrator.py calls check_watchlist_timeout() at 07:00 IST
- orchestrator.py calls record_human_approval() when Telegram reply received

**Calls:**
- sqlite3 (stdlib): reads screener_results and research_reports, writes watchlist table
- log_agent_action() (src/utils/logger.py): agent_logs
- send_checkpoint() (src/utils/notifier.py): Telegram + Gmail approval request
- send_alert() (src/utils/notifier.py): alerts on thin_universe or timeout
- send_info() (src/utils/notifier.py): info-level notifications

**Key constants / thresholds relevant to debugging:**
- `APPROVAL_DEADLINE_HOUR = 7`, `APPROVAL_DEADLINE_MINUTE = 0` — 07:00 IST hard deadline for human approval; check_watchlist_timeout() called by orchestrator at this time
- `SCORECARD_THRESHOLD = 28` — minimum score required for risk_agent approval; documented here, NOT enforced at watchlist stage (enforced by risk_agent on full scorecard in Phase 4)
- `SCORECARD_MAX_FULL = 40` — full scorecard max (all 8 criteria; Phase 4 risk_agent stage)
- `SCORECARD_MAX_FULL_NO_EARNINGS = 35` — full scorecard max when earnings upcoming (no earnings criterion)
- `SCORECARD_MAX_WATCHLIST = 20` — watchlist stage partial scorecard max (4 criteria only)
- `SCORECARD_MAX_WATCHLIST_NO_EARNINGS = 15` — watchlist stage max when earnings flag set
- Watchlist scorecard breakdown (max 20 → 15 with earnings flag): quality(always 5) + rank(5 if rank≤3 else 0) + regime(ABOVE_200DMA=5, BELOW_200DMA=2, BELOW_200DMA_10DAYS=0) + sentiment(Positive=5, Neutral=3, Mixed=1, Negative=0)
- Combined decision rule: if position_size_multiplier==0.0 (regime blocked) → SKIP with skip_reason="regime_blocked"; else if sentiment=="Negative" → SKIP with skip_reason="negative_sentiment"; else → PROCEED
- Mixed sentiment → PROCEED with 1 scorecard point (not skipped; only pure Negative skips)
- Both PROCEED and SKIP candidates written to watchlist table for audit trail (enables full trace of what was evaluated, not just what passed)
- research_reports filtered by run_date column value, NOT by DATE(completed_at) — robust across midnight boundaries and handles same-day reruns

---

## src/agents/screener_agent.py

**Purpose:** Runs the 3-step weekly selection pipeline (quality filter → momentum ranking → regime filter). Writes top 5 candidates to screener_results table. Runs every Monday at 22:00 IST; also callable standalone for Phase 4 emergency rescreens.

**Public API:**
- `run_screener_agent(run_date=None) -> ScreenerAgentResult` — executes full screener pipeline; run_date defaults to today; returns frozen ScreenerAgentResult dataclass; raises ScreenerAgentError on fatal errors
- `class ScreenerResult(frozen)` — symbol (str), rank (int, 1=highest), momentum_score (float), quality_passed (bool), regime (str in {"ABOVE_200DMA", "BELOW_200DMA", "BELOW_200DMA_10DAYS"}), position_size_multiplier (float in {1.0, 0.5, 0.0}), screened_at (datetime.datetime in IST), run_date (datetime.date)
- `class ScreenerAgentResult(frozen)` — run_date (datetime.date), symbols_screened (int, total Nifty 50 universe size), symbols_passed_quality (int), top5 (list[ScreenerResult], empty when thin_universe or regime_blocked), thin_universe (bool), regime_blocked (bool), completed_at (datetime.datetime in IST)
- `class ScreenerAgentError(Exception)` — message (str), phase (str in {"db_write", "ohlcv_fetch", "fundamentals_fetch", "quality_filter", "momentum", "regime"})

**Reads from:**
- fetch_ohlcv(): 400-day lookback OHLCV for all Nifty 50 symbols
- fetch_sector_indices(): Nifty 50 index OHLCV for regime filter 200 DMA
- get_nifty_universe_for_year() / fetch_nifty50_symbols() fallback: complete Nifty 50 constituent list
- get_fundamentals_for_date(): ROE, D/E, EPS, volume, price (no DB reads)

**Writes to:**
- screener_results table: INSERT OR REPLACE on UNIQUE(symbol, run_date); one row per top-5 candidate. regime_blocked and thin_universe still write top5 with position_size_multiplier=0.0 (Watchlist Builder decides whether to trade)

**Called by:**
- orchestrator.py at 22:00 IST every Monday (Phase 3 evening pipeline)
- monitor_agent.py for Phase 4 emergency intraweek rescreen (if Nifty drops > 3% close-to-close)

**Calls:**
- apply_quality_filter() → returns quality-filtered universe or empty set if thin_universe
- compute_momentum() → ranks top N by 12-1 momentum score
- apply_regime_filter() → applies Nifty 200 DMA regime, adds position_size_multiplier
- fetch_ohlcv() → 400-day OHLCV
- fetch_sector_indices() → Nifty 50 for regime check
- get_nifty_universe_for_year() / fetch_nifty50_symbols() → universe size
- get_fundamentals_for_date() → fundamentals
- log_agent_action() → agent_logs
- send_info() → info-level notifications
- send_alert() → alert-level notifications (on thin_universe)

**Key constants / thresholds:**
- `OHLCV_LOOKBACK_DAYS = 400` — calendar days of price history required
- `MIN_UNIVERSE_SIZE = 3` — minimum stocks that must pass quality filter; if fewer → thin_universe=True, empty top5, send_alert()
- `MAX_TOP_N = 5` — top 5 momentum-ranked candidates written to screener_results
- `AGENT_NAME = "screener_agent"` — for agent_logs
- thin_universe: when fewer than 3 stocks pass all 5 quality filters; system returns early with empty top5 and sends alert
- regime_blocked: when BELOW_200DMA_10DAYS (10+ consecutive days below 200 DMA); position_size_multiplier=0.0, but top5 still written so Watchlist Builder can assess market state
- INSERT OR REPLACE on UNIQUE(symbol, run_date): emergency rescreen on same date overwrites Monday run; most recent run is always authoritative

---

## src/agents/research_agent.py

**Purpose:** Evening pipeline agent (22:40 IST) — fetches news via Tavily Search API for top-5 screener candidates, synthesises sentiment via Gemini 2.5 Flash, writes results to research_reports table with race-condition-safe two-step DB write (INSERT then UPDATE completed_at).

**Public API:**
- `run_research_agent(run_date=None, symbols=None) -> ResearchAgentResult` — executes research pipeline; run_date defaults to today; symbols filters candidates (for testing); returns frozen ResearchAgentResult dataclass
- `class StockResearch(frozen)` — symbol (str), sentiment (str in {"Positive", "Negative", "Neutral", "Mixed"}), confidence (float 0.0–1.0), source_urls (list[str]), earnings_transcript_unavailable (bool), completed_at (datetime.datetime in IST)
- `class ResearchAgentResult(frozen)` — run_date (datetime.date), stocks_researched (int), results (list[StockResearch]), skipped_symbols (list[str]), completed_at (datetime.datetime in IST)
- `class ResearchAgentError(Exception)` — message (str), phase (str in {"db_read", "tavily_search", "gemini", "db_write"})

**Reads from:**
- screener_results table: top 5 candidates by momentum rank
- Tavily Search API (tavily-python SDK): real-time news with published_date per result
- Gemini 2.5 Flash API: sentiment synthesis from fetched news

**Writes to:**
- research_reports table: INSERT row with symbol, sentiment, confidence, source_urls, earnings_transcript_unavailable, completed_at=NULL, then UPDATE SET completed_at=datetime.now() AFTER all fields confirmed (prevents race condition where Watchlist Builder reads incomplete rows)

**Called by:**
- evening pipeline orchestrator at 22:40 IST (Phase 3, step 1)

**Calls:**
- Tavily Search API (tavily-python SDK; topic=news/finance, published_date parsed as ISO string)
- Gemini 2.5 Flash (google-genai SDK)
- src.utils.logger.log_agent_action()
- sqlite3 (stdlib)

**Key constants / thresholds:**
- `TAVILY_REQUEST_DELAY = 0.5` — seconds between Tavily Search queries (lower rate-limit requirement vs Brave)
- `MAX_SYMBOLS = 5` — processes only top 5 from screener_results
- `EARNINGS_LOOKBACK_DAYS = 5` — if earnings reported within last 5 days (checked via published_date ISO parse), switch to earnings transcript analysis
- `TRANSCRIPT_MIN_CHARS = 200` — minimum length for parseable earnings transcript; if shorter or unavailable → fall back to standard news synthesis
- `AGENT_NAME = "research_agent"` — for agent_logs
- Sentiment values: "Positive", "Negative", "Neutral", "Mixed" (exactly as returned by Gemini)
- Tavily published_date field: ISO 8601 string (replaces Brave Search fragile age-string heuristics for earnings detection)

## src/agents/signal_agent.py

**Purpose:** Morning pipeline agent (08:20 IST) — reads top screener candidates, fetches fresh 60-day OHLCV, computes RSI/MACD/Bollinger Bands/ATR, applies the combined decision rule (technical + sentiment + Groq advisory check), and writes all signals (BUY and HOLD) to the signals table for full audit trail.

**Public API:**
- `run_signal_agent(run_date: datetime.date | None = None, symbols: list[str] | None = None) -> SignalAgentResult` — executes the morning signal pipeline; run_date defaults to today IST; symbols overrides screener_results read (for testing); returns SignalAgentResult with late_start=True and empty signal lists if called after 08:50 IST; raises SignalAgentError on fatal DB or OHLCV failures; Groq/Gemini LLM failures handled gracefully
- `class StockSignal` — frozen dataclass: symbol (str), rsi (float), macd_signal (str: "BUY"/"HOLD"), bollinger_position (str: "ABOVE"/"MIDDLE"/"BELOW"), atr (float), groq_confidence (float 0.0–1.0; -1.0 sentinel when LLM unavailable), signal_type (str: "BUY"/"HOLD"), skip_reason (str | None — populated for HOLD, None for BUY), signalled_at (IST timezone-aware datetime)
- `class SignalAgentResult` — frozen dataclass: run_date (date), symbols_processed (int), buy_signals (list[StockSignal]), hold_signals (list[StockSignal]), late_start (bool), completed_at (IST datetime)
- `class SignalAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: "db_read" / "ohlcv_fetch" / "db_write")

**Reads from:**
- screener_results table: top 5 candidates by momentum rank for today's run_date
- research_reports table: sentiment + confidence per symbol (requires completed_at IS NOT NULL from today's run)
- yfinance + jugaad-data (via fetch_ohlcv): 60 calendar days of OHLCV for technical indicator calculation
- Groq API (requests.post to GROQ_API_ENDPOINT): morning confidence check on evening thesis vs technical indicators
- Gemini 2.5 Flash (google-genai SDK): fallback LLM if Groq fails or rate-limits

**Writes to:**
- signals table: one row per processed symbol with rsi, macd_signal, bollinger_position, atr, groq_confidence, signal_type, skip_reason, signalled_at, run_date; written for all symbols (both BUY and HOLD) for full audit trail

**Called by:**
- Morning pipeline orchestrator at 08:20 IST (Phase 4, Step 6)

**Calls:**
- `fetch_ohlcv()` (src/data/fetcher.py): fresh 60-day OHLCV per symbol
- `add_indicators()` (src/indicators/technical.py): RSI, MACD, Bollinger Bands, ATR on the fetched OHLCV
- Groq API via `requests.post()` — no SDK, direct HTTP; model: llama-3.3-70b-versatile
- Gemini 2.5 Flash via google-genai SDK — fallback when Groq fails
- `log_agent_action()` (src/utils/logger.py): all actions logged to agent_logs
- sqlite3 (stdlib): reads screener_results and research_reports, writes signals table

**Key constants / thresholds relevant to debugging:**
- `RSI_BUY_THRESHOLD = 40.0` — RSI < 40 triggers technical BUY signal; intentionally conservative, most days produce 0 BUY signals (correct behavior)
- `OHLCV_LOOKBACK_DAYS = 60` — calendar days of OHLCV fetched for indicator computation
- `GROQ_MODEL = "llama-3.3-70b-versatile"` — Groq model for advisory check
- `GROQ_API_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"` — direct HTTP (no SDK)
- `GROQ_TIMEOUT_SECONDS = 15` — timeout per Groq request
- `GROQ_CONFIDENCE_THRESHOLD = 0.6` — Groq confidence below this → downgrade BUY to HOLD; logged as groq_low_confidence
- `GEMINI_MODEL = "gemini-2.5-flash"` — Gemini fallback model
- `LLM_UNAVAILABLE_SENTINEL = -1.0` — groq_confidence stored as -1.0 when both LLMs fail; rule-based BUY kept (not skipped)
- `MAX_SYMBOLS = 5` — processes at most 5 symbols per run
- `DEADLINE_HOUR = 8`, `DEADLINE_MINUTE = 50` — hard 08:50 IST deadline; late_start=True triggers safe mode in orchestrator; no DB writes on late start
- Combined decision rule: technical BUY fires when rsi < 40.0 AND macd_hist > 0; blocked if research sentiment = "Negative"; Groq advisory: confidence < 0.6 → downgrade to HOLD; both LLMs failing → keep rule-based BUY, groq_confidence = -1.0
- Failure modes to check in agent_logs: `late_start`, `groq_low_confidence`, `llm_unavailable`, `negative_sentiment`, `ohlcv_fetch_failed`, `no_screener_results`

---

## src/agents/risk_agent.py

**Purpose:** Gatekeeper between human-approved watchlist and execution — runs all four kill switches (drawdown, consecutive losses, win rate, Sharpe), sizes positions using 1%-ATR formula, writes APPROVED/REJECTED decisions to risk_approvals table.

**Public API:**
- `run_risk_agent(run_date: datetime.date | None = None, db_path_override: str | None = None) -> RiskAgentResult` — reads human_approved=1 watchlist rows for run_date, runs all kill switch checks, sizes each symbol, writes results to risk_approvals; raises RiskAgentError on DB/PaperTrader failures
- `class RiskAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'db_write', 'paper_trader_init')
- `class RiskApproval` (frozen dataclass) — symbol, run_date (date), quantity (int), entry_price_approx (float), stop_loss (float), take_profit (float), position_size_multiplier (float), risk_amount (float), approval_status (str: 'APPROVED'/'REJECTED'), rejection_reason (str | None), approved_at (IST datetime)
- `class RiskAgentResult` (frozen dataclass) — run_date (date), kill_switch_fired (bool), kill_switch_reason (str | None), approved (list[RiskApproval]), rejected (list[RiskApproval]), portfolio_equity (float), peak_equity (float), current_drawdown_pct (float), completed_at (IST datetime)

**Reads from:**
- watchlist table: human_approved=1 rows with combined_decision='PROCEED' for run_date
- signals table: atr and signal_type='BUY' for run_date
- trades table: all rows ordered by closed_at ASC (for kill switch calculations)
- positions table: via PaperTrader.get_positions() (for open position count)

**Writes to:**
- risk_approvals table: one row per symbol per run_date with approval_status and sizing detail; INSERT OR REPLACE on UNIQUE(symbol, run_date)

**Called by:** orchestrator.py at 08:50 IST every trading morning; Execution Agent reads risk_approvals output

**Calls:**
- PaperTrader.get_pnl() and PaperTrader.get_positions() (for equity and open positions)
- fetch_ohlcv() with cache_expiry_hours=0 (fresh entry prices)
- log_agent_action() (src/utils/logger.py) for all decisions
- send_alert() and send_info() (src/utils/notifier.py) for kill switch and summary notifications

**Key constants / thresholds relevant to debugging:**
- `STARTING_CAPITAL = 10_000.0` — portfolio base equity
- `RISK_PCT = 0.01` — 1% risk per trade
- `STOP_LOSS_ATR_MULTIPLIER = 2.0` — normal stop-loss is 2× ATR below entry
- `TAKE_PROFIT_RATIO = 2.0` — take-profit is 2× stop distance above entry (1:2 risk-reward)
- `MAX_POSITION_PCT = 0.40` — hard cap: no single position > 40% of current equity
- `MAX_OPEN_POSITIONS = 2` — maximum 2 concurrent open positions
- `DRAWDOWN_KILL_SWITCH_PCT = 15.0` — trigger if (peak_equity - portfolio_equity) / peak_equity > 15%
- `CONSECUTIVE_LOSSES_KILL_SWITCH = 5` — trigger if last 5 trades all have pnl <= 0
- `WIN_RATE_KILL_SWITCH_PCT = 40.0` — trigger if win_rate < 40% (only checked after >= 20 trades)
- `SHARPE_KILL_SWITCH = 0.8` — trigger if Sharpe < 0.8 (only checked after >= 20 trades)
- `KILL_SWITCH_MIN_TRADES = 20` — minimum trades before win_rate and Sharpe checks activate
- **Kill switch evaluation order (hardcoded, first trigger wins):**
  1. drawdown_15pct
  2. consecutive_losses_5
  3. win_rate_below_40pct (requires >= 20 trades)
  4. sharpe_below_0.8 (requires >= 20 trades)

## src/agents/execution_agent.py

**Purpose:** Human checkpoint gateway for approved trade execution — reads APPROVED risk_approvals, sends human confirmation request via Telegram + Gmail, polls a checkpoint file for 8 minutes, validates current prices against approved prices, places CNC orders via PaperTrader.

**Public API:**
- `run_execution_agent(run_date: datetime.date | None = None, db_path_override: str | None = None) -> ExecutionResult` — reads APPROVED risk_approvals for run_date, sends checkpoint notification, polls for confirmation, validates prices, places orders; raises ExecutionAgentError on DB/PaperTrader failures
- `class ExecutionAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'db_write', 'checkpoint', 'paper_trader_init')
- `class OrderRecord` (frozen dataclass) — symbol, run_date (date), quantity (int), entry_price (float), stop_loss (float), take_profit (float), order_id (int, -1 if not placed), status (str: 'PLACED'/'SKIPPED_SLIPPAGE'/'SKIPPED_RECALC_ZERO'/'SKIPPED_PRICE_FETCH_FAILED'/'SKIPPED_ORDER_ERROR'), deviation_pct (float), recalculated (bool), placed_at (IST datetime | None)
- `class ExecutionResult` (frozen dataclass) — run_date (date), human_confirmed (bool), safe_mode (bool), safe_mode_reason (str | None), orders_placed (list[OrderRecord]), orders_skipped (list[OrderRecord]), completed_at (IST datetime)

**Reads from:**
- risk_approvals table: APPROVED rows for run_date with symbol, quantity, entry_price_approx, stop_loss, take_profit, risk_amount, position_size_multiplier
- watchlist table: context only (rank, sentiment, confidence, scorecard_score for checkpoint message)
- signals table: atr for recalculation if price deviates > 0.5%
- live prices: via fetch_ohlcv() with cache_expiry_hours=0 (fresh close price)
- execution_checkpoints table: to record checkpoint status (PENDING/CONFIRMED/TIMEOUT)

**Writes to:**
- execution_checkpoints table: one PENDING row per run_date when checkpoint sent; updated to CONFIRMED or TIMEOUT on resolution
- orders table via PaperTrader.place_order(): PENDING orders written BEFORE placement attempt

**Called by:** orchestrator.py at 09:05 IST every trading morning

**Calls:**
- PaperTrader.place_order() and PaperTrader.get_pnl(): order placement and portfolio equity check
- fetch_ohlcv() with cache_expiry_hours=0: fresh price validation
- send_checkpoint() and send_alert() (src/utils/notifier.py): human notification + confirmation gateway
- log_agent_action() (src/utils/logger.py): all decisions and price deviations

**Key constants / thresholds relevant to debugging:**
- `CHECKPOINT_FILE_PREFIX = "/tmp/indian-trader-checkpoint-"` — file path: `/tmp/indian-trader-checkpoint-{run_date}.txt`
- `CHECKPOINT_POLL_INTERVAL_SECS = 15` — poll frequency while waiting for human confirmation
- `CHECKPOINT_TIMEOUT_SECS = 480` — 8-minute timeout; no confirmation within window → safe mode
- `DEVIATION_RECALC_THRESHOLD = 0.005` — 0.5% deviation triggers position recalculation
- `DEVIATION_SKIP_THRESHOLD = 0.015` — 1.5% deviation → skip trade entirely (SKIPPED_SLIPPAGE)
- **Confirmation protocol:** checkpoint file content must exactly equal `run_date.isoformat()` (anti-stale guard; not "Y" or any other string)
- Position recalculation: formula = risk_amount ÷ (ATR × 2), capped at 40% equity and MAX_TRADE_AMOUNT, rounded DOWN with math.floor()
- No confirmation by 09:13 IST → safe_mode=True, no trades placed, logged as timeout_no_confirmation

---

## src/agents/monitor_agent.py

**Purpose:** Position monitoring and GTT reconciliation during market hours (09:15–15:30 IST) — checks all open positions against stop-loss and take-profit levels every 5 minutes, tightens stops on regime filter or LLM sentiment trigger, runs GTT reconciliation every 30 minutes, detects kill switches (informational only), and triggers emergency screener rescreen at 15:35 if Nifty drops >3%.

**Public API:**
- `run_monitor_agent(run_date: datetime.date | None = None, current_time: datetime.datetime | None = None, db_path_override: str | None = None) -> MonitorResult` — runs one monitoring tick; stateless per-call (no internal sleep loops); raises MonitorAgentError on fatal DB/PaperTrader failures
- `class MonitorAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'paper_trader_init', 'price_fetch', 'gtt_check', 'gtt_reconciliation', 'stop_tighten', 'emergency_rescreen')
- `class MonitorResult` (frozen dataclass) — positions_checked (int), exits_triggered (list[dict]), stops_tightened (int), gtt_reconciliation_ran (bool), kill_switch_detected (bool), emergency_rescreen_triggered (bool), completed_at (IST datetime)

**Reads from:**
- positions table via PaperTrader.get_positions(): all open positions with entry_price, stop_loss, take_profit, current_price
- trades table: all rows ordered by closed_at ASC (for kill switch drawdown and consecutive loss calculations)
- signals table: atr for current symbol + run_date (primary), then most recent atr for any date (fallback)
- screener_results table: most recent regime status (for tightening decision)
- research_reports table: latest completed_at sentiment/confidence per symbol (for LLM tightening)
- live prices: via fetch_ohlcv() and fetch_sector_indices() with cache_expiry_hours=0

**Writes to:**
- positions table via PaperTrader.update_stop_loss(): updated stop_loss when regime or LLM tightening fires
- trades table via PaperTrader.close_position(): on GTT exit with exit_reason in {STOP_LOSS, TAKE_PROFIT}
- agent_logs table (via log_agent_action): all monitoring ticks, GTT events, stops tightened, reconciliation findings

**Called by:** orchestrator.py every 5 minutes during 09:15–15:30 IST weekdays

**Calls:**
- PaperTrader.check_gtts(), PaperTrader.get_positions(), PaperTrader.update_stop_loss(): GTT evaluation and position management
- fetch_ohlcv(), fetch_sector_indices(): fresh prices for all positions and Nifty 50 (emergency rescreen)
- run_screener_agent(): at 15:35 IST if Nifty dropped >3% since previous close
- log_agent_action() (src/utils/logger.py): tick results, exits, stops tightened
- send_alert() (src/utils/notifier.py): GTT reconciliation issues and emergency rescreen alerts

**Key constants / thresholds relevant to debugging:**
- `MARKET_OPEN_HOUR = 9`, `MARKET_OPEN_MINUTE = 15` — market opens at 09:15 IST
- `MARKET_CLOSE_HOUR = 15`, `MARKET_CLOSE_MINUTE = 45` — market closes at 15:45 IST (only close minute used for emergency rescreen gate)
- `GTT_RECONCILIATION_INTERVAL_MINUTES = 30` — run recon when current_time.minute % 30 == 0 (00, 30 minutes)
- `EMERGENCY_RESCREEN_HOUR = 15`, `EMERGENCY_RESCREEN_MINUTE = 35` — trigger rescreen check at 15:35 IST only
- `STOP_LOSS_ATR_NORMAL = 2.0` — normal regime: stop at 2× ATR below entry
- `STOP_LOSS_ATR_TIGHT = 1.0` — tight regime (BELOW_200DMA or BELOW_200DMA_10DAYS): stop at 1× ATR below entry
- `TIGHTEN_REGIMES = frozenset({"BELOW_200DMA", "BELOW_200DMA_10DAYS"})` — regimes that trigger stop tightening
- `LLM_NEGATIVE_CONFIDENCE_THRESHOLD = 0.8` — LLM Negative sentiment must have confidence > 0.8 to trigger tighten
- `NIFTY_EMERGENCY_DROP_PCT = 3.0` — if close-to-close drop > 3%, run emergency rescreen
- **Stop tightening monotonic guard:** only tightens if new_stop > current_stop (prevents loosening)
- **GTT reconciliation:** validates stop_loss < entry_price and take_profit > entry_price; repairs using ATR if invalid (alert sent)
- **Kill switch detection:** informational only — does NOT halt monitoring of open positions; drawdown and consecutive losses checked regardless of min trades, win_rate and Sharpe only after 20 trades

---

## src/agents/reporter_agent.py

**Purpose:** End-of-day P&L reporting and kill switch status display — reads all trade and position data, computes daily/cumulative P&L, Sharpe ratio, drawdown, win rate, profit factor, writes daily_pnl and strategy_perf tables, generates markdown report, sends summary notification via both Telegram and Gmail.

**Public API:**
- `run_reporter_agent(report_date: datetime.date | None = None, db_path_override: str | None = None) -> ReporterResult` — computes all metrics, writes to DB and markdown file, sends notification; raises ReporterAgentError on DB/file/notification failures
- `class ReporterAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'db_write', 'report_write', 'notification')
- `class KillSwitchStatus` (frozen dataclass) — drawdown_status (str), win_rate_status (str), consecutive_losses (int), sharpe_status (str); values: "SAFE"/"APPROACHING"/"TRIGGERED"/"N/A -- insufficient trades"
- `class DailyReport` (frozen dataclass) — report_date, daily_pnl, cumulative_pnl, unrealized_pnl, equity, peak_equity, drawdown_pct, total_trades, win_count, loss_count, win_rate_pct, sharpe_ratio, profit_factor (float | None), trades_closed_today, wins_today, losses_today, open_positions (list[dict]), open_position_count, kill_switch_status (KillSwitchStatus), computed_at (IST datetime)
- `class ReporterResult` (frozen dataclass) — report_date (date), report (DailyReport), report_file_path (str), db_written (bool), notification_sent (dict[str, bool]), completed_at (IST datetime)

**Reads from:**
- trades table: all rows ordered by closed_at ASC with pnl, closed_at (for daily P&L and Sharpe calculation)
- positions table via PaperTrader.get_positions(): all open positions (for unrealized P&L)
- PaperTrader.get_pnl(): realized_pnl, unrealized_pnl, total_pnl, trade_count, win_count, loss_count

**Writes to:**
- daily_pnl table: one INSERT OR REPLACE per report_date with daily_pnl, cumulative_pnl, equity, drawdown_pct, peak_equity, trades_closed_today, win_count_today, open_positions_count
- strategy_perf table: one INSERT OR REPLACE per metric_date with total_trades, win_rate_pct, sharpe_ratio, max_drawdown_pct, profit_factor (NULL when no losses)
- reports/ directory: one markdown file per report_date with full summary, metrics table, open positions, kill switch status

**Called by:** orchestrator.py at 15:45 IST every trading day

**Calls:**
- PaperTrader.get_pnl() and PaperTrader.get_positions(): portfolio equity and open position details
- log_agent_action() (src/utils/logger.py): completion logs and kill switch warnings
- send_alert() (src/utils/notifier.py): summary notification to both Telegram and Gmail

**Key constants / thresholds relevant to debugging:**
- `STARTING_CAPITAL = 10_000.0` — portfolio base for equity and return calculations

---

## src/agents/orchestrator.py

**Purpose:** Python Agent SDK orchestrator — sequences all 10 trading agents across 4 daily sessions (evening/morning/monitor/report), detects current session from IST time, isolates errors per agent, enforces kill switch logic (when risk_agent fires, skips execution_agent and sets safe_mode=True), auto-starts dashboard on port 8765, and applies weekday gates to morning/monitor/report sessions.

**Public API:**
- `run_orchestrator(session: str | None = None, run_date: datetime.date | None = None, db_path_override: str | None = None) -> OrchestratorResult` — executes one orchestration cycle; session auto-detected from IST time if not provided; run_date defaults to today IST; db_path_override optional for testing; raises OrchestratorError on fatal DB/configuration failures
- `class AgentStepResult(frozen)` — agent_name (str), success (bool), error_message (str | None), started_at (IST datetime), completed_at (IST datetime)
- `class OrchestratorResult(frozen)` — session (str), run_date (date), safe_mode (bool), safe_mode_reason (str | None), steps (list[AgentStepResult]), started_at (IST datetime), completed_at (IST datetime)
- `class OrchestratorError(Exception)` — raised on fatal setup/DB failures; attributes: message (str)

**Reads from:**
- All DB tables (via agent calls): market_data, screener_results, research_reports, watchlist, morning_signals, signals, risk_approvals, orders, positions, trades, daily_pnl, agent_logs

**Writes to:**
- agent_logs table (via agent calls and orchestrator own logs)
- Session-specific tables (each agent writes to its own output table)

**Called by:** Windows Task Scheduler / manual CLI invocation / Python scripts

**Calls:**
- All 10 trading agents: run_data_collector_agent, run_screener_agent, run_research_agent, run_watchlist_agent, run_morning_validator_agent, run_signal_agent, run_risk_agent, run_execution_agent, run_monitor_agent, run_reporter_agent
- Dashboard auto-start: probes port 8765, spawns `python dashboard/server.py` if port is free
- log_agent_action() (src/utils/logger.py): orchestrator own status logs

**Key constants / thresholds relevant to debugging:**
- **Sessions (IST time windows):**
  - `"evening"` — 18:00–23:59 IST: runs Data Collector → Screener → Research → Watchlist Builder (Monday only)
  - `"morning"` — 06:00–09:14 IST: runs Morning Validator → Signal Agent → Risk Agent → Execution Agent (weekdays only)
  - `"monitor"` — 09:15–15:44 IST: runs Monitor Agent every 5 minutes (weekdays only)
  - `"report"` — 15:45–17:59 IST: runs Reporter Agent once daily (weekdays only)
- `MONITOR_SLEEP_SECONDS = 300` — 5-minute polling interval for monitor_agent per orchestrator spec
- `DASHBOARD_PORT = 8765` — socket probe for existing dashboard; spawns dashboard/server.py if port free
- `WEEKDAY_GATE = {1, 2, 3, 4, 5}` — ISO weekday set (Mon=1 to Fri=5); morning/monitor/report skip on weekends
- **Kill switch logic:** when risk_agent returns kill_switch_fired=True, orchestrator skips execution_agent entirely (does NOT call it), sets safe_mode=True in OrchestratorResult, and logs the reason
- **Safe mode reasons:** "kill_switch_fired: {reason}", "execution_timeout", "morning_pipeline_late", "human_checkpoint_timeout"
- Error isolation: if any single agent fails, that step is recorded with success=False and error_message populated; orchestrator continues to next agent (does NOT halt entire session)
- All timestamps IST (Asia/Kolkata) via ZoneInfo throughout
- `KILL_SWITCH_MIN_TRADES = 20` — minimum trades before win_rate and Sharpe checks display (otherwise "N/A")
- Kill switch approaching thresholds:
  - `DRAWDOWN_APPROACHING_PCT = 10.0` — approaching at 10%, triggered at 15%
  - `WIN_RATE_APPROACHING_PCT = 45.0` — approaching at 45%, triggered at 40%
  - `SHARPE_APPROACHING = 1.0` — approaching at 1.0, triggered at 0.8
- `CONSECUTIVE_LOSSES_LIMIT = 5` — display consecutive losses up to this count (1:5 ratio)
- **Sharpe calculation:** annualized (× sqrt(252)), population std dev, daily returns grouped by closed_at date
- **Drawdown calculation:** (peak_equity - current_equity) / peak_equity × 100
- **Profit factor:** sum(winning trades) / abs(sum(losing trades)); **returns None when no losses** (NULL in DB, "N/A" in markdown)
- `REPORTS_DIR = "reports"` — absolute path resolved relative to project root

---

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