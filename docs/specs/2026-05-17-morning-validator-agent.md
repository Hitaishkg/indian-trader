# Morning Validator Agent — Spec

Date: 2026-05-17
Module path: `src/agents/morning_validator_agent.py`
Phase: 4 (replaces orchestrator placeholder at lines 207–214)

---

## 1. Module Purpose

Runs at 08:00 IST every weekday morning. Reads today's human-approved watchlist, fetches the last 12 hours of news per stock via Tavily, uses a single Gemini call per stock to detect material overnight events (earnings, trading halt, circuit breaker, RBI decision, promoter fraud, SEBI investigation) that would invalidate a swing trade. Stocks flagged for material events are removed. Surviving stocks then receive a fresh morning OHLCV fetch (latest close as morning price proxy) and a regime re-confirmation against the Nifty 50 200 DMA. All survivors are written to the `morning_signals` table. Hard deadline is 08:15 IST — if exceeded, the agent enters safe mode, writes nothing, sends an alert, and returns without crashing. News-fetch failures per stock are non-fatal: the stock is kept with `overnight_news_checked=False`.

---

## 2. Public API

```python
def run_morning_validator_agent(
    run_date: datetime.date | None = None,
    db_path_override: str | None = None,
) -> MorningValidatorResult:
    """Run morning validation for today's watchlist.

    Reads watchlist rows where run_date matches and human_approved=1.
    For each stock: fetch last 12h news, call Gemini for material-event
    detection, fetch morning OHLCV close, re-confirm regime. Writes
    surviving stocks to morning_signals. Hard deadline 08:15 IST.

    Args:
        run_date: Date to validate for. Defaults to today in IST.
        db_path_override: Override DB path for tests. None → resolve from settings.

    Returns:
        MorningValidatorResult summarising what was validated, removed,
        and whether safe mode was activated.

    Raises:
        MorningValidatorError: On fatal DB read/write failures or
            missing TAVILY_API_KEY / GEMINI_API_KEY.
    """
```

```python
@dataclass(frozen=True)
class MorningValidatorResult:
    run_date: datetime.date
    watchlist_size: int                 # rows read from watchlist (human_approved=1)
    validated_count: int                # rows written to morning_signals
    removed_count: int                  # symbols dropped for overnight events
    removal_reasons: list[str]          # ["HDFCBANK: earnings_dropped", ...]
    regime_confirmed: bool              # True if regime unchanged from screener run
    regime_now: str                     # "ABOVE_200DMA"/"BELOW_200DMA"/"BELOW_200DMA_10DAYS"
    safe_mode: bool                     # True if 08:15 deadline exceeded
    completed_at: datetime.datetime     # IST tz-aware
```

```python
class MorningValidatorError(Exception):
    """Raised on fatal failures.

    Attributes:
        message: Human-readable error.
        phase: One of 'watchlist_read', 'news_fetch', 'ohlcv_fetch',
               'regime_fetch', 'db_write', 'config'.
    """
    def __init__(self, message: str, phase: str) -> None: ...
```

Module-level constants exposed for import by tests:

```python
AGENT_NAME: str = "morning_validator_agent"
DEADLINE_HOUR: int = 8
DEADLINE_MINUTE: int = 15
NEWS_LOOKBACK_HOURS: int = 12
TAVILY_MAX_RESULTS: int = 8
TAVILY_REQUEST_DELAY: float = 0.5
GEMINI_MODEL: str = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECONDS: int = 20
OHLCV_LOOKBACK_DAYS: int = 5  # to get latest close as morning proxy
REGIME_LOOKBACK_DAYS: int = 400
MATERIAL_EVENT_KEYWORDS: tuple[str, ...] = (
    "earnings", "quarterly results", "Q1", "Q2", "Q3", "Q4",
    "trading halt", "circuit breaker", "upper circuit", "lower circuit",
    "RBI rate", "repo rate", "MPC decision",
    "SEBI investigation", "SEBI order", "promoter fraud", "promoter pledge",
    "promoter resignation", "auditor resignation", "ratings downgrade",
)
```

---

## 3. Input Contract

### Reads from `watchlist` (sqlite)
- Filter: `run_date = ? AND human_approved = 1`
- Columns used: `symbol, sentiment, confidence, rank, regime, position_size_multiplier, scorecard_score, scorecard_max`
- Empty result → return immediately with `validated_count=0, removed_count=0, safe_mode=False`, no DB writes, no notifications.

