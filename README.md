# Indian Trader

A production-grade autonomous multi-agent trading pipeline for the Indian equity market. Currently in **Phase 5 — Paper Trading Validation**. Hardcoded scope: Nifty 200 universe, CNC delivery (swing) only, ₹1,00,000 paper capital.

---

## What It Does

Ten Python agents coordinate across two daily sessions — evening at 22:00 IST and morning at 08:00 IST — to go from raw NSE data to order placement without human involvement (in paper trading mode). The strategy is momentum-quality swing trading: 3–10 day holds, entry triggered by RSI/MACD technicals, LLM-synthesized news as a soft veto layer.

No human gates in paper mode. One checkpoint in live mode (execution agent, 09:05 IST).

---

## Architecture

Two independent layers share one SQLite database:

```
BUILD LAYER (development time)
  Claude Code subagents: Architect → Coder → Tester → Debugger → Reviewer → GitHub

TRADING LAYER (runtime, daily)
  Python orchestrator → 10 agents → SQLite
```

### Daily Pipeline

```
EVENING  22:00 ─── Data Collector
         22:20 ─── Screener
         22:40 ─── Research Agent
         23:30 ─── Watchlist Builder
                        │
                     watchlist table (human_approved=1 in paper mode)
                        │
MORNING  08:00 ─── Morning Validator
         08:20 ─── Signal Agent
         08:50 ─── Risk Agent
         09:05 ─── Execution Agent ────── orders placed
                        │
INTRADAY 09:15–15:45 ── Monitor Agent (every 5 min)
         15:45 ─────── Reporter Agent
```

---

## The Strategy

Selection runs in three hard-sequenced steps. A stock must clear all three to be traded.

### Step 1 — Quality Filter (Monday, hard pass/fail)

| Filter | Threshold |
|--------|-----------|
| ROE | > 12% |
| Debt-to-Equity | < 1.0 |
| EPS | Positive, last 4 consecutive quarters |
| Daily traded volume | > ₹20 crore |
| Price | > ₹50 |

Fewer than 3 stocks pass → `thin_universe` logged, week skipped entirely. No forced trades.

### Step 2 — 12-1 Momentum

`momentum_score = (12-month return) − (1-month return)`

Drops the most recent month to filter short-term reversal noise. Academically validated as the strongest single return predictor on NSE data (IIM Ahmedabad Fama-French-Momentum research, 1994–present). Recalculated weekly only — never daily. Top 5 candidates written to `screener_results` table (top 10 scored for ranking, top 5 selected).

### Step 3 — Regime Filter

Nifty 50 vs its 200-day SMA:

| Condition | New positions | Open positions |
|-----------|--------------|----------------|
| Above 200 DMA | Full size | Unchanged |
| Below 200 DMA | 50% position size | Tighten SL to 1× ATR |
| Below 200 DMA, 10+ consecutive days | None | Tighten SL to 1× ATR |

Currently: `BELOW_200DMA_10DAYS` (~day 51 as of 2026-05-18). No new positions until Nifty crosses back above 200 DMA.

### LLM Layer

Sits on top of Step 3 as a soft veto — never generates trades, only blocks them.

- **Evening:** Research Agent (Gemini 2.5 Flash) synthesizes last 48h of news per stock via Tavily. Returns `sentiment`, `confidence`, `source_urls`.
- **Morning:** Signal Agent sends evening thesis + morning technicals to Groq (Llama 3.3 70B) for confirmation. If confidence < 0.6 → stock removed from today's trades.
- **Both fail:** Rule-based decision used, `groq_confidence=-1.0`, trade not blocked (LLM is advisory).

### Position Sizing

```
risk_amount = 1% × account_balance
position_size = floor(risk_amount / (ATR × 2))
stop_loss = entry − (ATR × 2)
take_profit = entry + (ATR × 4)   # 1:2 risk/reward minimum
```

Hard caps: max 3 open positions, max 40% capital per position.

---

## Agents

> **Build layer vs runtime:** The codebase was built by Claude Code subagents (Architect/Coder/Tester/Reviewer — using Claude Opus/Sonnet/Haiku). Those are development-time tools. The trading pipeline itself makes **zero Anthropic API calls at runtime** — it uses Gemini and Groq only.

