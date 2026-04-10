# Constraint Audit Report — Indian Trader

**Date:** 2026-04-10
**Auditor:** Claude Sonnet 4.6 (automated, read-only)
**Scope:** Full constraint compliance check against `.claude/rules/` specs

---

## FINDINGS

---

### QUALITY FILTER (strategy.md Step 1)

**[PASS] ROE > 15% hard filter**
- IMPL: `src/strategy/quality_filter.py:297` — `float(roe) <= ROE_THRESHOLD` where `ROE_THRESHOLD = 0.15`
- Boundary: exactly 15% fails — correct (spec says ">15%")
- TEST: `tests/strategy/test_quality_filter.py:162` `test_roe_below_threshold`

**[PASS] D/E < 1.0 hard filter**
- IMPL: `src/strategy/quality_filter.py:313` — `DE_THRESHOLD = 1.0`; exactly 1.0 fails — correct
- TEST: `tests/strategy/test_quality_filter.py:177` `test_de_above_threshold`

**[PASS] EPS positive for last 4 consecutive quarters**
- IMPL: `src/strategy/quality_filter.py:318-331` — checks `eps_positive_4q` boolean from fundamentals.py
- TEST: `tests/strategy/test_quality_filter.py:192` `test_eps_negative`

**[PASS] Daily traded volume > ₹20 crore**
- IMPL: `VOLUME_VALUE_THRESHOLD = 20_000_000.0` at line 335
- TEST: `tests/strategy/test_quality_filter.py:207` `test_volume_too_low`

**[PASS] Price > ₹50**
- IMPL: `PRICE_THRESHOLD = 50.0` at line 340
- TEST: `tests/strategy/test_quality_filter.py:240` `test_price_too_low`

**[PASS] 52-week high proximity tiebreaker (within 30%)**
- IMPL: `PROXIMITY_THRESHOLD = 0.30`; passed downstream, never used to eliminate
- TEST: `tests/strategy/test_quality_filter.py:278` `test_soft_filter_not_eliminating`

**[PASS] Minimum 3 stocks rule → thin_universe + skip week**
- IMPL: `src/strategy/quality_filter.py:382-407` — `MIN_UNIVERSE_SIZE = 3`; logs `thin_universe`
- TEST: `tests/strategy/test_quality_filter.py:307` `test_thin_universe_fewer_than_3`

---

### MOMENTUM (strategy.md Step 2)

**[PASS] Formula: 12-month return minus 1-month return**
- IMPL: `src/strategy/momentum.py:177-179` — `momentum_score = twelve_month_return - one_month_return`
- TEST: `tests/strategy/test_momentum.py:72`

**[WARNING] Recalculation: weekly only enforcement missing**
- IMPL: No scheduling guard in `momentum.py` — module is stateless, computes on demand
- Scheduling enforcement belongs in orchestrator.py (not yet built)
- TEST: Untested at scheduling layer
- FIX: Orchestrator must gate screener_agent to Monday runs only

**[PASS] Top 5 from quality-filtered universe**
- IMPL: `DEFAULT_TOP_N = 5` at `src/strategy/momentum.py:232-233`

**[PASS] 2% tiebreaker → 52-week high proximity wins**
- IMPL: `src/strategy/momentum.py:310-349` — `_apply_tiebreaker()` with `TIEBREAKER_THRESHOLD = 0.02`

**[WARNING] Tiebreaker: single adjacent-pair pass only**
- IMPL: Single pass — misses non-adjacent ties (e.g., positions 1 and 3 within 2%)
- FIX (low priority): Replace with stable sort by `(momentum_score DESC, pct_from_52w_high ASC)`

---

### REGIME FILTER (strategy.md Step 3)

**[PASS] Uses Nifty 50 200-day SMA only**
- IMPL: `SMA_PERIOD = 200` at `src/strategy/regime.py:27`

**[PASS] Below 200 DMA: new positions at 50% size**
- IMPL: `POSITION_SIZE_BELOW = 0.5` at line 145

**[PASS] Below 200 DMA 10+ days: no new positions**
- IMPL: `BELOW_DMA_BLOCK_DAYS = 10` → `POSITION_SIZE_BLOCKED = 0.0` at lines 139-141

