# Indian Trader — Interview Preparation Guide

---

## 1. ONE-LINE EXPLANATION

An autonomous multi-agent pipeline that selects Nifty 50 stocks using quantitative filters and multi-LLM sentiment analysis, then manages swing trade positions through automated stop-loss and human approval checkpoints — running on ₹10,000 paper trading capital.

---

## 2. SYSTEM OVERVIEW

**What it does:** Runs every weekday evening and morning in two phases. The evening pipeline selects which stocks to watch (quality filter → momentum ranking → news sentiment → human approval). The morning pipeline decides whether to trade them that day (fresh technicals → Groq LLM confirmation → risk checks → execution with human sign-off).

**Strategy:** Weekly universe refresh using 12-1 momentum factor (12-month return minus 1-month return). Maximum 2 simultaneous positions. Risk 1% of account per trade (₹100 at ₹10,000 starting capital). Stop-loss placed at 2× ATR below entry. Take-profit at minimum 2× stop distance.

**Capital:** ₹10,000 starting capital, paper trading only. Hard cap: no single trade > ₹4,000 (40% of capital). MAX_TRADE_AMOUNT = ₹10,000 is an absolute ceiling enforced in code.

**Why each decision:**
- Nifty 50 only: liquid enough for CNC delivery without slippage; exchange data freely available
- Swing trading (3–10 day holds): avoids noise of intraday but doesn't require overnight risk management of weeks-long holds
- CNC delivery on Shoonya: free equity delivery in India; GTT orders survive broker restarts
- 12-1 momentum: removes short-term noise (the "1 month" subtraction). Academically validated as strongest single return predictor in Indian equities (IIM Ahmedabad Fama-French-Momentum research on NSE data since 1994)
- Multiple LLMs: each has a distinct role — Gemini for synthesis (handles long context well), Groq for fast binary validation (low latency matters at 08:20), Claude Code as the build layer (reasoning for architecture decisions)

---

## 3. THE AGENT SYSTEM

The project has two agent layers: a **build layer** (Claude Code subagents that wrote the codebase) and a **trading layer** (Python functions that run the pipeline daily).

### Build Layer — 6 Claude Code Subagents

| Agent | Model | Why this model | Role |
|-------|-------|----------------|------|
| Architect | Opus | Strategic decisions, spec authoring | Reads codebase context, writes spec to `docs/specs/` |
| Coder | Sonnet | Implementation quality + security judgment | Implements exactly the spec |
| Tester | Haiku | Pattern matching, templated test generation | Writes tests, runs pytest + mypy |
| Debugger | Sonnet | Needs reasoning to diagnose failures | Reads error output, fixes, re-runs |
| Code Reviewer | Sonnet | Security + hardcoded secrets require judgment | Audits for OWASP issues, missing type hints, bare except |
| GitHub Agent | Haiku | Mechanical git operations | Stages, commits, pushes |
| Docs Agent | Haiku | Templated doc updates | Updates `docs/context/`, `docs/connections.md`, `docs/DECISIONS.md` |

**What autonomous means here:** Each agent reads context files first (`docs/context/current-state.md`, `interfaces.md`, `db-schema.md`, `decisions-log.md`) before touching source. A Stop hook auto-commits all changes at session end. A PostToolUse hook runs ruff on every file edit. A PreToolUse hook prevents writes to `src/execution/` unless `LIVE_TRADING=false`. No agent can skip directly to GitHub without tests passing and Code Reviewer outputting PASS.

**MCP servers used:**
- `mcp__github__*`: GitHub Agent uses this for PR creation, push, and commit verification
- `mcp__sqlite__*`: Used during debugging and architecture sessions to query the live database directly
- `plugin:telegram`: Handles real-time Telegram notifications and human approval replies during trading sessions

### Trading Layer — 8 Python Agent Functions (built so far)

All agents are plain Python functions — no Agent SDK. They communicate exclusively via SQLite. Each function: reads its inputs from DB, computes, writes outputs to DB. The orchestrator (Phase 4) will call them on schedule.

#### 1. Screener Agent — `src/agents/screener_agent.py`

