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

## src/agents/research_agent.py

**Purpose:** Evening pipeline agent (22:40 IST) — fetches 3 Brave Search queries per top-5 screener candidate, synthesises news sentiment via Gemini 2.5 Flash, writes results to research_reports table with race-condition-safe two-step DB write (INSERT then UPDATE completed_at).

**Public API:**
- `run_research_agent(run_date=None, symbols=None) -> ResearchAgentResult` — executes research pipeline; run_date defaults to today; symbols filters candidates (for testing); returns frozen ResearchAgentResult dataclass
- `class StockResearch(frozen)` — symbol (str), sentiment (str in {"Positive", "Negative", "Neutral", "Mixed"}), confidence (float 0.0–1.0), source_urls (list[str]), earnings_transcript_unavailable (bool), completed_at (datetime.datetime in IST)
- `class ResearchAgentResult(frozen)` — run_date (datetime.date), stocks_researched (int), results (list[StockResearch]), skipped_symbols (list[str]), completed_at (datetime.datetime in IST)
- `class ResearchAgentError(Exception)` — message (str), phase (str in {"db_read", "brave_search", "gemini", "db_write"})

**Reads from:**
- screener_results table: top 5 candidates by momentum rank
- Brave Search API: 3 queries per stock (last 48 hours), news articles + URLs
- Gemini 2.5 Flash API: sentiment synthesis from fetched news

**Writes to:**
- research_reports table: INSERT row with symbol, sentiment, confidence, source_urls, earnings_transcript_unavailable, completed_at=NULL, then UPDATE SET completed_at=datetime.now() AFTER all fields confirmed (prevents race condition where Watchlist Builder reads incomplete rows)

**Called by:**
- evening pipeline orchestrator at 22:40 IST (Phase 3, step 1)

**Calls:**
- Brave Search API (HTTP GET with authorization header)
- Gemini 2.5 Flash (google-genai SDK)
- src.utils.logger.log_agent_action()
- sqlite3 (stdlib)

**Key constants / thresholds:**
- `BRAVE_REQUEST_DELAY = 1.1` — seconds between Brave Search queries (rate limit safety)
- `MAX_SYMBOLS = 5` — processes only top 5 from screener_results
- `EARNINGS_LOOKBACK_DAYS = 5` — if earnings reported within last 5 days, switch to earnings transcript analysis
- `TRANSCRIPT_MIN_CHARS = 200` — minimum length for parseable earnings transcript; if shorter or unavailable → fall back to standard news synthesis
- `AGENT_NAME = "research_agent"` — for agent_logs
- Sentiment values: "Positive", "Negative", "Neutral", "Mixed" (exactly as returned by Gemini)

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