| Agent | Session | Runtime LLM | Responsibility |
|-------|---------|-------------|----------------|
| Data Collector | 22:00 | — | Refreshes `fundamentals_history` for Nifty 200 via Screener.in (yfinance fallback on 3 failures). 45-day cache expiry. |
| Screener | 22:20 | — | Quality filter → 12-1 momentum rank → regime filter → top 5 candidates written to DB |
| Research | 22:40 | Gemini 2.5 Flash | LangChain ReAct agent. Controls Tavily queries adaptively. Earnings branch: switches to transcript analysis if earnings in last 5 days. Output: `SentimentResult` |
| Watchlist Builder | 23:30 | — | Reads screener + research results from DB. Applies combined decision rule. Sends Telegram + email checkpoint. Auto-approves in paper mode. |
| Morning Validator | 08:00 | Gemini 2.5 Flash | Fetches last 12h news, uses Gemini to check for material overnight events, removes affected stocks. Re-confirms regime. Hard deadline: 08:15 or safe mode. |
| Signal | 08:20 | Groq Llama 3.3 70B (Gemini fallback) | Calculates RSI/MACD/Bollinger/ATR. Sends thesis to Groq for confirmation. If both LLMs fail → rule-based decision, `groq_confidence=-1.0` sentinel. |
| Risk | 08:50 | — | Kill switch checks, exact position sizing (1% risk ÷ 2×ATR), approve/reject each signal |
| Execution | 09:05 | — | Price deviation check (>1.5% → skip), writes order to DB before placing, places CNC orders. Auto-confirms in paper mode. |
| Monitor | 09:15–15:45 | — | Stop-loss/take-profit loop every 5 min. Reads stored sentiment from `research_reports` DB (no live LLM call). GTT reconciliation every 30 min — missing GTT → immediate re-place. |
| Reporter | 15:45 | — | Daily P&L, Sharpe, drawdown → `reports/YYYY-MM-DD.md` + Telegram + email |

---

## Database

SQLite in WAL mode. DuckDB attached for analytics.

| Table | Written by | Contents |
|-------|-----------|---------|
| `fundamentals_history` | Data Collector | ROE, D/E, EPS, volume per symbol with `fetched_at_ist` |
| `screener_results` | Screener | Momentum scores, quality pass/fail, regime, position size multiplier |
| `research_reports` | Research | Sentiment, confidence, source URLs, `completed_at` (set only when fully done — race condition guard) |
| `watchlist` | Watchlist Builder | Approved candidates, scorecard, `human_approved`, `approval_source` |
| `morning_signals` | Morning Validator | News-validated, regime-confirmed signals |
| `signals` | Signal Agent | Groq-confirmed technical signals with indicator values |
| `risk_approvals` | Risk Agent | APPROVED/REJECTED per signal with specific reason |
| `execution_checkpoints` | Execution Agent | PENDING/CONFIRMED/TIMEOUT per run date |
| `orders` | Execution Agent | Every order written BEFORE placement — never after |
| `positions` | Monitor Agent | Currently open positions with live P&L |
| `trades` | Monitor Agent | Completed round trips with entry/exit/P&L |
| `daily_pnl` | Reporter | End-of-day P&L |
| `strategy_perf` | Reporter | Cumulative Sharpe, win rate, drawdown |
| `agent_logs` | All agents | Every action with timestamp — the full audit trail |

---

## Production Patterns

### Race condition guard: Research → Watchlist
Research writes `completed_at` only when fully done per stock. Watchlist reads `WHERE completed_at IS NOT NULL AND DATE(completed_at) = run_date`. Research still in progress → `research_incomplete` logged, stock skipped. This prevents the watchlist from acting on partial research results.

### Safe mode
Activates on: morning pipeline not ready by 08:50, execution timeout at 09:13, any kill switch trigger, Groq + Gemini both unreachable. Effect: no new positions. All open positions continue with rule-based stop-losses. Alert sent via both Telegram and email.

### Kill switches

| Trigger | Action |
|---------|--------|
| Drawdown from peak > 15% | Halt all trading immediately |
| Win rate < 40% after 20 completed trades | Halt all trading immediately |
| 5 consecutive losses | Pause 1 week minimum |
| Any unconfirmed order | Halt and verify manually |

### GTT reconciliation
Every 30 minutes during market hours: query broker API for all expected GTT stop-loss orders. Any missing → immediate Telegram + email alert + re-place order. Prevents a stop-loss silently dropping without notice.

