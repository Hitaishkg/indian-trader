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
