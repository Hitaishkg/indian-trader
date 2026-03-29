# Spec: src/backtest/runner.py -- Backtest Runner

**Date**: 2026-03-25 (revised)
**Author**: Architect Agent (Opus)
**Status**: Awaiting approval
**Phase**: 2 -- Strategy Core (step 5 of 7)

---

## 1. Module Purpose

Wraps the `backtesting.py` library to run the full three-step stock selection
strategy (quality filter, 12-1 momentum rank, regime filter) over a configurable
historical date range (2010--2023). Uses `get_fundamentals_for_date()` for
point-in-time fundamentals each simulated Monday, `get_nifty_universe_for_year()`
for survivorship-bias-free universe per year, and `fetch_ohlcv()` /
`fetch_sector_indices()` for OHLCV data. Simulates up to 2 simultaneous positions
with ATR-based stop-losses and take-profits. Returns a `BacktestResult` frozen
dataclass with all metrics needed by `src/backtest/validator.py` to evaluate the
5 backtest gates. This module never sets `gates_passed=True`.

---

## 2. Public API

### BacktestResult dataclass

```python
@dataclass(frozen=True)
class BacktestResult:
    """Complete output of a backtest run.

    All percentage fields are stored as positive floats representing
    percentage points (e.g., 14.2 means 14.2%, not 0.142).

    Attributes:
        start_date: First date of the backtest period.
        end_date: Last date of the backtest period.
        total_return_pct: Total portfolio return as percentage points.
        annualized_return_pct: CAGR as percentage points.
        sharpe_ratio: Annualized Sharpe ratio (risk-free rate = 0).
        max_drawdown_pct: Maximum peak-to-trough drawdown as positive
            percentage points (e.g. 14.2 means 14.2% drawdown).
        win_rate_pct: Percentage of winning trades (e.g. 55.0 means 55%).
        total_trades: Total number of completed round-trip trades.
        profit_factor: Gross profit / gross loss. float('inf') if zero
            losses with at least one win. 0.0 if no winning trades.
        regime_changes: Count of ABOVE<->BELOW 200 DMA transitions.
        regime_blocked_weeks: Count of weeks where regime was
            BELOW_200DMA_10DAYS and new entries were blocked.
        raw_stats: dict containing the full backtesting.py stats output
            plus custom keys for debugging.
        gates_passed: Always False when returned by run_backtest(). Only
            the backtest validator sets this to True.
    """

    start_date: datetime.date
    end_date: datetime.date
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    total_trades: int
    profit_factor: float
    regime_changes: int
    regime_blocked_weeks: int
    raw_stats: dict = field(default_factory=dict)
    gates_passed: bool = False
```

### run_backtest function

```python
def run_backtest(
    start_date: datetime.date,
    end_date: datetime.date,
    initial_cash: float = 10_000.0,
) -> BacktestResult:
    """Run the full strategy backtest over the given date range.

    Fetches OHLCV data via fetch_ohlcv(), Nifty 50 index data via
    fetch_sector_indices(), and point-in-time fundamentals via
    get_fundamentals_for_date() each simulated Monday. Applies the
    three-step stock selection pipeline (quality filter -> momentum
    rank -> regime filter) with weekly rebalancing. Maximum 2
    simultaneous positions.

    Args:
        start_date: First date of the backtest period (inclusive).
            Must be >= 2010-01-01.
        end_date: Last date of the backtest period (inclusive).
            Must be <= 2023-12-31.
        initial_cash: Starting portfolio value in INR. Default 10,000.

    Returns:
        BacktestResult with gates_passed=False. Caller must validate
        against gates separately using src/backtest/validator.py.

    Raises:
        ValueError: If start_date >= end_date.
        ValueError: If start_date < 2010-01-01 or end_date > 2023-12-31.
        ValueError: If initial_cash <= 0.
        BacktestError: If data fetching fails for the entire universe.
        BacktestError: If fundamentals_history table is empty.
    """
```

### BacktestError exception

```python
class BacktestError(Exception):
    """Raised when the backtest runner encounters a fatal error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase of the backtest failed. One of:
            "data_fetch", "strategy_init", "simulation",
            "stats_extraction".
    """

    def __init__(self, message: str, phase: str) -> None:
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")
```

---

## 3. Input Contract

### run_backtest parameters

| Parameter | Type | Constraint |
|-----------|------|------------|
| start_date | datetime.date | >= 2010-01-01, < end_date |
| end_date | datetime.date | <= 2023-12-31, > start_date |
| initial_cash | float | > 0.0 |

### Data dependencies (fetched internally by run_backtest)