### External calls
- `tavily.TavilyClient.search(query, topic="news", time_range="day", max_results=TAVILY_MAX_RESULTS, include_answer=False, search_depth="basic")` — one call per watchlist symbol.
- `ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=settings.gemini_api_key, temperature=0.0).with_structured_output(MaterialEventVerdict).invoke(messages)` — one call per stock that has news articles.
- `fetch_ohlcv(symbols=survivors, start_date=run_date - timedelta(days=5), end_date=run_date, cache_expiry_hours=0)` — one batch call after survivors are determined.
- `fetch_sector_indices(start_date=run_date - timedelta(days=400), end_date=run_date, cache_expiry_hours=0)` then `apply_regime_filter(ranked_df=empty_synthetic_df, nifty_ohlcv_df=nifty_slice, open_positions=None)` — only the `RegimeResult.regime` field is used.

### Preconditions
- `settings.tavily_api_key` set → else raise `MorningValidatorError(phase="config")`.
- `settings.gemini_api_key` set → else raise `MorningValidatorError(phase="config")`.
- DB path resolvable (same pattern as `screener_agent._resolve_db_path`).

---

## 4. Output Contract

### `morning_signals` table

CREATE TABLE IF NOT EXISTS at start of run:

```sql
CREATE TABLE IF NOT EXISTS morning_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    latest_price REAL NOT NULL,
    regime TEXT NOT NULL,
    position_size_multiplier REAL NOT NULL,
    overnight_news_checked INTEGER NOT NULL,   -- 1 if Tavily+Gemini ran, 0 if Tavily failed
    removal_reason TEXT,                        -- NULL for written rows (only kept for symmetry; survivors only)
    validated_at TEXT NOT NULL,
    UNIQUE(symbol, run_date)
);
CREATE INDEX IF NOT EXISTS idx_morning_signals_run_date ON morning_signals(run_date);
```

Note: removed stocks do NOT get a row. The `removal_reason` column is always NULL in practice for written rows. It is retained because db-schema.md describes it and to keep the column position stable; future versions may write a removal row instead. For now: survivors only.

Writes use `INSERT OR REPLACE` on `UNIQUE(symbol, run_date)` so re-runs overwrite cleanly.

### Guarantees
- `latest_price` is the most recent close from morning OHLCV fetch; if OHLCV fetch returns no rows for a symbol → that symbol is skipped (counted as removed with reason `ohlcv_unavailable`).
- `regime` is the Nifty 50 regime computed fresh this morning (NOT the regime from the watchlist row).
- `position_size_multiplier` is taken from the freshly computed regime (overrides any value from watchlist if regime changed overnight).
- All timestamps IST, ISO 8601 with seconds precision, timezone offset present.

### Safe mode behaviour
If `_ist_now() >= deadline_for(run_date)` is True at any check point AFTER watchlist read:
- Log `deadline_exceeded`.
- Send alert "Morning validator: 08:15 deadline exceeded — safe mode, no validations written."
- Return `MorningValidatorResult(safe_mode=True, validated_count=0, removed_count=0, ...)`.
- Do NOT write any rows.
- Do NOT raise.

Deadline check points (in order): immediately after watchlist read, after news+Gemini loop completes, after OHLCV fetch, after regime fetch. If any check fires, abort and return safe mode.

---

## 5. Implementation Details

### Step order
1. Resolve `run_date` and `db_path`. Log `morning_validation_started`.
2. Verify Tavily + Gemini keys (raise on missing).
3. Open SQLite, CREATE TABLE IF NOT EXISTS `morning_signals`, close.
4. Read watchlist rows for `run_date` where `human_approved=1`. If empty → return early with zero counts.
5. Deadline check #1.
6. Instantiate shared `TavilyClient` and shared `ChatGoogleGenerativeAI` (Pydantic structured output bound to `MaterialEventVerdict`).
7. For each symbol:
   - Build query: `f"{symbol} {company_name} news"` using `SYMBOL_TO_COMPANY` map imported from `research_agent` (or duplicate locally).
   - Call Tavily `time_range="day"`. On `Exception` → mark `overnight_news_checked=False`, KEEP the stock (do not remove on news fetch failure), log `news_fetch_failed`.
   - If articles returned and contain any keyword in `MATERIAL_EVENT_KEYWORDS` OR Gemini decides material → invoke `_check_material_event(symbol, articles)`.
   - The Gemini call returns `MaterialEventVerdict(is_material: bool, event_type: str, reasoning: str)`.
   - If `is_material=True` → remove the stock, append `f"{symbol}: {event_type}"` to `removal_reasons`, log `overnight_event_removal`.
   - If Gemini call itself fails → fail-open: KEEP the stock, mark `overnight_news_checked=True`, log `gemini_check_failed`.
   - Respect `TAVILY_REQUEST_DELAY` between Tavily calls.