**[PASS] Position tightening signal generation**
- IMPL: `src/strategy/regime.py:148-151` — `tighten_stops = True`; `PaperTrader.update_stop_loss()` exists
- NOTE: Actual tightening executed by monitor_agent (not yet built)

---

### SIGNAL GENERATION (strategy.md)

**[PASS] RSI < 40 → entry signal**
- IMPL: `RSI_BUY_THRESHOLD = 40.0` at `src/agents/signal_agent.py:54`

**[WARNING] MACD crossover vs histogram positive**
- IMPL: `src/agents/signal_agent.py:773-774` — uses `macd_hist > 0` (histogram positive), NOT a crossover
- A true crossover requires previous value ≤ 0 and current value > 0. Using histogram positive misses the "crossing point" semantic — triggers on any bullish MACD day
- TEST: Crossover vs histogram-positive distinction untested
- FIX: Fetch two rows; detect `prev_hist <= 0 and curr_hist > 0`

**[PASS] ATR used for stop-loss ONLY**
- IMPL: ATR not in `technical_buy` decision; only used in risk_agent for stop distance

**[PASS] LLM veto: BUY + Negative → SKIP**
- IMPL: `src/agents/signal_agent.py:798-816`

**[PASS] Both LLMs fail → rule-based BUY (not skip)**
- IMPL: `groq_confidence = -1.0` sentinel; threshold check skips when sentinel present

**[WARNING] Ollama fallback missing**
- SPEC (data.md): Ollama (local Llama 3.2 3B) is permanent fallback when both cloud tiers fail
- IMPL: No Ollama call anywhere in signal_agent.py
- FIX: Add Ollama HTTP call before falling back to rule-based BUY

---

### POSITION SIZING (strategy.md)

**[PASS] Risk per trade: 1% of current balance**
- IMPL: `RISK_PCT = 0.01` at `src/agents/risk_agent.py:41`

**[PASS] Formula: risk_amount / (ATR × 2)**
- IMPL: `math.floor(risk_amount / (atr * 2.0))` at line 407

**[PASS] Hard cap: no single position > 40% of capital**
- IMPL: `MAX_POSITION_PCT = 0.40` at line 41; enforced at lines 415-419

**[PASS] Maximum 2 open positions**
- IMPL: `MAX_OPEN_POSITIONS = 2` at line 42; guard at line 388

**[PASS] Always round DOWN**
- IMPL: `math.floor()` throughout

---

### STOP-LOSS / TAKE-PROFIT (strategy.md)

**[PASS] Entry stop-loss: 2× ATR below entry price**
- IMPL: `stop_distance = atr * 2.0; stop_loss = entry_price - stop_distance` at lines 404, 433

**[CRITICAL] Stop-loss 3% hard cap — NOT IMPLEMENTED**
- SPEC: "hard cap at 3% of entry price"
- IMPL: No cap applied after computing `atr * 2.0`. Volatile stocks can produce stop distances far exceeding 3%.
- TEST: No test for 3% cap
- FIX: `stop_distance = min(atr * 2.0, entry_price * 0.03)` — must apply BEFORE quantity calculation

**[PASS] Take-profit: 2× stop distance (1:2 R:R)**
- IMPL: `TAKE_PROFIT_RATIO = 2.0` at line 40; `take_profit = entry_price + (stop_distance * 2.0)`

**[WARNING] Regime tightening of open positions (2×ATR → 1×ATR)**
- IMPL: Regime signals `tighten_stops = True`; `update_stop_loss()` exists in paper_trader
- Actual tightening requires monitor_agent (not yet built)
- TEST: Untested end-to-end

**[WARNING] LLM tightening of open positions (Negative >0.8 confidence)**
- IMPL: No code reads sentiment for existing open positions and tightens their stops
- FIX: In monitor_agent — for each open position, if `sentiment == "Negative" and confidence > 0.8`: `pt.update_stop_loss(symbol, entry_price - atr * 1.0)`

---

### KILL SWITCHES (risk.md)