| Data | Source function | Key constraint |
|------|----------------|----------------|
| Stock OHLCV | `fetch_ohlcv(all_symbols, lookback_start, end_date, cache_expiry_hours=0)` | lookback_start = start_date - 400 calendar days for 252 trading day momentum lookback |
| Nifty 50 index | `fetch_sector_indices(lookback_start, end_date, cache_expiry_hours=0)` filtered to symbol=="NIFTY_50" | Needs 200+ rows before start_date for 200 DMA |
| Fundamentals | `get_fundamentals_for_date(universe, monday_date)` | Called each simulated Monday; requires fundamentals_history table pre-populated |
| Universe | `get_nifty_universe_for_year(year)` | Called per calendar year; returns empty list outside 2010-2023 |

**Pre-condition**: The caller must have called `fetch_historical_fundamentals()`
before invoking `run_backtest()`. The runner checks that the fundamentals_history
table has data and raises `BacktestError(phase="data_fetch")` if it is empty.

---

## 4. Output Contract

### BacktestResult guarantees

| Field | Type | NaN/None behavior | Guarantee |
|-------|------|-------------------|-----------|
| start_date | datetime.date | Never None | Matches input |
| end_date | datetime.date | Never None | Matches input |
| total_return_pct | float | Never NaN | Can be negative |
| annualized_return_pct | float | Never NaN | Can be negative |
| sharpe_ratio | float | Never NaN | 0.0 if no trades or flat equity |
| max_drawdown_pct | float | Never NaN | Always >= 0.0 |
| win_rate_pct | float | Never NaN | Range [0.0, 100.0]; 0.0 if no trades |
| total_trades | int | Never None | Always >= 0 |
| profit_factor | float | Never NaN | float('inf') if zero losses; 0.0 if zero wins |
| regime_changes | int | Never None | Always >= 0 |
| regime_blocked_weeks | int | Never None | Always >= 0 |
| raw_stats | dict | Never None | Always a dict (may be empty if no backtesting.py run) |
| gates_passed | bool | N/A | Always False |

---

## 5. Implementation Details

### 5.1 Architecture: backtesting.py with custom portfolio tracker

The `backtesting.py` library is designed for single-symbol backtests. Our
strategy trades a rotating multi-symbol portfolio with up to 2 positions.

**Solution**: Feed the Nifty 50 index OHLCV as the "dummy instrument" to
satisfy backtesting.py's interface requirement. The `Strategy` subclass's
`next()` method runs the full weekly pipeline internally and manages positions
via a custom `_PortfolioTracker` class. All actual trade simulation (entries,
exits, P&L) happens inside `_PortfolioTracker`, not through backtesting.py's
built-in `self.buy()` / `self.sell()`. After the simulation loop completes,
statistics are extracted from `_PortfolioTracker`, not from backtesting.py's
built-in stats.

This approach avoids fighting backtesting.py's single-instrument assumption
while still leveraging its date iteration loop and data alignment.

### 5.2 Data preparation phase (inside run_backtest, before Backtest.run)

Step-by-step:

1. Validate inputs: start_date < end_date, within [2010-01-01, 2023-12-31],
   initial_cash > 0.
2. `lookback_start = start_date - timedelta(days=LOOKBACK_CALENDAR_DAYS)` (400 days).
3. Collect all unique symbols across all years in range:
   ```
   all_symbols: set[str] = set()
   for year in range(start_date.year, end_date.year + 1):
       all_symbols.update(get_nifty_universe_for_year(year))
   ```
   If `all_symbols` is empty, raise `BacktestError(phase="data_fetch")`.
4. `ohlcv_df = fetch_ohlcv(list(all_symbols), lookback_start, end_date, cache_expiry_hours=0)`.
   If empty, raise `BacktestError(phase="data_fetch")`.
5. `sector_df = fetch_sector_indices(lookback_start, end_date, cache_expiry_hours=0)`.
6. Extract Nifty 50 data:
   ```
   nifty_full_df = sector_df[sector_df["symbol"] == "NIFTY_50"].copy()
   nifty_full_df = nifty_full_df.drop(columns=["symbol"])  # regime.py expects no symbol column
   ```
   Validate >= 200 rows; raise `BacktestError(phase="data_fetch")` if insufficient.
7. Build `nifty_bt_df` for backtesting.py: rename columns to Open, High, Low,
   Close, Volume (capitalized), set date as DatetimeIndex (timezone stripped,
   backtesting.py uses naive datetimes).
8. Pre-build `universe_by_year: dict[int, list[str]]`:
   ```
   {year: get_nifty_universe_for_year(year) for year in range(start_date.year, end_date.year + 1)}
   ```