8. Deadline check #2.
9. Determine `survivors = watchlist_symbols - removed`. If empty → skip OHLCV/regime, write no rows, send info, return.
10. Fetch fresh OHLCV for survivors (`cache_expiry_hours=0`). For any survivor with zero rows in returned df → drop from survivors and add `f"{symbol}: ohlcv_unavailable"` to `removal_reasons`.
11. Deadline check #3.
12. Fetch Nifty 50 sector index slice, compute regime via `apply_regime_filter()`. To call `apply_regime_filter` the agent must construct a minimal `ranked_df` containing all 7 momentum columns (symbol, momentum_score, twelve_month_return, one_month_return, rank, pct_from_52w_high, within_30pct_of_52w_high) populated with dummies for the survivor symbols — sufficient to extract `RegimeResult.regime` and the per-row `position_size_multiplier`.
13. Read prior regime from `screener_results` for `run_date` (most recent row); compute `regime_confirmed = (new_regime == prior_regime)`. If no screener row exists → `regime_confirmed=True` (no baseline to compare against).
14. Deadline check #4.
15. Build per-symbol `latest_price` from the OHLCV df (latest close).
16. Open new SQLite connection, BEGIN, INSERT OR REPLACE each survivor row, COMMIT, WAL checkpoint, close.
17. Log `morning_validation_completed` with summary outside any transaction.
18. Send notification:
   - If `removed_count > 0` → `send_alert("Morning validator: overnight events", "Removed: <reasons>. Survivors: <symbols>.")`
   - Else → `send_info("Morning validator: 0 removals. <N> stocks validated. Regime: <regime>.")`
19. Return `MorningValidatorResult`.

### Material-event detection prompt (Gemini)

System: `"You are a swing-trade risk analyst. Decide whether the supplied news for an NSE stock represents a material overnight event that would invalidate an existing or planned 3-10 day swing position."`

User: includes symbol, company name, today's date, list of `{title, url, content[:400], published_date}` from Tavily.

Output schema (pydantic):
```python
class MaterialEventVerdict(BaseModel):
    is_material: bool = Field(description="True if any item is a material event.")
    event_type: Literal[
        "earnings_dropped", "trading_halt", "circuit_breaker",
        "rbi_decision", "sebi_investigation", "promoter_fraud",
        "ratings_downgrade", "other_material", "none"
    ] = Field(description="Event type. 'none' if is_material=False.")
    reasoning: str = Field(description="One sentence explaining the decision.")
```

Material events (must trigger `is_material=True`): earnings report, trading halt, circuit breaker, RBI rate decision directly affecting the sector, promoter fraud allegation, SEBI investigation, ratings downgrade, auditor resignation, major regulatory action.

Non-material (must NOT trigger): analyst upgrades/downgrades, price target changes, minor sector news, general market commentary, individual broker reports, target-price revisions.

### Deadline check helper
```python
def _deadline_exceeded(run_date: datetime.date) -> bool:
    """True if current IST time has passed 08:15 for run_date."""
    deadline = datetime.datetime.combine(
        run_date,
        datetime.time(DEADLINE_HOUR, DEADLINE_MINUTE),
        tzinfo=IST,
    )
    return _ist_now() >= deadline
```

When `run_date != today` (e.g. backtest replay) — deadline check still uses `run_date`'s 08:15; this is correct because backtest replays should not exceed the historical deadline either.

---

## 6. Constants Summary

| Constant | Value | Reason |
|---|---|---|
| `AGENT_NAME` | `"morning_validator_agent"` | log_agent_action identifier |
| `DEADLINE_HOUR` / `DEADLINE_MINUTE` | 8 / 15 | Hard cut-off per agents-trading.md |
| `NEWS_LOOKBACK_HOURS` | 12 | Per rules — last 12h overnight |
| `TAVILY_MAX_RESULTS` | 8 | Match research_agent value |
| `TAVILY_REQUEST_DELAY` | 0.5 | Match research_agent throttle |
| `GEMINI_MODEL` | `"gemini-2.5-flash"` | Same model family as research_agent |
| `GEMINI_TIMEOUT_SECONDS` | 20 | Bounded per-stock latency |
| `OHLCV_LOOKBACK_DAYS` | 5 | Just enough to get a recent close |
| `REGIME_LOOKBACK_DAYS` | 400 | Required for 200 DMA computation |
| `MATERIAL_EVENT_KEYWORDS` | tuple of 15+ phrases | Pre-filter — short-circuits Gemini calls when no keywords appear (optimization) |

