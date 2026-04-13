# Backtest Audit — 2026-04-13

Audited by: A4 (backtest-expert)
Files reviewed: src/backtest/runner.py, src/backtest/validator.py,
src/strategy/quality_filter.py, src/strategy/momentum.py,
src/strategy/regime.py, src/indicators/technical.py,
src/data/fundamentals.py, docs/context/interfaces.md

---

## 1. Survivorship Bias

**Severity: MEDIUM — partially mitigated, residual risk present**

The backtest uses `get_nifty_universe_for_year(year)` at runner.py:878, which
returns year-specific Nifty 50 constituents from the `nifty_constituents` table.
This is the correct structural approach.

However, the universe data comes from `NIFTY_CONSTITUENTS_BY_SYMBOL` in
fundamentals.py:106 — a hardcoded dict compiled manually from "NSE semi-annual
reconstitution records (2010-2023)." The comment at fundamentals.py:104 is a
red flag:

> "Represents the 'stable core' — stocks present >= 80% of the 14-year backtest
> period, plus early-era stocks included to ensure adequate universe size pre-2015."

This is a partial-survivorship shortcut. It explicitly omits stocks that were
in the Nifty 50 for fewer than 80% of the 14-year period unless added as
"early-era" stocks. The dict has ~63 symbols total (rows 107–167), which covers
both long-tenured survivors and some shorter-tenure additions. Short-tenure
deletions that failed (e.g. a stock added in 2016 and removed in 2018 due to
poor performance) may be missing. This introduces modest upward return bias in
years where those deleted underperformers would have been in the universe.

The dict does include a set of known deletions: SAIL (2010–2015), DLF
(2010–2014), YESBANK (2014–2020), VEDL (2013–2018), ZEEL (2014–2020). This
partial inclusion of delisted/removed stocks substantially reduces but does not
eliminate survivorship bias.

**What cannot be determined from code alone:** Whether all stocks removed from
the Nifty 50 between 2010–2023 are present in the dict. No external NSE
reference was checked during this audit. The dict should be audited against the
full NSE reconstitution history.

**Recommended fix:** Cross-reference `NIFTY_CONSTITUENTS_BY_SYMBOL` against the
NSE India website's full historical index composition changes (available at
nseindia.com). Add any missing short-tenure removals (especially 2015–2022 adds
that were later removed due to poor performance). Priority is on stocks removed
for underperformance — these are the direction of survivorship bias.

---

## 2. Lookahead Bias

**Severity: LOW — well-controlled with one acceptable limitation noted**

### OHLCV slice (no lookahead — PASS)
runner.py:496 slices OHLCV strictly as:
```python
ohlcv_slice = self.__class__.ohlcv_df[
    self.__class__.ohlcv_df["date"].dt.date <= current_date
]
```
Same pattern applied to Nifty slice at runner.py:542. No future data leaks
into any signal calculation.

### Momentum calculation (no lookahead — PASS)
momentum.py:153–155 uses `rows.iloc[-1]` (most recent row in the slice),
`rows.iloc[-TWELVE_MONTH_LOOKBACK]` (252 rows back), and
`rows.iloc[-ONE_MONTH_LOOKBACK]` (21 rows back) — all computed on the already
date-bounded slice passed from runner.py. No `.shift()` needed because the
slice itself enforces the boundary.

### Fundamental data (acceptable limitation — documented)
`get_fundamentals_for_date` at fundamentals.py:1586–1589 applies a fiscal
year rule:
```python
if as_of_date.month <= 6:
    fiscal_year = as_of_date.year - 1
else:
    fiscal_year = as_of_date.year
```
This means from July onward, the full-year annual report for that fiscal year
is assumed available. Indian annual reports (for FY ending March 31) are
typically published May–August. Using them from July is reasonable but
conservative — a stock with a September 2016 trade date would use FY2016 data,
which becomes available ~May–August 2016. The 2-month buffer (July vs typical
May publication) provides adequate protection.

**One real limitation:** The annual fundamentals are a single snapshot per
fiscal year. They do not capture quarterly changes within the year. A company
that collapsed in Q3 (e.g. YESBANK in 2019) would still pass quality filters
using its prior-year FY report until the following July cutoff. This is an
accepted approximation, not a bias — quarterly data would require real-time
Screener.in data which is not available historically.