9. Verify fundamentals_history has data: open SQLite connection to
   `settings.database_url`, query `SELECT COUNT(*) FROM fundamentals_history`.
   If 0, raise `BacktestError(phase="data_fetch", message="fundamentals_history table empty; call fetch_historical_fundamentals() first")`.
10. Set class-level attributes on `_WeeklyMomentumStrategy` (see 5.4):
    - Set `first_valid_trade_date = start_date + timedelta(days=LOOKBACK_CALENDAR_DAYS)`.
      This is the warm-up period end. No trades open before this date.
      For a 2010-01-01 start, first_valid_trade_date ≈ 2011-02-05.
      Weekly rebalance steps still run during warm-up (for data alignment) but
      position opening is blocked by the warm-up guard in `next()`.
11. Instantiate `Backtest(data=nifty_bt_df, strategy=_WeeklyMomentumStrategy, cash=initial_cash, commission=0, exclusive_orders=False)`.
    Commission is 0 because our custom tracker handles P&L directly.
12. Call `bt.run()`.
13. Extract statistics from the `_PortfolioTracker` instance (see 5.5).
14. Build and return `BacktestResult`.

### 5.3 _PortfolioTracker helper class (private)

Not exported. Manages the multi-stock portfolio simulation.

```python
class _PortfolioTracker:
    """Tracks portfolio state across the multi-symbol backtest.

    Manages cash, open positions (max 2), closed trades, and daily
    equity curve. All position sizing, stop-loss, and take-profit
    logic is handled here.
    """

    def __init__(self, initial_cash: float) -> None:
        self.initial_cash: float = initial_cash
        self.cash: float = initial_cash
        self.positions: dict[str, _Position] = {}   # symbol -> _Position
        self.closed_trades: list[_ClosedTrade] = []
        self.equity_curve: list[float] = [initial_cash]
        self.regime_changes: int = 0
        self.regime_blocked_weeks: int = 0
        self._prev_regime: str | None = None


@dataclass
class _Position:
    """A single open position in the portfolio."""
    symbol: str
    quantity: int
    entry_price: float
    entry_date: datetime.date
    stop_loss: float
    take_profit: float
    atr_at_entry: float


@dataclass
class _ClosedTrade:
    """A completed round-trip trade."""
    symbol: str
    quantity: int
    entry_price: float
    exit_price: float
    entry_date: datetime.date
    exit_date: datetime.date
    pnl: float             # (exit_price - entry_price) * quantity
    exit_reason: str        # "STOP_LOSS", "TAKE_PROFIT", "REBALANCE"
```

**_PortfolioTracker methods:**

```python
def open_position(self, symbol: str, quantity: int, entry_price: float,
                  entry_date: datetime.date, stop_loss: float,
                  take_profit: float, atr: float) -> None:
    """Open a new position. Deducts cost from cash."""

def close_position(self, symbol: str, exit_price: float,
                   exit_date: datetime.date, exit_reason: str) -> None:
    """Close a position. Adds proceeds to cash. Appends to closed_trades."""

def check_stops(self, current_date: datetime.date,
                current_prices: dict[str, float]) -> list[str]:
    """Check all open positions against stop-loss and take-profit.
    Closes positions that hit either level. Returns list of closed symbols."""

def update_equity(self, current_prices: dict[str, float]) -> float:
    """Compute current equity (cash + mark-to-market positions).
    Appends to equity_curve. Returns current equity."""

def get_open_position_count(self) -> int:
    """Return number of currently open positions."""

def get_open_symbols(self) -> list[str]:
    """Return list of symbols with open positions."""

def tighten_stops(self, symbols: list[str], atr_multiplier: float) -> None:
    """Tighten stop-losses for given symbols. New stop = entry - (atr * multiplier).
    Only tightens (moves stop up), never loosens."""

def record_regime(self, regime: str) -> None:
    """Track regime transitions. Increments regime_changes on state change.
    Increments regime_blocked_weeks when BELOW_200DMA_10DAYS."""
```

### 5.4 Strategy subclass (weekly cadence inside backtesting.py)

```python
class _WeeklyMomentumStrategy(Strategy):
    """backtesting.py Strategy subclass that runs the weekly pipeline.

    Class-level attributes are set by run_backtest() before Backtest.run():
    """
    # Set by run_backtest() before Backtest.run()
    ohlcv_df: ClassVar[pd.DataFrame]                    # full multi-symbol OHLCV
    nifty_ohlcv_df: ClassVar[pd.DataFrame]              # date + close only, for regime.py
    universe_by_year: ClassVar[dict[int, list[str]]]
    initial_cash: ClassVar[float]
    tracker: ClassVar[_PortfolioTracker]                # shared tracker instance
    first_valid_trade_date: ClassVar[datetime.date]     # start_date + LOOKBACK_CALENDAR_DAYS; no trades before this
```

