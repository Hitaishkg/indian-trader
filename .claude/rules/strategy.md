# Trading Strategy

## What we trade
- Instruments: Nifty 50 stocks + ETFs (NiftyBees, ITBEES)
- Mode: Swing trading — 3 to 10 day holds, CNC delivery orders on Shoonya
- Intraday: locked out until 3 months of profitable paper trading is complete
- Capital conflict rule: when intraday is eventually added, it uses capital from
  closed swing positions ONLY. Cannot run swing and intraday from the same
  ₹10,000 pool simultaneously. If both swing positions are open, intraday blocked.

---

## Step 1 — Quality Filter (runs every Monday, hard pass/fail)

Every stock must pass ALL five criteria. Fail any one → eliminated, no exceptions.

| Filter | Threshold | Type |
|--------|-----------|------|
| ROE | > 15% | Hard |
| Debt-to-equity | < 1.0 | Hard |
| EPS | Positive last 4 consecutive quarters | Hard |
| Daily traded volume | > ₹20 crore | Hard |
| Price | > ₹50 | Hard |
| 52-week high proximity | Within 30% of 52-week high | Soft — tiebreaker only |

Minimum universe rule: if fewer than 3 stocks pass → skip the week entirely,
hold cash, log reason as thin_universe. Do not force trades.

Awareness: ROE >15% will naturally skew the universe toward consumer, IT, and
private financials. PSU banks and infrastructure often excluded. This is expected.

---

## Step 2 — 12-1 Momentum Rank

Formula: (12-month total return) minus (last 1-month return)

This removes short-term noise and captures the underlying directional trend.
Academically validated as the strongest single return predictor in Indian equities
(IIM Ahmedabad Fama-French-Momentum research, NSE data since 1994).

Recalculation: weekly only — every Monday before market open. Never daily.
Daily recalculation of a monthly factor produces noise and unnecessary churn.
Hold the rank fixed for the entire week. Only update mid-week if a stock
triggers a kill switch or the regime filter removes it.

Top 5 stocks from quality-filtered universe are the weekly candidates.
Tiebreaker: if two stocks score within 2% of each other → the stock closer
to its 52-week high wins.

---

## Step 3 — Regime Filter

Index: Nifty 50 200-day SMA only. Use this consistently everywhere in code.

| Nifty 50 vs 200 DMA | Action on new trades | Action on open positions |
|--------------------|---------------------|-------------------------|
| Above 200 DMA | Trade normally, full position size | No change |
| Below 200 DMA | Reduce new position sizes by 50% | Tighten all open stop-losses: 2× ATR → 1× ATR |
| Below 200 DMA 10+ consecutive days | No new positions, full cash | Tighten all open stop-losses: 2× ATR → 1× ATR |

The tightening on open positions does NOT force an exit. It reduces how much
you give back if the market continues falling.

---

## Signal Generation

### Technical signals (pure Python, calculated every morning)
- RSI < 40 → entry signal
- MACD crossover → directional confirmation
- Bollinger Band position → mean reversion context
- ATR → stop-loss calculation ONLY. Never used as an entry signal.

### LLM signals (Gemini free tier, run every evening on weekly candidates)
- Fetches last 48 hours of news via Tavily Search API (3 queries per stock)
- Synthesises: sentiment (Positive / Negative / Neutral / Mixed) + confidence score
- Returns actual source URLs — required field, not optional
- Earnings branch: if a stock reported earnings in the last 5 days →
  switch to earnings transcript analysis instead of standard news synthesis.
  If transcript not retrievable → flag as earnings_transcript_unavailable,
  fall back to standard news synthesis. Never skip the stock.

### Combined decision rule

| Technical signal | LLM sentiment | Decision |
|-----------------|---------------|----------|
| BUY | Positive or Neutral | PROCEED |
| BUY | Negative | SKIP — log reason |
| HOLD or SELL | Any | SKIP |
| In open position | Negative, confidence > 0.8 | Tighten stop-loss to 1× ATR (do not exit) |

LLM validates and blocks trades. It never generates trades from scratch.
Technical signals lead. LLM has veto power only.

---

## Position Sizing

- Risk per trade: 1% of current account balance (₹100 at ₹10,000 starting capital)
- Formula: position size = risk amount ÷ (ATR × 2)
- Hard cap: no single position > 40% of total capital (₹4,000)
- Maximum 2 open positions simultaneously
- Fractional shares: always round DOWN to nearest whole share. Effective risk
  may be slightly below 1% — this is correct and conservative, not an error.

Example: stock at ₹500, ATR = ₹10, stop-loss = 2× ATR = ₹20 below entry.
Position size = ₹100 ÷ ₹20 = 5 shares = ₹2,500 total position.

For high-price stocks (e.g. HDFC Bank at ₹1,600 with ATR ₹35):
Stop = 2× ATR = ₹70. Size = ₹100 ÷ ₹70 = 1.4 → round down to 1 share.
Effective risk = ₹70, not ₹100. This is fine — conservative is correct.

---

## Stop-Loss and Take-Profit

- Entry stop-loss: 2× ATR below entry price, hard cap at 3% of entry price
- Take-profit: minimum 2× stop-loss distance (1:2 risk-reward minimum)
- Both placed as GTT orders on Shoonya immediately after entry
- Stop-loss exits: always autonomous — no human approval ever required
- Regime tightening: 2× ATR → 1× ATR when Nifty drops below 200 DMA
- LLM tightening: 2× ATR → 1× ATR when sentiment turns Negative >0.8 on held position
- GTT reconciliation: every 30 minutes during market hours, verify all GTT orders
  still active in Shoonya system. If any missing → alert immediately + re-place

---

## Known Limitations (accepted, not solvable)

LLM confidence is self-referential: Gemini rates its own output confidence.
Two articles from different outlets may rewrite the same press release.
Mitigation: LLM is a soft filter only. Research Agent must return actual source
URLs. Manually spot-check 3 research reports per week during paper trading.

Thin market periods: during pre-budget, elections, sideways markets — LLM will
frequently return Mixed or Negative sentiment on most stocks. System will produce
very few trades. This is CORRECT behavior, not a bug. Log every skipped trade
explicitly: "skipped because conditions poor" vs "skipped because of a bug."
These look identical from the output. The log is how you tell them apart.