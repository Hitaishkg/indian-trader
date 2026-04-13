# Walk-Forward Backtest Results

**Run date**: 2026-04-13
**Run by**: A1 (Teammate agent)
**Status**: NOT EXECUTED — see Execution Blockers section

---

## Planned Walk-Forward Split

| Period | Start | End | Purpose |
|--------|-------|-----|---------|
| Train (in-sample) | 2010-01-01 | 2018-12-31 | Strategy development / parameter validation |
| Test (out-of-sample) | 2019-01-01 | 2023-12-31 | Overfitting detection |

---

## Execution Blockers

The backtest was not executed. Two blockers prevented it:

### 1. No shell execution access
The agent session does not have Bash execution permission. `run_backtest()` in
`src/backtest/runner.py` must be run as a Python process — it cannot be invoked
via read-only file tools.

### 2. `fundamentals_history` table likely unpopulated
`run_backtest()` calls `_check_fundamentals_history_populated()` at step 8 of its
pipeline (line ~965 in runner.py). This raises `BacktestError(phase="data_fetch")`
if the table is empty or missing. The `fundamentals_history` table is populated by
`fetch_historical_fundamentals()` in `src/data/fundamentals.py`, which scrapes
Screener.in for annual fundamentals per symbol going back to 2010. As of the only
live report available (`reports/2026-04-14.md`), the system has logged 1 completed
paper trade and cumulative P&L of -₹42.23 — consistent with a system still in
early Phase 4 paper trading, where the historical fundamentals pre-population step
has not yet been run.

---

## What the Backtest Would Do (Code Analysis)

`run_backtest(start_date, end_date, initial_cash=10_000.0)` in
`src/backtest/runner.py` runs this pipeline:

1. **Collect Nifty 50 universe per year** via `get_nifty_universe_for_year()` —
   returns the actual constituents for each calendar year 2010–2023 (survivorship
   bias partially mitigated by using year-specific membership).

2. **Fetch OHLCV** via `fetch_ohlcv()` for all symbols, starting 400 calendar days
   before `start_date` (the `LOOKBACK_CALENDAR_DAYS` warm-up window needed for
   200-day SMA and 12-month momentum).

3. **Fetch Nifty 50 index data** via `fetch_sector_indices()` for the regime filter.

4. **Load point-in-time fundamentals** via `get_fundamentals_for_date()` each
   simulated Monday — uses fiscal year rules to avoid lookahead bias on ROE, D/E,
   EPS data.

5. **Weekly rebalance loop** (every Monday or first trading day of each ISO week):
   - Apply quality filter: ROE > 15%, D/E < 1.0, positive EPS, volume > ₹20cr, price > ₹50
   - If fewer than 3 pass → `thin_universe`, skip week
   - Compute 12-1 momentum score (12-month return minus 1-month return)
   - Select top 2 candidates (max 2 open positions)
   - Apply 200-day SMA regime filter:
     - Above 200 DMA → trade normally
     - Below 200 DMA → 50% position size, tighten open stops to 1× ATR
     - Below 200 DMA 10+ consecutive days → block new entries entirely
   - Close positions not in new top-2 (REBALANCE exit)
   - Open new positions with 1% risk sizing, rounded down

6. **Daily stop/take-profit checks** against close prices.

7. **Statistics extracted from `_PortfolioTracker`**:
   - Sharpe: annualized from daily equity returns, risk-free = 0
   - Max drawdown: peak-to-trough from equity curve
   - Win rate: % of closed trades with PnL > 0
   - Profit factor: gross profit / gross loss
   - Trade count: all completed round-trips (stop, take-profit, or rebalance exits)

---

## Gate Thresholds (from `src/backtest/validator.py`)

All five gates must pass on the **test period** before paper trading can advance
to Phase 3. Gate evaluation is by `validate_backtest()` — it never sets
`gates_passed=True` itself; that is the validator's exclusive responsibility.

| Gate | Threshold | Source constant |
|------|-----------|-----------------|
| Sharpe ratio | > 1.0 | `SHARPE_THRESHOLD = 1.0` |
| Max drawdown | < 15.0% | `MAX_DRAWDOWN_THRESHOLD = 15.0` |
| Win rate | > 40.0% | `WIN_RATE_THRESHOLD = 40.0` |
| Total trades | >= 100 | `MIN_TRADES_THRESHOLD = 100` |
| Profit factor | > 1.3 | `PROFIT_FACTOR_THRESHOLD = 1.3` |

