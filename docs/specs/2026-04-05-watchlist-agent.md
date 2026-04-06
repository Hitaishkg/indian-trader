# Spec: src/agents/watchlist_agent.py

**Date:** 2026-04-05
**Agent tier:** Opus (claude-opus-4-5)
**Phase:** 3, Step 4
**Status:** Awaiting approval

---

## 1. Purpose

`watchlist_agent.py` runs at approximately 23:30 IST each evening, after `research_agent.py` has completed. It reads today's top-5 screener candidates from `screener_results` and their completed sentiment reports from `research_reports`, applies the combined decision rule (screener rank = BUY intent, LLM sentiment as veto), computes a partial pre-trade scorecard for human visibility, sends a Telegram+Gmail checkpoint message listing all PROCEED candidates for human approval, and writes one row per candidate to the `watchlist` table. It also exposes two helper functions called by the orchestrator: `check_watchlist_timeout()` (called at 07:00 IST next morning to mark unanswered rows) and `record_human_approval()` (called when the human replies via Telegram). This module is Opus-tier because it synthesises screener rank, sentiment, and regime signals into a final combined decision that determines which trades will be proposed to the human the next morning — the highest-stakes judgement in the evening pipeline.

---

## 2. Public API

### Exception

```python
class WatchlistAgentError(Exception):
    def __init__(self, message: str, phase: str) -> None: ...
    # Valid phases: "db_read", "db_write", "notification", "timeout_check"
```

`message` and `phase` stored as instance attributes. `super().__init__(f"[{phase}] {message}")`.

---

### WatchlistCandidate (frozen dataclass)

Internal intermediate object built during the decision phase. Not written to DB — used to compute `WatchlistEntry` objects.

```python
@dataclass(frozen=True)
class WatchlistCandidate:
    symbol: str
    rank: int                              # from screener_results (1 = best)
    momentum_score: float
    regime: str                            # "ABOVE_200DMA" / "BELOW_200DMA" / "BELOW_200DMA_10DAYS"
    position_size_multiplier: float        # 1.0 / 0.5 / 0.0
    sentiment: str                         # "Positive" / "Negative" / "Neutral" / "Mixed"
    confidence: float                      # 0.0–1.0
    earnings_transcript_unavailable: bool
    combined_decision: str                 # "PROCEED" or "SKIP"
    skip_reason: str | None                # None if PROCEED; reason string if SKIP
    scorecard_score: int                   # 0–20 (partial; RSI/MACD/risk-reward scored tomorrow)
    scorecard_max: int                     # 20 normally; 15 when earnings_transcript_unavailable=True
```

**scorecard_max note:** At watchlist stage, only 4 criteria are scoreable (quality=5, rank=5, regime=5, sentiment=5 = max 20). When `earnings_transcript_unavailable=True`, the sentiment criterion is still scored but `scorecard_max` becomes 15 (the "no earnings in next 5 days" criterion — worth 5 at full scorecard — is excluded, reducing the full-scorecard max from 40 to 35, and reducing the watchlist-stage max from 20 to 15). See Section 5 for full breakdown.

---

### WatchlistEntry (frozen dataclass)

Written to the `watchlist` DB table. One row per candidate (PROCEED and SKIP both written — full audit trail).

```python
@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    combined_decision: str               # "PROCEED" or "SKIP"
    scorecard_score: int
    scorecard_max: int
    sentiment: str
    confidence: float
    rank: int
    regime: str
    position_size_multiplier: float
    human_approved: bool                 # False until record_human_approval() called
    approval_source: str | None          # None until approved or timed out
    added_at: datetime.datetime          # IST-aware
    run_date: datetime.date
```

---

### WatchlistAgentResult (frozen dataclass)

Returned by `run_watchlist_agent()`.

