# Public Interfaces

## src/data/validator.py

- `validate_data(ohlcv_df: pd.DataFrame, fundamentals_df: pd.DataFrame, db_path: str, trading_calendar: list[datetime.date] | None = None) -> DataQualityReport` — runs all quality checks, logs to agent_logs, raises DataQualityError if score < 0.6
- `class DataQualityReport` — frozen dataclass with per_stock_scores, universe_quality_score, failed_roe_symbols, roe_missing_symbols, de_coverage_ratio, de_coverage_low, gap_violations, checked_at_ist, universe_size
- `class DataQualityError(Exception)` — raised when universe_quality_score < 0.60; carries universe_quality_score and report attributes

## src/config/settings.py

- `load_settings(env_path: str | None = None) -> Settings` — loads .env, validates all variables, returns frozen Settings dataclass; raises ConfigurationError with all problems listed
- `class Settings` — frozen dataclass with 16 typed config fields; secrets masked in __repr__/\_\_str\_\_
- `class ConfigurationError(Exception)` — raised at startup; carries errors: list[str]
- `settings: Settings` — module-level singleton; import via `from src.config.settings import settings`

## src/data/fetcher.py

- `fetch_ohlcv(symbols: list[str], start_date: datetime.date, end_date: datetime.date, cache_expiry_hours: int = 24) -> pd.DataFrame` — fetches OHLCV; yfinance primary, jugaad-data fallback; returns normalised DataFrame
- `fetch_nifty50_symbols() -> list[str]` — returns 50 Nifty 50 constituent symbols (updated 2026-03-22; update quarterly)
- `fetch_sector_indices(start_date: datetime.date, end_date: datetime.date, cache_expiry_hours: int = 24) -> pd.DataFrame` — fetches sector indices via yfinance only
- `class FetchError(Exception)` — raised when both sources fail; carries symbol, yfinance_error, jugaad_error attributes

## src/data/cleaner.py

- `clean_ohlcv(df: pd.DataFrame, price_floor: float = 1.0) -> tuple[pd.DataFrame, CleaningReport]` — cleans OHLCV; returns (cleaned_df, report)
- `class CleaningReport` — frozen dataclass with symbols_processed, rows_input, rows_output, duplicates_removed, missing_close_filled, missing_ohlv_filled, negative_price_flags, consistency_flags, price_floor_flags, cleaned_at_ist

## src/data/fundamentals.py

- `fetch_fundamentals(symbols: list[str], force_refresh: bool = False) -> pd.DataFrame` — fetches fundamentals from Screener.in with yfinance fallback; returns one row per symbol (failed symbols included with NaN)
- `get_cache_age_days(symbol: str) -> float | None` — returns cache age in days or None if no cache exists
- `fetch_historical_fundamentals(symbols: list[str], force_refresh: bool = False) -> None` — fetches annual historical fundamentals from Screener.in and stores to fundamentals_history SQLite table; 3-strike yfinance fallback; 45-day staleness check; returns None (callers query via get_fundamentals_for_date)
- `get_fundamentals_for_date(symbols: list[str], as_of_date: datetime.date) -> pd.DataFrame` — returns point-in-time fundamentals for as_of_date with no lookahead bias; fiscal year rule: month<=6 → year-1, month>=7 → year; output columns match fetch_fundamentals() (minus pe_ratio/cache_age_days) for quality_filter.py compatibility; eps_positive_4q is annual approximation for historical data
- `get_nifty_universe_for_year(year: int) -> list[str]` — returns NSE symbols in Nifty 50 for given calendar year (2010-2023); lazily populates nifty_constituents table on first call; returns empty list if year out of range

## src/utils/logger.py

- `setup_logging(db_path: str | None = None) -> None` — configures root logger with StreamHandler + SQLiteHandler; idempotent
- `get_logger(name: str) -> logging.Logger` — returns named logger inheriting from root
- `log_agent_action(agent_name: str, action: str, level: str = "INFO", symbol: str | None = None, result: str | None = None, data_quality_score: float | None = None) -> None` — direct structured write to agent_logs, bypassing LogRecord pipeline
- `class SQLiteHandler(logging.Handler)` — thread-safe handler with public method `write_row(logged_at, agent_name, level, action, symbol, result, data_quality_score) -> None`

