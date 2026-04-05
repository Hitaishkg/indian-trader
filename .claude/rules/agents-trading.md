# Trading Layer — Python Agent SDK

Ten agents controlled by the Python orchestrator (src/agents/orchestrator.py).
The orchestrator runs via Python Agent SDK. Agents are Python classes.
They communicate by reading and writing to the shared SQLite database.

## Model Tiers

| Tier | Model | Agents |
|------|-------|--------|
| **Opus** | claude-opus-4-5 | Research Agent, Watchlist Builder |
| **Haiku** | claude-haiku-4-5 | Data Collector, Screener, Morning Validator, Signal Agent, Risk Agent, Execution Agent, Monitor Agent, Reporter Agent |

Opus is assigned where the agent makes a genuine selection or strategic decision —
synthesising conflicting information, judging news quality, or deciding which stocks
to trade. All other agents execute rules, apply math, or call external LLMs.

---

## Evening Session (runs daily at 22:00 IST)

### 1. Data Collector Agent — 22:00 — **Haiku**
- Fetches OHLCV for all Nifty 50 stocks via jugaad-data
- Downloads sector index data (NIFTY_IT, NIFTY_BANK, NIFTY_AUTO, etc.)
- Checks NSE website for corporate announcements
- On failure: retries 3 times with 2-minute intervals, then alerts via
  Telegram + email and halts the evening session entirely
- Writes to: market_data table

### 2. Screener Agent — 22:20 — **Haiku**
- Reads market_data for all Nifty 50 stocks
- Applies quality filter: ROE, D/E, EPS, volume, price (see strategy.md)
- If fewer than 3 stocks pass → logs thin_universe, skips rest of evening
- Calculates 12-1 momentum factor for all passing stocks
- Applies regime filter (Nifty 50 200 DMA check)
- Ranks top 5 candidates, applies 2% tiebreaker rule
- Writes to: screener_results table

### 3. Research Agent — 22:40 — **Opus**
- Takes top 5 candidates from screener_results
- For each stock: 3 Brave Search queries (last 48h news)
- Earnings check: if earnings reported in last 5 days → switch to transcript
  analysis. If transcript not retrievable → flag earnings_transcript_unavailable,
  fall back to standard news synthesis
- Sends fetched content to Gemini 2.5 Flash free tier for synthesis
- Required output fields per stock: sentiment, confidence, source_urls (list),
  completed_at (timestamp), earnings_transcript_unavailable (bool)
- Critical: writes completed_at to research_reports ONLY when fully done
  for that stock. Watchlist Builder reads this flag to prevent race conditions.
- Writes to: research_reports table

### 4. Watchlist Builder Agent — 23:30 — **Opus**
- Reads screener_results + research_reports
- Only reads research where completed_at IS NOT NULL and IS from today's run
- Stocks without completed_at flag → logged as research_incomplete, skipped
- Applies combined decision rule: both screener rank AND sentiment must agree
- Produces final watchlist: max 5 stocks with trade type and full rationale
- Sends notification via Telegram AND email (both, always):
  "Tomorrow's watchlist: X stocks. Top pick: Y (score: Z). Approve by 08:00?"
- Default if no human response by 08:00 → watchlist kept as-is, logged
- Writes to: watchlist table

---

## Morning Session (runs daily at 08:00 IST)

### 5. Morning Validator Agent — 08:00 — **Haiku**
- NEWS CHECK RUNS FIRST before any signal generation
- Fetches last 12 hours of news for each watchlist stock via Brave Search
- If major overnight event (earnings, circuit breaker, trading halt, RBI
  decision directly affecting the sector) → removes stock from watchlist,
  logs reason as overnight_event_removal
- Fetches fresh morning OHLCV and pre-market indicative prices
- Re-confirms regime filter still valid
- Hard deadline: must complete by 08:15. If not → alert + safe mode
- Writes to: morning_signals table

### 6. Signal Agent — 08:20 — **Haiku**
- Reads validated watchlist from morning_signals
- Calculates technical indicators: RSI, MACD, Bollinger Bands, ATR
- For each stock, sends to Groq (Llama 3.3 70B free tier):
  "Evening thesis: [X]. Morning technicals: [Y]. Does the thesis still hold?
  Confidence score 0-1?"
