# Risk Management

## Kill Switch Criteria

Stop ALL trading immediately if any of these trigger:

| Trigger | Threshold | Action |
|---------|-----------|--------|
| Drawdown from peak | > 15% (₹1,500 from ₹10,000) | Stop all trading immediately |
| Win rate | < 40% after minimum 20 completed trades | Stop all trading immediately |
| Consecutive losses | 5 in a row | Pause minimum 1 week |
| Paper trading Sharpe | < 0.8 over 8-week paper period | Do not go live |
| Any unconfirmed order | Order placed but not confirmed | Halt and verify manually |

All five criteria are active from the first trade onwards. No warm-up period.

---

## Post-Kill-Switch Restart Conditions

These are defined in advance specifically to eliminate two failure modes:
revenge trading (bigger size to recover losses) and fearful trading
(so small it cannot matter). Having this written down removes the decision
when under emotional pressure.

- After any kill switch fires and written review is completed:
  risk per trade drops to 0.5% for first 10 trades post-resume
- Returns to 1% only after 5 consecutive winning trades post-resume
- If the drawdown kill switch fires a second time → stop entirely,
  rebuild strategy from scratch in paper mode before any live trading again
- Restart requires a written document: what failed, what changed, why resuming

The written review requirement is not optional. No restart without it.

---

## Go-Live Gate

Before a single rupee of real money is deployed, ALL of these must pass:

1. Minimum 8 weeks of paper trading completed
2. Minimum 20 completed paper trades (entry + exit, not just entries)
3. Sharpe ratio ≥ 0.8 over the full 8-week paper period
4. Maximum drawdown stayed below 15% throughout the entire paper period
5. Win rate above 40%
6. GTT reconciliation tested and confirmed working in paper mode
7. Every kill switch deliberately triggered and confirmed working in paper mode
8. Restart conditions tested at least once in paper mode
9. Written go/no-go decision document completed

Sharpe ≥ 0.8 replaces "beats Nifty buy-and-hold" because it adjusts for
quality of returns, not just absolute level. A lucky 2-week bull run cannot
game a Sharpe calculation.

---

## Pre-Trade Scorecard (paper trading phase only)

Run this manually before approving each Execution Agent checkpoint during
paper trading. Score out of 40. Only proceed if score is 28 or above.
Creates an audit trail. Forces structured thinking before each trade.

| Criterion | Max Score | Notes |
|-----------|-----------|-------|
| Stock passed all 5 quality filters | 5 | Binary pass/fail |
| Momentum rank in top 3 of weekly universe | 5 | Top 3 preferred over bottom of top 5 |
| Regime filter: Nifty above 200 DMA | 5 | Full score only if clearly above |
| RSI confirms entry signal (< 40) | 5 | Binary |
| MACD confirms direction | 5 | Crossover present |
| LLM sentiment Positive or Neutral | 5 | Negative = 0 points, trade skipped |
| Risk-reward ratio ≥ 1:2 | 5 | Minimum, not target |
| No earnings in next 5 days | 5 | If earnings upcoming: max possible = 35, threshold still 28 |

Earnings note: upcoming earnings reduces max possible score to 35.
Threshold of 28 still applies. Not blocked — just harder to approve.
This is intentional: earnings events introduce binary gap risk that destroys
swing trade setups.

---

## Backtest Gates (Phase 2 milestone)

Strategy must pass ALL of these before Phase 3 starts.
If any gate fails → adjust strategy parameters, never adjust the gate.

| Gate | Threshold |
|------|-----------|
| Sharpe ratio | > 1.0 |
| Maximum drawdown | < 15% |
| Win rate | > 40% |
| Minimum trades in test period | 100 |
| Profit factor | > 1.3 |

Backtest period: 2010–2023 historical data via jugaad-data.
This covers: 2010-2011 correction, 2015-2016 mid-cap crash,
2020 COVID crash and recovery, 2021 bull run, 2022 bear market.
Validate the 200 DMA regime filter specifically over the 2013-2014
sideways period to confirm it does not produce excessive whipsawing.