**`init(self)` method:**
- Initialize `self._last_rebalance_iso_key: tuple[int, int] = (-1, -1)` (tracks (iso_year, iso_week) of last rebalance).
- Initialize `self._first_valid_trade_date: datetime.date` = class attribute `first_valid_trade_date` (set by run_backtest before Backtest.run).
- No indicator computation here (indicators are computed on-demand per symbol).

**`next(self)` method (called every trading day by backtesting.py):**

```
current_date = self.data.index[-1].date()

# FIX 3: Skip Saturday/Sunday bars (NSE has occasional special Saturday sessions)
if current_date.weekday() >= 5:
    return

current_year = current_date.year
iso_cal = current_date.isocalendar()
current_iso_key = (iso_cal[0], iso_cal[1])  # (iso_year, iso_week)

# --- DAILY: check stop-losses and take-profits ---
open_syms = self.tracker.get_open_symbols()
if open_syms:
    current_prices = _get_prices_for_date(self.ohlcv_df, current_date, open_syms)
    self.tracker.check_stops(current_date, current_prices)

# Update daily equity
all_prices = _get_prices_for_date(self.ohlcv_df, current_date, self.tracker.get_open_symbols())
self.tracker.update_equity(all_prices)

# --- WEEKLY: rebalance on first trading day of each new ISO (year, week) pair ---
# Uses (iso_year, iso_week) not just iso_week to handle year boundaries correctly.
# Handles Diwali week and other multi-day holiday blocks: first bar of the week
# triggers rebalance regardless of which day of the week it falls on.
if current_iso_key == self._last_rebalance_iso_key:
    return
self._last_rebalance_iso_key = current_iso_key

# FIX 2: Warm-up period — no trades for first LOOKBACK_CALENDAR_DAYS from start
if current_date < self._first_valid_trade_date:
    return

# 1. Get universe for current year
universe = self.universe_by_year.get(current_year, [])
if not universe:
    return

# 2. Get point-in-time fundamentals for this week's rebalance date
monday_date = _find_monday(current_date)
try:
    fundamentals_df = get_fundamentals_for_date(universe, monday_date)
except (ValueError, FundamentalsError):
    log + return  # skip week on fundamentals error

# 3. Get OHLCV slice up to current_date (no lookahead)
ohlcv_slice = self.ohlcv_df[self.ohlcv_df["date"].dt.date <= current_date]
if ohlcv_slice.empty:
    return

# 4. Apply quality filter
try:
    quality_df, filter_report = apply_quality_filter(fundamentals_df, ohlcv_slice)
except ValueError:
    log + return

if filter_report.thin_universe:
    log "thin_universe" + return

# 5. Apply momentum ranking -- top 2 only
try:
    ranked_df, momentum_report = compute_momentum(quality_df, ohlcv_slice, top_n=2)
except ValueError:
    log + return

if ranked_df.empty:
    return

# 6. Get Nifty data slice for regime filter (no lookahead)
nifty_slice = self.nifty_ohlcv_df[self.nifty_ohlcv_df["date"].dt.date <= current_date]

# 7. Apply regime filter
open_positions_list = [{"symbol": s} for s in self.tracker.get_open_symbols()]
try:
    filtered_df, regime_result = apply_regime_filter(
        ranked_df, nifty_slice, open_positions_list
    )
except ValueError:
    log + return  # insufficient nifty data early in backtest

# 8. Record regime state
self.tracker.record_regime(regime_result.regime)

# 9. Tighten stops if regime says so
if regime_result.tighten_stops and regime_result.stop_tighten_symbols:
    self.tracker.tighten_stops(
        regime_result.stop_tighten_symbols, atr_multiplier=STOP_LOSS_ATR_TIGHT
    )

# 10. If regime blocks new entries, stop here
if regime_result.regime == "BELOW_200DMA_10DAYS":
    return

# 11. Close positions not in current top 2 (rebalance out)
current_prices = _get_prices_for_date(
    self.ohlcv_df, current_date, self.tracker.get_open_symbols()
)
new_candidates = list(filtered_df["symbol"]) if not filtered_df.empty else []
for sym in list(self.tracker.get_open_symbols()):
    if sym not in new_candidates:
        price = current_prices.get(sym)
        if price is not None:
            self.tracker.close_position(sym, price, current_date, "REBALANCE")

# 12. Open new positions for top-ranked candidates not already held
current_equity = self.tracker.cash + sum(
    p.quantity * current_prices.get(p.symbol, p.entry_price)
    for p in self.tracker.positions.values()
)

for _, row in filtered_df.iterrows():
    if self.tracker.get_open_position_count() >= MAX_POSITIONS:
        break
    symbol = str(row["symbol"])
    if symbol in self.tracker.get_open_symbols():
        continue

    # Get ATR for position sizing
    sym_ohlcv = ohlcv_slice[ohlcv_slice["symbol"] == symbol].copy()
    if sym_ohlcv.empty or len(sym_ohlcv) < ATR_PERIOD + 1:
        continue
    atr_series = compute_atr_series(sym_ohlcv)
    atr_val = float(atr_series.iloc[-1])
    if pd.isna(atr_val) or atr_val <= 0:
        continue

    entry_price = float(sym_ohlcv.iloc[-1]["close"])
    if entry_price <= 0:
        continue

    # Determine stop-loss ATR multiplier from regime
    stop_multiplier = STOP_LOSS_ATR_NORMAL
    if regime_result.tighten_stops:
        stop_multiplier = STOP_LOSS_ATR_TIGHT

    stop_distance = atr_val * stop_multiplier

    # Hard cap: stop-loss no more than 3% below entry
    max_stop_distance = entry_price * STOP_LOSS_MAX_PCT
    if stop_distance > max_stop_distance:
        stop_distance = max_stop_distance

    stop_loss = entry_price - stop_distance
    take_profit = entry_price + (stop_distance * TAKE_PROFIT_RATIO)

    # Position sizing: 1% of equity / stop_distance, round DOWN
    risk_amount = current_equity * RISK_PER_TRADE
    risk_amount *= regime_result.position_size_multiplier
    if risk_amount <= 0:
        continue

    quantity = int(risk_amount / stop_distance)  # int() truncates = floor
    if quantity < 1:
        continue

    # Hard cap: no single position > 40% of equity
    position_value = quantity * entry_price
    max_position_value = current_equity * MAX_POSITION_PCT
    if position_value > max_position_value:
        quantity = int(max_position_value / entry_price)
        if quantity < 1:
            continue

    # Hard cap: MAX_TRADE_AMOUNT
    position_value = quantity * entry_price
    if position_value > MAX_TRADE_AMOUNT:
        quantity = int(MAX_TRADE_AMOUNT / entry_price)
        if quantity < 1:
            continue

    # Deduct cost from cash; open position
    self.tracker.open_position(
        symbol=symbol,
        quantity=quantity,
        entry_price=entry_price,
        entry_date=current_date,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr=atr_val,
    )
```

