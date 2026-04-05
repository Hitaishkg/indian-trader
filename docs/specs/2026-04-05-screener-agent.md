# Spec: src/agents/screener_agent.py

**Date**: 2026-04-05
**Phase**: 3 — Intelligence Layer (Step 3 of 4)
**Author**: Architect Agent (claude-opus-4-5)

---

## 1. Purpose

The Screener Agent runs the complete three-step stock selection pipeline (quality filter → momentum ranking → regime filter) and writes the top 5 candidates to the `screener_results` table. It runs every Monday at 22:00 IST (scheduled by the orchestrator) and can also be invoked standalone for emergency rescreens (e.g., when the Monitor Agent detects a Nifty 50 single-day close-to-close drop > 3%). Its output feeds directly into the Research Agent (which reads `screener_results` to determine which symbols to research) and the Signal Agent (which reads `screener_results` for morning confirmation). It is the source of truth for `screener_results` and reads no DB tables itself.

---

## 2. Public API

### 2.1 Exception class

```python
class ScreenerAgentError(Exception):
    """Raised when the Screener Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with a message and phase identifier.

        Args:
            message: Human-readable error description.
            phase: One of 'db_write', 'ohlcv_fetch', 'fundamentals_fetch',
                   'quality_filter', 'momentum', 'regime'.
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")
```

Valid phase strings: `"db_write"`, `"ohlcv_fetch"`, `"fundamentals_fetch"`, `"quality_filter"`, `"momentum"`, `"regime"`.

Note: `"db_read"` is not a valid phase — the screener_agent reads no DB tables. The phase `"db_write"` is the only DB phase.

### 2.2 ScreenerResult dataclass

```python
@dataclass(frozen=True)
class ScreenerResult:
    """Result for a single top-5 candidate."""

    symbol: str
    rank: int                            # 1 = highest momentum
    momentum_score: float
    quality_passed: bool
    regime: str                          # "ABOVE_200DMA", "BELOW_200DMA", "BELOW_200DMA_10DAYS"
    position_size_multiplier: float      # 1.0 / 0.5 / 0.0
    screened_at: datetime.datetime       # IST timezone-aware
    run_date: datetime.date
```

### 2.3 ScreenerAgentResult dataclass

```python
@dataclass(frozen=True)
class ScreenerAgentResult:
    """Full output of run_screener_agent()."""

    run_date: datetime.date
    symbols_screened: int                # len(nifty_universe) — total input size
    symbols_passed_quality: int          # number that passed all 5 quality filters
    top5: list[ScreenerResult]           # empty list when thin_universe or regime_blocked
    thin_universe: bool                  # True when < 3 stocks passed quality filter
    regime_blocked: bool                 # True when BELOW_200DMA_10DAYS
    completed_at: datetime.datetime      # IST timezone-aware
```

### 2.4 Entry point

```python
def run_screener_agent(
    run_date: datetime.date | None = None,
) -> ScreenerAgentResult:
    """Run the full 3-step screener pipeline and write top 5 to screener_results.

    Args:
        run_date: Date to run for. Defaults to datetime.date.today() in IST.

    Returns:
        ScreenerAgentResult with pipeline summary and top5 candidates.

    Raises:
        ScreenerAgentError: On fatal errors in any pipeline phase.
    """
```

---

## 3. Inputs

| Source | Call | Notes |
|--------|------|-------|
| Nifty 50 universe | `get_nifty_universe_for_year(run_date.year)` | Returns `list[str]` of NSE symbols |
| Stock OHLCV | `fetch_ohlcv(symbols, start_date, end_date, cache_expiry_hours=0)` | 400-day lookback from `run_date`; `cache_expiry_hours=0` forces fresh fetch to ensure full date range coverage (known cache limitation from decisions log) |
| Nifty 50 index OHLCV | `fetch_sector_indices(start_date, end_date, cache_expiry_hours=0)` then filter `symbol == "NIFTY_50"`, drop symbol column | DO NOT use `fetch_ohlcv(["^NSEI"])` — `fetch_sector_indices()` is the established pattern (confirmed in backtest/runner.py and decisions log); `apply_regime_filter` requires no symbol column |
| Fundamentals | `get_fundamentals_for_date(symbols, run_date)` | Returns `pd.DataFrame` with one row per symbol |
| DB reads | None | screener_agent is the source of screener_results; it reads no DB tables |