### ATR for position sizing (no lookahead — PASS)
runner.py:605–612 uses `sym_ohlcv = ohlcv_slice[ohlcv_slice["symbol"] == symbol]`
and `atr_series.iloc[-1]` — the OHLCV slice is already bounded to `<= current_date`.

### Entry price (PASS)
runner.py:616 uses `sym_ohlcv.sort_values("date").iloc[-1]["close"]` — the
closing price of the current simulation date. This represents entering at
today's close, which is the standard backtesting convention for a signal
generated from today's data. No next-day open is used. Acceptable.

---

## 3. Transaction Costs

**Severity: HIGH — completely absent**

runner.py:998 sets `commission=0` in the `Backtest()` constructor. Furthermore,
the `_PortfolioTracker.open_position()` and `close_position()` methods use raw
entry/exit prices with no friction deductions (runner.py:211–212, 252–253).

For CNC (delivery) orders on Indian equities, realistic round-trip costs are:

| Cost component | Rate | Direction |
|---|---|---|
| STT | 0.1% | buy + sell |
| Exchange charges (NSE) | ~0.00345% | both legs |
| SEBI turnover fee | 0.0001% | both legs |
| Stamp duty | 0.015% | buy only |
| Shoonya brokerage | ₹0 | CNC delivery |
| **Total round-trip** | **~0.22–0.25%** | |

No slippage is modeled. For Nifty 50 stocks with >₹20 crore daily volume, bid-ask
slippage is typically 0.05–0.10% per leg, adding another ~0.10–0.20% round-trip.

**Total realistic round-trip friction: ~0.32–0.45%**

With a take-profit distance of 2× ATR (typically 2–5% for Nifty 50 stocks) and
stop-loss of 1× ATR, a missing 0.4% round-trip cost erodes approximately 8–20%
of the gross return on winning trades and widens the effective loss on losing
trades. On a strategy targeting 1:2 risk-reward this is a meaningful distortion.

A backtest passing the Sharpe > 1.0 and profit factor > 1.3 gates without any
transaction costs may fail those gates when costs are applied — particularly for
the profit factor gate, where a 0.4% friction on each of 100+ trades can flip
borderline outcomes.

**Required fix (HIGH priority):** Add transaction cost to each trade. The
correct approach for the custom `_PortfolioTracker` is:
- On `open_position`: deduct `quantity * entry_price * 0.0015` (STT buy + stamp + half exchange)
- On `close_position`: deduct `quantity * exit_price * 0.0010` (STT sell + half exchange)
- Alternatively: pass `commission=0.0025` (0.25%) to the `Backtest()` constructor
  if switching to backtesting.py's built-in order management. Note: backtesting.py's
  commission applies per-leg, so 0.0025 per leg = 0.5% round-trip — slightly
  conservative but acceptable given slippage is not separately modeled.

---

## 4. Position Sizing Consistency

**Severity: NONE — correctly implemented**

The live system spec (strategy.md) defines:
- Risk per trade: 1% of account balance
- Stop-loss: 2× ATR (tightened to 1× under regime stress)
- Position size: `risk_amount / stop_distance`, rounded DOWN
- Hard cap: 40% of capital per position, ₹10,000 absolute max

The backtest implementation at runner.py:635–658 matches exactly:
- `risk_amount = current_equity * RISK_PER_TRADE` (RISK_PER_TRADE=0.01)
- `risk_amount *= regime_result.position_size_multiplier` (applies 0.5× under
  BELOW_200DMA, 0.0× when blocked)
- `quantity = int(risk_amount / stop_distance)` — `int()` truncates = floor, matches
  the "round DOWN" spec requirement
- 40% cap enforced at runner.py:647: `int(max_position_value / entry_price)`
- ₹10,000 MAX_TRADE_AMOUNT cap enforced at runner.py:656: `int(MAX_TRADE_AMOUNT / entry_price)`

Both cap calculations use `int()` (floor), consistent with live system spec.

One minor note: regime-adjusted `risk_amount` under BELOW_200DMA gives 0.5%
effective risk. This is the intended behavior per the spec — no issue.

---

## 5. Regime Filter Validation (2013-2014)

**Severity: MEDIUM — no explicit whipsaw measurement in output**

The regime filter implementation is correct and is applied during the backtest
simulation. The 200-day SMA computation at regime.py:221 and
`count_consecutive_days_below_200dma` at regime.py:225–266 are correctly
computed on the date-bounded nifty slice.

The 2013-2014 period specifically is covered by the backtest date range
(2010-2023). `BELOW_200DMA_10DAYS` blocking and regime change tracking are
implemented (`tracker.regime_changes`, `tracker.regime_blocked_weeks`).

