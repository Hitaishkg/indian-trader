# Module Connections Reference

> Maintained by the Docs Agent. Updated automatically after every build.
> Every section is replaced (not appended) when a module is updated.
> For system overview and debugging guide, see `docs/SYSTEM.md`.

---

<!-- Docs Agent: insert new module sections below this line, alphabetically by path -->

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