```python
@dataclass(frozen=True)
class WatchlistAgentResult:
    run_date: datetime.date
    candidates_evaluated: int            # total rows read from screener_results (quality_passed=1)
    proceed_count: int                   # candidates with combined_decision="PROCEED"
    skipped_count: int                   # candidates with combined_decision="SKIP"
    approved_symbols: list[str]          # symbols written with human_approved=False (pending)
    human_responded: bool                # always False at run time; updated by check_watchlist_timeout()
    completed_at: datetime.datetime      # IST-aware
```

**Note:** `approved_symbols` at run time contains the PROCEED symbols (pending approval), not yet human-confirmed. The field name reflects "symbols written to watchlist for potential approval".

---

### Entry point

```python
def run_watchlist_agent(
    run_date: datetime.date | None = None,
) -> WatchlistAgentResult:
    """Run the Watchlist Builder Agent for the given date.

    Reads screener_results and research_reports, applies the combined
    decision rule, computes partial pre-trade scorecard, writes all
    candidates (PROCEED and SKIP) to watchlist table, and sends a
    checkpoint notification for human approval.

    Args:
        run_date: Date to run for. Defaults to today in IST.

    Returns:
        WatchlistAgentResult with run summary.

    Raises:
        WatchlistAgentError: On DB read failure (phase='db_read'),
                             DB write failure (phase='db_write'), or
                             notification failure when both channels fail
                             (phase='notification').
    """
```

---

### Orchestrator-facing helpers

```python
def check_watchlist_timeout(run_date: datetime.date) -> None:
    """Mark unanswered watchlist rows as timed out at 07:00 IST.

    Called by the orchestrator at 07:00 IST the morning after run_watchlist_agent().
    Finds all rows for run_date where human_approved=0 and approval_source IS NULL,
    sets approval_source='timeout_skip', and sends an alert notification.

    Args:
        run_date: The date whose pending rows should be timed out.

    Raises:
        WatchlistAgentError: On DB write failure (phase='timeout_check').
    """

def record_human_approval(
    symbol: str,
    run_date: datetime.date,
    approved: bool,
) -> None:
    """Record a human approval or rejection for a watchlist symbol.

    Called by the orchestrator when it parses a Telegram reply.
    Updates the watchlist row for (symbol, run_date):
      - human_approved = 1 if approved else 0
      - approval_source = "human_explicit"

    If no row exists for (symbol, run_date), logs a warning and returns
    without error (no-op). Does NOT raise.

    Args:
        symbol: NSE ticker symbol to approve/reject.
        run_date: The watchlist run date this approval applies to.
        approved: True to approve, False to reject.
    """
```

---

## 3. Inputs — DB reads

Both reads happen in a single READ phase with one connection open-and-close.

### screener_results read

```sql
SELECT symbol, rank, momentum_score, regime, position_size_multiplier
FROM screener_results
WHERE run_date = ?
  AND quality_passed = 1
ORDER BY rank ASC
```

- `run_date` parameter: `run_date.isoformat()` (ISO date string, e.g. `"2026-04-05"`)
- The `screener_results.run_date` column stores ISO date strings (confirmed from screener_agent.py source).
- Limit: at most 5 rows (upstream screener writes max 5).

### research_reports read

Fetch one completed row per symbol. Execute once per screener symbol (in a loop), or fetch all symbols in a single query using an IN clause:

```sql
SELECT symbol, sentiment, confidence, earnings_transcript_unavailable
FROM research_reports
WHERE symbol = ?
  AND run_date = ?
  AND completed_at IS NOT NULL
ORDER BY completed_at DESC
LIMIT 1
```

- `run_date` parameter: `run_date.isoformat()`
- Uses `research_reports.run_date` column (set by research_agent at INSERT time) — more robust than `DATE(completed_at)` which is fragile against midnight boundary edge cases.
- `completed_at IS NOT NULL` is the race condition guard: only fully-processed rows.
- `ORDER BY completed_at DESC LIMIT 1` takes the most recent completed row if somehow more than one exists for the same symbol+run_date.

### Join logic (in Python, not SQL)