## src/utils/notifier.py

- `send_alert(subject: str, message: str) -> dict[str, bool]` — sends ALERT to both Telegram and Gmail; returns {"telegram": bool, "gmail": bool}
- `send_checkpoint(subject: str, message: str) -> dict[str, bool]` — sends CHECKPOINT to both channels
- `send_info(message: str) -> dict[str, bool]` — sends INFO to Telegram only; gmail always False
- `class NotificationType(Enum)` — ALERT, CHECKPOINT, INFO

## src/indicators/technical.py

- `add_indicators(df: pd.DataFrame, rsi_period: int = 14, macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9, bb_length: int = 20, bb_std: float = 2.0, atr_period: int = 14) -> pd.DataFrame` — computes RSI, MACD, Bollinger Bands, ATR per symbol; returns new DataFrame with 8 added columns: rsi, macd, macd_signal, macd_hist, bb_upper, bb_mid, bb_lower, atr; raises ValueError on missing columns or empty input
- `compute_atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series` — standalone ATR (Wilder smoothing) for a single-symbol DataFrame; raises ValueError if high/low/close missing
- Constants: `MINIMUM_LOOKBACK=26`, `RSI_PERIOD=14`, `MACD_FAST=12`, `MACD_SLOW=26`, `MACD_SIGNAL_PERIOD=9`, `BB_LENGTH=20`, `BB_STD=2.0`, `ATR_PERIOD=14`

## src/strategy/regime.py

- `apply_regime_filter(ranked_df: pd.DataFrame, nifty_ohlcv_df: pd.DataFrame, open_positions: list[dict[str, object]] | None = None) -> tuple[pd.DataFrame, RegimeResult]` — determines market regime from Nifty 50 200 DMA; returns (filtered_df with position_size_multiplier added, RegimeResult); empty filtered_df when BELOW_200DMA_10DAYS
- `class RegimeResult` — frozen dataclass: regime (str), nifty_close, sma_200, consecutive_days_below (int), position_size_multiplier (float), tighten_stops (bool), stop_tighten_symbols (list[str]), computed_at_ist (str)
- `compute_200dma(nifty_ohlcv_df: pd.DataFrame) -> float` — 200-day SMA of Nifty 50 close; raises ValueError if <200 rows
- `count_consecutive_days_below_200dma(nifty_ohlcv_df: pd.DataFrame) -> int` — counts consecutive days (from most recent) close < rolling 200 SMA; returns 0 if above
- Constants: `SMA_PERIOD=200`, `BELOW_DMA_BLOCK_DAYS=10`, `POSITION_SIZE_ABOVE=1.0`, `POSITION_SIZE_BELOW=0.5`, `POSITION_SIZE_BLOCKED=0.0`, `AGENT_NAME="regime"`
- nifty_ohlcv_df requires only "date" and "close" (no symbol column); ranked_df requires all 7 momentum.py output columns
- Raises ValueError on missing columns or <200 nifty rows; empty ranked_df is valid (not an error)

## src/strategy/momentum.py

- `compute_momentum(quality_df: pd.DataFrame, ohlcv_df: pd.DataFrame, top_n: int = 5) -> tuple[pd.DataFrame, MomentumReport]` — computes 12-1 momentum scores for quality-filtered symbols; returns (ranked_df, report). Symbols with <252 rows excluded. Tiebreaker: within 2% relative diff → lower pct_from_52w_high wins.
- `class MomentumReport` — frozen dataclass: scored_count, selected_count, insufficient_history_count, tiebreaker_applied_count, computed_at_ist
- Constants: `TWELVE_MONTH_LOOKBACK=252`, `ONE_MONTH_LOOKBACK=21`, `DEFAULT_TOP_N=5`, `TIEBREAKER_THRESHOLD=0.02`, `AGENT_NAME="momentum"`
- Output DataFrame columns (sorted by rank asc): symbol, momentum_score, twelve_month_return, one_month_return, rank (int64), pct_from_52w_high, within_30pct_of_52w_high
- Raises ValueError on: empty inputs, missing required columns, top_n < 1

## src/strategy/quality_filter.py