---

## Train Period Metrics

| Metric | Value | Gate | Pass/Fail |
|--------|-------|------|-----------|
| Sharpe ratio | NOT RUN | > 1.0 | — |
| Max drawdown | NOT RUN | < 15% | — |
| Win rate | NOT RUN | > 40% | — |
| Total trades | NOT RUN | >= 100 | — |
| Profit factor | NOT RUN | > 1.3 | — |

---

## Test Period Metrics (Out-of-Sample)

| Metric | Value | Gate | Pass/Fail |
|--------|-------|------|-----------|
| Sharpe ratio | NOT RUN | > 1.0 | — |
| Max drawdown | NOT RUN | < 15% | — |
| Win rate | NOT RUN | > 40% | — |
| Total trades | NOT RUN | >= 100 | — |
| Profit factor | NOT RUN | > 1.3 | — |

---

## Overfitting Assessment

Cannot be performed without actual execution results. The walk-forward
methodology planned was:

- **Overfitting signal**: test metrics degrade to < 50% of train metrics
  (per backtest-expert skill guidance: "Out-of-sample < 50% of in-sample
  performance" is a warning sign)
- **Parameters to check**: the strategy has 3 fixed parameters
  (ROE threshold 15%, D/E threshold 1.0, momentum lookback 12-1 months) — low
  parameter count reduces overfitting risk inherently
- **Regime dependency risk**: the 200-day SMA regime filter adds a structural
  regime dependency. The spec requires explicit validation over the 2013-2014
  sideways period to confirm no excessive whipsawing — this cannot be confirmed
  without execution.

---

## What Is Needed to Run the Backtest

To execute this walk-forward analysis, the following must be done in sequence:

1. **Populate `fundamentals_history` table**:
   ```python
   from src.data.fundamentals import fetch_historical_fundamentals
   from src.data.fetcher import fetch_nifty50_symbols
   symbols = fetch_nifty50_symbols()
   fetch_historical_fundamentals(symbols, force_refresh=False)
   ```
   This is a Screener.in scraping job. Expect 50+ requests with 2–5s delays
   (~5–10 minutes total). Writes to `fundamentals_history` SQLite table.

2. **Run the walk-forward backtest**:
   ```python
   import datetime
   from src.backtest.runner import run_backtest
   from src.backtest.validator import validate_backtest

   # Train period
   train = run_backtest(
       start_date=datetime.date(2010, 1, 1),
       end_date=datetime.date(2018, 12, 31),
   )
   train_val = validate_backtest(train)

   # Test period (out-of-sample)
   test = run_backtest(
       start_date=datetime.date(2019, 1, 1),
       end_date=datetime.date(2023, 12, 31),
   )
   test_val = validate_backtest(test)
   ```

3. **Update this file** with actual metric values and overfitting assessment.

---

## Recommendation

**Cannot recommend proceeding to paper trading or adjusting strategy.**

No numeric results exist. The system is currently in early Phase 4 paper trading
(1 completed trade, equity ₹9,957.77). The historical backtest infrastructure is
built and correct in code structure, but the prerequisite data population step
(`fetch_historical_fundamentals`) must be run before any backtest can execute.

Next action: run `fetch_historical_fundamentals()` on a machine with shell access
and Screener.in connectivity, then re-run this walk-forward analysis.

---

## Backtest Infrastructure Assessment (Code Review)

The runner code is structurally sound for walk-forward use:

- **No lookahead bias detected**: `get_fundamentals_for_date()` applies fiscal
  year rules (month <= 6 uses prior year data); OHLCV slices are filtered to
  `<= current_date` before any calculation.
- **Survivorship bias partially mitigated**: `get_nifty_universe_for_year(year)`
  returns the actual Nifty 50 membership for each calendar year, not the current
  2026 membership. Stocks that left the index are not retroactively included.
- **Low parameter count**: 3 strategy parameters (ROE threshold, D/E threshold,
  momentum formula) reduces curve-fitting risk relative to strategies with 10+
  tunable parameters.
- **Commission not modeled**: `Backtest(commission=0)` at line ~998 in runner.py.
  For a real assessment, broker commission (Shoonya CNC delivery ~0.1–0.5% per
  leg) and slippage should be added. This is a known gap — live results will be
  worse than backtest results.
- **Stop-loss uses close prices**: `check_stops()` triggers on daily close, not
  intraday low. This understates stop-loss frequency — real stops on intraday
  wicks would exit more trades at a loss.