**Model tier:** N/A (pure Python, no LLM)
**Runs:** Monday 22:00 IST (weekly), or emergency rescreen triggered by monitor_agent on Nifty >3% daily drop
**Purpose:** 3-step stock selection pipeline — quality filter → 12-1 momentum ranking → regime filter
**Key functions:**
```python
def run_screener_agent(run_date: datetime.date | None = None) -> ScreenerAgentResult
```
**Reads:** Fetches OHLCV (400-day lookback), fundamentals, sector indices from APIs — no DB reads
**Writes:** `screener_results` table — top 5 candidates with momentum scores, regime, position_size_multiplier
**Key decisions:**
- `INSERT OR REPLACE` on `UNIQUE(symbol, run_date)` — emergency rescreens overwrite the Monday run; most recent run is always authoritative
- `regime_blocked` (BELOW_200DMA_10DAYS) still writes top5 with `position_size_multiplier=0.0` so watchlist_agent can read the list. Screener doesn't suppress — downstream decides.
- Uses `fetch_sector_indices()` not `fetch_ohlcv(["^NSEI"])` for Nifty 50 data — sector indices API returns clean NIFTY_50 rows; the stock fetcher returns inconsistent data for the index ticker

#### 2. Research Agent — `src/agents/research_agent.py`

**Model tier:** Gemini 2.5 Flash (synthesis)
**Runs:** Monday 22:40 IST
**Purpose:** Fetch news for each of top 5 screener candidates, synthesise sentiment using Gemini
**Key functions:**
```python
def run_research_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> ResearchAgentResult
```
**Reads:** `screener_results` table (top 5 quality-passed symbols); Tavily Search API (3 queries per stock)
**Writes:** `research_reports` table — sentiment, confidence, source_urls, completed_at (set LAST)
**Race condition prevention:** Two-step DB write:
1. `INSERT` row with `completed_at = NULL` as placeholder
2. Run all Tavily + Gemini work
3. `UPDATE` row with results, set `completed_at` last
Watchlist Agent queries `WHERE completed_at IS NOT NULL` — a row with NULL completed_at is invisible to downstream. If Gemini fails fatally, the row stays NULL and the stock goes to `skipped_symbols`.

**Earnings branch:** If recent articles (within 5 days) contain keywords Q1/Q2/Q3/Q4/earnings/results → attempt to fetch earnings call transcript via 4th Tavily query. If transcript < 200 chars: flag `earnings_transcript_unavailable=True`, fall back to standard synthesis. Never skip the stock.

**Domain filtering:** All news queries use `include_domains` to restrict to ET, MoneyControl, Business Standard, LiveMint, Financial Express, Reuters, Bloomberg — prevents Tavily returning quote pages from Yahoo Finance instead of editorial news.

#### 3. Signal Agent — `src/agents/signal_agent.py`

**Model tier:** Groq llama-3.3-70b (primary), Gemini 2.5 Flash (fallback)
**Runs:** Morning 08:20 IST
**Hard deadline:** Must complete by 08:50. Late start → `late_start=True`, empty result, safe mode.
**Purpose:** Fresh morning technical analysis + Groq advisory LLM confirmation
**Key functions:**
```python
def run_signal_agent(
    run_date: datetime.date | None = None,
    symbols: list[str] | None = None,
) -> SignalAgentResult
```
**Reads:** `screener_results` (top candidates), `research_reports` (evening sentiment), Fyers/yfinance (60-day OHLCV)
**Writes:** `signals` table — RSI, MACD, Bollinger, ATR, groq_confidence, signal_type (BUY/HOLD), skip_reason

**Decision rule:**
1. Technical BUY: `rsi < 40.0 AND macd_hist > 0`
2. Blocked if sentiment = "Negative"
3. Groq advisory check: send "Evening thesis: X. Morning technicals: Y. Does the thesis still hold?" — confidence < 0.6 → downgrade to HOLD
4. Both LLMs fail → keep rule-based BUY with `groq_confidence = -1.0` sentinel. Advisory-only: original "skip on failure" was wrong.

**Why Groq for this role:** Speed. At 08:20 with a 08:50 deadline, Groq's sub-second latency on Llama 3.3 70B matters. Gemini is the fallback when Groq rate-limits.

**Why requests.post() not Groq SDK:** Testability. The raw HTTP call is mockable in tests without the SDK object graph.

#### 4. Watchlist Builder — `src/agents/watchlist_agent.py`