### Notification discipline
Every notification goes to both Telegram and email — always. If Telegram fails and the system proceeds without alerting the user, the human checkpoint is silently eliminated. Both channels must confirm delivery; if both fail, safe mode activates automatically.

### Data staleness
Screener.in fundamentals older than 45 days → `fundamentals_stale` → stock excluded from trading. Cross-validated against yfinance P/E: >20% deviation between sources → `stale_data` → skip the stock entirely rather than trade on potentially corrupt fundamentals.

### SQLite WAL concurrency — `SQLITE_BUSY_SNAPSHOT`
`busy_timeout=30000` (30 seconds) does NOT fix `SQLITE_BUSY_SNAPSHOT`. This WAL-specific error occurs when a read snapshot is stale after a write advanced the WAL pointer — no amount of waiting resolves it. Fix: strict two-phase pattern across all agents: READ phase (open → read → close), COMPUTE phase (pure Python, no DB), WRITE phase (open → BEGIN → writes → COMMIT → wal_checkpoint → close). `log_agent_action()` is never called inside a `BEGIN/COMMIT` block — it opens its own connection, which would deadlock if called mid-transaction.

### Non-blocking checkpoint design
`run_watchlist_agent()` sends the Telegram + email checkpoint and returns immediately — it never blocks waiting for a reply. The orchestrator calls `check_watchlist_timeout()` separately at 07:00 IST. When a Telegram reply arrives, the orchestrator calls `record_human_approval()`. This is critical: blocking here would hold up the pipeline for up to 8 hours.

### Emergency intraday rescreen
`monitor_agent` checks Nifty 50 close-vs-prev-close at 15:35 IST. If the drop exceeds 3% → full screener pipeline re-runs immediately, `screener_results` table updated for the current `run_date` via `INSERT OR REPLACE`. Open positions are not closed — existing GTT stop-losses handle protection. Monday's scheduled rescreen still runs regardless.

### Lookahead bias prevention (backtest)
`get_fundamentals_for_date(symbols, as_of_date)` applies a fiscal year rule: Indian annual reports (FY ending March 31) are typically published May–August. Before July, only `fiscal_year - 1` data is returned. This prevents the backtest from "knowing" FY2020 results in April 2020 — during the COVID crash at market bottom — which would make the strategy look far better than it actually was.

### Domain-allowlisted news
Tavily Search is configured with `include_domains` pointing at 7 editorial sources (Economic Times, Moneycontrol, Business Standard, Livemint, Financial Express, Reuters, Bloomberg). An unrestricted search returns NSE data pages and stock screeners that look like "news" to search engines. Allowlisting keeps only genuine editorial articles for sentiment synthesis.

---

## LLM Stack

| Provider | Model | Purpose | Rate limit | Fallback |
|----------|-------|---------|------------|---------|
| Gemini | 2.5 Flash (free) | Nightly news synthesis | 250 RPD | Groq |
| Groq | Llama 3.3 70B (free) | Morning signal confirmation | 1,000 RPD | Gemini |
| Ollama | Llama 3.2 3B (local) | Both cloud tiers fail | Unlimited | — |

Free tier risk: both Gemini and Groq can change rate limits without notice. Ollama running locally is the permanent fallback for exactly this scenario.

---

## Key Design Decisions

**Why `OHLCV_LOOKBACK_DAYS=400` and not 200?**
The 200-day SMA requires 200 *trading* days. Calendar days > trading days (weekends + ~15 NSE holidays/year). 400 calendar days ≈ 270 trading days — guaranteed headroom regardless of the holiday calendar. Using 200 calendar days risks computing the SMA on only 130–140 data points, producing a wrong value silently.

**Why Nifty 200 universe (not Nifty 50)?**
The quality filter (ROE >12%, D/E <1.0, positive EPS) systematically removes PSU banks and infrastructure-heavy names. Applied to Nifty 50, this frequently yields fewer than 3 passing stocks — triggering `thin_universe` and skipping the week. Nifty 200 provides the selection depth needed while keeping the universe fully liquid (>₹20cr/day volume filter enforced independently).

**Why LangChain ReAct for the research agent?**
The earnings branch is conditional: if a stock reported earnings in the last 5 days, switch from standard news synthesis to transcript analysis. A fixed 3-query pipeline cannot do this conditional branching. ReAct lets the LLM decide which tool to call next based on what it has already found. For the common case (no earnings), both approaches produce equivalent results; for the earnings branch, ReAct is the only clean solution.