**Lookback calculation**:
```python
end_date = run_date
start_date = run_date - datetime.timedelta(days=OHLCV_LOOKBACK_DAYS)  # 400 days
```

---

## 4. Pipeline Steps

### Step 0 — DB setup (before any data fetch)

Open a DB connection, execute `CREATE TABLE IF NOT EXISTS screener_results (...)`, close connection immediately. Failure raises `ScreenerAgentError(phase="db_write")`.

### Step 1 — Quality Filter

1. Call `apply_quality_filter(fundamentals_df, ohlcv_df)` → `tuple[pd.DataFrame, FilterReport]`
2. `quality_df` = the returned filtered DataFrame (passing symbols only)
3. `filter_report` = the FilterReport

If `filter_report.thin_universe is True` (fewer than 3 symbols passed):
- Log `"thin_universe: only N stocks passed quality filter"`
- Call `send_alert(subject="Screener: thin universe", message="Screener: thin universe — only N stocks passed quality filter. No watchlist today.")`
- Write results to DB with empty top5 (see Section 5 for how thin_universe is recorded)
- Return `ScreenerAgentResult(thin_universe=True, top5=[], regime_blocked=False, ...)`
- Do NOT proceed to Steps 2 or 3

### Step 2 — Momentum Ranking

1. Call `compute_momentum(quality_df, ohlcv_df, top_n=MAX_TOP_N)` → `tuple[pd.DataFrame, MomentumReport]`
2. `ranked_df` = the returned ranked DataFrame (top 5, sorted by rank ascending)
3. The tiebreaker (within 2%, lower `pct_from_52w_high` wins) is handled internally by `compute_momentum` — no special handling needed here

If `ranked_df.empty` (all passing symbols lacked sufficient OHLCV history):
- This is treated as a thin_universe outcome equivalent
- Log as `"thin_universe: 0 symbols had sufficient momentum history"`
- Send alert and return early, same as Step 1 thin_universe path

### Step 3 — Regime Filter

1. Extract Nifty 50 data: `nifty_ohlcv_df = sector_df[sector_df["symbol"] == "NIFTY_50"].copy().drop(columns=["symbol"])`
2. Call `apply_regime_filter(ranked_df, nifty_ohlcv_df, open_positions=None)` → `tuple[pd.DataFrame, RegimeResult]`
3. `filtered_df` = returned DataFrame (includes `position_size_multiplier` column)
4. `regime_result` = the RegimeResult

Regime mapping:

| `regime_result.regime` | `position_size_multiplier` | `regime_blocked` | Action |
|------------------------|---------------------------|-----------------|--------|
| `"ABOVE_200DMA"` | 1.0 | False | Normal |
| `"BELOW_200DMA"` | 0.5 | False | Reduce sizing, still write results |
| `"BELOW_200DMA_10DAYS"` | 0.0 | True | Send alert, still write results with multiplier=0.0 |

When `regime_blocked=True`:
- Call `send_alert(subject="Screener: regime blocked", message="Screener: BELOW_200DMA_10DAYS for 10+ consecutive days. No new positions today. Position size multiplier = 0.")`
- Still write all top5 results to DB with `position_size_multiplier=0.0`
- The Watchlist Builder reads `position_size_multiplier` and acts accordingly; the screener agent does NOT suppress writes

**Important**: `apply_regime_filter` returns an empty DataFrame when `BELOW_200DMA_10DAYS`. In this case, build `ScreenerResult` objects directly from `ranked_df` (the pre-regime DataFrame) with `position_size_multiplier=0.0` and `regime="BELOW_200DMA_10DAYS"`. Do not skip writing — the downstream Watchlist Builder needs the top5 list even if sizing is blocked.