After fetching both result sets, build a dict keyed by symbol for research results. For each screener row:
- If the symbol has a matching research row → build `WatchlistCandidate` with full data.
- If no matching research row → log `"research_incomplete: {symbol}"` and skip this symbol. Do NOT include in candidates list.

---

## 4. Combined decision rule

All screener candidates are treated as BUY-intent signals (having a screener rank implies the strategy wants to buy them). The decision rule:

| Condition | combined_decision | skip_reason |
|-----------|------------------|-------------|
| `position_size_multiplier == 0.0` | "SKIP" | "regime_blocked" |
| sentiment == "Negative" | "SKIP" | "negative_sentiment" |
| sentiment == "Positive" | "PROCEED" | None |
| sentiment == "Neutral" | "PROCEED" | None |
| sentiment == "Mixed" | "PROCEED" | None |

**Evaluation order:** check `position_size_multiplier == 0.0` first (regime_blocked takes priority over sentiment). Then check sentiment.

**Rationale for Mixed → PROCEED:** strategy.md's combined decision rule table shows three PROCEED rows (Positive, Neutral, and implicitly anything non-Negative). Mixed means contradictory signals but not definitively Negative. The LLM's veto power applies only to confirmed Negative sentiment. Mixed gets a lower scorecard score (1 point vs Positive 5) which informs the human during review.

---

## 5. Pre-trade scorecard

Computed for **all** candidates (PROCEED and SKIP alike) — the human can see why a candidate was scored low.

The full scorecard (from risk.md) has 8 criteria worth 40 points total (or 35 when earnings upcoming). At watchlist stage, only 4 of those 8 criteria are computable:

| Criterion | Max points | Computable at watchlist stage? | How to score |
|-----------|-----------|-------------------------------|-------------|
| Stock passed all 5 quality filters | 5 | Yes — always 5 (quality_passed=1 is a prerequisite to be a candidate) | Always 5 |
| Momentum rank in top 3 | 5 | Yes | rank ≤ 3 → 5 points; rank 4 or 5 → 0 points |
| Regime filter: Nifty above 200 DMA | 5 | Yes | "ABOVE_200DMA" → 5; "BELOW_200DMA" → 2; "BELOW_200DMA_10DAYS" → 0 |
| RSI confirms entry signal (< 40) | 5 | **No** — signal_agent runs next morning | Award 0; note in log |
| MACD confirms direction | 5 | **No** — signal_agent runs next morning | Award 0; note in log |
| LLM sentiment Positive or Neutral | 5 | Yes | Positive → 5; Neutral → 3; Mixed → 1; Negative → 0 |
| Risk-reward ratio ≥ 1:2 | 5 | **No** — ATR from signal_agent | Award 0; note in log |
| No earnings in next 5 days | 5 | Partial — use earnings_transcript_unavailable as proxy | `earnings_transcript_unavailable=False` → 5 points; `True` → criterion excluded (scorecard_max reduced) |

**Scorecard computation rules:**

1. `scorecard_score` = quality(5) + rank_points + regime_points + sentiment_points
   - Maximum at watchlist stage when no earnings flag: 5 + 5 + 5 + 5 = **20**
   - RSI, MACD, and risk-reward contribute 0 (deferred to signal_agent)

2. `scorecard_max` at watchlist stage:
   - `earnings_transcript_unavailable=False`: scorecard_max = **20** (4 criteria scored)
   - `earnings_transcript_unavailable=True`: the "no earnings in 5 days" criterion is excluded from the full scorecard (making full max = 35 instead of 40). At watchlist stage this reduces max from 20 to **15**.

3. **The threshold of 28/40 is NOT enforced here.** Watchlist agent computes and reports the partial score for human visibility only. The full scorecard is completed by `signal_agent.py` the next morning (adding RSI, MACD, risk-reward) and enforced by `risk_agent.py`.

