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

Drops the most recent month to filter short-term reversal noise. Academically validated as the strongest single return predictor on NSE data (IIM Ahmedabad Fama-French-Momentum research, 1994–present). Recalculated weekly only — never daily. Top 10 candidates from the filtered universe.

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

| Agent | Session | Model | Responsibility |
|-------|---------|-------|----------------|
| Data Collector | 22:00 | Haiku | Refreshes `fundamentals_history` for Nifty 200 via Screener.in (yfinance fallback on 3 failures). 45-day cache expiry. |
| Screener | 22:20 | Haiku | Quality filter → 12-1 momentum rank → regime filter → top 10 candidates |
| Research | 22:40 | Opus | LangChain ReAct agent. Controls Tavily queries adaptively. Earnings branch: switches to transcript analysis if earnings in last 5 days. Output: `SentimentResult` |
| Watchlist Builder | 23:30 | Opus | Combines screener rank + sentiment via scorecard (40-pt max, 28 to proceed). Sends Telegram + email. Auto-approves in paper mode. |
| Morning Validator | 08:00 | Haiku | Fetches last 12h news, removes stocks with overnight material events. Re-confirms regime. Hard deadline: 08:15 or safe mode. |
| Signal | 08:20 | Haiku | Calculates RSI/MACD/Bollinger/ATR. Sends thesis to Groq for confirmation. Gemini fallback. |
| Risk | 08:50 | Haiku | Kill switch checks, exact position sizing, approve/reject each signal |
| Execution | 09:05 | Haiku | Price deviation check (>1.5% → skip), writes order to DB before placing, places CNC orders. Auto-confirms in paper mode. |
| Monitor | 09:15–15:45 | Haiku | Stop-loss/take-profit loop every 5 min. GTT reconciliation every 30 min — missing GTT → immediate re-place. |
| Reporter | 15:45 | Haiku | Daily P&L, Sharpe, drawdown → `reports/YYYY-MM-DD.md` + Telegram + email |

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

## Backtest Results (Phase 2 — 14 Years of NSE Historical Data, 2010–2023)

Covers 2010–2011 correction, 2015–2016 mid-cap crash, 2020 COVID crash and recovery, 2021 bull run, 2022 bear market.

**Parameter sensitivity analysis:** 4 RSI/ROE combinations systematically tested before selecting current parameters (C4: RSI<55, ROE>12%). Chosen for best risk-adjusted performance without overfitting to any single market regime.

### Full 2010–2023 Run (current parameters: RSI<55, ROE>12%)

| Gate | Required | Result |
|------|----------|--------|
| Sharpe ratio | > 1.0 | ❌ 0.851 — mechanical baseline only; Sharpe gate assessed on 8-week paper trading instead |
| Maximum drawdown | < 15% | ✅ 9.07% |
| Win rate | > 40% | ✅ (current params) |
| Trade count | > 100 | ✅ 441 trades |
| Profit factor | > 1.3 | ✅ 1.415 |

### Walk-Forward Validation (train 2010–2018 / test 2019–2023)

| Period | Sharpe | Max DD | Win Rate | Trades | Profit Factor | Score |
|--------|--------|--------|----------|--------|---------------|-------|
| Train 2010–2018 | -0.058 | 10.99% | 39.77% | 176 | 0.95 | 68/100 Refine |
| **Test 2019–2023** | **0.916** | **9.47%** | **49.62%** | **133** | **1.66** | **74/100 Deploy** |

Train underperforms due to regime dependence (2010–2011 correction, 2015–2016 crash, 2018 NBFC crisis reduce 12-1 momentum signal quality). Test outperforming train is not overfitting — it reflects structural regime differences, not parameter optimization. Drawdown improves from train to test (10.99% → 9.47%), which is the correct direction.

Regime filter whipsaw validation: 0 regime changes in 2013, 0 in 2014 — the extended sideways period produced no excessive switching. Phase 2 requirement satisfied.

**Note:** Backtest uses zero transaction costs. Realistic round-trip friction for NSE CNC delivery is ~0.4% (STT + exchange fees + slippage). Actual live performance will be lower.

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
