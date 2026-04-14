# Walk-Forward Backtest Results

**Run date**: 2026-04-14  
**Method**: Train/test split — same fixed strategy parameters, no parameter optimization

---

## Periods

| Period | Dates | Sharpe | Max DD | Win Rate | Trades | Profit Factor | Return | Gates |
|--------|-------|--------|--------|----------|--------|---------------|--------|-------|
| Train | 2010–2018 | -0.058 | 10.99% | 39.77% | 176 | 0.9465 | -4.59% | 2/5 ❌ |
| Test | 2019–2023 | 0.916 | 9.47% | 49.62% | 133 | 1.6472 | +46.95% | 4/5 ⚠️ |

### Gate breakdown

| Gate | Threshold | Train | Test |
|------|-----------|-------|------|
| Sharpe > 1.0 | >1.0 | -0.058 ❌ | 0.916 ❌ |
| Max drawdown | <15% | 10.99% ✅ | 9.47% ✅ |
| Win rate | >40% | 39.77% ❌ | 49.62% ✅ |
| Trade count | ≥100 | 176 ✅ | 133 ✅ |
| Profit factor | >1.3 | 0.9465 ❌ | 1.6472 ✅ |

---

## Backtest-Expert Evaluation

| Period | Score | Verdict |
|--------|-------|---------|
| Train 2010–2018 | 68/100 | **Refine** |
| Test 2019–2023 | 74/100 | **Deploy** |

Train red flag: negative expectancy (-0.098%/trade). No red flags on test.

---

## Overfitting Assessment

Train Sharpe is negative; test Sharpe is 0.916. Test **outperforms** train — this is not overfitting, it is regime dependence. The strategy is momentum-based and performs poorly during 2010–2018 (2010–2011 correction, 2015–2016 mid-cap crash, 2018 NBFC crisis) and much better in 2019–2023 (COVID recovery + sustained bull run).

Drawdown check: test (9.47%) < train (10.99%) — OK.

---

## Regime Filter — 2013–2014 Whipsaw Check

- 2013: **0** regime changes  
- 2014: **0** regime changes

Zero whipsaw in the extended sideways 2013–2014 period. Phase 2 requirement satisfied.

---

## Phase 2 Gate Verdict

**FAIL — Phase 3 cannot begin.**

Strategy fails 3/5 gates on the train period (2010–2018). Test period is close (4/5, Sharpe 0.916 vs gate 1.0) but cannot override train failure.

### Root causes to investigate before iterating

1. **Point-in-time fundamentals coverage for 2010–2015**: Screener.in fetch populated current data only. Early years may have sparse coverage causing poor quality filter decisions.
2. **Excessive exposure during 2015–2016, 2018 downturns**: 33 regime changes in train vs 14 in test suggests the regime filter did not block trading soon enough.
3. **Momentum persistence weaker in 2010–2018**: High-volatility crash-recovery cycles reduce 12-1 momentum signal quality.

### Recommended next steps

1. Run `SELECT COUNT(*), MIN(fiscal_year), MAX(fiscal_year) FROM fundamentals_history` to confirm data coverage
2. Inspect per-year P&L (check `regime_changes_by_year` for 2015, 2016, 2018)
3. Do not adjust gates — adjust strategy or data quality per phases.md rule

---

## Files

- `reports/backtest_eval_2026-04-14_034144.md` — Train 2010–2018 detailed evaluation  
- `reports/backtest_eval_2026-04-14_034151.md` — Test 2019–2023 detailed evaluation