---

## 7. Logging

All via `log_agent_action(agent_name=AGENT_NAME, ...)`:

| When | action | level | symbol | result |
|---|---|---|---|---|
| Run start | `f"morning_validation_started: {run_date}"` | INFO | None | None |
| Watchlist read | `f"watchlist_loaded: {n} approved stocks"` | INFO | None | None |
| Empty watchlist | `"empty_watchlist: no approved stocks, skipping"` | INFO | None | `"empty"` |
| Per-stock news ok | `f"news_fetched: {n_articles} articles"` | DEBUG | symbol | `"ok"` |
| Tavily failure | `f"news_fetch_failed: {exc}"` | WARNING | symbol | `"error"` |
| Gemini removal | `f"overnight_event_removal: {event_type} — {reasoning}"` | WARNING | symbol | `"removed"` |
| Gemini failure | `f"gemini_check_failed: {exc} — keeping stock"` | WARNING | symbol | `"error"` |
| OHLCV missing | `"ohlcv_unavailable"` | WARNING | symbol | `"removed"` |
| Regime change | `f"regime_changed: {prior} → {current}"` | WARNING | None | None |
| Regime confirmed | `f"regime_confirmed: {regime}"` | INFO | None | None |
| Deadline exceeded | `"deadline_exceeded: safe mode activated"` | ERROR | None | `"safe_mode"` |
| DB write done | `f"morning_signals_written: {n} rows"` | INFO | None | `"ok"` |
| Run complete | `f"morning_validation_completed: validated={v} removed={r}"` | INFO | None | `"ok"` |

All `log_agent_action` calls must happen OUTSIDE any open SQLite transaction (per CLAUDE.md known gotcha).

---

## 8. Error Handling

- `settings.tavily_api_key` or `settings.gemini_api_key` missing → raise `MorningValidatorError(phase="config")` immediately.
- `sqlite3.Error` during table CREATE / watchlist SELECT → raise `MorningValidatorError(phase="watchlist_read")` or `phase="db_write"`.
- Tavily per-stock exception → catch broad `Exception`, log warning, KEEP stock with `overnight_news_checked=False`. Never raise.
- Gemini per-stock exception → catch broad `Exception`, log warning, KEEP stock with `overnight_news_checked=True`. Never raise.
- `FetchError` / `Exception` on bulk OHLCV fetch → raise `MorningValidatorError(phase="ohlcv_fetch")` only if the entire batch fails. Individual symbols missing from the result df are handled by the per-symbol drop in step 10.
- `Exception` on sector-index fetch or regime computation → raise `MorningValidatorError(phase="regime_fetch")`.
- `sqlite3.Error` on results INSERT → rollback, close, raise `MorningValidatorError(phase="db_write")`.
- No `except:` (bare). All except blocks specify the exception class.

---

## 9. Out of Scope

- No technical indicator computation (signal_agent does that at 08:20).
- No re-running quality filter (screener already ran).
- No re-running momentum ranking (screener already ran).
- No re-fetching fundamentals.
- No order placement.
- No human checkpoint — execution_agent owns that at 09:05.
- No GTT reconciliation.
- No price-slippage handling.
- No stock additions — only removals from the watchlist.
- Does NOT modify the `watchlist` table — the watchlist is the source of truth; this agent's outputs live in `morning_signals` only.
- No special handling for `regime_now == BELOW_200DMA_10DAYS` beyond writing the survivor rows with `position_size_multiplier=0.0` — execution_agent / risk_agent decide what to do with zero-sized positions.

---

## 10. Test Hints (Tester Agent must cover at least 10 scenarios)