4. Document clearly in code comments: partial score at watchlist stage = max 20 (or 15 with earnings flag). Full score scored by signal_agent. Threshold enforced by risk_agent.

---

## 6. Notification — human checkpoint

Send via `send_checkpoint(subject, message)`.

**Subject:** `"Watchlist ready — {N} candidates. Approve by 07:00 IST"`

**Message body format:**

```
{N} candidates evaluated. {M} proceed, {K} skipped.

PROCEED candidates (reply to approve):

1. {SYMBOL} | Sentiment: {sentiment} ({confidence:.2f}) | Rank: {rank} | Regime: {regime}
   Partial score: {scorecard_score}/{scorecard_max} (RSI/MACD/risk-reward scored tomorrow)
   Reply: APPROVE {SYMBOL} or SKIP {SYMBOL}

2. {SYMBOL2} | ...

Skipped candidates: {comma-separated SKIP symbols} ({skip reasons})

Reply APPROVE ALL to approve all PROCEED candidates.
Timeout: no response by 07:00 IST → all trades skipped today.
```

**When no PROCEED candidates exist** (all SKIP or 0 candidates): do NOT send `send_checkpoint()`. Instead send `send_info("No tradeable candidates today: {reasons summary}")`. Return `WatchlistAgentResult` with `proceed_count=0`.

**Notification failure:** If `send_checkpoint()` returns `{"telegram": False, "gmail": False}` (both channels failed) → raise `WatchlistAgentError(message="Both notification channels failed for checkpoint", phase="notification")`. This is the only fatal notification failure because the human checkpoint cannot be bypassed.

---

## 7. Human response handling

### run_watchlist_agent() flow

1. DB READ: read screener_results + research_reports (single connection, close immediately).
2. Compute candidates, combined decisions, scorecards (pure Python, no DB).
3. DB WRITE: open fresh connection → `BEGIN` → `INSERT OR REPLACE` all candidate rows into watchlist → `COMMIT` → `PRAGMA wal_checkpoint(PASSIVE)` → close. All rows written with `human_approved=0`, `approval_source=NULL`.
4. Log all actions (OUTSIDE any BEGIN/COMMIT block).
5. Send checkpoint notification (PROCEED candidates only, or send_info if none).
6. Return `WatchlistAgentResult`.

The function returns immediately after sending the notification. It does NOT block/poll.

### check_watchlist_timeout(run_date)

Called by orchestrator at 07:00 IST.

1. Open connection → `BEGIN` → find all rows for `run_date` where `human_approved=0` AND `approval_source IS NULL` → update those rows: set `approval_source='timeout_skip'` → `COMMIT` → close.
2. If any rows were timed out → send `send_alert(subject="Watchlist timeout", message="No human response by 07:00 IST. All trades skipped today.")`.
3. Log `"watchlist_timeout: no human response by 07:00 IST"`.
4. If no pending rows found (all already resolved) → no-op, no alert.
5. Raises `WatchlistAgentError(phase="timeout_check")` on DB failure only.

### record_human_approval(symbol, run_date, approved)

Called by orchestrator when it parses a Telegram reply.

1. Open connection → look up watchlist row for `(symbol, run_date)`.
2. If row not found → `log_agent_action(level="WARNING", action=f"record_human_approval: no row found for {symbol} on {run_date}")` → return (no-op, no raise).
3. If row found → `BEGIN` → UPDATE: `human_approved = 1 if approved else 0`, `approval_source = 'human_explicit'` → `COMMIT` → close.
4. Log `"human_approval_recorded: {symbol} approved={approved}"`.
5. This function never raises. Wrap DB operations in `try/except sqlite3.Error` and log on failure, return silently.

**Telegram message parsing** (APPROVE SYMBOL, APPROVE ALL, SKIP SYMBOL) is handled by the orchestrator or a dedicated Telegram webhook handler — **out of scope for this module**. `record_human_approval()` is the write interface only.

---

## 8. watchlist table DDL