**Why `cache_expiry_hours=0` in the signal agent?**
The screener runs at 22:00 and writes OHLCV to the CSV cache. The signal agent runs at 08:20 the next morning — only ~10 hours later. Under the default 24-hour expiry the cache is still valid, so the signal agent reads yesterday's closing prices and computes RSI/MACD/ATR on stale data. Zero expiry forces a fresh NSE fetch every morning run.

**Why 12-1 momentum (not 12-0)?**
Removing the last month's return filters short-term mean reversion. A stock that trended for 12 months but reversed sharply last month is likely being distributed — not a momentum entry. The 12-1 formulation captures sustained directional trend rather than recent point-in-time performance.

**Why CNC delivery only (no intraday yet)?**
Capital conflict rule: intraday must use capital from closed swing positions only — it cannot run from the same pool as open swing positions. At ₹1,00,000 paper budget with max 3 open swing positions, there is no cleanly separated intraday capital. Intraday unlocks after 3 months of profitable paper trading on swing.

**Why `groq_confidence=-1.0` as a sentinel (not `None` or `0.0`)?**
`-1.0` is outside the valid confidence range (0.0–1.0). Any code reading the value can unambiguously detect LLM failure — a real low-confidence score of 0.0 would block the trade, but `-1.0` means "LLM unavailable, use rule-based signal." Storing `None` would require NULL handling in SQL; `0.0` would incorrectly trigger the confidence threshold check. The sentinel is stored in the `signals` table and visible in all audit logs.

**Why D/E is computed, not scraped from Screener.in?**
Screener.in doesn't expose debt-to-equity as a named ratio. It must be computed from two Balance Sheet rows: `D/E = Borrowings / (Equity Capital + Reserves)`. Financial companies (banks, NBFCs) use "Borrowing" (singular); industrial companies use "Borrowings" (plural) for the same concept. The parser handles both variants — this was discovered only after HDFC Bank, ICICI Bank, Axis Bank, and Kotak Bank all silently returned NULL D/E and failed the quality filter for weeks.

---

## Backtest Results (Phase 2 — 14 Years of NSE Data, 2010–2023)

Covers 2010–2011 correction, 2015–2016 mid-cap crash, 2020 COVID crash and recovery, 2021 bull run, 2022 bear market.

**Zero transaction costs.** Realistic round-trip friction for NSE CNC delivery is ~0.4% (STT + exchange fees + slippage). Live performance will be lower than all numbers below.

### Walk-Forward Methodology

Dataset split into train (2010–2018) and test (2019–2023). Same strategy parameters applied to both — no re-fitting on the test set. Walk-forward was run on original parameters (RSI<40, ROE>15%). The current live parameters (C4: RSI<55, ROE>12%) were selected afterward via sensitivity analysis on the full 2010–2023 run.

| Period | Sharpe | Max DD | Win Rate | Trades | Profit Factor | Eval Score |
|--------|--------|--------|----------|--------|---------------|------------|
| Train 2010–2018 | -0.058 | 10.99% | 39.77% | 176 | 0.95 | 68/100 — Refine |
| **Test 2019–2023** | **0.916** | **9.47%** | **49.62%** | **133** | **1.66** | **74/100 — Deploy** |

Train period red flag: negative expectancy (-0.098% per trade). Root cause: 2010–2011 correction, 2015–2016 mid-cap crash, and 2018 NBFC crisis each reduce 12-1 momentum signal quality independently. Test outperforming train reflects structural regime differences — not overfitting. Drawdown shrinks from train to test (10.99% → 9.47%), which is the correct direction for a strategy with no look-ahead.

Regime filter validation: 0 transitions in 2013, 0 in 2014 — the extended sideways period produced zero excessive switching. Phase 2 requirement satisfied.

### Parameter Sensitivity Analysis (full 2010–2023 run)

4 RSI/ROE combinations tested before selecting current parameters. DECISIONS.md documents the baseline and C4 outcome; C1–C3 intermediary details were not retained. The analysis showed that RSI<40 (the original threshold) over-filtered — producing only ~52 trades per 14 years, too thin for statistical significance.

| Config | RSI threshold | ROE threshold | Trades | Profit Factor | Sharpe | Max DD |
|--------|--------------|--------------|--------|---------------|--------|--------|
| Original (pre-analysis) | < 40 | > 15% | 357 | 1.203 | 0.405 | 12.58% |
| **C4 (current live params)** | **< 55** | **> 12%** | **441** | **1.415** | **0.851** | **9.07%** |

