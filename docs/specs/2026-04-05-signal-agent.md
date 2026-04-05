# Spec: Signal Agent

**Date**: 2026-04-05
**Module**: `src/agents/signal_agent.py`
**Phase**: 3, Step 2
**Status**: Awaiting approval

---

## 1. Module Purpose and Run Time

The Signal Agent is the morning confirmation step in the trading pipeline. It runs daily at 08:20 IST. It reads the top-ranked screener candidates, fetches fresh morning OHLCV, computes technical indicators (RSI, MACD, Bollinger Bands, ATR), reads research sentiment from the previous evening's research run, applies the combined decision rule from `strategy.md`, and sends a Groq LLM confidence check as an advisory filter. Results — both BUY and HOLD signals — are written to the `signals` table for full audit trail.

The module is a plain Python function. It does not use the Python Agent SDK or Claude API.

Hard deadline: must complete by 08:50 IST. If run starts after 08:50 IST, the agent logs `late_start` and returns an empty result without writing any signals. This triggers safe mode in the orchestrator.

---

## 2. Public API

```python
import datetime
from dataclasses import dataclass


class SignalAgentError(Exception):
    """Raised when the Signal Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed: 'db_read', 'ohlcv_fetch', 'db_write'.
    """

    def __init__(self, message: str, phase: str) -> None: ...


@dataclass(frozen=True)
class StockSignal:
    """Signal result for a single stock."""

    symbol: str
    rsi: float
    macd_signal: str          # "BUY" or "HOLD"
    bollinger_position: str   # "ABOVE", "MIDDLE", or "BELOW"
    atr: float
    groq_confidence: float    # 0.0 to 1.0; -1.0 sentinel when LLM unavailable
    signal_type: str          # "BUY" or "HOLD"
    skip_reason: str | None   # populated when signal_type="HOLD", None on BUY
    signalled_at: datetime.datetime  # IST timezone-aware


@dataclass(frozen=True)
class SignalAgentResult:
    """Full output of run_signal_agent()."""

    run_date: datetime.date
    symbols_processed: int
    buy_signals: list[StockSignal]
    hold_signals: list[StockSignal]
    late_start: bool           # True if run started after 08:50 IST
    completed_at: datetime.datetime  # IST timezone-aware


def run_signal_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> SignalAgentResult:
    """Run the signal agent for the given date.

    Reads top screener candidates, computes technical indicators on fresh
    OHLCV, applies the combined decision rule, runs Groq advisory check,
    and writes all results to the signals table.

    Args:
        run_date: Date to run for. Defaults to today in IST.
        symbols: Override — use these symbols instead of reading screener_results.
                 Used in testing. If provided, screener_results table is not read.

    Returns:
        SignalAgentResult with per-stock signals and run metadata.
        Returns result with late_start=True and empty signal lists if run
        starts after 08:50 IST.

    Raises:
        SignalAgentError: If DB read fails (phase='db_read'),
                         if OHLCV fetch fails for all symbols (phase='ohlcv_fetch'),
                         or if DB write fails (phase='db_write').
                         Groq/Gemini LLM failures are handled gracefully
                         and do not raise.
    """
    ...
```

---

## 3. Inputs

### 3.1 From `screener_results` table (when `symbols` parameter is None)

```sql
SELECT symbol, rank
FROM screener_results
WHERE screened_at LIKE ? || '%'
  AND quality_passed = 1
  AND rank IS NOT NULL
ORDER BY rank ASC
LIMIT 5
```

Parameter: `run_date.isoformat()` (e.g. `"2026-04-05"`)

If no rows match: return `SignalAgentResult` with `symbols_processed=0` and empty lists. Not an error. Log `no_screener_results`.

### 3.2 From `research_reports` table

For each symbol, read the most recent completed research report:

```sql
SELECT sentiment, confidence
FROM research_reports
WHERE symbol = ?
  AND completed_at IS NOT NULL
ORDER BY completed_at DESC
LIMIT 1
```

If no completed research report exists for a symbol: use `sentiment="Neutral"`, `confidence=0.3` as defaults. Log `research_missing_for_symbol`. Do not skip the symbol.

### 3.3 From `symbols` override (testing)