**[PASS] Drawdown > 15% from peak → stop all trading**
- IMPL: `DRAWDOWN_KILL_SWITCH_PCT = 15.0` at `src/agents/risk_agent.py:43`
- TEST: `tests/agents/test_risk_agent.py:356` `test_01_drawdown_15pct_fires`

**[PASS] Win rate < 40% after 20 trades**
- IMPL: `WIN_RATE_KILL_SWITCH_PCT = 40.0`; gated on `KILL_SWITCH_MIN_TRADES = 20`
- TEST: `tests/agents/test_risk_agent.py:426` `test_03_win_rate_below_40pct_fires`

**[WARNING] 5 consecutive losses: 1-week pause not enforced**
- IMPL: Detection correct at `risk_agent.py:236-241`; but no mechanism blocks runs for 7 calendar days
- TEST: Detection tested; pause duration untested
- FIX: Add `kill_switch_log` table; orchestrator skips runs when `consecutive_losses_5` fired within 7 days

**[WARNING] Consecutive losses boundary: `<= 0.0` includes break-even as loss**
- IMPL: `all(float(t["pnl"]) <= 0.0 ...)` — break-even trades count as losses
- FIX: Consider `< 0.0` unless break-even losses are intentional

**[PASS] Paper Sharpe < 0.8 → do not go live**
- IMPL: `SHARPE_KILL_SWITCH = 0.8` at line 46
- TEST: `tests/agents/test_risk_agent.py:485` `test_04_sharpe_below_0_8_fires`

**[CRITICAL] Unconfirmed order kill switch — NOT IMPLEMENTED**
- SPEC: "Any unconfirmed order → Halt and verify manually"
- IMPL: No code checks for PENDING orders that fail to reach FILLED state
- FIX: In execution_agent — after placing order, poll for confirmation; timeout → `send_alert()` + halt

---

### DATA VALIDATION (data.md)

**[PASS] ROE plausibility: -50% to 200%**
- IMPL: `ROE_MIN = -0.50; ROE_MAX = 2.00` at `src/data/validator.py:26-27`

**[PASS] D/E coverage: 80% minimum**
- IMPL: `DE_COVERAGE_THRESHOLD = 0.80` at line 29

**[PASS] OHLCV gaps: max 5 consecutive trading days**
- IMPL: `MAX_OHLCV_GAP_DAYS = 5` at line 32
- NOTE: Uses `pd.bdate_range()` (Mon-Fri) when no NSE calendar provided — will false-positive on Indian holidays

**[PASS] Screener.in 45-day cache expiry**
- IMPL: `CACHE_EXPIRY_SECONDS = 45 * 86400` at `src/data/fundamentals.py:66`

**[PASS] 3-strike yfinance fallback**
- IMPL: `MAX_STRIKES = 3` at `src/data/fundamentals.py:82`

**[PASS] P/E cross-validation: 20% deviation → skip**
- IMPL: `PE_CROSS_VALIDATION_THRESHOLD = 0.20` at `src/data/fundamentals.py:83`

**[WARNING] NSE holiday calendar not passed to validator**
- `main.py` passes no `trading_calendar` to `validate_data()` — uses Mon-Fri approximation
- FIX: Maintain static NSE holiday list in `src/data/`; pass to `validate_data()` from `main.py`

---

### ADDITIONAL GAPS

**[WARNING] Research agent uses Tavily (not Brave Search MCP)**
- IMPL: `TavilyClient` in `src/agents/research_agent.py:26` — migration documented in current-state.md
- FIX: Update `.claude/rules/strategy.md` and `.claude/rules/data.md` to replace Brave references with Tavily

**[WARNING] Research agent: raw `print()` at module level**
- IMPL: `src/agents/research_agent.py:61` — `print("Using model:", GEMINI_MODEL)` on import
- FIX: Remove or replace with `log_agent_action()`

**[INFO] Tavily `time_range="week"` vs prompt saying "last 48 hours"**
- IMPL: `src/agents/research_agent.py:586` — `time_range="week"` fetches 7 days; Gemini prompt says 48h
- FIX: Either change to `time_range="day"` or update prompt to say "last week"