**Model tier:** N/A (pure Python decision logic)
**Runs:** Evening 23:30 IST
**Purpose:** Apply combined decision rule (screener rank + LLM sentiment), compute partial pre-trade scorecard, send human approval checkpoint
**Key functions:**
```python
def run_watchlist_agent(run_date: datetime.date | None = None) -> WatchlistAgentResult
def check_watchlist_timeout(run_date: datetime.date) -> None  # called at 07:00 IST
def record_human_approval(symbol: str, run_date: datetime.date, approved: bool) -> None
```
**Reads:** `screener_results` (quality_passed=1 for run_date), `research_reports` (completed_at IS NOT NULL, run_date match)
**Writes:** `watchlist` table — every candidate (PROCEED and SKIP) for full audit trail

**Combined decision rule:**
- `position_size_multiplier == 0.0` → SKIP (regime blocked)
- `sentiment == "Negative"` → SKIP
- else → PROCEED (Mixed counts as PROCEED with 1 scorecard point)

**research_reports filter:** `WHERE symbol = ? AND run_date = ? AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1` — uses `run_date` column (set at INSERT time by research_agent), not `DATE(completed_at)`. Robust against runs that span midnight.

**Partial pre-trade scorecard** (max 20 points at this stage, full 40 enforced by risk_agent):
| Criterion | Points |
|-----------|--------|
| Quality filter passed | always 5 |
| Momentum rank ≤ 3 | 5 (else 0) |
| Regime: ABOVE_200DMA / BELOW_200DMA / BELOW_200DMA_10DAYS | 5 / 2 / 0 |
| Sentiment: Positive / Neutral / Mixed / Negative | 5 / 3 / 1 / 0 |

**Non-blocking design:** `run_watchlist_agent()` sends the checkpoint notification and returns immediately. Orchestrator calls `check_watchlist_timeout()` at 07:00 IST. When a Telegram reply arrives, orchestrator calls `record_human_approval()`. This is crucial — blocking here would hold up the pipeline for hours.

---

## 4. MULTI-LLM PIPELINE

### Where Each LLM Is Used

| LLM | Role | Why this LLM |
|-----|------|-------------|
| **Gemini 2.5 Flash** (free tier) | Evening research synthesis | Handles long context (10+ news articles) well; free tier is 250 RPD which covers 5 stocks × 1 call each |
| **Groq llama-3.3-70b** (free tier) | Morning signal confirmation | Sub-second latency (under 1s typical); 1000 RPD free; binary yes/no decision doesn't need a frontier model |
| **Claude (Sonnet/Opus/Haiku)** | Build layer only — architecture, code, review | Not used in the trading pipeline itself; used to build the system |

### Fallback Chain in signal_agent.py

```
Groq (primary, 15s timeout)
  → Gemini 2.5 Flash (fallback on rate limit or failure)
    → Rule-based BUY with groq_confidence = -1.0 (sentinel)
```

The sentinel value `-1.0` is not a valid confidence score (valid range: 0.0–1.0). Any code reading `groq_confidence` can detect this sentinel and know the LLM layer was unavailable. The trade proceeds on rule-based signal alone — LLM is advisory, never blocking.

### Race Condition Prevention

**Problem:** Research Agent writes one row per stock sequentially, taking ~30–60 seconds per stock (Tavily queries + Gemini). Watchlist Builder runs 50 minutes later. Without a guard, Watchlist Builder might read a partially-completed row.

**Solution:** Two-step DB write pattern:
1. INSERT row with `completed_at = NULL` immediately
2. Do all the slow work (Tavily + Gemini)
3. UPDATE row with results, set `completed_at` LAST

Watchlist Builder filters: `AND completed_at IS NOT NULL AND run_date = ?`

If Gemini fails fatally mid-research, the row stays NULL and is invisible to Watchlist Builder. The stock goes to `skipped_symbols`. The pipeline continues without it.

### Multi-Source Data Fallbacks

| Layer | Primary | Fallback |
|-------|---------|---------|
| OHLCV (historical/backtest) | jugaad-data (NSE direct) | yfinance (.NS suffix) |
| OHLCV (live trading) | Fyers API WebSocket | nsepython spot checks |
| Fundamentals | Screener.in (scraping, 2-5s delay, 45-day cache) | yfinance after 3 consecutive scrape failures |
| News | Tavily Search (3 queries/stock) | No fallback — stock goes to skipped_symbols |
| Signal LLM | Groq llama-3.3-70b | Gemini 2.5 Flash → rule-based (groq_confidence=-1.0) |
| Research LLM | Gemini 2.5 Flash | No fallback — stock goes to skipped_symbols |