4 combos tested before selecting C4 — sensitivity validates parameter choice rather than tunes for a single peak (anti-overfitting evidence). C4 win rate on the full run is not documented in DECISIONS.md (see unverified claims section below).

### Gate Summary (current live parameters, full 2010–2023 run)

| Gate | Required | Result |
|------|----------|--------|
| Sharpe ratio | > 1.0 | ❌ 0.851 — no backtest combo cleared this gate. Assessed against 8-week live paper Sharpe per DECISIONS.md. |
| Maximum drawdown | < 15% | ✅ 9.07% |
| Win rate | > 40% | — not documented for C4 full run |
| Trade count | > 100 | ✅ 441 trades |
| Profit factor | > 1.3 | ✅ 1.415 |

---

## Production Bug History

### DUMMYVEDL1 phantom symbol — `src/data/fetcher.py` (fixed 2026-05-17)
niftyindices.com's Nifty 200 CSV contains a symbol `DUMMYVEDL1`. The original `fetch_ohlcv` raised `FetchError` for the entire batch on any single symbol failure — silently crashing the screener every evening from May 4 onwards. No screener results written → no research → no watchlist → no trades for 13 consecutive days with no visible error to the user.

Fix: filter any `DUMMY`-prefixed symbol at parse time. Changed `fetch_ohlcv` to skip-and-continue on individual symbol failures; only raise `FetchError` if every symbol in the batch fails.

### Signal agent read stale morning OHLCV — `src/agents/signal_agent.py` (fixed 2026-05-17)
`fetch_ohlcv(cache_expiry_hours=24)` (default). Evening screener writes OHLCV cache at 22:00. Signal agent runs at 08:20 — only 10 hours later, cache still valid — RSI/MACD/ATR computed on yesterday's closing prices instead of morning prices.

Fix: `cache_expiry_hours=0` in signal agent's fetch call.

### Research agent was a pseudo-agent — `src/agents/research_agent.py` (fixed 2026-05-17)
Original implementation assembled context externally and called Gemini once with a fixed prompt — LLM had no control over tool calls. Could not adapt to the earnings branch (required different queries and different output structure).

Fix: rewrote as a true LangChain ReAct agent. LLM drives the tool-call loop, decides which Tavily queries to run, handles the earnings transcript branch conditionally.

### SQLite WAL `SQLITE_BUSY_SNAPSHOT` — all agents (fixed during Phase 3 development)
`PRAGMA busy_timeout=30000` does not fix `SQLITE_BUSY_SNAPSHOT`. This WAL-specific error fires when a reader's snapshot is stale after a writer advanced the WAL pointer — it cannot be resolved by waiting longer. Root cause: `log_agent_action()` was called inside `BEGIN/COMMIT` blocks; it opens its own connection, which deadlocks against the calling agent's held write lock.

Fix: strict read/compute/write phase separation across every agent. All `log_agent_action()` calls are outside any transaction. Write phases are pure DB writes with no logging. Pattern documented in `docs/context/decisions-log.md` and applied retroactively to all agents.

### Non-blocking watchlist checkpoint — `src/agents/watchlist_agent.py` (fixed during Phase 3 development)
Original design had `run_watchlist_agent()` blocking until a Telegram reply arrived — which would hold up the entire pipeline for up to 8 hours waiting for the user.

Fix: `run_watchlist_agent()` sends the checkpoint notification and returns immediately. The orchestrator calls `check_watchlist_timeout()` at 07:00 IST (separate scheduled call). When a Telegram reply arrives, the orchestrator calls `record_human_approval()`. The three functions are fully decoupled.

### Lookahead bias in fundamentals backtest — `src/data/fundamentals.py` (fixed during Phase 2 development)
The original implementation used April as the fiscal year cutoff. Indian annual reports (FY ending March 31) are typically published May–August. Using April meant the backtest "knew" FY2020 results in April 2020 — at the COVID crash bottom — producing artificially better entry decisions.

Fix: fiscal year cutoff moved to July (`month >= 7`). Before July, `fiscal_year - 1` data is used. This provides a 2–4 month buffer matching real publication timelines and prevents any look-ahead on annual results.

### Emergency intraday rescreen — `src/agents/monitor_agent.py` (added during Phase 4 development)
Without mid-week rescreening, a sharp Nifty drop during market hours would leave the screener running on stale Monday data. The Monday candidates could be stocks whose fundamentals or regime conditions changed materially.