```sql
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    combined_decision TEXT NOT NULL,
    scorecard_score INTEGER NOT NULL,
    scorecard_max INTEGER NOT NULL,
    sentiment TEXT NOT NULL,
    confidence REAL NOT NULL,
    rank INTEGER NOT NULL,
    regime TEXT NOT NULL,
    position_size_multiplier REAL NOT NULL,
    human_approved INTEGER NOT NULL DEFAULT 0,
    approval_source TEXT,
    added_at TEXT NOT NULL,
    UNIQUE(symbol, run_date)
);
```

`run_date` stored as ISO date string (e.g. `"2026-04-05"`).
`added_at` stored as ISO 8601 IST timestamp with timezone (e.g. `"2026-04-05T23:30:15+05:30"`).
`human_approved`: 0 = not approved, 1 = approved.
`approval_source`: NULL (pending), `"human_explicit"`, or `"timeout_skip"`.

**Note on existing schema:** The `db-schema.md` had a preliminary watchlist table definition with different columns (`trade_type`, `thesis`, `approved_by_human`, `approved_at`, `built_at`). This spec supersedes that schema. The Coder Agent must use the DDL above.

---

## 9. DB connection pattern

Same pattern as `screener_agent.py` and `signal_agent.py`:

```python
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

def _open_connection(db_path: str) -> sqlite3.Connection:
    # isolation_level=None (autocommit) — explicit BEGIN/COMMIT used throughout
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn
```

**Phase separation:**
- READ phase: open → read screener_results → read research_reports → close immediately (before any computation).
- WRITE phase (watchlist rows): open fresh → `BEGIN` → `INSERT OR REPLACE` all rows → `COMMIT` → `PRAGMA wal_checkpoint(PASSIVE)` → close.
- WRITE phase (timeout/approval): same pattern, single-statement or batch.

**Critical:** All `log_agent_action()` calls must be OUTSIDE any `BEGIN/COMMIT` block. `log_agent_action()` opens its own connection internally; calling it inside a transaction risks `SQLITE_BUSY_SNAPSHOT`. This constraint is documented in `screener_agent.py` and must be followed here.

**DB path resolution:** same `_resolve_db_path()` helper as other agents — strip `sqlite:///` prefix from `settings.database_url`, join with project root if relative.

**Table setup:** `_setup_table(db_path)` — create watchlist table if not exists, open connection, execute DDL, close. Called once at start of `run_watchlist_agent()`.

---

## 10. Logging

All calls use `log_agent_action(agent_name=AGENT_NAME, ...)`.

| Log action | Level | When |
|-----------|-------|------|
| `"watchlist_run_started: {run_date}"` | INFO | Start of run_watchlist_agent() |
| `"screener_results_read: {N} candidates"` | INFO | After reading screener_results |
| `"research_incomplete: {symbol}"` | WARNING | Symbol in screener but no completed research |
| `"combined_decision: {symbol} → {PROCEED/SKIP} reason={reason}"` | INFO | After applying decision rule per candidate |
| `"scorecard: {symbol} score={X}/{Y}"` | INFO | After computing scorecard per candidate |
| `"checkpoint_sent: {N} PROCEED candidates"` | INFO | After send_checkpoint() succeeds |
| `"no_tradeable_candidates: all skipped"` | INFO | When proceed_count=0, before send_info |
| `"watchlist_written: {N} rows"` | INFO | After WRITE phase completes |
| `"watchlist_run_completed: {proceed} PROCEED, {skip} SKIP"` | INFO | End of run_watchlist_agent() |
| `"watchlist_timeout: no human response by 07:00 IST"` | WARNING | In check_watchlist_timeout() when rows timed out |
| `"human_approval_recorded: {symbol} approved={bool}"` | INFO | In record_human_approval() |
| `"record_human_approval: no row found for {symbol} on {run_date}"` | WARNING | Symbol not found in watchlist |

---

## 11. Constants