---

## 5. screener_results Table DDL

Execute at agent startup (before any data fetch), on the write-phase connection:

```sql
CREATE TABLE IF NOT EXISTS screener_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    momentum_score REAL NOT NULL,
    quality_passed INTEGER NOT NULL,
    regime TEXT NOT NULL,
    position_size_multiplier REAL NOT NULL,
    screened_at TEXT NOT NULL,
    UNIQUE(symbol, run_date)
);
```

**On UNIQUE conflict**: use `INSERT OR REPLACE` to handle re-runs on the same date (e.g., emergency rescreen overwrites the Monday run). This is correct behaviour — the most recent run is always authoritative.

Note: The `db-schema.md` context file has a different column set for `screener_results` (includes `roe`, `debt_to_equity`, `momentum_12_1`, `regime_above_200dma`). This spec supersedes that placeholder schema. The Coder Agent must also update `docs/context/db-schema.md` after implementation.

---

## 6. DB Connection Pattern

Follow the exact pattern from `signal_agent.py`.

```python
# WAL pragmas (module-level constant)
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL pragmas applied.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        An open sqlite3.Connection with isolation_level=None (autocommit).
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn
```

**Two-phase structure** — since screener_agent reads no DB tables:

- **SETUP phase**: open connection → `CREATE TABLE IF NOT EXISTS` → close immediately
- **WRITE phase** (after all computation): open fresh connection → `BEGIN` → `INSERT OR REPLACE` all rows → `COMMIT` → `PRAGMA wal_checkpoint(PASSIVE);` → close

ROLLBACK on exception:
```python
except sqlite3.Error as exc:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    conn.close()
    raise ScreenerAgentError(message=f"DB write failed: {exc}", phase="db_write") from exc
```

**DB path resolution**: same as `research_agent.py` — strip `sqlite:///` prefix from `settings.database_url`, resolve relative paths against project root.

---

## 7. Nifty OHLCV for Regime Filter

Use `fetch_sector_indices()`, not `fetch_ohlcv()`. This is the established pattern in `backtest/runner.py` and is confirmed in the decisions log.

```python
sector_df = fetch_sector_indices(start_date, end_date, cache_expiry_hours=0)
nifty_ohlcv_df = sector_df[sector_df["symbol"] == "NIFTY_50"].copy()
nifty_ohlcv_df = nifty_ohlcv_df.drop(columns=["symbol"])
```

`apply_regime_filter` expects `nifty_ohlcv_df` with only `"date"` and `"close"` columns (no symbol column) and at least 200 rows. The 400-day lookback provides sufficient history.

If `fetch_sector_indices` raises any exception → wrap in `ScreenerAgentError(phase="ohlcv_fetch")`.

---

## 8. Logging

All log calls use `log_agent_action(agent_name=AGENT_NAME, ...)`.

| Action string | Level | When |
|---------------|-------|------|
| `"screener_run_started: {run_date}"` | INFO | Entry, before any work |
| `"universe_fetched: {N} symbols"` | INFO | After `get_nifty_universe_for_year` |
| `"ohlcv_fetched: {N} rows"` | INFO | After `fetch_ohlcv` |
| `"fundamentals_fetched: {N} symbols"` | INFO | After `get_fundamentals_for_date` |
| `"quality_filter_complete: {N} passed of {M}"` | INFO | After quality filter |
| `"thin_universe: only {N} stocks passed quality filter"` | WARNING | When < 3 pass |
| `"momentum_scored: {N} candidates ranked"` | INFO | After momentum |
| `"regime_status: {regime}"` | INFO | After regime check |
| `"regime_blocked: BELOW_200DMA_10DAYS, no new positions"` | WARNING | When regime blocked |
| `"top5_selected: [{SYM1}, {SYM2}, ...]"` | INFO | Before write phase |
| `"screener_run_completed: {N} passed quality, top5=[...]"` | INFO | At end |

---

## 9. Notifications