**[WARNING] Groq confidence threshold 0.6 undocumented in spec**
- IMPL: `GROQ_CONFIDENCE_THRESHOLD = 0.6` at `src/agents/signal_agent.py:62` — BUY downgraded to HOLD below this
- SPEC: Only defines LLM veto via Negative sentiment; no confidence-based downgrade mentioned
- FIX: Document in strategy.md, or remove (LLM should veto via sentiment only)

**[INFO] Sharpe uses population variance (N), not sample variance (N-1)**
- IMPL: `src/agents/risk_agent.py:252-255` — divides by `len(daily_returns)` not `len - 1`
- FIX: Use `statistics.stdev()` — low priority until sample sizes grow

**[CRITICAL] execution_agent.py not built**
- All risk sizing feeds into risk_approvals table that no agent reads and acts upon
- No human checkpoint, no CNC order placement, no GTT placement

**[CRITICAL] monitor_agent.py not built**
- No stop-loss monitoring, no GTT reconciliation, no regime/LLM tightening on open positions

---

## PRIORITY FIX LIST

### CRITICAL (fix before paper trading starts)
1. **Stop-loss 3% hard cap** — `risk_agent.py`: `stop_distance = min(atr * 2.0, entry_price * 0.03)`
2. **Unconfirmed order kill switch** — in `execution_agent.py`: poll for confirmation, halt on timeout
3. **`execution_agent.py` not built** — human checkpoint + CNC via PaperTrader
4. **`monitor_agent.py` not built** — stop-loss monitoring + GTT reconciliation

### WARNING (fix in current sprint)
5. **MACD crossover** — `signal_agent.py`: compare prev/curr histogram for actual crossover
6. **Ollama fallback** — add local Ollama call in signal_agent when both cloud LLMs fail
7. **1-week pause enforcement** — orchestrator must block runs 7 days after `consecutive_losses_5`
8. **LLM stop tightening** — `monitor_agent.py`: check nightly sentiment for open positions
9. **Groq 0.6 threshold** — document in strategy.md or remove the downgrade behavior
10. **Spec files not updated for Tavily** — update `.claude/rules/strategy.md` and `data.md`
11. **NSE holiday calendar** — pass static holiday list to `validate_data()` in `main.py`

### INFO (low priority)
12. Consecutive losses boundary: `<= 0.0` vs `< 0.0` (break-even classification)
13. Sharpe: population vs sample variance
14. `research_agent.py:61` — remove raw `print()` statement
15. Tavily `time_range="week"` vs 48-hour prompt framing

---

## WHAT IS CORRECTLY IMPLEMENTED

Quality filter (all 5 hard filters + thin_universe), momentum 12-1 formula, 2% tiebreaker, all three regime states (ABOVE/BELOW/BELOW_10DAYS), RSI<40 signal, LLM veto (BUY+Negative→SKIP), Groq→Gemini→rule-based fallback, 1% risk per trade, ATR×2 sizing formula, 40% position cap, max 2 positions, floor() quantities, 1:2 R:R take-profit, all 4 kill switch detections with 20-trade gate, drawdown peak equity computation, Screener.in 45-day cache, 3-strike fallback, P/E cross-validation, OHLCV gap detection, paper_trader BEFORE-execution order write, dual-channel notifications (Telegram+Gmail), race condition prevention via `completed_at` flag.

---

## CALIBRATION NOTES (Indian Market)

- **ROE >15%** excludes PSU banks, infra, real-estate from Nifty 50 (~25-30 stocks qualify). Expected.
- **D/E <1.0** further excludes NTPC, POWERGRID, ONGC. Universe: IT, FMCG, private financials, pharma. Conservative.
- **200 DMA 10-day block** would have kept system in cash through March-April 2020 V-shaped recovery. Correct conservative behavior; set expectations accordingly.
- **Volume >₹20cr** rarely filters any Nifty 50 constituent in normal markets. May affect NiftyBees/ITBEES ETFs.
- **ATR 14-day period** — standard; 7-day or 10-day ATR may better capture recent volatility for 3-10 day swing trades.
- **Screener.in HTML changes** — 45-day cache mitigates but won't catch structural changes mid-cache. Monitor for `fundamentals_failed` spikes.