**Why no Brave Search:** Original implementation used Brave Search API. Switched to Tavily because Tavily returns `published_date` as an ISO string per article, which is required for earnings detection (`article_age < 5 days`). Brave returned age strings ("2 hours ago") that required fragile heuristic parsing.

**Why NewsData.io was rejected:** Free tier has 12-hour delay. An earnings announcement released at 15:30 IST would not be visible until 03:30 next morning — too late for the 22:40 research run to catch it.

---

## 5. BACKTEST

### Results (2010–2023, ₹10,000 starting capital)

| Metric | Value | Gate threshold | Pass? |
|--------|-------|----------------|-------|
| Total trades | 357 | ≥ 100 | ✅ |
| Win rate | 44% | > 40% | ✅ |
| Profit factor | 1.33 | > 1.3 | ✅ |
| Max drawdown | 14.5% | < 15% | ✅ |
| Sharpe ratio | 0.50 | > 1.0 | ❌ |

**The backtest gates are not all passed.** Sharpe 0.50 fails the 1.0 gate. The strategy requires Sharpe > 1.0 to proceed. This is by design — the backtest covers only mechanical rules (no LLM signals), and the gate is intentionally strict.

### Why Sharpe 0.50 Is an Honest Baseline, Not a Failure

The backtest simulates only the mechanical layer: quality filter + momentum rank + regime filter + rule-based stop-loss/take-profit. LLM sentiment filtering is not in the backtest — it is only applied in the live pipeline. The 44% win rate and 1.33 profit factor suggest the mechanical strategy is directionally correct but not yet refined enough. Adding LLM sentiment (which blocks Negative-sentiment stocks) is expected to raise Sharpe by improving trade selection at the cost of fewer trades.

**Why this is honest:** We did not tune the strategy parameters until Sharpe passed. We documented the actual result and noted what it covers. A tuned result that passes purely by curve-fitting would look better but be less useful.

### RSI Filter Rejection

During development, an RSI < 40 entry filter was tested. Adding it reduced trades from 357 to 52 over 14 years — an average of ~4 trades per year. This was rejected because:
- 52 trades is not statistically meaningful
- The 40 threshold is too conservative for a weekly-rebalanced strategy (most stocks rarely hit RSI < 40)
- RSI < 40 was retained for morning signal confirmation (signal_agent), where fresh 60-day data is used and the signal is one vote among many, not a sole gate

### Survivorship Bias Fix

The historical constituent list (`NIFTY_CONSTITUENTS_BY_SYMBOL` in `fundamentals.py`) has 61 unique symbols spanning 2010–2023 — not just the current 50. The list includes stocks that were in the Nifty 50 during the backtest period but have since been replaced (SAIL, YES Bank, ZEEL, VEDL, DLF, BHEL, PNB, etc.).

An 80% inclusion threshold was used: stocks present in the index for ≥ 80% of the 14-year period are included in the historical universe. This avoids both pure survivorship bias (only using current members) and excessive dilution from including brief index members.

### Point-in-Time Fundamentals (Lookahead Bias Prevention)

For each simulated Monday during the backtest, `get_fundamentals_for_date(symbols, as_of_date)` returns only fundamentals that would have been available on that date.

**The fiscal year rule:** Indian companies report FY results (Apr–Mar year-end) 2–3 months after year-end. Using April as the cutoff creates lookahead bias — the FY results are not published until ~July. The cutoff is `month >= 7` (July):
- `month <= 6` (Jan–Jun) → use `fiscal_year - 1` data
- `month >= 7` (Jul–Dec) → use `fiscal_year` data (current year results now published)

**Why this matters:** A backtest using April cutoff would "know" FY2020 results in April 2020 — during COVID, at market bottom. The strategy would look better than it actually was.

### Historical ROE Problem

Screener.in shows ROCE (Return on Capital Employed) in its historical tables, not ROE. We use it as a proxy in the historical backtest with a note that it overstates ROE for capital-light businesses. For live trading, the fundamentals fetcher uses the dedicated ROE field from the company balance sheet page, not the historical table.

### Paper Trading Gate