- `apply_quality_filter(fundamentals_df: pd.DataFrame, ohlcv_df: pd.DataFrame, lookback_days: int = 252) -> tuple[pd.DataFrame, FilterReport]` — applies all 5 hard quality filters (ROE, D/E, EPS, volume, price) to fundamentals_df × ohlcv_df; returns (filtered_df, report); raises ValueError on empty inputs or missing required columns
- `class FilterReport` — frozen dataclass: universe_size, passed_count, failed_count, thin_universe, filter_failure_counts (dict[str, int] keyed "roe"/"debt_equity"/"eps"/"volume"/"price"), filtered_at_ist
- Constants: `ROE_THRESHOLD=0.15`, `DE_THRESHOLD=1.0`, `VOLUME_VALUE_THRESHOLD=20_000_000.0`, `PRICE_THRESHOLD=50.0`, `PROXIMITY_THRESHOLD=0.30`, `DEFAULT_LOOKBACK_DAYS=252`, `MIN_UNIVERSE_SIZE=3`, `AGENT_NAME="quality_filter"`
- Output DataFrame columns (passing symbols only): symbol, roe, debt_to_equity, avg_daily_value, latest_price, high_52w, pct_from_52w_high, within_30pct_of_52w_high, passed_hard_filters; empty DataFrame with same schema returned when thin_universe

## src/backtest/runner.py

- `run_backtest(start_date: datetime.date, end_date: datetime.date, initial_cash: float = 10_000.0) -> BacktestResult` — runs full three-step strategy (quality filter -> momentum rank -> regime filter) over historical date range using backtesting.py; returns BacktestResult with gates_passed=False; raises ValueError on invalid inputs, BacktestError on data/simulation failures
- `class BacktestResult` — frozen dataclass with fields: start_date, end_date, total_return_pct (float), annualized_return_pct (float), sharpe_ratio (float), max_drawdown_pct (float, positive), win_rate_pct (float), total_trades (int), profit_factor (float, inf if zero losses with wins, 0.0 if no wins), regime_changes (int), regime_blocked_weeks (int), raw_stats (dict), gates_passed (bool, always False)
- `class BacktestError(Exception)` — raised on fatal backtest errors; attributes: message (str), phase (str: "data_fetch", "strategy_init", "simulation", "stats_extraction")
- Constants: `AGENT_NAME="backtest_runner"`, `RISK_PER_TRADE=0.01`, `MAX_POSITIONS=2`, `MAX_POSITION_PCT=0.40`, `MAX_TRADE_AMOUNT=10_000.0`, `STOP_LOSS_ATR_NORMAL=2.0`, `STOP_LOSS_ATR_TIGHT=1.0`, `STOP_LOSS_MAX_PCT=0.03`, `TAKE_PROFIT_RATIO=2.0`, `ATR_PERIOD=14`, `MIN_BACKTEST_START=date(2010,1,1)`, `MAX_BACKTEST_END=date(2023,12,31)`, `LOOKBACK_CALENDAR_DAYS=400`

## src/backtest/validator.py

- `validate_backtest(result: BacktestResult) -> ValidationResult` — evaluates a BacktestResult against all 5 backtest gates; returns ValidationResult with per-gate breakdown; raises ValueError if result.total_trades < 0
- `class GateResult` — frozen dataclass: gate_name (str), threshold (str), actual_value (float | int), passed (bool)
- `class ValidationResult` — frozen dataclass: all_gates_passed (bool), gate_results (tuple[GateResult, ...] of exactly 5 in order sharpe_ratio/max_drawdown/win_rate/total_trades/profit_factor), validated_result (BacktestResult with gates_passed=True on pass, original unchanged on fail)
- Constants: `AGENT_NAME="backtest_validator"`, `SHARPE_THRESHOLD=1.0`, `MAX_DRAWDOWN_THRESHOLD=15.0`, `WIN_RATE_THRESHOLD=40.0`, `MIN_TRADES_THRESHOLD=100`, `PROFIT_FACTOR_THRESHOLD=1.3`

## src/agents/signal_agent.py