- If Groq confidence < 0.6 → stock removed from today's trades, logged
- If Groq fails or rate-limits → Gemini 2.5 Flash fallback (same prompt)
- If both fail → keep rule-based decision, set groq_confidence=-1.0, do NOT block the trade
  (Override: LLM is advisory only. Original "skip all trades" behavior removed 2026-04-05.)
- Hard deadline: MUST complete by 08:50. If pipeline not ready by 08:50
  → safe mode: no new positions, monitor existing positions with rules only
- Writes to: signals table

### 7. Risk Agent — 08:50 — **Haiku**
- Reads all approved signals from signals table
- Checks every signal against ALL kill switch criteria (see risk.md)
- Calculates exact position size: risk amount ÷ (ATR × 2), rounds DOWN
- Verifies: daily loss limit not breached, drawdown kill switch not triggered,
  max 2 open positions not exceeded, max trade amount not exceeded
- If any kill switch triggered → halts ALL trading, sends urgent alert,
  logs halt reason to agent_logs
- Stamps each signal APPROVED or REJECTED with specific reason
- Writes to: risk_approvals table

### 8. Execution Agent — 09:05 ⚑ HUMAN CHECKPOINT — **Haiku**
- Sends summary via Telegram AND email (both):
  "2 trades approved:
   HDFC Bank: BUY 3 shares @ ₹1,580 | SL: ₹1,532 | TP: ₹1,660
   Reason: [thesis]. Proceed? Reply Y to confirm."
- Waits 8 minutes for human confirmation
- On timeout (no response by 09:13) → safe mode, no trades placed, logged
- On confirmation: checks current market price vs approval price
  - Deviation > 0.5% → recalculate position size and stop-loss before placing
  - Deviation > 1.5% → skip this trade entirely, log as price_slippage_exceeded
- Places CNC delivery orders via Shoonya API
- Places GTT stop-loss and take-profit orders immediately after each fill
- Writes every order to orders table BEFORE placing — never after
- Writes to: orders table

---

## Market Hours (09:15–15:45 IST)

### 9. Monitor Agent — every 5 minutes — **Haiku**
- Checks all open positions against stop-loss and take-profit levels
- Stop-loss hit → exit immediately, ALWAYS autonomous, no human approval
- Take-profit hit → exit immediately, ALWAYS autonomous
- GTT reconciliation every 30 minutes: query Shoonya API to verify all
  expected GTT orders are still active. If any GTT order missing →
  alert immediately via Telegram + email + re-place the order
- Updates positions table continuously
- Writes to: positions table, trades table (on close)

### 10. Reporter Agent — 15:45 IST — **Haiku**
- Reads all trade and position data from SQLite
- Calculates: daily P&L, win/loss count, current drawdown, running Sharpe
- Generates daily report as reports/YYYY-MM-DD.md
- Updates daily_pnl and strategy_perf tables
- Sends summary notification via Telegram + email

---

## Database Tables

SQLite WAL mode. Apply at every startup:
```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA cache_size=-64000;
PRAGMA synchronous=NORMAL;
```

| Table | Written by | Contents |
|-------|-----------|---------|
| market_data | Data Collector | OHLCV per symbol per day |
| screener_results | Screener Agent | Momentum scores, quality pass/fail |
| research_reports | Research Agent | Sentiment, confidence, URLs, completed_at |
| watchlist | Watchlist Builder | Approved candidates for next day |
| morning_signals | Morning Validator | News-validated, regime-confirmed signals |
| signals | Signal Agent | Groq-confirmed technical signals |
| risk_approvals | Risk Agent | Approved/rejected with specific reasons |
| orders | Execution Agent | Every order BEFORE placement |
| positions | Monitor Agent | Currently open positions |
| trades | Monitor Agent | Completed round trips with P&L |
| daily_pnl | Reporter Agent | End-of-day P&L per trading day |
| agent_logs | All agents | Every action with timestamp and result |

DuckDB for analytics (attach to SQLite for fast aggregation):
```python
conn = duckdb.connect()
conn.execute("ATTACH 'data/trading.db' AS db (TYPE SQLITE)")
```

---

## Safe Mode Behavior

Safe mode activates when any of these occur:
- Morning pipeline not ready by 08:50
- Human confirmation timeout at 09:13
- Any kill switch trigger
- Groq AND Gemini both failing

Safe mode means: no new positions opened. All existing positions continue
to be monitored by Monitor Agent with rule-based stop-losses (no LLM needed).
Alert sent via Telegram + email. Manual override available.
Log the safe mode activation with exact reason every time.