When `symbols` is provided, skip the `screener_results` read. Use the provided list directly (up to first 5 entries; additional entries silently ignored). The `research_reports` read still occurs per symbol.

### 3.4 OHLCV data

Fetched via `fetch_ohlcv()` from `src/data/fetcher.py`. Lookback: 60 calendar days ending on `run_date`. This guarantees at least 40+ trading days, exceeding `MINIMUM_LOOKBACK=26` required by `add_indicators()`.

```python
start_date = run_date - datetime.timedelta(days=60)
ohlcv_df = fetch_ohlcv(symbols=target_symbols, start_date=start_date, end_date=run_date)
```

If `fetch_ohlcv()` raises `FetchError` for all symbols: raise `SignalAgentError(phase='ohlcv_fetch')`. If it fails for a subset, those symbols are added to the result's `hold_signals` with `skip_reason="ohlcv_fetch_failed"` and written to the signals table as HOLD.

---

## 4. Technical Indicator Computation

Called once on the full multi-symbol OHLCV DataFrame:

```python
from src.indicators.technical import add_indicators

ohlcv_with_indicators = add_indicators(ohlcv_df)
```

Uses all default parameters: `rsi_period=14`, `macd_fast=12`, `macd_slow=26`, `macd_signal=9`, `bb_length=20`, `bb_std=2.0`, `atr_period=14`.

For each symbol, extract the most recent row (latest trading date <= `run_date`):

- `rsi`: the `rsi` column value for that row
- `macd_hist`: the `macd_hist` column value for that row (used for BUY signal logic)
- `close`: the `close` column value for that row (used for Bollinger position)
- `bb_upper`, `bb_lower`: used for Bollinger position
- `atr`: the `atr` column value for that row

If `add_indicators()` raises `ValueError` for a symbol (e.g. fewer than 26 rows despite the 60-day window): that symbol becomes HOLD with `skip_reason="insufficient_indicator_data"`.

---

## 5. Combined Decision Rule

Applied per symbol using the most recent indicator values and research sentiment.

### Step 1 — Technical BUY signal

A technical BUY signal fires when BOTH of the following are true:
- `rsi < RSI_BUY_THRESHOLD` (40.0)
- `macd_hist > 0` (MACD histogram positive — directional confirmation)

If either condition is false: technical signal is HOLD. `signal_type = "HOLD"`, `skip_reason = "no_technical_buy_signal"`.

### Step 2 — Bollinger Band position

Compute regardless of BUY/HOLD (always written to signals table for audit):
- `close < bb_lower` → `bollinger_position = "BELOW"`
- `close > bb_upper` → `bollinger_position = "ABOVE"`
- Otherwise → `bollinger_position = "MIDDLE"`

Bollinger position is NOT a BUY condition. It is context only.

### Step 3 — Sentiment filter (only applies when technical BUY signal fired)

| Research sentiment | Action |
|-------------------|--------|
| "Positive" | Proceed to Groq check |
| "Neutral" | Proceed to Groq check |
| "Negative" | Downgrade to HOLD; `skip_reason = "negative_sentiment"`; skip Groq check |
| "Mixed" | Proceed to Groq check (Mixed is not a block) |

### Step 4 — Groq advisory check (only when technical BUY and non-Negative sentiment)

If step 3 proceeds, call Groq. If Groq confidence >= `GROQ_CONFIDENCE_THRESHOLD` (0.6): signal_type remains "BUY". If Groq confidence < 0.6: downgrade to HOLD, `skip_reason = "groq_low_confidence"`.

If Groq is unavailable and Gemini fallback also fails: keep the rule-based BUY decision, `groq_confidence = -1.0` (sentinel for LLM unavailable), log `llm_unavailable`. The BUY is NOT blocked by LLM unavailability.

### Signal type determination summary

| Technical BUY | Sentiment | Groq confidence | Final signal_type | skip_reason |
|--------------|-----------|-----------------|-------------------|-------------|
| No | Any | Not called | HOLD | "no_technical_buy_signal" |
| Yes | Negative | Not called | HOLD | "negative_sentiment" |
| Yes | Positive/Neutral/Mixed | >= 0.6 | BUY | None |
| Yes | Positive/Neutral/Mixed | < 0.6 | HOLD | "groq_low_confidence" |
| Yes | Positive/Neutral/Mixed | Unavailable | BUY | None (groq_confidence=-1.0) |