```python
AGENT_NAME: str = "watchlist_agent"
APPROVAL_DEADLINE_HOUR: int = 7    # 07:00 IST
APPROVAL_DEADLINE_MINUTE: int = 0

# Scorecard constants — documented but threshold NOT enforced by this module
SCORECARD_THRESHOLD: int = 28         # enforced by risk_agent on full scorecard
SCORECARD_MAX_FULL: int = 40          # full scorecard max (all 8 criteria)
SCORECARD_MAX_FULL_NO_EARNINGS: int = 35  # full scorecard max when earnings_transcript_unavailable
SCORECARD_MAX_WATCHLIST: int = 20     # watchlist-stage max (4 criteria: quality, rank, regime, sentiment)
SCORECARD_MAX_WATCHLIST_NO_EARNINGS: int = 15  # watchlist-stage max when earnings_transcript_unavailable
```

---

## 12. Error handling

- `sqlite3.Error` on READ → raise `WatchlistAgentError(phase="db_read")`
- `sqlite3.Error` on WRITE (watchlist rows) → raise `WatchlistAgentError(phase="db_write")`
- `sqlite3.Error` in `check_watchlist_timeout()` → raise `WatchlistAgentError(phase="timeout_check")`
- Both notification channels fail for checkpoint → raise `WatchlistAgentError(phase="notification")`
- `record_human_approval()` DB failure → log WARNING, return silently (never raises)
- No bare `except` clauses. Always catch specific exceptions (`sqlite3.Error`, `requests.RequestException` if applicable).
- Notification failures on `send_info()` (no candidates case) → log WARNING only, do NOT raise. Human needs to know there are no trades but this is not a fatal pipeline failure.

---

## 13. Known callers

| Caller | Call | When |
|--------|------|------|
| `src/agents/orchestrator.py` | `run_watchlist_agent()` | ~23:30 IST, after research_agent completes |
| `src/agents/orchestrator.py` | `check_watchlist_timeout()` | 07:00 IST next morning |
| `src/agents/orchestrator.py` | `record_human_approval(symbol, run_date, approved)` | When Telegram reply arrives |

**Reads from:**
- `screener_results` table (written by `screener_agent.py`)
- `research_reports` table (written by `research_agent.py`)

**Writes to:**
- `watchlist` table

---

## 14. Test hints

15 test scenarios. Tests should use an in-memory SQLite DB or a temp file. Seed `screener_results` and `research_reports` tables directly rather than mocking the agents.

1. **Happy path — 3 PROCEED:** 3 candidates all Positive sentiment, human approves all via `record_human_approval()` → `approved_symbols` has all 3, `human_approved=1` in DB.

2. **Negative sentiment blocks one:** 2 Positive + 1 Negative → `proceed_count=2`, the Negative candidate has `combined_decision="SKIP"`, `skip_reason="negative_sentiment"`.

3. **Research incomplete:** 2 screener rows but only 1 matching research row → 1 candidate, 1 `"research_incomplete: {symbol}"` log, `candidates_evaluated=1`.

4. **regime_blocked:** `position_size_multiplier=0.0` on a candidate → `combined_decision="SKIP"`, `skip_reason="regime_blocked"`, even if sentiment is Positive.

5. **No completed research today:** all `research_reports.completed_at` are NULL → 0 candidates, `send_info()` called instead of `send_checkpoint()`, no `WatchlistAgentError` raised.

6. **check_watchlist_timeout — no response:** 2 PROCEED rows written with `human_approved=0, approval_source=NULL`. Call `check_watchlist_timeout(run_date)` → both rows updated to `approval_source='timeout_skip'`, `send_alert()` called.

7. **check_watchlist_timeout — partial response:** 2 PROCEED rows. `record_human_approval(sym1, run_date, True)` called for first. Then `check_watchlist_timeout()` → only the unapproved row gets `approval_source='timeout_skip'`.

8. **record_human_approval — single symbol approved:** Row exists, approved=True → `human_approved=1`, `approval_source='human_explicit'` in DB.