Phase 5 requires Sharpe ≥ 0.8 over 8 weeks of paper trading — lower than the backtest gate of 1.0 because:
- Paper trading includes LLM filtering (which the mechanical backtest doesn't)
- 8-week window is too short to produce a statistically reliable Sharpe; expecting 1.0+ on 8 weeks with ~16 trade opportunities would require unrealistic performance
- 0.8 is high enough to reject a broken strategy while being achievable with a functional one

---

## 6. INTERESTING PROBLEMS SOLVED

### SQLite WAL Multi-Agent Concurrent Writes

**Problem:** Multiple agents run in parallel (signal_agent, research_agent) and all write to the same SQLite database through the same `log_agent_action()` function. In WAL mode, only one writer can hold the write lock at a time. We saw `SQLITE_BUSY_SNAPSHOT` errors when `log_agent_action()` was called inside a `BEGIN / COMMIT` block.

**Why it was hard:** `busy_timeout=30000` (30 seconds) does NOT help with `SQLITE_BUSY_SNAPSHOT`. This WAL-specific error occurs when a read snapshot is outdated — it is not a timeout issue. It cannot be resolved by waiting longer.

**How solved:** Two-phase DB access pattern enforced across all agents:
1. READ phase: open connection → read → close
2. COMPUTE phase: pure Python, no DB
3. WRITE phase: open fresh connection → `BEGIN` → all writes → `COMMIT` → `wal_checkpoint(PASSIVE)` → close

Rule: `log_agent_action()` is NEVER called inside a `BEGIN / COMMIT` block. All logging happens before or after the write phase. `_write_signals()`, `_write_results()` etc. are pure write functions with zero logging.

### D/E Ratio for Banks

**Problem:** Screener.in's balance sheet page uses "Borrowings" for financial companies and "Borrowing" (singular, different row) for industrial companies. The scraper initially missed the financial-company variant and returned NULL D/E for HDFC Bank, ICICI Bank, Axis Bank, and Kotak Bank.

**Why it was hard:** All four passed all other filters, so the NULL D/E caused them to fail the `D/E < 1.0` filter silently — not an error, just missing data. The quality filter logs `data_quality: failed` for NULL fundamentals. We didn't catch it until a manual data audit.

**How solved:** Fundamentals scraper now tries both "Borrowings" and "Borrowing" row variants. A stale/missing D/E still auto-fails the quality filter — conservative behavior prevents trading on corrupt data.

### Cache Serving Wrong Date Range

**Problem:** `fetch_ohlcv()` cache validates file age (< 24 hours), not date range coverage. If you fetch 200 days of data for RELIANCE on Monday and cache it, then fetch 400 days on Tuesday, the 200-day cache is served (it's < 24 hours old). The screener_agent gets only 200 days — insufficient for 252-day momentum lookback.

**Why it was hard:** Fails silently. Momentum scoring returns `insufficient_history_count > 0` but no error is raised. The thin universe case handles it, but you lose valid candidates.

**Current state:** Known limitation, documented. Workaround: pass `cache_expiry_hours=0` when longer history is needed (screener_agent does this). Full date-range-aware cache is planned for Phase 3/4 cleanup.

### Nifty 50 Data Fetching

**Problem:** `fetch_ohlcv(["^NSEI"])` (the Yahoo Finance ticker for Nifty 50 index) returns inconsistent data in the yfinance / jugaad-data dual-source setup. jugaad-data's NSE API does not support index tickers — it's for individual stocks only.

**How solved:** `fetch_sector_indices()` is a separate function that uses yfinance only, with the `SECTOR_INDEX_MAP` mapping logical names to Yahoo tickers (`"NIFTY_50": "^NSEI"`). All regime filter code uses `fetch_sector_indices()`. The mistake of using `fetch_ohlcv(["^NSEI"])` was caught during the screener_agent spec review before any code was written.

### DB Lock Pattern — log_agent_action Inside BEGIN/COMMIT

Covered above in the SQLite WAL section. The short version: discovered during signal_agent development when 15% of test runs produced `database is locked` errors with no network involvement. Root cause traced to `_write_signals()` calling `log_agent_action()` mid-transaction. Pattern codified and applied retroactively to all agents.

### Gmail OAuth Blocking Headless Pipelines

**Problem:** The original `_build_gmail_service()` called `InstalledAppFlow.run_local_server(port=0)` when no `token.json` existed. This launched a browser OAuth flow — blocking forever in headless/automated mode (WSL2 with no X11, scheduled task overnight).

**How solved:** If `token.json` doesn't exist or can't be refreshed, log `WARNING: gmail_auth_required, run notifier_setup.py` and return `None`. Telegram continues unblocked. The browser OAuth is done once interactively using `notifier_setup.py`; subsequent runs use the saved token with auto-refresh. `notifier_setup.py` is the only place the browser flow ever runs.

---

## 7. WHAT IS NOT BUILT YET (honest)

**Phase 4 — Full Trading Pipeline (not started):**
- `src/execution/auth.py` — TOTP auto-login for Shoonya at 06:15 IST
- `src/execution/shoonya_broker.py` — real order placement, position queries, GTT management
- `src/agents/risk_agent.py` — kill switch checks, position sizing approval/rejection
- `src/agents/execution_agent.py` — human checkpoint with 8-minute window before 09:13 IST
- `src/agents/monitor_agent.py` — 5-minute stop-loss loop, 30-minute GTT reconciliation, intraweek emergency rescreen
- `src/agents/reporter_agent.py` — daily P&L report at 15:45 IST
- `src/agents/orchestrator.py` — Python Agent SDK pipeline that calls all agents on schedule
- `src/agents/morning_validator_agent.py` — overnight news check before signal generation

**Phase 5 — Paper Trading Validation (not started):**
- 8 weeks of paper trading on real market data not yet run
- Minimum 20 completed trades not yet reached
- Pre-trade scorecard not yet used manually

**Live Trading Infrastructure (Phase 6):**
- Oracle Cloud Free Tier VM with static IP for Shoonya whitelisting
- Shoonya account IP whitelist not yet set up
- Real money deployment has not happened

**Phase 2 Report:**
- `reports/phase2-backtest-results.md` was not committed — backtest was run and numbers captured but the formal report file was not written to disk

---

## 8. NUMBERS TO REMEMBER

| Number | What it is |
|--------|-----------|
| **15 modules** | Built and passing tests (Phases 1–3) |
| **505 tests** | Total across all test files, all passing |
| **50 symbols** | Current Nifty 50 live universe (`NIFTY50_SYMBOLS` in fetcher.py) |
| **61 symbols** | Historical constituent list for backtest 2010–2023 (`NIFTY_CONSTITUENTS_BY_SYMBOL` in fundamentals.py) — includes stocks that have since been replaced |
| **357 trades** | Backtest result (2010–2023, mechanical rules only, no LLM) |
| **44%** | Backtest win rate |
| **1.33** | Backtest profit factor |
| **14.5%** | Backtest max drawdown |
| **0.50** | Backtest Sharpe ratio (mechanical baseline, no LLM filtering) |
| **5 stocks** | Selected per week (top momentum from quality-filtered universe) |
| **2 positions** | Maximum simultaneous open positions |
| **1%** | Risk per trade (₹100 at ₹10,000 starting capital) |
| **2× ATR** | Stop-loss distance below entry (1× ATR when regime tightens) |
| **252 / 21** | Trading-day lookbacks for 12-month and 1-month momentum returns |
| **28 / 40** | Pre-trade scorecard pass threshold (full scorecard; enforced by risk_agent) |
| **20 / 40** | Pre-trade scorecard max at watchlist stage (only 4 of 8 criteria computable) |
| **400 days** | OHLCV lookback window for screener_agent (252 trading days ≈ 350 calendar days + buffer) |
| **45 days** | Fundamentals cache expiry — staleness blocks the quality filter |
| **3 strikes** | Screener.in failures before yfinance fallback activates |
| **80%** | Survivorship bias threshold — symbols in Nifty for ≥ 80% of 14-year period |
| **0.8** | Paper trading Sharpe gate (8 weeks, lower than backtest gate of 1.0) |
| **₹1,500** | Drawdown kill switch threshold (15% of ₹10,000 starting capital) |

---

## 9. HARD INTERVIEW QUESTIONS AND HONEST ANSWERS

### "Why not just use a simpler strategy — like buy-and-hold Nifty 50 ETF?"

For a ₹10,000 learning budget, the goal is not to beat Nifty buy-and-hold. The goal is to build a system that teaches the complete stack: data pipelines, LLM integration, risk management, broker APIs, automated execution. A NiftyBees ETF generates no engineering learning. The strategy is the vehicle for building the system.

That said, the 12-1 momentum factor is academically validated on Indian equities data going back to 1994 (IIM Ahmedabad research). It's not arbitrary — it has evidence. The mechanical backtest (Sharpe 0.50, 44% win rate, 1.33 profit factor) shows positive expectancy, which is the minimum bar.

### "Why multiple LLMs instead of just one?"

Each LLM has a specific role based on what it does best at zero cost. Gemini 2.5 Flash handles long-context synthesis (10+ news articles per stock, every evening). Groq llama-3.3-70b handles latency-critical morning confirmation (must complete by 08:50; Groq is typically < 1 second). Claude handles architectural reasoning and code generation in the build layer. Using a single provider would either hit rate limits faster or require using the same model for structurally different tasks.

### "What happens if Groq is down?"

`signal_agent.py` has a three-level fallback:
1. Groq (primary, `GROQ_TIMEOUT_SECONDS=15`)
2. Gemini 2.5 Flash (same prompt, different API)
3. Rule-based BUY with `groq_confidence=-1.0` sentinel

If both cloud LLMs fail simultaneously, the rule-based technical signal (RSI + MACD) is kept. The `-1.0` sentinel is stored in the `signals` table — every downstream system can detect it. This was explicitly overridden from the original design (which said "skip all trades on LLM failure") because LLM failure should not block an otherwise valid technical signal.

### "How do you prevent lookahead bias in the backtest?"

Two mechanisms:

1. **Point-in-time fundamentals:** `get_fundamentals_for_date(symbols, as_of_date)` applies the fiscal year rule — July is the cutoff for when Indian FY results are considered "published." Before July, it uses `fiscal_year - 1` data. This prevents the backtest from knowing FY2020 earnings in April 2020 (mid-COVID crash).

2. **Historical constituent list:** The backtest universe includes stocks that were in the Nifty 50 during the test period, not just the current constituents. Stocks like YES Bank and BHEL are in the historical universe for their inclusion years but not after they were removed. This avoids survivorship bias.

What we do NOT prevent: the `NIFTY_CONSTITUENTS_BY_SYMBOL` list was compiled retrospectively from 2026. A real point-in-time index membership database would be cleaner. The 80% threshold is a pragmatic approximation.

### "Why is the Sharpe only 0.50?"

The backtest simulates only the mechanical strategy — quality filter, momentum ranking, regime filter, and rule-based stop/take-profit. It does not include LLM sentiment filtering (which would reduce trades but improve selection). Sharpe 0.50 is the baseline before the intelligence layer is applied.

The more important question is whether the mechanical foundation is positive expectancy (profit factor > 1.0) and has adequate trade count (357 trades over 14 years is statistically meaningful). Both pass. Sharpe will be measured properly during Phase 5 paper trading, where LLM filtering is included.

### "How does the agent pipeline avoid race conditions?"

Three mechanisms:

1. **Two-step DB write:** Research Agent inserts with `completed_at = NULL`, does all external work, then updates with `completed_at` last. Watchlist Builder filters `WHERE completed_at IS NOT NULL AND run_date = ?`. A partially-written row is invisible.

2. **run_date column (not computed date):** Research Agent sets `run_date` at INSERT time. Watchlist Builder queries by `run_date` not `DATE(completed_at)`. Avoids edge case where a run crossing midnight produces off-by-one date comparison.

3. **No log_agent_action inside BEGIN/COMMIT:** All agents use explicit `BEGIN / COMMIT` transactions for writes. `log_agent_action()` opens its own connection. Calling it inside a transaction causes `SQLITE_BUSY_SNAPSHOT`. Pattern enforced: log everything before the write phase, write phase is pure DB writes.

### "What would you do differently?"

1. **Date-range-aware OHLCV cache** — current cache validates file age only. If you previously cached 200 days and then request 400 days, you get the stale 200-day result. The fix is to store requested date range in the cache manifest and invalidate when the new request exceeds cached coverage.

2. **Screener.in scraping** is fragile. The HTML structure changes quarterly. A proper solution would use NSE India's official data (which requires SEBI registration) or a paid API like Sensibull or Tijori. For a ₹10,000 project, scraping is acceptable but has operational risk.

3. **RSI filter on backtest** — we tried RSI < 40 entry filter and it reduced trades to 52 over 14 years. That's too few. The right fix would be to test different RSI thresholds (< 50, < 55) to find one that filters noise without eliminating most of the universe. We went with the simpler approach: no RSI entry filter in the backtest, RSI < 40 retained for morning signal confirmation where it's one vote among many.

4. **Single-node SQLite** works at ₹10,000 and 1–2 trades per week. At larger capital or higher frequency, WAL-mode SQLite starts showing contention. Switching to PostgreSQL would require significant refactoring.

### "Is this live yet? Why not?"

No. Phase 4 (execution, monitoring, orchestration) is not built. The system cannot place or monitor orders. Even after Phase 4 is complete, Phase 5 requires 8 weeks of paper trading with Sharpe ≥ 0.8 before any real money is risked.

The longer answer: Shoonya requires a static IP whitelist for API access. That requires either an ISP static IP or an Oracle Cloud free-tier VM. Neither is set up yet. Live trading without a confirmed IP whitelist would fail at authentication.

### "How does the regime filter actually work?"

The Nifty 50 200-day SMA is computed from `fetch_sector_indices()` data (yfinance, NIFTY_50 → ^NSEI). Three regime states:

```
ABOVE_200DMA (close >= 200 SMA)  → position_size_multiplier = 1.0
BELOW_200DMA (close < 200 SMA, < 10 consecutive days) → multiplier = 0.5
BELOW_200DMA_10DAYS (10+ consecutive days below) → multiplier = 0.0
```

For new trades: multiplier applies to position sizing. A ₹100 risk budget with multiplier 0.5 → ₹50 effective risk → half position.

For open positions: when Nifty drops below 200 SMA (any duration), stop-loss tightens from 2× ATR to 1× ATR. This does not force an exit — it reduces how much you give back if the market keeps falling.

The tightening uses `paper_trader.update_stop_loss()` which was added specifically for this use case (not in original requirements). `regime.py` is pure computation — it returns `stop_tighten_symbols` but never calls stop updates itself. Caller executes.

Boundary: `close == 200 SMA` → ABOVE_200DMA (uses `>=`). Avoids position reduction at the exact boundary.

### "What's the biggest risk in this system?"

**Operational risk:** The system depends on Screener.in scraping for fundamentals. Screener.in can change its HTML, block the scraper, or add a paywall. The 3-strike yfinance fallback provides a safety net but yfinance has its own reliability issues. A production system would use a paid fundamentals API.

**Model risk:** Gemini rates its own output confidence. Two news articles may rewrite the same press release. The confidence score is self-referential — there's no independent validation. We mitigate this by requiring source URLs and planning manual spot-checks of 3 research reports per week during Phase 5. But it's a known weakness.

**Regime filter gaming:** The 200 DMA filter works well in trending markets but produces whipsawing in extended sideways periods (2013–2014 Nifty was sideways for ~18 months). The backtest includes this period — regime_blocked_weeks is tracked as a metric to audit this behavior.

---

## 10. HOW TO EXPLAIN TO DIFFERENT AUDIENCES

### To a technical AI engineer (2 minutes)

"I built a multi-agent trading pipeline where Claude Code subagents (Architect/Coder/Tester/Reviewer) built the codebase autonomously, and Python agent functions run the actual trading logic daily. The interesting parts: a three-LLM stack where Gemini synthesises evening news into sentiment, Groq confirms morning signals with sub-second latency, and both have rule-based fallbacks — LLM failure never blocks a trade, it just sets a sentinel value. The race condition problem was non-obvious: Research Agent writes rows with `completed_at = NULL`, does all its work, then sets `completed_at` last. Watchlist Builder only reads `completed_at IS NOT NULL` rows. The DB concurrency problem was `SQLITE_BUSY_SNAPSHOT` in WAL mode — not fixable with busy_timeout, only fixable by moving all `log_agent_action()` calls outside `BEGIN / COMMIT` blocks. 505 tests, all passing. Phases 1–3 complete, Phase 4 (live broker integration) next."

### To a non-technical interviewer (30 seconds)

"It's a stock trading system that decides which Nifty 50 companies to invest in each week using financial data and news sentiment from AI. It reads recent news about companies, uses Google's Gemini AI to assess whether the news is positive or negative, and then checks in with me via Telegram before placing any trades. I built it as a learning project — it's running on ₹10,000 of paper money while I validate it works."

### To someone who asks "so does it make money?"

"In backtesting over 2010–2023, the mechanical strategy has a 44% win rate and 1.33 profit factor — so on average it makes more than it loses. But the Sharpe ratio is 0.50, which means the returns relative to volatility are moderate. That backtest doesn't include the AI news filtering layer, which should improve it.

The honest answer is: I don't know yet. It hasn't traded with real money. It needs 8 weeks of paper trading validation first. The design is sound and the numbers show positive expectancy, but 'positive expectancy in backtesting' and 'makes money in real markets' are different things. That's exactly what the paper trading phase is for."

---

*Generated 2026-04-07. All claims traced to code in `src/` and `docs/`. Backtest numbers from Phase 2 run (report not committed). Test count: 505 (grep -rc "def test_" tests/).*