### 5.5 Statistics extraction (after Backtest.run completes)

After `bt.run()` returns, extract statistics from `tracker`:

```python
equity_curve = tracker.equity_curve
trades = tracker.closed_trades

# Total return
total_return_pct = ((equity_curve[-1] - initial_cash) / initial_cash) * 100

# Annualized return (CAGR)
total_days = (end_date - start_date).days
years = total_days / 365.25
if years > 0 and equity_curve[-1] > 0:
    annualized_return_pct = ((equity_curve[-1] / initial_cash) ** (1 / years) - 1) * 100
else:
    annualized_return_pct = 0.0

# Sharpe ratio: annualized, risk-free = 0
daily_returns = []
for i in range(1, len(equity_curve)):
    if equity_curve[i - 1] > 0:
        daily_returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
if daily_returns and statistics.stdev(daily_returns) > 0:
    sharpe_ratio = (statistics.mean(daily_returns) / statistics.stdev(daily_returns)) * math.sqrt(252)
else:
    sharpe_ratio = 0.0

# Max drawdown
running_max = initial_cash
max_dd = 0.0
for eq in equity_curve:
    running_max = max(running_max, eq)
    dd = (running_max - eq) / running_max if running_max > 0 else 0.0
    max_dd = max(max_dd, dd)
max_drawdown_pct = max_dd * 100

# Win rate
wins = [t for t in trades if t.pnl > 0]
win_rate_pct = (len(wins) / len(trades)) * 100 if trades else 0.0

# Profit factor
gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
gross_loss = abs(sum(t.pnl for t in trades if t.pnl <= 0))
if gross_loss > 0:
    profit_factor = gross_profit / gross_loss
elif gross_profit > 0:
    profit_factor = float('inf')
else:
    profit_factor = 0.0
```

### 5.6 Private helper functions