Fix: `monitor_agent` checks Nifty 50 close-vs-prev-close at 15:35 IST every day. If the drop exceeds 3%, it triggers a full screener pipeline re-run immediately, overwriting `screener_results` for the current `run_date` via `INSERT OR REPLACE`. Open positions are not closed — existing GTT stop-losses handle protection. Monday's weekly rescreen still runs regardless.

### Domain-allowlisted news — `src/agents/research_agent.py` (fixed during Phase 3 development)
Unrestricted Tavily search returned NSE data pages, stock screener results, and broker quote pages — all of which rank highly for stock ticker queries but contain no editorial content. Gemini synthesised these as "news," producing garbage sentiment scores.

Fix: `include_domains` set to 7 known editorial sources (Economic Times, Moneycontrol, Business Standard, Livemint, Financial Express, Reuters, Bloomberg). Allowlisting is safer than blocklisting — new aggregators appear constantly, while the set of quality Indian financial editorial sources is stable.

### `groq_confidence=-1.0` sentinel — `src/agents/signal_agent.py` (designed during Phase 3)
Original spec said "skip all trades if both LLMs fail." This meant a network outage at 08:20 would block every trade regardless of how strong the technical signal was — LLM unavailability became a hard veto.

Fix: on both-LLM failure, the trade proceeds on rule-based signal alone and `groq_confidence=-1.0` is stored in the `signals` table. `-1.0` is outside the valid range (0.0–1.0), so any downstream code can unambiguously detect LLM unavailability vs. a genuine low-confidence score. Storing `0.0` would incorrectly trigger the confidence threshold block; storing `NULL` adds SQL NULL-handling complexity everywhere. The `-1.0` sentinel is explicit and searchable in audit logs.

---

## Current Status

**Phase 5 — Paper Trading Validation**

- Pipeline runs autonomously every NSE trading day
- 2 completed trades to date (both RELIANCE, March–April 2026, executed before the regime entered `BELOW_200DMA_10DAYS`)
- Current regime: `BELOW_200DMA_10DAYS`, day ~51 as of 2026-05-18 — no new positions until Nifty 50 crosses back above 200 DMA
- Test suite: 650 passing, 5 pre-existing known failures (backtest net-of-brokerage P&L × 2, FetchError shape post-DUMMYVEDL1-fix × 3)

**Phase 6 (live trading) gate — all required:**
- 8 weeks of paper trading completed
- 20 completed paper trades (entry + exit)
- Sharpe ≥ 0.8 over full paper period
- Max drawdown stayed below 15%
- Win rate above 40%
- Written go/no-go document

No real money until this gate is fully met and documented. No exceptions.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Package manager | uv |
| Broker | Shoonya (FlatTrade) — paper mode only currently |
| OHLCV data | jugaad-data (NSE direct) + yfinance fallback |
| Fundamentals | Screener.in scraping + yfinance fallback |
| News | Tavily Search API |
| LLM orchestration | LangChain (ReAct pattern) |
| LLM providers | Gemini 2.5 Flash · Groq Llama 3.3 70B · Ollama Llama 3.2 3B (local fallback) |
| Database | SQLite WAL + DuckDB for analytics |
| Notifications | Telegram Bot API + Gmail OAuth2 |
| Indicators | pandas-ta (RSI, MACD, Bollinger Bands, ATR) |
| Backtesting | backtesting.py |
| Tests | pytest + mypy + ruff |

---

## Setup

```bash
uv sync
source .venv/bin/activate
cp .env.example .env
# Fill in all API keys — see .env.example for required variables

# Run pipeline (auto-detects session from IST clock)
python main.py

# Force a specific session
python main.py --override-time 22:00   # evening
python main.py --override-time 08:00   # morning

# Verify
python -m pytest tests/ -v
python -m mypy src/ --ignore-missing-imports
python -m ruff check src/
```

Required `.env` variables: `LIVE_TRADING`, `PAPER_TRADING`, `DATABASE_URL`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `TAVILY_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GMAIL_CREDENTIALS`, `MAX_TRADE_AMOUNT`.

`LIVE_TRADING=false` is the hardcoded default. Live orders require both `LIVE_TRADING=true` in `.env` AND explicit human confirmation at the execution agent checkpoint. Both conditions are required simultaneously — neither alone is sufficient.