- `run_signal_agent(run_date: datetime.date | None = None, symbols: list[str] | None = None) -> SignalAgentResult` — reads top screener candidates, computes RSI/MACD/BB/ATR on fresh 60-day OHLCV, applies combined decision rule (technical + sentiment + Groq), writes all signals (BUY and HOLD) to signals table; returns empty result with late_start=True if called after 08:50 IST
- `class SignalAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'ohlcv_fetch', 'db_write')
- `class StockSignal` — frozen dataclass: symbol, rsi (float), macd_signal (str: BUY/HOLD), bollinger_position (str: ABOVE/MIDDLE/BELOW), atr (float), groq_confidence (float 0-1, or -1.0 sentinel when LLM unavailable), signal_type (str: BUY/HOLD), skip_reason (str | None), signalled_at (IST datetime)
- `class SignalAgentResult` — frozen dataclass: run_date (date), symbols_processed (int), buy_signals (list[StockSignal]), hold_signals (list[StockSignal]), late_start (bool), completed_at (IST datetime)
- Constants: `AGENT_NAME="signal_agent"`, `DEADLINE_HOUR=8`, `DEADLINE_MINUTE=50`, `RSI_BUY_THRESHOLD=40.0`, `OHLCV_LOOKBACK_DAYS=60`, `GROQ_MODEL="llama-3.3-70b-versatile"`, `GROQ_API_ENDPOINT`, `GROQ_TIMEOUT_SECONDS=15`, `GROQ_CONFIDENCE_THRESHOLD=0.6`, `GEMINI_MODEL="gemini-2.5-flash"`, `LLM_UNAVAILABLE_SENTINEL=-1.0`, `MAX_SYMBOLS=5`, `VALID_SIGNAL_TYPES`, `VALID_BOLLINGER_POSITIONS`, `VALID_MACD_SIGNALS`
- Decision rule: technical BUY fires when rsi < 40.0 AND macd_hist > 0; blocked if sentiment="Negative"; Groq advisory check applied (confidence < 0.6 → downgrade to HOLD); both LLMs failing → keep rule-based BUY with groq_confidence=-1.0

## src/agents/screener_agent.py

- `run_screener_agent(run_date: datetime.date | None = None) -> ScreenerAgentResult` — runs full 3-step screener pipeline (quality filter → momentum → regime), writes top 5 to screener_results table; returns ScreenerAgentResult; raises ScreenerAgentError on fatal errors
- `class ScreenerAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_write', 'ohlcv_fetch', 'fundamentals_fetch', 'quality_filter', 'momentum', 'regime')
- `class ScreenerResult` — frozen dataclass: symbol (str), rank (int, 1=highest), momentum_score (float), quality_passed (bool), regime (str: 'ABOVE_200DMA'/'BELOW_200DMA'/'BELOW_200DMA_10DAYS'), position_size_multiplier (float: 1.0/0.5/0.0), screened_at (IST datetime), run_date (date)
- `class ScreenerAgentResult` — frozen dataclass: run_date (date), symbols_screened (int, total Nifty universe size), symbols_passed_quality (int), top5 (list[ScreenerResult], empty when thin_universe or regime_blocked), thin_universe (bool), regime_blocked (bool), completed_at (IST datetime)
- Constants: `AGENT_NAME="screener_agent"`, `OHLCV_LOOKBACK_DAYS=400`, `MIN_UNIVERSE_SIZE=3`, `MAX_TOP_N=5`, `MOMENTUM_TIEBREAKER_PCT=2.0`
- DB: writes to screener_results (INSERT OR REPLACE on UNIQUE(symbol, run_date)); reads no DB tables
- screener_results DDL: id AUTOINCREMENT, symbol, run_date (TEXT ISO date), rank, momentum_score, quality_passed (INTEGER 0/1), regime, position_size_multiplier, screened_at (TEXT ISO timestamp), UNIQUE(symbol, run_date)

## src/agents/research_agent.py