```python
def _find_monday(current_date: datetime.date) -> datetime.date:
    """Return the Monday of the ISO week containing current_date.

    Args:
        current_date: Any date.

    Returns:
        The Monday of that week (may be before or equal to current_date).
    """
    return current_date - timedelta(days=current_date.weekday())


def _get_prices_for_date(
    ohlcv_df: pd.DataFrame,
    target_date: datetime.date,
    symbols: list[str],
) -> dict[str, float]:
    """Get closing prices for given symbols on target_date.

    If no data exists for the exact date (holiday), uses the most recent
    prior trading day's close price. Returns empty dict entries for symbols
    with no data at all.

    Args:
        ohlcv_df: Full multi-symbol OHLCV DataFrame.
        target_date: The date to look up prices for.
        symbols: List of symbols to get prices for.

    Returns:
        Dict mapping symbol to closing price as float. Symbols with no
        available data are omitted from the dict.
    """


def _prepare_bt_dataframe(nifty_df: pd.DataFrame) -> pd.DataFrame:
    """Convert Nifty 50 OHLCV to backtesting.py format.

    Renames columns to Open, High, Low, Close, Volume (capitalized).
    Sets date as DatetimeIndex. Strips timezone info (backtesting.py
    requires naive datetimes).

    Args:
        nifty_df: Nifty 50 DataFrame with columns date, open, high,
            low, close, volume (no symbol column).

    Returns:
        DataFrame with DatetimeIndex and capitalized OHLCV columns.
    """


def _check_fundamentals_history_populated() -> None:
    """Verify fundamentals_history table has data.

    Opens a read-only SQLite connection to settings.database_url,
    queries row count. Raises BacktestError if table is empty or
    does not exist.

    Raises:
        BacktestError: If fundamentals_history is empty or missing.
    """
```

---

## 6. Constants

| Constant | Value | Explanation |
|----------|-------|-------------|
| `AGENT_NAME` | `"backtest_runner"` | Used in all log_agent_action() calls |
| `RISK_PER_TRADE` | `0.01` | 1% of current equity risked per trade |
| `MAX_POSITIONS` | `2` | Maximum simultaneous open positions |
| `MAX_POSITION_PCT` | `0.40` | No single position > 40% of equity |
| `MAX_TRADE_AMOUNT` | `10_000.0` | Hard cap per trade (INR), from CLAUDE.md |
| `STOP_LOSS_ATR_NORMAL` | `2.0` | Normal stop-loss = 2x ATR below entry |
| `STOP_LOSS_ATR_TIGHT` | `1.0` | Tight stop-loss = 1x ATR (when below 200 DMA) |
| `STOP_LOSS_MAX_PCT` | `0.03` | Hard cap: stop-loss max 3% of entry price |
| `TAKE_PROFIT_RATIO` | `2.0` | Take-profit = 2x stop-loss distance from entry |
| `ATR_PERIOD` | `14` | ATR lookback period, matches technical.py |
| `MIN_BACKTEST_START` | `datetime.date(2010, 1, 1)` | Earliest allowed start date |
| `MAX_BACKTEST_END` | `datetime.date(2023, 12, 31)` | Latest allowed end date |
| `LOOKBACK_CALENDAR_DAYS` | `400` | Calendar days subtracted from start_date for price lookback (covers 252 trading days with margin) |
| `WEEKLY_REBALANCE_DAY` | `0` | Monday = 0 in Python's weekday() |

---

## 7. Logging

All logging via `log_agent_action()` with `agent_name="backtest_runner"`.

| When | Action string | Level | Result |
|------|---------------|-------|--------|
| Backtest starts | `"backtest_start: {start_date} to {end_date}, cash={initial_cash}"` | INFO | "ok" |
| Data fetch complete | `"data_fetched: {n_symbols} symbols, {n_rows} ohlcv rows, {n_nifty} nifty rows"` | INFO | "ok" |
| Weekly rebalance | `"weekly_rebalance: week={iso_week}, year={year}, universe_size={n}"` | INFO | "ok" |
| Thin universe skip | `"thin_universe: {passed_count} stocks passed quality filter, skipping week"` | WARNING | "thin_universe" |
| Regime blocked | `"regime_blocked: BELOW_200DMA_10DAYS, no new entries this week"` | WARNING | "blocked" |
| Position opened | `"position_opened: {symbol}, qty={qty}, entry={price:.2f}, sl={sl:.2f}, tp={tp:.2f}"` | INFO | "ok" |
| Position closed (stop) | `"position_closed: {symbol}, exit={price:.2f}, reason=STOP_LOSS, pnl={pnl:.2f}"` | INFO | "ok" |
| Position closed (TP) | `"position_closed: {symbol}, exit={price:.2f}, reason=TAKE_PROFIT, pnl={pnl:.2f}"` | INFO | "ok" |
| Position closed (rebal) | `"position_closed: {symbol}, exit={price:.2f}, reason=REBALANCE, pnl={pnl:.2f}"` | INFO | "ok" |
| Stop tightened | `"stop_tightened: {symbol}, old_sl={old:.2f}, new_sl={new:.2f}"` | INFO | "ok" |
| Regime change | `"regime_change: {old_regime} -> {new_regime}"` | INFO | "ok" |
| Backtest complete | `"backtest_complete: {total_trades} trades, sharpe={sharpe:.2f}, dd={dd:.1f}%, wr={wr:.1f}%"` | INFO | "ok" |
| Data fetch error | `"data_fetch_failed: {error_msg}"` | ERROR | "error" |
| Week skipped (error) | `"week_skipped: {monday_date}, reason={error_type}"` | WARNING | "skipped" |

