"""Backtest runner for the Indian Trader strategy pipeline.

Wraps the backtesting.py library to run the full three-step stock selection
strategy (quality filter -> 12-1 momentum rank -> regime filter) over a
configurable historical date range (2010-2023). Uses get_fundamentals_for_date()
for point-in-time fundamentals each simulated Monday. Returns a BacktestResult
frozen dataclass with all metrics needed by src/backtest/validator.py.

This module never sets gates_passed=True. Gate evaluation is the responsibility
of src/backtest/validator.py.
"""

from __future__ import annotations

import datetime
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import ClassVar

import pandas as pd
from backtesting import Backtest, Strategy

from src.config.settings import settings
from src.data.fetcher import fetch_ohlcv, fetch_sector_indices
from src.data.fundamentals import FundamentalsError, get_fundamentals_for_date, get_nifty_universe_for_year
from src.indicators.technical import compute_atr_series
from src.strategy.momentum import compute_momentum
from src.strategy.quality_filter import apply_quality_filter
from src.strategy.regime import apply_regime_filter
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "backtest_runner"
RISK_PER_TRADE: float = 0.01
MAX_POSITIONS: int = 2
MAX_POSITION_PCT: float = 0.40
MAX_TRADE_AMOUNT: float = 10_000.0
STOP_LOSS_ATR_NORMAL: float = 2.0
STOP_LOSS_ATR_TIGHT: float = 1.0
STOP_LOSS_MAX_PCT: float = 0.03
TAKE_PROFIT_RATIO: float = 2.0
ATR_PERIOD: int = 14
MIN_BACKTEST_START: datetime.date = datetime.date(2010, 1, 1)
MAX_BACKTEST_END: datetime.date = datetime.date(2023, 12, 31)
LOOKBACK_CALENDAR_DAYS: int = 400
WEEKLY_REBALANCE_DAY: int = 0  # Monday


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class BacktestError(Exception):
    """Raised when the backtest runner encounters a fatal error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase of the backtest failed. One of:
            "data_fetch", "strategy_init", "simulation",
            "stats_extraction".
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise BacktestError.

        Args:
            message: Human-readable error description.
            phase: One of "data_fetch", "strategy_init", "simulation",
                "stats_extraction".
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public result dataclass
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Private helper dataclasses
# ---------------------------------------------------------------------------


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
    pnl: float
    exit_reason: str  # "STOP_LOSS", "TAKE_PROFIT", "REBALANCE"


# ---------------------------------------------------------------------------
# Private portfolio tracker
# ---------------------------------------------------------------------------


class _PortfolioTracker:
    """Tracks portfolio state across the multi-symbol backtest.

    Manages cash, open positions (max 2), closed trades, and daily
    equity curve. All position sizing, stop-loss, and take-profit
    logic is handled here.
    """

    def __init__(self, initial_cash: float) -> None:
        """Initialise tracker with starting cash.

        Args:
            initial_cash: Starting portfolio value in INR.
        """
        self.initial_cash: float = initial_cash
        self.cash: float = initial_cash
        self.positions: dict[str, _Position] = {}
        self.closed_trades: list[_ClosedTrade] = []
        self.equity_curve: list[float] = [initial_cash]
        self.regime_changes: int = 0
        self.regime_blocked_weeks: int = 0
        self._prev_regime: str | None = None

    def open_position(
        self,
        symbol: str,
        quantity: int,
        entry_price: float,
        entry_date: datetime.date,
        stop_loss: float,
        take_profit: float,
        atr: float,
    ) -> None:
        """Open a new position. Deducts cost from cash.

        Args:
            symbol: NSE ticker symbol.
            quantity: Number of shares to buy (must be >= 1).
            entry_price: Fill price in INR.
            entry_date: Date the position was opened.
            stop_loss: Stop-loss trigger price in INR.
            take_profit: Take-profit trigger price in INR.
            atr: ATR value at entry (used for stop tightening).
        """
        cost = quantity * entry_price
        self.cash -= cost
        self.positions[symbol] = _Position(
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            entry_date=entry_date,
            stop_loss=stop_loss,
            take_profit=take_profit,
            atr_at_entry=atr,
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"position_opened: {symbol}, qty={quantity}, "
                f"entry={entry_price:.2f}, sl={stop_loss:.2f}, tp={take_profit:.2f}"
            ),
            symbol=symbol,
            result="ok",
        )

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_date: datetime.date,
        exit_reason: str,
    ) -> None:
        """Close a position. Adds proceeds to cash. Appends to closed_trades.

        Args:
            symbol: NSE ticker symbol of position to close.
            exit_price: Exit price in INR.
            exit_date: Date the position was closed.
            exit_reason: One of "STOP_LOSS", "TAKE_PROFIT", "REBALANCE".
        """
        if symbol not in self.positions:
            return
        pos = self.positions.pop(symbol)
        proceeds = pos.quantity * exit_price
        self.cash += proceeds
        pnl = (exit_price - pos.entry_price) * pos.quantity
        self.closed_trades.append(
            _ClosedTrade(
                symbol=symbol,
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_date=pos.entry_date,
                exit_date=exit_date,
                pnl=pnl,
                exit_reason=exit_reason,
            )
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"position_closed: {symbol}, exit={exit_price:.2f}, "
                f"reason={exit_reason}, pnl={pnl:.2f}"
            ),
            symbol=symbol,
            result="ok",
        )

    def check_stops(
        self,
        current_date: datetime.date,
        current_prices: dict[str, float],
    ) -> list[str]:
        """Check all open positions against stop-loss and take-profit.

        Closes positions that hit either level.

        Args:
            current_date: The current simulation date.
            current_prices: Dict mapping symbol to current closing price.

        Returns:
            List of symbols that were closed.
        """
        closed: list[str] = []
        for symbol, pos in list(self.positions.items()):
            price = current_prices.get(symbol)
            if price is None:
                continue
            if price <= pos.stop_loss:
                self.close_position(symbol, pos.stop_loss, current_date, "STOP_LOSS")
                closed.append(symbol)
            elif price >= pos.take_profit:
                self.close_position(symbol, pos.take_profit, current_date, "TAKE_PROFIT")
                closed.append(symbol)
        return closed

    def update_equity(self, current_prices: dict[str, float]) -> float:
        """Compute current equity (cash + mark-to-market positions).

        Appends to equity_curve.

        Args:
            current_prices: Dict mapping symbol to current closing price.

        Returns:
            Current equity value as float.
        """
        mark_to_market = sum(
            pos.quantity * current_prices.get(pos.symbol, pos.entry_price)
            for pos in self.positions.values()
        )
        equity = self.cash + mark_to_market
        self.equity_curve.append(equity)
        return equity

    def get_open_position_count(self) -> int:
        """Return number of currently open positions."""
        return len(self.positions)

    def get_open_symbols(self) -> list[str]:
        """Return list of symbols with open positions."""
        return list(self.positions.keys())

    def tighten_stops(self, symbols: list[str], atr_multiplier: float) -> None:
        """Tighten stop-losses for given symbols.

        New stop = entry_price - (atr_at_entry * atr_multiplier).
        Only tightens (moves stop up), never loosens.

        Args:
            symbols: List of symbols to tighten stops for.
            atr_multiplier: ATR multiplier for the new stop distance.
        """
        for symbol in symbols:
            if symbol not in self.positions:
                continue
            pos = self.positions[symbol]
            new_stop = pos.entry_price - (pos.atr_at_entry * atr_multiplier)
            if new_stop > pos.stop_loss:
                old_sl = pos.stop_loss
                self.positions[symbol] = _Position(
                    symbol=pos.symbol,
                    quantity=pos.quantity,
                    entry_price=pos.entry_price,
                    entry_date=pos.entry_date,
                    stop_loss=new_stop,
                    take_profit=pos.take_profit,
                    atr_at_entry=pos.atr_at_entry,
                )
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=(
                        f"stop_tightened: {symbol}, old_sl={old_sl:.2f}, new_sl={new_stop:.2f}"
                    ),
                    symbol=symbol,
                    result="ok",
                )

    def record_regime(self, regime: str) -> None:
        """Track regime transitions.

        Increments regime_changes on state change.
        Increments regime_blocked_weeks when BELOW_200DMA_10DAYS.

        Args:
            regime: Current regime string ("ABOVE_200DMA", "BELOW_200DMA",
                "BELOW_200DMA_10DAYS").
        """
        if self._prev_regime is not None and regime != self._prev_regime:
            self.regime_changes += 1
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"regime_change: {self._prev_regime} -> {regime}",
                result="ok",
            )
        if regime == "BELOW_200DMA_10DAYS":
            self.regime_blocked_weeks += 1
        self._prev_regime = regime


# ---------------------------------------------------------------------------
# Strategy subclass for backtesting.py
# ---------------------------------------------------------------------------


class _WeeklyMomentumStrategy(Strategy):  # type: ignore[misc]
    """backtesting.py Strategy subclass that runs the weekly pipeline.

    Class-level attributes are set by run_backtest() before Backtest.run().
    All trade logic runs via the shared _PortfolioTracker instance; we do
    NOT call self.buy() or self.sell() from backtesting.py.
    """

    # Set by run_backtest() before Backtest.run()
    ohlcv_df: ClassVar[pd.DataFrame]
    nifty_ohlcv_df: ClassVar[pd.DataFrame]
    universe_by_year: ClassVar[dict[int, list[str]]]
    initial_cash: ClassVar[float]
    tracker: ClassVar[_PortfolioTracker]
    first_valid_trade_date: ClassVar[datetime.date]

    def init(self) -> None:
        """Initialise per-run state."""
        self._last_rebalance_iso_key: tuple[int, int] = (-1, -1)
        self._first_valid_trade_date: datetime.date = self.__class__.first_valid_trade_date

    def next(self) -> None:  # noqa: C901  (complexity is intentional — mirrors spec)
        """Run daily pipeline logic. Called every bar by backtesting.py."""
        current_date: datetime.date = self.data.index[-1].date()

        # Skip Saturday/Sunday bars
        if current_date.weekday() >= 5:
            return

        current_year: int = current_date.year
        iso_cal = current_date.isocalendar()
        current_iso_key: tuple[int, int] = (iso_cal[0], iso_cal[1])

        # --- DAILY: check stop-losses and take-profits ---
        open_syms = self.tracker.get_open_symbols()
        if open_syms:
            current_prices = _get_prices_for_date(self.ohlcv_df, current_date, open_syms)
            self.tracker.check_stops(current_date, current_prices)

        # Update daily equity
        all_prices = _get_prices_for_date(
            self.ohlcv_df, current_date, self.tracker.get_open_symbols()
        )
        self.tracker.update_equity(all_prices)

        # --- WEEKLY: rebalance on first trading day of each new ISO (year, week) pair ---
        if current_iso_key == self._last_rebalance_iso_key:
            return
        self._last_rebalance_iso_key = current_iso_key

        # Warm-up guard: no trades until sufficient history available
        if current_date < self._first_valid_trade_date:
            return

        # Outer guard: catch any unexpected exception so a single week never
        # aborts the entire backtest.
        try:
            self._run_weekly_rebalance(current_date, current_year)
        except Exception as exc:  # noqa: BLE001
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"week_skipped: {current_date}, reason={type(exc).__name__}: {exc}",
                level="WARNING",
                result="skipped",
            )

    def _run_weekly_rebalance(
        self, current_date: datetime.date, current_year: int
    ) -> None:
        """Execute one weekly rebalance step.

        Args:
            current_date: The current simulation date.
            current_year: Calendar year of current_date.
        """
        # 1. Get universe for current year
        universe = self.__class__.universe_by_year.get(current_year, [])
        if not universe:
            return

        log_agent_action(
            agent_name=AGENT_NAME,
            action=(
                f"weekly_rebalance: week={current_date.isocalendar()[1]}, "
                f"year={current_year}, universe_size={len(universe)}"
            ),
            result="ok",
        )

        # 2. Get point-in-time fundamentals for this week's Monday date
        monday_date = _find_monday(current_date)
        try:
            fundamentals_df = get_fundamentals_for_date(universe, monday_date)
        except (ValueError, FundamentalsError) as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"week_skipped: {monday_date}, reason={type(exc).__name__}",
                level="WARNING",
                result="skipped",
            )
            return

        # 3. Get OHLCV slice up to current_date (no lookahead)
        ohlcv_slice = self.__class__.ohlcv_df[
            self.__class__.ohlcv_df["date"].dt.date <= current_date
        ]
        if ohlcv_slice.empty:
            return

        # 4. Apply quality filter
        try:
            quality_df, filter_report = apply_quality_filter(fundamentals_df, ohlcv_slice)
        except ValueError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"week_skipped: {monday_date}, reason=ValueError in quality_filter: {exc}",
                level="WARNING",
                result="skipped",
            )
            return

        if filter_report.thin_universe:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=(
                    f"thin_universe: {filter_report.passed_count} stocks passed "
                    "quality filter, skipping week"
                ),
                level="WARNING",
                result="thin_universe",
            )
            return

        # 5. Apply momentum ranking — top 2 only (max 2 positions)
        try:
            ranked_df, _momentum_report = compute_momentum(quality_df, ohlcv_slice, top_n=2)
        except ValueError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"week_skipped: {monday_date}, reason=ValueError in momentum: {exc}",
                level="WARNING",
                result="skipped",
            )
            return

        if ranked_df.empty:
            return

        # 6. Get Nifty data slice for regime filter (no lookahead)
        nifty_slice = self.__class__.nifty_ohlcv_df[
            self.__class__.nifty_ohlcv_df["date"].dt.date <= current_date
        ]

        # 7. Apply regime filter
        open_positions_list: list[dict[str, object]] = [{"symbol": s} for s in self.tracker.get_open_symbols()]
        try:
            filtered_df, regime_result = apply_regime_filter(
                ranked_df, nifty_slice, open_positions_list
            )
        except ValueError as exc:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"week_skipped: {monday_date}, reason=ValueError in regime: {exc}",
                level="WARNING",
                result="skipped",
            )
            return

        # 8. Record regime state
        self.tracker.record_regime(regime_result.regime)

        # 9. Tighten stops if regime says so
        if regime_result.tighten_stops and regime_result.stop_tighten_symbols:
            self.tracker.tighten_stops(
                regime_result.stop_tighten_symbols, atr_multiplier=STOP_LOSS_ATR_TIGHT
            )

        # 10. If regime blocks new entries, stop here
        if regime_result.regime == "BELOW_200DMA_10DAYS":
            log_agent_action(
                agent_name=AGENT_NAME,
                action="regime_blocked: BELOW_200DMA_10DAYS, no new entries this week",
                level="WARNING",
                result="blocked",
            )
            return

        # 11. Close positions not in current top 2 (rebalance out)
        current_prices = _get_prices_for_date(
            self.__class__.ohlcv_df, current_date, self.tracker.get_open_symbols()
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
            try:
                atr_series = compute_atr_series(sym_ohlcv)
            except ValueError:
                continue
            atr_val = float(atr_series.iloc[-1])
            if pd.isna(atr_val) or atr_val <= 0:
                continue

            entry_price = float(sym_ohlcv.sort_values("date").iloc[-1]["close"])
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

            self.tracker.open_position(
                symbol=symbol,
                quantity=quantity,
                entry_price=entry_price,
                entry_date=current_date,
                stop_loss=stop_loss,
                take_profit=take_profit,
                atr=atr_val,
            )


# ---------------------------------------------------------------------------
# Private helper functions
# ---------------------------------------------------------------------------


def _find_monday(current_date: datetime.date) -> datetime.date:
    """Return the Monday of the ISO week containing current_date.

    Args:
        current_date: Any date.

    Returns:
        The Monday of that ISO week (may be before or equal to current_date).
    """
    return current_date - datetime.timedelta(days=current_date.weekday())


def _get_prices_for_date(
    ohlcv_df: pd.DataFrame,
    target_date: datetime.date,
    symbols: list[str],
) -> dict[str, float]:
    """Get closing prices for given symbols on or before target_date.

    If no data exists for the exact date (holiday), uses the most recent
    prior trading day's close price. Symbols with no data at all are
    omitted from the returned dict.

    Args:
        ohlcv_df: Full multi-symbol OHLCV DataFrame with 'symbol', 'date',
            'close' columns.
        target_date: The date to look up prices for.
        symbols: List of symbols to get prices for.

    Returns:
        Dict mapping symbol to closing price as float. Symbols with no
        available data are omitted.
    """
    prices: dict[str, float] = {}
    if not symbols:
        return prices

    # Filter to relevant symbols and dates up to target_date
    mask = (ohlcv_df["symbol"].isin(symbols)) & (
        ohlcv_df["date"].dt.date <= target_date
    )
    relevant = ohlcv_df[mask]
    if relevant.empty:
        return prices

    # For each symbol, take the most recent row
    for symbol in symbols:
        sym_rows = relevant[relevant["symbol"] == symbol]
        if sym_rows.empty:
            continue
        latest_row = sym_rows.sort_values("date").iloc[-1]
        prices[symbol] = float(latest_row["close"])

    return prices


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
    df = nifty_df.copy()
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    # Ensure date column becomes the index as naive datetime
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        # Strip timezone if present
        if df["date"].dt.tz is not None:
            df["date"] = df["date"].dt.tz_localize(None)
        df = df.set_index("date")
    df = df.sort_index()
    return df


def _check_fundamentals_history_populated() -> None:
    """Verify fundamentals_history table has data.

    Opens a read-only SQLite connection to settings.database_url,
    queries row count. Raises BacktestError if table is empty or
    does not exist.

    Raises:
        BacktestError: If fundamentals_history is empty or missing.
    """
    db_path = settings.database_url.replace("sqlite:///", "")
    try:
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM fundamentals_history")
            count = cursor.fetchone()[0]
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        raise BacktestError(
            message=(
                "fundamentals_history table empty; "
                "call fetch_historical_fundamentals() first"
            ),
            phase="data_fetch",
        ) from exc

    if count == 0:
        raise BacktestError(
            message=(
                "fundamentals_history table empty; "
                "call fetch_historical_fundamentals() first"
            ),
            phase="data_fetch",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
    # -----------------------------------------------------------------------
    # 1. Validate inputs
    # -----------------------------------------------------------------------
    if initial_cash <= 0:
        raise ValueError(f"initial_cash must be > 0, got {initial_cash}")
    if start_date >= end_date:
        raise ValueError(
            f"start_date must be < end_date, got {start_date} >= {end_date}"
        )
    if start_date < MIN_BACKTEST_START:
        raise ValueError(
            f"start_date must be >= {MIN_BACKTEST_START}, got {start_date}"
        )
    if end_date > MAX_BACKTEST_END:
        raise ValueError(
            f"end_date must be <= {MAX_BACKTEST_END}, got {end_date}"
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"backtest_start: {start_date} to {end_date}, cash={initial_cash}"
        ),
        result="ok",
    )

    # -----------------------------------------------------------------------
    # 2. Compute lookback start for momentum and regime warm-up
    # -----------------------------------------------------------------------
    lookback_start = start_date - datetime.timedelta(days=LOOKBACK_CALENDAR_DAYS)

    # -----------------------------------------------------------------------
    # 3. Collect all unique symbols across all years in range
    # -----------------------------------------------------------------------
    all_symbols: set[str] = set()
    universe_by_year: dict[int, list[str]] = {}
    for year in range(start_date.year, end_date.year + 1):
        year_symbols = get_nifty_universe_for_year(year)
        universe_by_year[year] = year_symbols
        all_symbols.update(year_symbols)

    if not all_symbols:
        raise BacktestError(
            message="No symbols returned from get_nifty_universe_for_year() for the given date range",
            phase="data_fetch",
        )

    # -----------------------------------------------------------------------
    # 4. Fetch stock OHLCV data
    # -----------------------------------------------------------------------
    try:
        ohlcv_df = fetch_ohlcv(list(all_symbols), lookback_start, end_date, cache_expiry_hours=0)
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"data_fetch_failed: {exc}",
            level="ERROR",
            result="error",
        )
        raise BacktestError(
            message=f"fetch_ohlcv failed: {exc}",
            phase="data_fetch",
        ) from exc

    if ohlcv_df.empty:
        raise BacktestError(
            message="fetch_ohlcv returned empty DataFrame",
            phase="data_fetch",
        )

    # -----------------------------------------------------------------------
    # 5. Fetch sector index data (includes Nifty 50)
    # -----------------------------------------------------------------------
    try:
        sector_df = fetch_sector_indices(lookback_start, end_date, cache_expiry_hours=0)
    except Exception as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"data_fetch_failed: {exc}",
            level="ERROR",
            result="error",
        )
        raise BacktestError(
            message=f"fetch_sector_indices failed: {exc}",
            phase="data_fetch",
        ) from exc

    # -----------------------------------------------------------------------
    # 6. Extract Nifty 50 data — drop symbol column (regime.py expects none)
    # -----------------------------------------------------------------------
    nifty_full_df = sector_df[sector_df["symbol"] == "NIFTY_50"].copy()
    nifty_full_df = nifty_full_df.drop(columns=["symbol"])

    if len(nifty_full_df) < 200:
        raise BacktestError(
            message=(
                f"Insufficient Nifty 50 rows for 200-day SMA: got {len(nifty_full_df)}"
            ),
            phase="data_fetch",
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"data_fetched: {len(all_symbols)} symbols, "
            f"{len(ohlcv_df)} ohlcv rows, {len(nifty_full_df)} nifty rows"
        ),
        result="ok",
    )

    # -----------------------------------------------------------------------
    # 7. Build nifty_bt_df for backtesting.py (capitalized columns, naive DatetimeIndex)
    # -----------------------------------------------------------------------
    try:
        nifty_bt_df = _prepare_bt_dataframe(nifty_full_df)
    except Exception as exc:
        raise BacktestError(
            message=f"Failed to prepare Nifty DataFrame for backtesting.py: {exc}",
            phase="strategy_init",
        ) from exc

    # -----------------------------------------------------------------------
    # 8. Verify fundamentals_history table has data
    # -----------------------------------------------------------------------
    _check_fundamentals_history_populated()

    # -----------------------------------------------------------------------
    # 9. Set class-level attributes on _WeeklyMomentumStrategy
    # -----------------------------------------------------------------------
    first_valid_trade_date = start_date + datetime.timedelta(days=LOOKBACK_CALENDAR_DAYS)

    # Ensure date column is datetime type for slicing
    if "date" in ohlcv_df.columns and not pd.api.types.is_datetime64_any_dtype(ohlcv_df["date"]):
        ohlcv_df = ohlcv_df.copy()
        ohlcv_df["date"] = pd.to_datetime(ohlcv_df["date"])

    if "date" in nifty_full_df.columns and not pd.api.types.is_datetime64_any_dtype(nifty_full_df["date"]):
        nifty_full_df = nifty_full_df.copy()
        nifty_full_df["date"] = pd.to_datetime(nifty_full_df["date"])

    tracker = _PortfolioTracker(initial_cash)

    _WeeklyMomentumStrategy.ohlcv_df = ohlcv_df
    _WeeklyMomentumStrategy.nifty_ohlcv_df = nifty_full_df
    _WeeklyMomentumStrategy.universe_by_year = universe_by_year
    _WeeklyMomentumStrategy.initial_cash = initial_cash
    _WeeklyMomentumStrategy.tracker = tracker
    _WeeklyMomentumStrategy.first_valid_trade_date = first_valid_trade_date

    # -----------------------------------------------------------------------
    # 10. Run the backtest
    # -----------------------------------------------------------------------
    try:
        bt = Backtest(
            data=nifty_bt_df,
            strategy=_WeeklyMomentumStrategy,
            cash=initial_cash,
            commission=0,
            exclusive_orders=False,
        )
        bt_stats = bt.run()
    except Exception as exc:
        raise BacktestError(
            message=f"backtesting.py raised during run(): {exc}",
            phase="simulation",
        ) from exc

    # -----------------------------------------------------------------------
    # 11. Extract statistics from _PortfolioTracker
    # -----------------------------------------------------------------------
    try:
        equity_curve = tracker.equity_curve
        trades = tracker.closed_trades

        if len(equity_curve) < 2:
            # No data produced — return safe zero-trade result
            return BacktestResult(
                start_date=start_date,
                end_date=end_date,
                total_return_pct=0.0,
                annualized_return_pct=0.0,
                sharpe_ratio=0.0,
                max_drawdown_pct=0.0,
                win_rate_pct=0.0,
                total_trades=0,
                profit_factor=0.0,
                regime_changes=tracker.regime_changes,
                regime_blocked_weeks=tracker.regime_blocked_weeks,
                raw_stats={},
                gates_passed=False,
            )

        # Total return
        total_return_pct = ((equity_curve[-1] - initial_cash) / initial_cash) * 100

        # Annualized return (CAGR)
        total_days = (end_date - start_date).days
        years = total_days / 365.25
        if years > 0 and equity_curve[-1] > 0:
            annualized_return_pct = (
                (equity_curve[-1] / initial_cash) ** (1 / years) - 1
            ) * 100
        else:
            annualized_return_pct = 0.0

        # Sharpe ratio: annualized, risk-free = 0
        daily_returns: list[float] = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] > 0:
                daily_returns.append(
                    (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
                )
        if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0:
            sharpe_ratio = (
                statistics.mean(daily_returns) / statistics.stdev(daily_returns)
            ) * math.sqrt(252)
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
            profit_factor = float("inf")
        else:
            profit_factor = 0.0

        # Build raw_stats dict from backtesting.py output
        try:
            raw_stats = dict(bt_stats)
        except Exception:  # noqa: BLE001
            raw_stats = {}

        raw_stats["custom_equity_curve_len"] = len(equity_curve)
        raw_stats["custom_closed_trades"] = len(trades)
        raw_stats["custom_regime_changes"] = tracker.regime_changes
        raw_stats["custom_regime_blocked_weeks"] = tracker.regime_blocked_weeks

    except Exception as exc:
        raise BacktestError(
            message=f"Failed to extract statistics from tracker: {exc}",
            phase="stats_extraction",
        ) from exc

    log_agent_action(
        agent_name=AGENT_NAME,
        action=(
            f"backtest_complete: {len(trades)} trades, "
            f"sharpe={sharpe_ratio:.2f}, dd={max_drawdown_pct:.1f}%, "
            f"wr={win_rate_pct:.1f}%"
        ),
        result="ok",
    )

    return BacktestResult(
        start_date=start_date,
        end_date=end_date,
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct,
        sharpe_ratio=sharpe_ratio,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=win_rate_pct,
        total_trades=len(trades),
        profit_factor=profit_factor,
        regime_changes=tracker.regime_changes,
        regime_blocked_weeks=tracker.regime_blocked_weeks,
        raw_stats=raw_stats,
        gates_passed=False,
    )
