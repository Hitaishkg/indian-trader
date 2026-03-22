# Build Phases

Do not skip phases. Each phase has one milestone that gates the next.
No real money until Phase 5 milestone is fully met and documented.

---

## Phase 1 — Foundation (Week 1–2)

Goal: end-to-end pipeline flow working with zero strategy sophistication.
Paper trades logged to database. Data quality confirmed on real data.

Build order (strict — do not reorder):
1. `src/data/validator.py` — FIRST. Validates actual live NSE data quality.
   Checks ROE plausibility, D/E coverage, OHLCV gaps. Logs data_quality_score.
2. `src/config/settings.py` — loads and validates all .env variables at startup.
   Fails loudly if required variables missing. No silent defaults for secrets.
3. `src/data/fetcher.py` — yfinance + jugaad-data OHLCV, caching to CSV with expiry
4. `src/data/cleaner.py` — validates data, handles missing values, flags anomalies
5. `src/data/fundamentals.py` — Screener.in scraper with 45-day cache expiry,
   3-strike yfinance fallback, cross-validation check vs yfinance P/E
6. `src/utils/logger.py` — structured logging to SQLite agent_logs table
7. `src/utils/notifier.py` — Telegram + Gmail (both channels, always)
8. `src/execution/paper_trader.py` — simulated orders with realistic P&L tracking
9. `main.py` — runs the full pipeline in dry-run mode end-to-end

Milestone: `python main.py` completes without errors. Paper trades are logged
to the database. Data validator confirms quality scores on real Nifty 50 data.

---

## Phase 2 — Strategy Core (Week 3–4)

Goal: real strategy implemented and validated over 2010–2023 historical data.
All backtest gates must pass before Phase 3 begins.

Build order:
1. `src/indicators/technical.py` — RSI, MACD, Bollinger Bands, ATR via pandas-ta
2. `src/strategy/quality_filter.py` — all 5 hard filters + minimum 3-stock rule
3. `src/strategy/momentum.py` — 12-1 momentum factor, weekly recalculation only
4. `src/strategy/regime.py` — Nifty 50 200 DMA filter with open position tightening
5. `src/backtest/runner.py` — backtesting.py wrapper for strategy validation
6. `src/backtest/validator.py` — checks all 5 backtest gates (Sharpe, drawdown,
   win rate, trade count, profit factor)
7. Run backtest over 2010–2023 — all 5 gates must pass

Validate the regime filter specifically over 2013–2014 (extended sideways market)
to confirm the 200 DMA filter does not cause excessive whipsawing during flat periods.

Milestone: strategy passes all backtest gates over 2010–2023 period.
Written record of backtest results saved to reports/phase2-backtest-results.md

---

## Phase 3 — Intelligence Layer (Week 5–6)

Goal: LLM research and signal confirmation integrated on top of validated strategy.

Build order:
1. `src/agents/research_agent.py` — Gemini synthesis, returns URLs + completed_at flag
2. `src/agents/signal_agent.py` — Groq morning confirmation with Gemini fallback
3. `src/agents/screener_agent.py` — full 3-step selection pipeline integrated
4. `src/agents/watchlist_agent.py` — reads only completed research (race condition fix)
5. Integration tests covering full evening pipeline with real market data

Milestone: full evening pipeline runs end-to-end and produces a watchlist with
LLM-validated rationale. Race condition prevention confirmed working via logs.

---

## Phase 4 — Full Trading Pipeline (Week 7–8)

Goal: paper trading running automatically every trading day.
Human checkpoint working. GTT reconciliation active.

Build order:
1. `src/execution/auth.py` — TOTP auto-login for Shoonya, runs at 06:15 via scheduler
2. `src/execution/shoonya_broker.py` — order placement, position queries, GTT management
3. `src/agents/risk_agent.py` — all kill switch checks, position sizing, approve/reject
4. `src/agents/execution_agent.py` — human checkpoint, price validity check, CNC orders
5. `src/agents/monitor_agent.py` — stop-loss/take-profit loop + GTT reconciliation every 30 min
6. `src/agents/reporter_agent.py` — daily P&L report at 15:45
7. `src/agents/orchestrator.py` — Python Agent SDK pipeline scheduling all agents
8. Windows Task Scheduler setup — daily automated runs at correct times
9. Kill switch testing — deliberately trigger every kill switch in paper mode,
   confirm each one halts trading and sends correct alerts

Milestone: system runs automatically every trading day in paper mode.
Every kill switch confirmed working. GTT reconciliation confirmed working.
Human checkpoint delivering to both Telegram and email.

---

## Phase 5 — Validation (Weeks 9–16)

Goal: 8 weeks of paper trading on real market data. All go-live gates met.

During this phase:
- Run paper trading every trading day for minimum 8 weeks
- Complete minimum 20 paper trades (entry + exit)
- Run the pre-trade scorecard manually before every Execution Agent checkpoint
- Manually spot-check 3 Research Agent reports per week — verify source URLs
  are genuinely independent (not rewrites of the same wire report)
- Log every skipped trade explicitly: bad conditions vs potential bug
- These look identical from output. Only logs distinguish them.

At end of Phase 5, write a go/no-go decision document covering:
- Sharpe ratio over the full 8-week period (must be ≥ 0.8)
- Maximum drawdown reached (must stay below 15%)
- Win rate over all completed trades (must be > 40%)
- Assessment of data quality issues encountered
- Assessment of LLM signal quality (based on spot-check findings)
- Explicit go or no-go decision with written reasoning

Do not go live without this document. No exceptions.

---

## Phase 6 — Live Trading (Month 4+)

Goal: real money, conservative, only what paper trading proved works.

Steps in order:
1. Set up Oracle Cloud Free Tier VM — permanent static IP at zero cost
2. Whitelist the static IP with Shoonya account
3. Move system to the VPS (or keep local with static IP from ISP)
4. Start with ₹5,000 only — keep ₹5,000 in reserve
5. Maximum 1 trade per week for first 4 weeks
6. Scale only after 4 consecutive profitable live weeks
7. Deploy second ₹5,000 only after consistent documented real returns

Do not rush Phase 6. The system built and validated in Phases 1–5 is the
real asset. The ₹10,000 is a learning budget. If the strategy works with
₹5,000, it works with more capital when you add it. If it fails with ₹5,000,
you have ₹5,000 to restart without losing everything.