---

## 6. Groq Integration

### 6.1 Model and client

Model: `llama-3.3-70b-versatile` (free tier, 1,000 RPD).

HTTP call via `requests.post()` to the Groq API endpoint:
- URL: `https://api.groq.com/openai/v1/chat/completions`
- Auth: `Authorization: Bearer {settings.groq_api_key}`
- Content-Type: `application/json`
- Timeout: `GROQ_TIMEOUT_SECONDS` (15)

### 6.2 Prompt template

```
Evening thesis: {sentiment} sentiment (confidence: {research_confidence:.2f}).
Morning technicals: RSI={rsi:.1f}, MACD={'bullish' if macd_hist > 0 else 'bearish'},
BB={'below' if bollinger_position == 'BELOW' else 'above' if bollinger_position == 'ABOVE' else 'middle'} band.
Does the thesis still hold? Reply JSON only: {"confidence": 0.0-1.0, "reasoning": "one sentence"}
```

Where `sentiment` and `research_confidence` come from `research_reports`, and technical values come from `add_indicators()` output.

### 6.3 Response parsing

1. Parse response body as JSON
2. Extract `choices[0].message.content` string
3. Strip markdown code fences if present (same pattern as `research_agent.py`)
4. Parse inner content as JSON
5. Extract `confidence` (float) and `reasoning` (str)
6. Clamp `confidence` to `[0.0, 1.0]`

If any parsing step fails: treat as Groq failure, proceed to Gemini fallback.

### 6.4 Fallback chain

**Groq fails → Gemini fallback:**

Same prompt sent to Gemini 2.5 Flash via `google-genai` SDK:

```python
from google import genai
from google.genai import types as genai_types

client = genai.Client(api_key=settings.gemini_api_key)
response = client.models.generate_content(
    model=GEMINI_MODEL,
    contents=prompt,
    config=genai_types.GenerateContentConfig(
        system_instruction=_SIGNAL_SYSTEM_PROMPT
    ),
)
```

Gemini system instruction:
```
You are a trading signal validator for Indian equities. Given an evening research thesis and morning technical indicators, assess whether the thesis still holds. Reply ONLY with JSON: {"confidence": 0.0-1.0, "reasoning": "one sentence"}. Be conservative — default confidence is 0.5 when uncertain.
```

**Both fail:**
- Keep rule-based decision (BUY or HOLD from steps 1-3)
- Set `groq_confidence = -1.0` (sentinel value; written to DB as -1.0)
- Log `llm_unavailable`
- Do NOT skip or block the trade

### 6.5 Groq API key

`settings.groq_api_key` must be non-None. If None at run start: log `groq_api_key_missing`, skip Groq for all symbols (treat as LLM unavailable, use rule-based decisions only). Do not raise.

---

## 7. Output — `signals` Table

### 7.1 DDL

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    rsi REAL NOT NULL,
    macd_signal TEXT NOT NULL,
    bollinger_position TEXT NOT NULL,
    atr REAL NOT NULL,
    groq_confidence REAL NOT NULL,
    signal_type TEXT NOT NULL,
    skip_reason TEXT,
    signalled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_date
    ON signals(symbol, run_date);