1. **Empty watchlist** — no rows where `human_approved=1` for run_date → returns immediately, `validated_count=0`, no DB writes, no notifications.
2. **All survive, regime unchanged** — 3 stocks, Tavily returns benign news, Gemini returns `is_material=False` for all → 3 rows in morning_signals, `regime_confirmed=True`.
3. **One stock removed for earnings** — Gemini returns `is_material=True, event_type="earnings_dropped"` → that symbol absent from morning_signals; others present; `removal_reasons` contains `"<SYMBOL>: earnings_dropped"`.
4. **Tavily failure for one stock** — exception raised inside `search()` → stock kept, `overnight_news_checked=0` in DB row.
5. **Gemini failure for one stock** — exception inside `with_structured_output().invoke()` → stock kept, log shows `gemini_check_failed`.
6. **OHLCV missing for one survivor** — fetcher returns df without that symbol → symbol dropped, `removal_reasons` contains `"<SYMBOL>: ohlcv_unavailable"`.
7. **Regime changed overnight** — prior screener regime was `ABOVE_200DMA`, current computes to `BELOW_200DMA` → `regime_confirmed=False`, all written rows have `regime="BELOW_200DMA"` and `position_size_multiplier=0.5`.
8. **Deadline exceeded** — monkeypatch `_ist_now()` to return 08:16 → safe mode, no DB rows written, alert sent, `safe_mode=True` in result.
9. **Missing Tavily key** — `settings.tavily_api_key=None` → raises `MorningValidatorError(phase="config")` before any external call.
10. **Missing Gemini key** — `settings.gemini_api_key=None` → raises `MorningValidatorError(phase="config")`.
11. **INSERT OR REPLACE behaviour** — run twice in the same minute for the same run_date → no duplicates; second run's values present.
12. **db_path_override honoured** — passing a tmp path creates the morning_signals table there and writes there, not into the default DB.
13. **Material keyword bypass** — Tavily returns 0 articles → Gemini call short-circuited; stock kept with `overnight_news_checked=True`.
14. **All removed** — every stock flagged material → 0 rows written, alert sent listing all removals, `validated_count=0`, `removed_count=N`.
15. **Non-material news only** — analyst upgrade / price target change → Gemini returns `is_material=False` → stock kept (verifies prompt distinguishes correctly with a stub LLM).

For LLM-dependent tests, monkeypatch `ChatGoogleGenerativeAI` factory and `TavilyClient` at the module level. Pattern: replicate the stubs used in `tests/agents/test_research_agent.py`.

---

## 11. File Locations

| Path | Action |
|---|---|
| `src/agents/morning_validator_agent.py` | Create |
| `src/agents/__init__.py` | Already exists — no changes |
| `tests/agents/test_morning_validator_agent.py` | Create (Tester Agent) |

Orchestrator integration is a separate small follow-up: after this module is built and passes tests, replace `_run_morning_validator` body in `src/agents/orchestrator.py` lines 207–214 with a call to `run_morning_validator_agent(run_date=run_date, db_path_override=db_path_override)` and add a try/except for `MorningValidatorError` (log, continue — same pattern as `_run_data_collection`). That replacement is NOT part of this spec — Architect Agent will spec it separately if needed, or the orchestrator already follows the same template.

---

## 12. pyproject.toml

No new dependencies. The module reuses what is already declared:
- `tavily-python` (used by research_agent)
- `langchain-google-genai` (used by research_agent)
- `langchain-core` (used by research_agent)
- `pydantic` (used by research_agent)

Confirm in pyproject.toml before coding. If any are missing → Coder Agent flags and stops.

---

## 13. Key Design Decisions

1. **Removals only, never additions.** Watchlist is the source of truth. Morning validator can drop stocks but never add. Keeps the data flow strictly one-way.
2. **Fail-open on news/Gemini errors.** If Tavily or Gemini fail for a stock, the stock is kept (not removed). This is symmetric with the rule "if both LLMs fail, keep rule-based BUY" in signal_agent. Marked with `overnight_news_checked=0` so downstream can see degraded data.
3. **Fresh regime, not watchlist regime.** Regime is recomputed from fresh Nifty 50 OHLCV — overnight gap could flip the regime, and the morning is precisely when that needs to be detected.
4. **No row written for removed stocks.** Simpler downstream: execution_agent and signal_agent can just SELECT * FROM morning_signals WHERE run_date=?. Removal audit lives in agent_logs.
5. **Single Gemini call per stock.** No iterative ReAct loop here — speed matters (08:15 deadline). One structured output call with all the articles inlined.
6. **Material event keyword pre-filter is optional pruning.** Even if no keyword matches, still call Gemini once because keywords can miss novel phrasings. Keep the keyword list as logging context, not as a hard gate. (Implementation decision: always call Gemini when ≥1 article is returned. Skip Gemini only when 0 articles.)
7. **Deadline check at 4 points, not continuously.** Simpler than a timeout thread, sufficient because the bottleneck is the per-stock Tavily+Gemini loop.
8. **morning_signals table DDL matches db-schema.md columns plus a few additions** (`latest_price`, `position_size_multiplier`, `overnight_news_checked`) needed for execution_agent. db-schema.md will be updated by the Docs Agent post-build.
