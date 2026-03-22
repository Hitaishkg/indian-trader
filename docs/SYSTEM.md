# Indian Trader — System Overview

> This file is maintained by the Docs Agent. Updated automatically after every build.
> For full API details and function signatures, see `docs/connections.md`.
> For architectural decisions and build history, see `docs/DECISIONS.md`.

---

## What This System Does

Swing trading system for Indian equities (NSE). Runs two sessions daily:
- **Evening (22:00 IST)**: Research and watchlist building
- **Morning (08:00 IST)**: Signal validation and trade execution

Trades Nifty 50 stocks and ETFs (NiftyBees, ITBEES) via Shoonya (Finvasia).
Zero delivery brokerage. CNC orders with GTT stop-losses.

---

## Module Map

| Module | Purpose | Phase | Status |
|--------|---------|-------|--------|
| src/data/validator.py | Data quality gate — validates OHLCV and fundamentals | 1 | ✅ Built |
| src/config/settings.py | Env loading and validation at startup | 1 | ✅ Built |
| src/data/fetcher.py | yfinance + jugaad-data OHLCV with CSV caching | 1 | ✅ Built |
| src/data/cleaner.py | Missing value handling, anomaly flagging | 1 | ✅ Built |
| src/data/fundamentals.py | Screener.in scraper with fallback + cache | 1 | ⏳ Pending |
| src/utils/logger.py | Structured logging to SQLite agent_logs | 1 | ⏳ Pending |
| src/utils/notifier.py | Telegram + Gmail notifications (both always) | 1 | ⏳ Pending |
| src/execution/paper_trader.py | Simulated orders with P&L tracking | 1 | ⏳ Pending |
| src/indicators/technical.py | RSI, MACD, Bollinger Bands, ATR | 2 | ⏳ Pending |
| src/strategy/quality_filter.py | 5-filter hard pass/fail screen | 2 | ⏳ Pending |
| src/strategy/momentum.py | 12-1 momentum factor, weekly recalc | 2 | ⏳ Pending |
| src/strategy/regime.py | Nifty 50 200 DMA filter | 2 | ⏳ Pending |
| src/backtest/runner.py | backtesting.py wrapper | 2 | ⏳ Pending |
| src/backtest/validator.py | Backtest performance gate checks | 2 | ⏳ Pending |
| src/agents/research_agent.py | Gemini news synthesis (Opus) | 3 | ⏳ Pending |
| src/agents/signal_agent.py | Groq morning confirmation | 3 | ⏳ Pending |
| src/agents/screener_agent.py | 3-step stock selection pipeline | 3 | ⏳ Pending |
| src/agents/watchlist_agent.py | Final watchlist builder (Opus) | 3 | ⏳ Pending |
| src/execution/auth.py | Shoonya TOTP auto-login | 4 | ⏳ Pending |
| src/execution/shoonya_broker.py | Shoonya order placement and GTT | 4 | ⏳ Pending |
| src/agents/risk_agent.py | Kill switch checks, position sizing | 4 | ⏳ Pending |
| src/agents/execution_agent.py | Human checkpoint + order placement | 4 | ⏳ Pending |
| src/agents/monitor_agent.py | Stop-loss loop + GTT reconciliation | 4 | ⏳ Pending |
| src/agents/reporter_agent.py | Daily P&L report | 4 | ⏳ Pending |
| src/agents/orchestrator.py | Python Agent SDK pipeline controller | 4 | ⏳ Pending |

---

## Data Flow

```
EVENING (22:00 IST)
  jugaad-data / yfinance
       ↓
  Data Collector → market_data (DB)
       ↓
  Screener Agent → screener_results (DB)
       ↓
  Research Agent [Opus] → Brave Search → Gemini → research_reports (DB)
       ↓
  Watchlist Builder [Opus] → watchlist (DB) → Telegram + Email notification

MORNING (08:00 IST)
  Morning Validator → news check FIRST → morning_signals (DB)
       ↓
  Signal Agent → Groq → signals (DB)
       ↓
  Risk Agent → risk_approvals (DB)
       ↓
  Execution Agent ⚑ → human checkpoint → orders (DB) → Shoonya API

MARKET HOURS (every 5 min)
  Monitor Agent → positions (DB) → GTT reconciliation → Shoonya API
  Reporter Agent (15:45) → daily_pnl (DB) → reports/YYYY-MM-DD.md
```

---

## Database Quick Reference

| Table | Written by | Key contents |
|-------|-----------|-------------|
| market_data | Data Collector | OHLCV per symbol per day |
| screener_results | Screener Agent | Momentum scores, quality pass/fail |
| research_reports | Research Agent | Sentiment, confidence, URLs, completed_at |
| watchlist | Watchlist Builder | Approved candidates for next trading day |
| morning_signals | Morning Validator | News-validated, regime-confirmed |
| signals | Signal Agent | Groq-confirmed technical signals |
| risk_approvals | Risk Agent | Approved/rejected with specific reasons |
| orders | Execution Agent | Every order BEFORE placement |
| positions | Monitor Agent | Currently open positions |
| trades | Monitor Agent | Completed round trips with P&L |
| daily_pnl | Reporter Agent | End-of-day P&L |
| agent_logs | All agents | Every action with timestamp |

SQLite location: `data/trading.db`
WAL mode pragmas applied at every connection open.

---

## Debugging Guide

| Problem | Where to look first |
|---------|-------------------|
| Bad data quality score | `agent_logs WHERE event_type='universe_score'` |
| Stock missing from screener | `agent_logs WHERE event_type='roe_check' AND symbol='X'` |
| Research not completed for a stock | `research_reports WHERE completed_at IS NULL` |
| Trade not placed | `orders` table + `risk_approvals WHERE symbol='X'` |
| Kill switch fired | `agent_logs WHERE event_type='kill_switch'` |
| GTT order missing | `agent_logs WHERE detail LIKE '%gtt%'` |
| LLM fallback triggered | `agent_logs WHERE detail LIKE '%fallback%'` |
| Regime filter active | `agent_logs WHERE event_type='regime_check'` |
| Screener.in fallback active | `agent_logs WHERE event_type='screener_fallback'` |
| Stale fundamentals data | `agent_logs WHERE event_type='fundamentals_stale'` |
| Safe mode activated | `agent_logs WHERE event_type='safe_mode'` |

---

## Key Thresholds (quick reference)

| Parameter | Value | Defined in |
|-----------|-------|-----------|
| ROE minimum (strategy) | > 15% | strategy/quality_filter.py |
| ROE plausibility range (data) | -50% to 200% | data/validator.py |
| D/E maximum | < 1.0 | strategy/quality_filter.py |
| D/E data coverage minimum | > 80% | data/validator.py |
| Volume floor | > ₹20 crore daily | strategy/quality_filter.py |
| OHLCV gap threshold | 5 consecutive trading days | data/validator.py |
| Data quality halt threshold | < 0.6 universe score | data/validator.py |
| Max drawdown kill switch | 15% | risk/manager.py |
| Max risk per trade | 1% of balance | risk/manager.py |
| Max simultaneous positions | 2 | risk/manager.py |
| Fundamentals cache expiry | 45 days | data/fundamentals.py |
| GTT reconciliation interval | 30 minutes | agents/monitor_agent.py |
| Morning pipeline deadline | 08:50 IST | agents/signal_agent.py |
| Execution timeout | 8 minutes | agents/execution_agent.py |