- **On successful completion** (always, even if regime_blocked): `send_info("Screener complete: {N} stocks passed quality filter. Top 5: {list}. Regime: {regime}.")`
- **On thin_universe**: `send_alert(subject="Screener: thin universe", message="Screener: thin universe — only {N} stocks passed quality filter. No watchlist today.")`
- **On regime_blocked**: `send_alert(subject="Screener: regime blocked", message="Screener: BELOW_200DMA_10DAYS for 10+ consecutive days. No new positions today. Position size multiplier = 0.")`

`send_info` sends to Telegram only (Gmail always False per notifier.py). `send_alert` sends to both Telegram and Gmail.

When both thin_universe and a successful completion message would apply, only the alert fires (thin_universe returns early before the completion send_info is reached).

---

## 10. Hard Deadline / Timing

- **Scheduled run**: every Monday at 22:00 IST (orchestrator schedules this).
- **Standalone invocation**: supported — Monitor Agent calls `run_screener_agent()` directly when Nifty drops > 3% close-to-close during market hours (Phase 4 feature; see decisions log 2026-04-05).
- **No hard time deadline** for this agent (unlike signal_agent's 08:50 cutoff). If it runs long, it runs long — no safe-mode cutoff.
- `run_date` defaults to `datetime.date.today()` (evaluated in IST) when None.

---

## 11. Module-Level Constants

```python
AGENT_NAME: str = "screener_agent"
OHLCV_LOOKBACK_DAYS: int = 400
MIN_UNIVERSE_SIZE: int = 3          # mirrors quality_filter.MIN_UNIVERSE_SIZE
MAX_TOP_N: int = 5
MOMENTUM_TIEBREAKER_PCT: float = 2.0  # documented here; actual enforcement is inside compute_momentum
```

---

## 12. Error Handling

Each pipeline phase catches specific exceptions and raises `ScreenerAgentError` with the appropriate phase string:

| Phase string | Triggered by |
|--------------|-------------|
| `"ohlcv_fetch"` | `FetchError` or any `Exception` from `fetch_ohlcv` or `fetch_sector_indices` |
| `"fundamentals_fetch"` | `FundamentalsError` or `ValueError` from `get_fundamentals_for_date` |
| `"quality_filter"` | `ValueError` from `apply_quality_filter` |
| `"momentum"` | `ValueError` from `compute_momentum` |
| `"regime"` | `ValueError` from `apply_regime_filter` |
| `"db_write"` | `sqlite3.Error` from any DB operation |

Rules:
- No bare `except` clauses — always catch specific exception types.
- On fundamentals partial failure: symbols with `data_quality="failed"` or `data_quality="fundamentals_stale"` auto-fail the quality filter inside `apply_quality_filter`. No special handling needed in screener_agent.
- `ScreenerAgentError` is not caught within `run_screener_agent` — it propagates to the caller (orchestrator or monitor_agent). The orchestrator handles alerting on fatal failure.

---

## 13. Known Callers

| Caller | When | Note |
|--------|------|------|
| `src/agents/orchestrator.py` | Monday 22:00 IST (scheduled) | Primary caller |
| `src/agents/monitor_agent.py` | Emergency rescreen: Nifty 50 close-to-close drop > 3% | Phase 4 feature; calls `run_screener_agent()` standalone |

Downstream consumers of `screener_results`:
- `src/agents/research_agent.py` — reads `screener_results` to determine which symbols to research (filters on `run_date` and `quality_passed=1`)
- `src/agents/signal_agent.py` — reads `screener_results` for morning signal confirmation

---

## 14. Test Hints

Write tests in `tests/agents/test_screener_agent.py` mirroring the source structure. All strategy layer calls (`apply_quality_filter`, `compute_momentum`, `apply_regime_filter`) and data calls (`fetch_ohlcv`, `fetch_sector_indices`, `get_fundamentals_for_date`, `get_nifty_universe_for_year`) should be patched with `unittest.mock.patch`.

| # | Scenario | What to assert |
|---|----------|----------------|
| 1 | Happy path: full universe → 5 quality stocks → regime ABOVE_200DMA | `len(result.top5) == 5`, `result.thin_universe == False`, `result.regime_blocked == False`, `send_info` called once |
| 2 | thin_universe: only 2 stocks pass quality filter | `result.thin_universe == True`, `result.top5 == []`, `send_alert` called once with "thin universe" subject, `send_info` not called |
| 3 | Exactly 3 stocks pass quality filter (minimum) | Pipeline continues normally, `result.thin_universe == False` |
| 4 | regime=BELOW_200DMA | `result.regime_blocked == False`, all `ScreenerResult.position_size_multiplier == 0.5`, results written to DB |
| 5 | regime=BELOW_200DMA_10DAYS | `result.regime_blocked == True`, all `ScreenerResult.position_size_multiplier == 0.0`, `send_alert` called with "regime blocked" subject, top5 still populated and written to DB |
| 6 | OHLCV fetch raises `FetchError` | `ScreenerAgentError` raised with `phase="ohlcv_fetch"` |
| 7 | Fundamentals fetch raises `FundamentalsError` | `ScreenerAgentError` raised with `phase="fundamentals_fetch"` |
| 8 | DB write raises `sqlite3.Error` | `ScreenerAgentError` raised with `phase="db_write"` |
| 9 | Tiebreaker applied: two adjacent stocks within 2% momentum score | `compute_momentum` called with correct args; result rank order reflects tiebreaker (test by mocking `compute_momentum` return value with pre-tiebroken ranked_df) |
| 10 | All stocks fail quality filter (0 pass) | `result.thin_universe == True`, `result.symbols_passed_quality == 0`, `result.top5 == []` |
| 11 | `run_date=None` | `result.run_date == datetime.date.today()` |
| 12 | `symbols_screened` equals length of universe returned by `get_nifty_universe_for_year` | Assert `result.symbols_screened == len(mock_universe)` |
| 13 | UNIQUE(symbol, run_date) conflict: same symbol/date run twice | Second run's `INSERT OR REPLACE` succeeds without error; DB row reflects second run values |
| 14 | `ScreenerResult.screened_at` is IST timezone-aware | `result.top5[0].screened_at.tzinfo is not None`, `str(result.top5[0].screened_at.tzinfo) == "Asia/Kolkata"` |
| 15 | `send_info` called exactly once on happy path; `send_alert` not called | Mock both; assert call counts |

---

## 15. Implementation Notes for Coder Agent

1. **Import order**: `from src.data.fetcher import fetch_ohlcv, fetch_sector_indices, FetchError` — both fetchers needed.
2. **Fundamentals import**: `from src.data.fundamentals import get_fundamentals_for_date, get_nifty_universe_for_year, FundamentalsError`
3. **regime_blocked with empty filtered_df**: When `apply_regime_filter` returns an empty DataFrame (BELOW_200DMA_10DAYS), build `ScreenerResult` objects from `ranked_df` (the pre-regime ranked DataFrame) using `position_size_multiplier=0.0` and `regime="BELOW_200DMA_10DAYS"`. The ranked_df row columns are: `symbol`, `momentum_score`, `twelve_month_return`, `one_month_return`, `rank`, `pct_from_52w_high`, `within_30pct_of_52w_high`.
4. **DB schema update**: After implementing, update `docs/context/db-schema.md` to replace the placeholder `screener_results` schema with the actual DDL from Section 5.
5. **send_info signature**: `send_info(message: str) -> dict[str, bool]` — no `subject` parameter (Telegram only).
6. **`_ist_now()` helper**: define as a module-level private function returning `datetime.datetime.now(ZoneInfo("Asia/Kolkata"))` (return a datetime object, not a string) — used to populate `screened_at` on `ScreenerResult` and `completed_at` on `ScreenerAgentResult`.
7. **get_nifty_universe_for_year for live dates**: For live runs in 2026+, `get_nifty_universe_for_year(2026)` returns an empty list (range is 2010–2023 only). In this case, fall back to `fetch_nifty50_symbols()` from `src.data.fetcher`. The Coder Agent must implement this fallback.