---

## 8. Error Handling

| Exception | Raised when | Caller should |
|-----------|-------------|---------------|
| `ValueError` | Invalid inputs (bad dates, negative cash) | Fix inputs and retry |
| `BacktestError(phase="data_fetch")` | OHLCV/fundamentals fetch fails for entire universe, or fundamentals_history empty | Check data sources; run fetch_historical_fundamentals() first |
| `BacktestError(phase="strategy_init")` | Cannot set up Strategy class attributes or Backtest instance | Check data format |
| `BacktestError(phase="simulation")` | backtesting.py raises during run() | Inspect raw error message in exception |
| `BacktestError(phase="stats_extraction")` | Cannot extract stats from tracker (e.g. empty equity curve) | Inspect equity curve |

**Internal exception handling within the weekly loop:**

Exceptions from dependency modules (`ValueError` from quality_filter, momentum,
regime; `FundamentalsError` from fundamentals) are caught at the weekly rebalance
level inside `next()`. A single week's failure skips that week and logs a WARNING.
It does not abort the entire backtest. This is critical because early weeks may
lack sufficient data (e.g., < 252 rows for momentum, < 200 rows for regime).

If the entire simulation produces zero equity curve entries beyond the initial
value, the runner still returns a valid `BacktestResult` with `total_trades=0`.
It does not raise.

Never use bare `except:`. Catch only:
- `ValueError` from strategy modules
- `FundamentalsError` from fundamentals.py
- `Exception` as an outer guard in `next()` only, with full error logging

---

## 9. Out of Scope

- **Gate evaluation**: This module does NOT check Sharpe > 1.0, drawdown < 15%,
  etc. That is `src/backtest/validator.py`.
- **LLM signals**: No Gemini/Groq/Ollama calls. The backtest tests the
  quantitative pipeline only. LLM signals cannot be backtested because the
  historical news corpus and model behavior would not match real-time conditions.
- **Technical entry signals (RSI < 40, MACD crossover)**: Not applied as entry
  gates in the Phase 2 backtest. The backtest validates stock selection quality
  (quality + momentum + regime), not entry timing. Technical signals are Phase 3+.
- **Bollinger Bands**: Computed by add_indicators() but not used. Irrelevant here.
- **Live/paper broker calls**: No Shoonya API, no PaperTrader. Pure simulation.
- **Database writes**: The runner does not write results to any DB table.
  BacktestResult is returned in memory. The validator or caller decides persistence.
- **Intraday simulation**: All entries and exits at daily close prices.
- **Transaction costs / slippage**: Not modeled in Phase 2. Can be added later.
- **Plotting or visualization**: No charts. `raw_stats` is available for external use.
- **Human checkpoint**: Always auto-approved in backtest.
- **GTT reconciliation**: Not applicable in simulation.

---

## 10. Test Hints

Tests go in `tests/backtest/test_runner.py`. All tests use synthetic data
(small DataFrames with known values), never real market data. Mock
`fetch_ohlcv`, `fetch_sector_indices`, `get_fundamentals_for_date`, and
`get_nifty_universe_for_year` to avoid network calls and DB dependencies.

1. **test_backtest_result_frozen**: `BacktestResult` raises `FrozenInstanceError`
   on attribute mutation. Verify `gates_passed` cannot be set to True after
   construction.

2. **test_backtest_result_defaults**: `gates_passed` defaults to `False`.
   `raw_stats` defaults to empty dict.

3. **test_invalid_date_range**: `start_date >= end_date` raises `ValueError`.
   `start_date < 2010-01-01` raises `ValueError`. `end_date > 2023-12-31`
   raises `ValueError`.

4. **test_invalid_initial_cash**: `initial_cash <= 0` raises `ValueError`.

5. **test_empty_universe**: Mock `get_nifty_universe_for_year()` to return `[]`
   for all years. Should raise `BacktestError` with `phase="data_fetch"`.