- `run_research_agent(run_date: datetime.date | None = None, symbols: list[str] | None = None) -> ResearchAgentResult` — runs Tavily Search + Gemini synthesis for top-5 screener candidates; writes to research_reports table with completed_at set last; raises ResearchAgentError on DB read/write failures; Tavily/Gemini failures handled gracefully per-stock
- `class ResearchAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'tavily_search', 'gemini', 'db_write')
- `class StockResearch` — frozen dataclass: symbol, sentiment (str: Positive/Negative/Neutral/Mixed), confidence (float 0-1), source_urls (list[str]), earnings_transcript_unavailable (bool), completed_at (IST datetime)
- `class ResearchAgentResult` — frozen dataclass: run_date (date), stocks_researched (int), results (list[StockResearch]), skipped_symbols (list[str]), completed_at (IST datetime)
- Constants: `AGENT_NAME="research_agent"`, `TAVILY_REQUEST_DELAY=0.5`, `TAVILY_MAX_RESULTS=10`, `GEMINI_MODEL="gemini-2.5-flash-preview-04-17"`, `GEMINI_QUOTA_RETRY_DELAY=60`, `VALID_SENTIMENTS`, `FALLBACK_SENTIMENT="Neutral"`, `FALLBACK_CONFIDENCE=0.3`, `EARNINGS_KEYWORDS`, `EARNINGS_AGE_LIMIT_DAYS=5`, `TRANSCRIPT_MIN_CHARS=200`, `MAX_SYMBOLS=5`, `SYMBOL_TO_COMPANY`

## src/agents/watchlist_agent.py

- `run_watchlist_agent(run_date: datetime.date | None = None) -> WatchlistAgentResult` — reads screener_results + research_reports, applies combined decision rule, computes partial pre-trade scorecard, writes all candidates (PROCEED and SKIP) to watchlist table, sends checkpoint notification for human approval; returns immediately (non-blocking); raises WatchlistAgentError on DB read/write failures or when both notification channels fail
- `check_watchlist_timeout(run_date: datetime.date) -> None` — called by orchestrator at 07:00 IST; marks all pending rows (human_approved=0, approval_source IS NULL) as approval_source='timeout_skip'; sends alert if any rows timed out; raises WatchlistAgentError(phase='timeout_check') on DB failure
- `record_human_approval(symbol: str, run_date: datetime.date, approved: bool) -> None` — records human approval or rejection; sets human_approved=1/0 and approval_source='human_explicit'; no-op + WARNING if symbol not found; never raises
- `class WatchlistAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'db_write', 'notification', 'timeout_check')
- `class WatchlistCandidate` — frozen dataclass (intermediate, not written to DB): symbol, rank (int), momentum_score (float), regime (str), position_size_multiplier (float), sentiment (str), confidence (float), earnings_transcript_unavailable (bool), combined_decision (str: "PROCEED"/"SKIP"), skip_reason (str | None), scorecard_score (int), scorecard_max (int)
- `class WatchlistEntry` — frozen dataclass (written to DB): symbol, combined_decision, scorecard_score, scorecard_max, sentiment, confidence, rank, regime, position_size_multiplier, human_approved (bool), approval_source (str | None), added_at (IST datetime), run_date (date)
- `class WatchlistAgentResult` — frozen dataclass: run_date (date), candidates_evaluated (int), proceed_count (int), skipped_count (int), approved_symbols (list[str]), human_responded (bool, always False at run time), completed_at (IST datetime)
- Constants: `AGENT_NAME="watchlist_agent"`, `APPROVAL_DEADLINE_HOUR=7`, `APPROVAL_DEADLINE_MINUTE=0`, `SCORECARD_THRESHOLD=28`, `SCORECARD_MAX_FULL=40`, `SCORECARD_MAX_FULL_NO_EARNINGS=35`, `SCORECARD_MAX_WATCHLIST=20`, `SCORECARD_MAX_WATCHLIST_NO_EARNINGS=15`
- Combined decision rule: position_size_multiplier==0.0 → SKIP(regime_blocked); sentiment=="Negative" → SKIP(negative_sentiment); else → PROCEED
- Scorecard at watchlist stage (max 20, or 15 with earnings flag): quality(always 5) + rank(5 if rank≤3 else 0) + regime(ABOVE=5, BELOW=2, BELOW_10DAYS=0) + sentiment(Positive=5, Neutral=3, Mixed=1, Negative=0)
- Reads from: screener_results (quality_passed=1 rows for run_date), research_reports (completed_at IS NOT NULL, ORDER BY completed_at DESC LIMIT 1 per symbol)
- Writes to: watchlist table (UNIQUE on symbol+run_date; approval_source NULL until check_watchlist_timeout or record_human_approval)