```

### 7.2 One row written per symbol processed

Every symbol from the input list is written to `signals`, regardless of whether `signal_type` is "BUY" or "HOLD". Full audit trail is required.

### 7.3 Column values per row

| Column | Value |
|--------|-------|
| symbol | NSE ticker symbol |
| run_date | `run_date.isoformat()` (e.g. `"2026-04-05"`) |
| rsi | RSI value from `add_indicators()` output, latest row for symbol |
| macd_signal | `"BUY"` if `macd_hist > 0`, else `"HOLD"` |
| bollinger_position | `"BELOW"`, `"ABOVE"`, or `"MIDDLE"` |
| atr | ATR value from `add_indicators()` output, latest row for symbol |
| groq_confidence | Groq/Gemini confidence float, or `-1.0` if LLM unavailable |
| signal_type | `"BUY"` or `"HOLD"` |
| skip_reason | `NULL` for BUY; reason string for HOLD (see Section 5 table) |
| signalled_at | IST ISO 8601 timestamp at moment of writing this row |

### 7.4 Write timing

All rows are written after ALL symbols are processed (not one-by-one). If any DB INSERT fails: raise `SignalAgentError(phase='db_write')` and do not write any remaining rows. The partial write state is acceptable — the orchestrator must handle incomplete signal runs.

---

## 8. Hard Deadline Check

At the very start of `run_signal_agent()`, before any DB reads or OHLCV fetches:

```python
now_ist = datetime.datetime.now(tz=IST)
deadline = now_ist.replace(hour=8, minute=50, second=0, microsecond=0)
if now_ist > deadline:
    log_agent_action(agent_name=AGENT_NAME, action="late_start", level="WARNING", result="skipped")
    return SignalAgentResult(
        run_date=run_date or now_ist.date(),
        symbols_processed=0,
        buy_signals=[],
        hold_signals=[],
        late_start=True,
        completed_at=now_ist,
    )
```

The deadline check uses IST wall clock. When `run_date` is provided explicitly (e.g. in tests), the deadline check is still applied against the real current IST time. To disable the deadline check in tests, pass `run_date` to a date that has already passed AND mock `datetime.datetime.now()` to return a time before 08:50.

---

## 9. Error Handling

| Error condition | Exception | Behaviour |
|----------------|-----------|-----------|
| Run starts after 08:50 IST | No exception | Return empty result with `late_start=True`. Log `late_start`. |
| DB read failure (screener_results or research_reports) | `SignalAgentError(phase='db_read')` | Raised. Cannot determine targets or sentiment. |
| OHLCV fetch fails for ALL symbols | `SignalAgentError(phase='ohlcv_fetch')` | Raised. No indicators possible. |
| OHLCV fetch fails for one symbol | No exception | Symbol becomes HOLD with `skip_reason="ohlcv_fetch_failed"`. |
| `add_indicators()` raises `ValueError` for a symbol | No exception | Symbol becomes HOLD with `skip_reason="insufficient_indicator_data"`. Log warning. |
| No screener results for run_date | No exception | Return result with `symbols_processed=0`. Log `no_screener_results`. |
| No BUY signals produced | No exception | Return result with `buy_signals=[]`. Log `no_signals_today`. This is correct behaviour, not a bug. |
| Groq API key missing | No exception | Skip Groq for all symbols. Log `groq_api_key_missing`. Rule-based decisions stand. |
| Groq HTTP error or parse failure | No exception | Proceed to Gemini fallback. |
| Gemini failure | No exception | Keep rule-based decision. Set `groq_confidence=-1.0`. Log `llm_unavailable`. |
| DB write failure (INSERT to signals) | `SignalAgentError(phase='db_write')` | Raised. Data integrity compromised. |

Never use bare `except:`. Specific exceptions to catch:
- `sqlite3.Error` for all DB operations
- `requests.RequestException` for Groq HTTP calls
- `json.JSONDecodeError` for JSON parsing
- `ValueError` from `add_indicators()` and `fetch_ohlcv()`
- `Exception` from `client.models.generate_content()` (Gemini), with specific type logged

---

## 10. Constants

```python
AGENT_NAME: str = "signal_agent"

# Hard deadline
DEADLINE_HOUR: int = 8
DEADLINE_MINUTE: int = 50

# Technical thresholds
RSI_BUY_THRESHOLD: float = 40.0        # RSI < this → BUY technical signal
OHLCV_LOOKBACK_DAYS: int = 60          # calendar days of OHLCV to fetch

# Groq
GROQ_MODEL: str = "llama-3.3-70b-versatile"
GROQ_API_ENDPOINT: str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT_SECONDS: int = 15
GROQ_CONFIDENCE_THRESHOLD: float = 0.6  # below this → downgrade BUY to HOLD

# Gemini fallback
GEMINI_MODEL: str = "gemini-2.5-flash"

# Sentinel for LLM unavailable
LLM_UNAVAILABLE_SENTINEL: float = -1.0

# Max symbols to process per run
MAX_SYMBOLS: int = 5

