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

## src/execution/paper_trader.py

- `class PaperTrader` — simulated CNC swing trade execution engine; raises ValueError on construction if settings.live_trading is True
- `PaperTrader.__init__(db_path: str | None = None) -> None` — opens SQLite connection with WAL pragmas; creates orders, trades, positions tables if not present; db_path derived from settings.database_url when None
- `PaperTrader.place_order(symbol: str, side: str, quantity: int, entry_price: float, stop_loss: float, take_profit: float) -> int` — writes PENDING order to orders table BEFORE simulating fill; opens position (BUY) or closes position (SELL); returns orders.id
- `PaperTrader.close_position(symbol: str, exit_price: float, exit_reason: str) -> int` — writes PENDING SELL order, inserts completed trade into trades table, removes from positions; exit_reason in {"STOP_LOSS","TAKE_PROFIT","MANUAL_EXIT","REGIME_TIGHTENED"}; returns trades.id
- `PaperTrader.get_positions() -> list[dict[str, object]]` — returns all rows from positions table as list of dicts; empty list if no open positions
- `PaperTrader.get_pnl() -> dict[str, float]` — returns {"realized_pnl", "unrealized_pnl", "total_pnl", "trade_count", "win_count", "loss_count"} aggregated from trades and positions tables
- `PaperTrader.check_gtts(current_prices: dict[str, float]) -> list[dict[str, object]]` — checks all open positions against stop_loss/take_profit; triggers close_position on hit; updates unrealized P&L on no-hit; never raises; returns list of triggered dicts with keys symbol, exit_price, exit_reason, trade_id
- `PaperTrader.update_stop_loss(symbol: str, new_stop_loss: float) -> None` — updates stop_loss in positions table for regime/LLM tightening; raises ValueError if no position or new_stop_loss >= entry_price