## src/agents/risk_agent.py

- `run_risk_agent(run_date: datetime.date | None = None, db_path_override: str | None = None) -> RiskAgentResult` — reads human_approved=1 watchlist rows, runs all four kill switch checks, sizes each approved symbol using 1%-ATR formula, writes all results to risk_approvals table; raises RiskAgentError on DB/paper_trader failures
- `class RiskAgentError(Exception)` — raised on fatal errors; attributes: message (str), phase (str: 'db_read', 'db_write', 'paper_trader_init')
- `class RiskApproval` — frozen dataclass: symbol, run_date (date), quantity (int), entry_price_approx (float), stop_loss (float), take_profit (float), position_size_multiplier (float), risk_amount (float), approval_status (str: 'APPROVED'/'REJECTED'), rejection_reason (str | None), approved_at (IST datetime)
- `class RiskAgentResult` — frozen dataclass: run_date (date), kill_switch_fired (bool), kill_switch_reason (str | None), approved (list[RiskApproval]), rejected (list[RiskApproval]), portfolio_equity (float), peak_equity (float), current_drawdown_pct (float), completed_at (IST datetime)
- Constants: `AGENT_NAME="risk_agent"`, `STARTING_CAPITAL=10_000.0`, `RISK_PCT=0.01`, `STOP_LOSS_ATR_MULTIPLIER=2.0`, `TAKE_PROFIT_RATIO=2.0`, `MAX_POSITION_PCT=0.40`, `MAX_OPEN_POSITIONS=2`, `DRAWDOWN_KILL_SWITCH_PCT=15.0`, `WIN_RATE_KILL_SWITCH_PCT=40.0`, `CONSECUTIVE_LOSSES_KILL_SWITCH=5`, `SHARPE_KILL_SWITCH=0.8`, `KILL_SWITCH_MIN_TRADES=20`
- Kill switch order (hardcoded): drawdown_15pct → consecutive_losses_5 → win_rate_below_40pct → sharpe_below_0.8; first trigger wins
- Reads from: watchlist (human_approved=1, combined_decision='PROCEED'), signals (signal_type='BUY'), trades (all rows ordered by closed_at ASC), positions (via PaperTrader.get_positions())
- Writes to: risk_approvals table (INSERT OR REPLACE on UNIQUE(symbol, run_date))
- Called by: orchestrator (08:50 IST), Execution Agent reads risk_approvals output

## src/execution/paper_trader.py

- `class PaperTrader` — simulated CNC swing trade execution engine; raises ValueError on construction if settings.live_trading is True
- `PaperTrader.__init__(db_path: str | None = None) -> None` — opens SQLite connection with WAL pragmas; creates orders, trades, positions tables if not present; db_path derived from settings.database_url when None
- `PaperTrader.place_order(symbol: str, side: str, quantity: int, entry_price: float, stop_loss: float, take_profit: float) -> int` — writes PENDING order to orders table BEFORE simulating fill; opens position (BUY) or closes position (SELL); returns orders.id
- `PaperTrader.close_position(symbol: str, exit_price: float, exit_reason: str) -> int` — writes PENDING SELL order, inserts completed trade into trades table, removes from positions; exit_reason in {"STOP_LOSS","TAKE_PROFIT","MANUAL_EXIT","REGIME_TIGHTENED"}; returns trades.id
- `PaperTrader.get_positions() -> list[dict[str, object]]` — returns all rows from positions table as list of dicts; empty list if no open positions
- `PaperTrader.get_pnl() -> dict[str, float]` — returns {"realized_pnl", "unrealized_pnl", "total_pnl", "trade_count", "win_count", "loss_count"} aggregated from trades and positions tables
- `PaperTrader.check_gtts(current_prices: dict[str, float]) -> list[dict[str, object]]` — checks all open positions against stop_loss/take_profit; triggers close_position on hit; updates unrealized P&L on no-hit; never raises; returns list of triggered dicts with keys symbol, exit_price, exit_reason, trade_id
- `PaperTrader.update_stop_loss(symbol: str, new_stop_loss: float) -> None` — updates stop_loss in positions table for regime/LLM tightening; raises ValueError if no position or new_stop_loss >= entry_price