# Valid values
VALID_SIGNAL_TYPES: frozenset[str] = frozenset({"BUY", "HOLD"})
VALID_BOLLINGER_POSITIONS: frozenset[str] = frozenset({"ABOVE", "MIDDLE", "BELOW"})
VALID_MACD_SIGNALS: frozenset[str] = frozenset({"BUY", "HOLD"})

# WAL pragmas (applied to every SQLite connection)
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)
```

---

## 11. Logging

All logging via `log_agent_action()` from `src/utils/logger.py`.

| When | agent_name | action | level | symbol | result |
|------|------------|--------|-------|--------|--------|
| Run starts | `signal_agent` | `"signal_run_started for {run_date}"` | `INFO` | None | None |
| Late start detected | `signal_agent` | `"late_start: current time {time} exceeds 08:50 deadline"` | `WARNING` | None | `"skipped"` |
| No screener results | `signal_agent` | `"no_screener_results for {run_date}"` | `INFO` | None | `"empty"` |
| Research missing for symbol | `signal_agent` | `"research_missing_for_symbol: using neutral defaults"` | `WARNING` | `{symbol}` | `"fallback"` |
| OHLCV fetch failed for symbol | `signal_agent` | `"ohlcv_fetch_failed"` | `WARNING` | `{symbol}` | `"error"` |
| Insufficient indicator data | `signal_agent` | `"insufficient_indicator_data"` | `WARNING` | `{symbol}` | `"skipped"` |
| Technical BUY signal fired | `signal_agent` | `"technical_buy_signal rsi={rsi:.1f} macd_hist={macd_hist:.4f}"` | `DEBUG` | `{symbol}` | `"ok"` |
| Sentiment blocks trade | `signal_agent` | `"negative_sentiment_block confidence={confidence:.2f}"` | `INFO` | `{symbol}` | `"skipped"` |
| Groq API key missing | `signal_agent` | `"groq_api_key_missing: skipping LLM check for all symbols"` | `WARNING` | None | `"fallback"` |
| Groq success | `signal_agent` | `"groq_confidence={confidence:.2f} reasoning={reasoning}"` | `INFO` | `{symbol}` | `"ok"` |
| Groq failure | `signal_agent` | `"groq_failed: {error}, trying gemini fallback"` | `WARNING` | `{symbol}` | `"retry"` |
| Gemini fallback success | `signal_agent` | `"gemini_fallback_confidence={confidence:.2f}"` | `INFO` | `{symbol}` | `"ok"` |
| Both LLMs failed | `signal_agent` | `"llm_unavailable: keeping rule_based decision"` | `WARNING` | `{symbol}` | `"fallback"` |
| Groq low confidence | `signal_agent` | `"groq_low_confidence={confidence:.2f}: downgrading BUY to HOLD"` | `INFO` | `{symbol}` | `"skipped"` |
| BUY signal written | `signal_agent` | `"buy_signal written"` | `INFO` | `{symbol}` | `"ok"` |
| HOLD signal written | `signal_agent` | `"hold_signal written reason={skip_reason}"` | `INFO` | `{symbol}` | `"ok"` |
| No BUY signals produced | `signal_agent` | `"no_signals_today"` | `INFO` | None | `"ok"` |
| Run completed | `signal_agent` | `"signal_run_completed: {n_buy} BUY, {n_hold} HOLD"` | `INFO` | None | `"ok"` |

---

## 12. Processing Order

Symbols are processed sequentially. The Groq API call is per-symbol (only for symbols with technical BUY and non-Negative sentiment). The Gemini client is created once per `run_signal_agent()` call and reused if fallback is needed.

---

## 13. Out of Scope

- This module does NOT implement Morning Validator logic (overnight news check). That is `src/agents/morning_validator_agent.py` (Phase 4).
- This module reads from `screener_results` directly, not from a morning_signals table (which does not yet exist in Phase 3).
- This module does NOT send notifications. That is the orchestrator's responsibility.
- This module does NOT apply regime filter position size adjustments. That is the Risk Agent's responsibility.
- This module does NOT calculate position sizes. ATR is written to signals table for the downstream Risk Agent to use.
- This module does NOT check the 08:15 hard deadline of the Morning Validator. It only checks its own 08:50 deadline.

---

## 14. Test Hints

The Tester Agent must cover at minimum these 10 scenarios:

1. **BUY signal with positive sentiment and Groq confirms**: RSI=35, macd_hist=0.5, sentiment="Positive", Groq returns confidence=0.75. Verify `signal_type="BUY"`, `groq_confidence=0.75`, `skip_reason=None` in both return value and DB.

2. **Technical HOLD (RSI too high)**: RSI=55, macd_hist=0.5, sentiment="Positive". Verify `signal_type="HOLD"`, `skip_reason="no_technical_buy_signal"`, Groq is NOT called.

3. **Technical HOLD (MACD bearish)**: RSI=35, macd_hist=-0.3, sentiment="Positive". Verify `signal_type="HOLD"`, `skip_reason="no_technical_buy_signal"`, Groq is NOT called.

4. **Negative sentiment blocks BUY**: RSI=30, macd_hist=0.8, sentiment="Negative". Verify `signal_type="HOLD"`, `skip_reason="negative_sentiment"`, Groq is NOT called.

5. **Groq low confidence downgrades BUY**: RSI=35, macd_hist=0.5, sentiment="Neutral", Groq returns confidence=0.45. Verify `signal_type="HOLD"`, `skip_reason="groq_low_confidence"`, `groq_confidence=0.45`.

6. **Both LLMs fail — rule-based BUY preserved**: RSI=35, macd_hist=0.5, sentiment="Positive". Mock Groq to raise `requests.RequestException`. Mock Gemini to raise `Exception`. Verify `signal_type="BUY"`, `groq_confidence=-1.0`, `skip_reason=None`.

7. **Late start returns empty result**: Mock `datetime.datetime.now()` to return 08:55 IST. Verify `SignalAgentResult.late_start=True`, `symbols_processed=0`, no rows written to signals table.

8. **Research missing uses neutral defaults**: Symbol has no completed `research_reports` row. Verify the symbol is still processed (not skipped), sentiment defaults to "Neutral", confidence to 0.3, and this is logged as `research_missing_for_symbol`.

9. **OHLCV fetch fails for one symbol**: `fetch_ohlcv()` raises `FetchError` for symbol "WIPRO" but succeeds for others. Verify WIPRO is written to signals table as HOLD with `skip_reason="ohlcv_fetch_failed"`, other symbols processed normally.

10. **Full audit trail — all symbols written regardless of signal_type**: Pass 3 symbols; 1 gets BUY, 2 get HOLD for different reasons. Verify all 3 rows exist in signals table with correct `signal_type` and `skip_reason` values.

11. **Bollinger position computed correctly for all three cases**: Provide close prices that are below bb_lower, above bb_upper, and between the bands. Verify `bollinger_position` values "BELOW", "ABOVE", "MIDDLE" respectively.

12. **OHLCV lookback window**: Verify `fetch_ohlcv()` is called with `start_date = run_date - timedelta(days=60)` and `end_date = run_date`.

13. **symbols override bypasses screener_results**: Pass `symbols=["TCS", "INFY"]`. Verify no SQL query to `screener_results`. Verify both symbols are processed.

14. **MAX_SYMBOLS cap**: Pass `symbols=["A", "B", "C", "D", "E", "F", "G"]`. Verify only 5 are processed and written to signals table.

15. **Groq API key missing — all symbols use rule-based decisions**: Set `settings.groq_api_key` to None. Verify `groq_api_key_missing` logged once, all symbols with technical BUY remain BUY (not blocked), `groq_confidence=-1.0` for each.

---

## 15. File Locations

| File | Action |
|------|--------|
| `src/agents/__init__.py` | Already exists (created for research_agent.py) |
| `src/agents/signal_agent.py` | Create (main module) |
| `tests/agents/__init__.py` | Already exists (created for research_agent.py) |
| `tests/agents/test_signal_agent.py` | Create (tests) |

---

## 16. Dependencies

No new packages required. All dependencies already present:
- `requests` — Groq HTTP calls
- `google-genai>=1.0.0` — Gemini fallback (already installed at 1.69.0)
- `src/indicators/technical.py` — `add_indicators()`
- `src/data/fetcher.py` — `fetch_ohlcv()`
- `src/config/settings.py` — `settings` singleton
- `src/utils/logger.py` — `log_agent_action()`