9. **record_human_approval — symbol not in watchlist:** Symbol not present → no DB write, no exception raised, WARNING logged.

10. **Scorecard — rank points:** rank=2 → 5 momentum points; rank=4 → 0 momentum points.

11. **Scorecard — earnings flag reduces scorecard_max:** `earnings_transcript_unavailable=True` → `scorecard_max=15` (not 20).

12. **Scorecard — BELOW_200DMA regime:** regime="BELOW_200DMA", sentiment=Positive → regime points = 2, sentiment points = 5, total = 5+0+2+5 = 12/20 (assuming rank > 3 for rank=0).

13. **DB write failure on watchlist insert:** Mock DB to raise `sqlite3.Error` on INSERT → `WatchlistAgentError(phase="db_write")` raised.

14. **Mixed sentiment → PROCEED with 1 scorecard point:** sentiment="Mixed", rank=1, regime="ABOVE_200DMA" → `combined_decision="PROCEED"`, `scorecard_score = 5 + 5 + 5 + 1 = 16`.

15. **All candidates SKIP — no checkpoint sent:** All 5 candidates have Negative sentiment → `proceed_count=0`, `send_info()` called (not `send_checkpoint()`), `WatchlistAgentError` is NOT raised.

---

## 15. Implementation notes for the Coder Agent

### research_reports table schema discovery

The `research_reports` table in the actual codebase (from `research_agent.py`) has these columns:
`id, symbol, run_date, sentiment, confidence, source_urls, earnings_transcript_unavailable, completed_at, raw_response, created_at`

The `run_date` column exists (research_agent inserts it). Filter by `run_date = ?` and `completed_at IS NOT NULL`. Do NOT use `DATE(completed_at)` — it is fragile against midnight boundary edge cases. Use `ORDER BY completed_at DESC LIMIT 1` per symbol to take the most recent completed row.

### screener_results run_date column

`screener_agent.py` stores `run_date` as `r.run_date.isoformat()` (ISO date string). The read query should filter `WHERE run_date = ?` with `run_date.isoformat()`.

### SKIP candidates are written to watchlist

Both PROCEED and SKIP candidates are written to the `watchlist` table (full audit trail). Only PROCEED candidates are listed in the checkpoint notification.

### scorecard_max alignment with full-scorecard spec

The full pre-trade scorecard (risk.md) has max 40 (or 35 with earnings). At watchlist stage:
- 3 criteria deferred to signal_agent (RSI=5, MACD=5, risk-reward=5 = 15 points deferred)
- Therefore watchlist max = 40 - 15 = 25... BUT the "no earnings in 5 days" criterion (worth 5 in the full scorecard) is handled at watchlist stage via `earnings_transcript_unavailable`. If that flag is False, the criterion is scored (5 points, always awarded since we're using it as a proxy). If True, the criterion is excluded and max drops.

**Correction from the task spec (Section 5):** The task spec says scorecard_max=20 normally and 15 with earnings. This implies the quality criterion (always 5) + rank (0 or 5) + regime (0, 2, or 5) + sentiment (0, 1, 3, or 5) = max 20, with RSI/MACD/risk-reward/earnings all deferred. The "no earnings in 5 days" criterion at full-scorecard stage is worth 5 points — but at watchlist stage, instead of awarding 5 points when earnings flag is False, the scorecard_max is set to 20 with that criterion excluded (it's scored by signal_agent who has access to an earnings calendar). This avoids double-counting.

**Coder should implement exactly:** scorecard_max = 20 normally, 15 when `earnings_transcript_unavailable=True`. The 4 scored criteria = quality(5) + rank(0 or 5) + regime(0, 2, or 5) + sentiment(0, 1, 3, or 5).

### Imports

```python
import datetime
import os
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_checkpoint, send_info
```

No external LLM libraries. No Tavily. No Gemini. This agent is pure Python + SQLite + notifier.