However, the `BacktestResult` dataclass exposes `regime_changes` (int) and
`regime_blocked_weeks` (int) but no per-period breakdown. The backtest validator
(`validate_backtest`) does not check whipsaw-specific metrics — there is no gate
for "max regime transitions per year" or "% of weeks blocked during sideways
market."

**What phases.md requires (phases.md, Phase 2):**
> "Validate the 200 DMA regime filter specifically over the 2013–2014 sideways
> period to confirm it does not produce excessive whipsawing."

This validation cannot be performed with the current output structure because
`regime_changes` is a single scalar over the entire 2010-2023 period. There is
no per-year breakdown of regime transitions or blocked weeks in `BacktestResult`
or `raw_stats`.

**Required fix (MEDIUM priority):** Add a `regime_changes_by_year: dict[int, int]`
field to `BacktestResult` and populate it in `_PortfolioTracker.record_regime()`.
A count of regime transitions in 2013 and 2014 specifically should then be
inspected manually and documented in `reports/phase2-backtest-results.md`.
A transition count > 8 in a single calendar year would indicate whipsawing.

---

## 6. Minimum Universe Rule

**Severity: NONE — correctly implemented**

The thin universe rule (fewer than 3 stocks pass quality filter → skip week,
hold cash) is enforced in two places:

1. `quality_filter.py:382` — `thin_universe: bool = passed_count < MIN_UNIVERSE_SIZE`
   (MIN_UNIVERSE_SIZE=3), returns empty DataFrame when triggered.
2. `runner.py:514–524` — checks `filter_report.thin_universe` and returns early
   from `_run_weekly_rebalance` when True, logging "thin_universe" and skipping
   the week entirely.

No trades are placed in thin-universe weeks. Cash is held. This matches the
strategy spec exactly.

---

## Overall Assessment

The backtest is structurally sound: point-in-time fundamentals prevent lookahead
bias on fundamentals, OHLCV slicing is correctly bounded, position sizing
matches the live system, and the thin universe rule is enforced. The regime
filter is correctly applied during simulation.

**Two issues require action before backtest results can be trusted as go/no-go
gates:**

1. **Transaction costs (HIGH):** The zero-commission setting systematically
   overstates all return metrics. Sharpe, profit factor, and win rate from this
   backtest are optimistic by an unknown but significant margin. Do not use
   current backtest results to make go/no-go decisions. Fix costs first, re-run.

2. **Survivorship bias (MEDIUM):** The `NIFTY_CONSTITUENTS_BY_SYMBOL` dict
   covers major removed stocks but explicitly omits shorter-tenure removals via
   the ">=80% of period" filter. This introduces upward return bias. Cross-check
   against full NSE reconstitution history before treating results as validated.

One additional gap: the regime filter whipsaw validation required by phases.md
cannot be done with current output — `regime_changes` is a single aggregate,
not broken down by year.

---

## Recommended Fixes (Priority Order)

1. **[HIGH] Add transaction costs** — runner.py `_PortfolioTracker.open_position()`
   and `close_position()`: deduct ~0.15% on entry (STT buy + stamp + exchange)
   and ~0.10% on exit (STT sell + exchange). Alternatively set `commission=0.0025`
   in `Backtest()` constructor if switching to backtesting.py order management.
   Re-run all backtest gates after applying costs.

2. **[MEDIUM] Audit survivorship bias in constituent dict** — Cross-reference
   `NIFTY_CONSTITUENTS_BY_SYMBOL` (fundamentals.py:106) against NSE India's
   full semi-annual reconstitution announcements 2010–2023. Add any removed
   stocks missing from the dict (priority: stocks removed for underperformance
   2015–2022). Document the audit result in a comment in fundamentals.py.

3. **[MEDIUM] Add per-year regime breakdown to BacktestResult** — Add
   `regime_changes_by_year: dict[int, int]` to `BacktestResult` dataclass and
   populate it during simulation. Use this to verify 2013–2014 whipsaw
   behavior as required by phases.md Phase 2 milestone.

4. **[LOW] Document fiscal year publication lag assumption** — The July cutoff
   for switching fiscal years (fundamentals.py:1586-1589) is reasonable but
   undocumented. Add a comment explaining the assumption (Indian annual reports
   typically published May–August; July provides a 2-month safety buffer) so
   future maintainers understand the design choice.