6. **test_thin_universe_every_week**: Mock fundamentals so fewer than 3 stocks
   pass quality filter every week. Result should have `total_trades == 0` and
   `win_rate_pct == 0.0`.

7. **test_max_2_positions_enforced**: Feed data where 5+ stocks pass all filters.
   Verify that `_PortfolioTracker` never holds more than 2 positions at any point
   during the simulation.

8. **test_regime_blocking**: Feed Nifty 50 data that stays below 200 DMA for 10+
   consecutive days. Verify `regime_blocked_weeks > 0` in the result and no new
   positions opened during the blocked period.

9. **test_regime_transition_counting**: Feed Nifty 50 data with known crossovers
   (above -> below -> above). Verify `regime_changes` matches the expected count.

10. **test_stop_loss_execution**: Create a scenario where a position's price drops
    below its stop-loss. Verify the position is closed with `exit_reason="STOP_LOSS"`
    and PnL is negative.

11. **test_take_profit_execution**: Create a scenario where a position's price rises
    above take-profit. Verify closed with `exit_reason="TAKE_PROFIT"` and PnL
    is positive.

12. **test_position_sizing_round_down**: Verify quantity is always rounded DOWN
    (int truncation, never ceiling). Set up a case where risk_amount / stop_distance
    = 3.9 and verify quantity = 3, not 4.

13. **test_position_cap_40_percent**: Verify no single position value exceeds 40%
    of current equity at entry time.

14. **test_stop_loss_tightening**: When regime transitions to BELOW_200DMA, verify
    open positions have stop-loss tightened from 2x ATR to 1x ATR. Verify the
    tightening only moves the stop up, never down.

15. **test_profit_factor_edge_cases**: All wins: `profit_factor == float('inf')`.
    All losses: `profit_factor == 0.0`. No trades: `profit_factor == 0.0`.

16. **test_sharpe_zero_when_flat**: If no trades are made (flat equity curve),
    `sharpe_ratio == 0.0`.

17. **test_rebalance_closes_stale_positions**: If a held position's symbol is no
    longer in the top 2 after a weekly rebalance, it is closed with
    `exit_reason="REBALANCE"`.

18. **test_fundamentals_history_empty**: Mock the DB to have an empty
    fundamentals_history table. Verify `BacktestError(phase="data_fetch")` is raised.

19. **test_weekend_bars_skipped**: Feed a data series that includes a Saturday bar
    (weekday() == 5). Verify that bar triggers no rebalance and no position opens.

20. **test_warmup_period_no_trades**: Set start_date such that first_valid_trade_date
    is well after start_date. Verify no positions are opened during warm-up. Verify
    the first position (if conditions are met) opens on or after first_valid_trade_date.

21. **test_diwali_week_rebalance**: Feed a week where Monday and Tuesday are missing
    (holiday block). Verify rebalance triggers on the first available bar of that ISO
    week (e.g. Wednesday), not on a fixed weekday. Track (iso_year, iso_week) not
    just iso_week.

---

## 11. File Locations

| File | Purpose |
|------|---------|
| `src/backtest/__init__.py` | Empty init file to make backtest a Python package |
| `src/backtest/runner.py` | The backtest runner module (this spec) |
| `tests/backtest/__init__.py` | Empty init file for test package |
| `tests/backtest/test_runner.py` | Tests for the backtest runner |

---

## 12. pyproject.toml Changes

Add `backtesting.py` as a project dependency:

```toml
"Backtesting>=0.3.3",
```

The PyPI package name is `Backtesting` (capital B). The import statement is
`from backtesting import Backtest, Strategy`.

Also add `statistics` -- this is a stdlib module (no pip install needed), but
note it is used for `statistics.mean()` and `statistics.stdev()` in the stats
extraction phase.

---

## 13. Backtest Gate Thresholds (reference only -- enforced by validator.py)

| Gate | Threshold |
|------|-----------|
| Sharpe ratio | > 1.0 |
| Maximum drawdown | < 15% |
| Win rate | > 40% |
| Minimum trades | >= 100 |
| Profit factor | > 1.3 |

These are NOT checked by runner.py. The runner computes metrics;
`src/backtest/validator.py` evaluates against gates and sets `gates_passed`.

---

## 14. Regime Filter Validation Period (reference)

Per phases.md: "Validate the regime filter specifically over 2013--2014
(extended sideways market) to confirm the 200 DMA filter does not cause
excessive whipsawing during flat periods."

This is done by the caller, not by runner.py. The caller can invoke
`run_backtest(date(2013, 1, 1), date(2014, 12, 31))` and inspect
`result.regime_changes` and `result.regime_blocked_weeks`.
