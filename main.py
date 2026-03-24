# Step 1: stdlib imports only
import datetime
import os
import sys

import pandas as pd

# Step 2: settings loads at import time — this is unavoidable and correct.
# It does NOT produce log output, so it is safe before setup_logging().
try:
    from src.config.settings import settings, ConfigurationError
except Exception as exc:
    print(f"[main] Fatal: configuration failed: {exc}", file=sys.stderr)
    sys.exit(1)

# Step 3: Import setup_logging and call it IMMEDIATELY, before any other
# src module import.
from src.utils.logger import setup_logging, log_agent_action
setup_logging()

# Step 4: Now safe to import modules that may produce log output at import time.
from src.data.fetcher import fetch_ohlcv, FetchError  # noqa: E402
from src.data.cleaner import clean_ohlcv  # noqa: E402
from src.data.fundamentals import fetch_fundamentals  # noqa: E402
from src.data.validator import validate_data, DataQualityError  # noqa: E402
from src.execution.paper_trader import PaperTrader  # noqa: E402
from src.utils.notifier import send_info, send_alert  # noqa: E402

SYMBOLS: list[str] = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]


def compute_atr(symbol_df: pd.DataFrame, period: int = 14) -> float | None:
    """Compute Average True Range for a single symbol's OHLCV data.

    Uses the standard Wilder smoothing method: TR = max(H-L, |H-Cp|, |L-Cp|),
    then exponential moving average over `period` bars.

    Args:
        symbol_df: OHLCV DataFrame for ONE symbol, sorted by date ascending.
                   Must have columns: high, low, close.
        period: ATR lookback period. Default 14.

    Returns:
        ATR value as float, or None if insufficient data (< period+1 rows).
    """
    if len(symbol_df) < period + 1:
        return None
    high = symbol_df["high"]
    low = symbol_df["low"]
    close_prev = symbol_df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - close_prev).abs(),
        (low - close_prev).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
    return float(atr)


def main() -> None:
    """Run the Phase 1 end-to-end dry-run pipeline."""
    try:
        log_agent_action("main", "Pipeline started", level="INFO", result="ok")

        # Section 16: ensure data/ directory exists before DB operations
        os.makedirs("data", exist_ok=True)

        # Section 5: OHLCV fetch — 30-day window ending today
        end_date = datetime.date.today()
        start_date = end_date - datetime.timedelta(days=30)
        ohlcv_df = fetch_ohlcv(SYMBOLS, start_date, end_date)
        log_agent_action(
            "main",
            f"Fetched OHLCV for {len(SYMBOLS)} symbols, {len(ohlcv_df)} rows",
            level="INFO",
            result="ok",
        )

        # Section 6: fundamentals fetch
        fundamentals_df = fetch_fundamentals(SYMBOLS)
        log_agent_action(
            "main",
            f"Fetched fundamentals for {len(SYMBOLS)} symbols",
            level="INFO",
            result="ok",
        )

        # Section 7: data cleaning
        cleaned_df, cleaning_report = clean_ohlcv(ohlcv_df)
        log_agent_action(
            "main",
            (
                f"Cleaning: {cleaning_report.rows_input} rows in, "
                f"{cleaning_report.rows_output} out, "
                f"{cleaning_report.duplicates_removed} duplicates removed"
            ),
            level="INFO",
            result="ok",
        )

        # Section 8: data validation
        db_path = settings.database_url.replace("sqlite:///", "")
        report = validate_data(cleaned_df, fundamentals_df, db_path)
        log_agent_action(
            "main",
            f"Data validation passed: score={report.universe_quality_score:.4f}",
            level="INFO",
            result="ok",
            data_quality_score=report.universe_quality_score,
        )

        # Section 10: paper trade — select first symbol with data
        selected_symbol: str | None = None
        for sym in SYMBOLS:
            if sym in cleaned_df["symbol"].values:
                selected_symbol = sym
                break

        if selected_symbol is None:
            raise ValueError("No symbols with data found in cleaned OHLCV DataFrame")

        symbol_df = (
            cleaned_df[cleaned_df["symbol"] == selected_symbol]
            .sort_values("date")
        )

        entry_price = float(symbol_df["close"].iloc[-1])

        atr = compute_atr(symbol_df)

        if atr is not None and atr > 0:
            log_agent_action(
                "main",
                f"ATR for {selected_symbol}: {atr:.2f}",
                level="INFO",
                result="ok",
            )
            atr_display = f"{atr:.2f}"
            raw_stop_distance = atr * 2.0
            max_stop_distance = entry_price * 0.03
            stop_distance = min(raw_stop_distance, max_stop_distance)
            stop_loss = round(entry_price - stop_distance, 2)
            take_profit = round(entry_price + (stop_distance * 2.0), 2)
        else:
            log_agent_action(
                "main",
                f"ATR unavailable for {selected_symbol}, using fallback",
                level="INFO",
                result="ok",
            )
            atr_display = "N/A (fallback used)"
            stop_loss = round(entry_price * 0.98, 2)
            take_profit = round(entry_price * 1.04, 2)

        quantity = 1

        trader = PaperTrader()

        # Section 15: idempotency check before place_order()
        existing_positions = trader.get_positions()
        already_open = any(p["symbol"] == selected_symbol for p in existing_positions)

        if already_open:
            log_agent_action(
                "main",
                f"Position already open for {selected_symbol}, skipping trade",
                level="INFO",
                result="ok",
            )
            order_id = "N/A (position already open)"
        else:
            order_id_int = trader.place_order(
                symbol=selected_symbol,
                side="BUY",
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            order_id = str(order_id_int)
            log_agent_action(
                "main",
                (
                    f"Paper trade: BUY 1 {selected_symbol} @ {entry_price} "
                    f"SL={stop_loss} TP={take_profit} order_id={order_id}"
                ),
                level="INFO",
                result="ok",
            )

        pnl = trader.get_pnl()
        positions = trader.get_positions()

        log_agent_action(
            "main",
            (
                f"P&L: realized={pnl['realized_pnl']} "
                f"unrealized={pnl['unrealized_pnl']} "
                f"total={pnl['total_pnl']}"
            ),
            level="INFO",
            result="ok",
        )

        log_agent_action(
            "main",
            "Pipeline completed successfully",
            level="INFO",
            result="ok",
            data_quality_score=report.universe_quality_score,
        )

        # Section 13: Telegram notification on success
        message = (
            "Phase 1 dry-run complete\n\n"
            f"Symbols: {', '.join(SYMBOLS)}\n"
            f"Data quality score: {report.universe_quality_score:.4f}\n"
            f"Cleaning: {cleaning_report.duplicates_removed} duplicates removed\n\n"
            f"Paper trade: BUY 1 {selected_symbol} @ {entry_price:.2f}\n"
            f"  Stop-loss: {stop_loss:.2f}\n"
            f"  Take-profit: {take_profit:.2f}\n"
            f"  ATR: {atr_display}\n\n"
            f"P&L: realized={pnl['realized_pnl']:.2f} "
            f"unrealized={pnl['unrealized_pnl']:.2f} "
            f"total={pnl['total_pnl']:.2f}\n"
            f"Positions open: {len(positions)}"
        )
        notify_result = send_info(message)
        telegram_status = "sent" if notify_result.get("telegram") else "skipped"

        # Section 14: stdout summary
        print("========================================")
        print("  Phase 1 Dry-Run — Pipeline Complete")
        print("========================================")
        print()
        print("Data:")
        print(f"  Symbols fetched:    {len(SYMBOLS)}")
        print(f"  OHLCV rows:         {len(cleaned_df)}")
        print(f"  Quality score:      {report.universe_quality_score:.4f}")
        print(f"  Duplicates removed: {cleaning_report.duplicates_removed}")
        print()
        print("Paper Trade:")
        print(f"  Symbol:       {selected_symbol}")
        print("  Side:         BUY")
        print("  Quantity:     1")
        print(f"  Entry price:  {entry_price:.2f}")
        print(f"  Stop-loss:    {stop_loss:.2f}")
        print(f"  Take-profit:  {take_profit:.2f}")
        print(f"  ATR (14d):    {atr_display}")
        print(f"  Order ID:     {order_id}")
        print()
        print("P&L:")
        print(f"  Realized:     {pnl['realized_pnl']:.2f}")
        print(f"  Unrealized:   {pnl['unrealized_pnl']:.2f}")
        print(f"  Total:        {pnl['total_pnl']:.2f}")
        print(f"  Trades:       {pnl['trade_count']}")
        print(f"  Open pos:     {len(positions)}")
        print()
        print(f"Telegram notification: {telegram_status}")
        print("========================================")
        print("Phase 1 COMPLETE")

    except ConfigurationError as exc:
        log_agent_action("main", f"Configuration error: {exc}", level="ERROR", result="error")
        send_alert("Pipeline failed", f"Configuration error: {exc}")
        sys.exit(1)
    except FetchError as exc:
        log_agent_action("main", f"Data fetch failed: {exc}", level="ERROR", result="error")
        send_alert("Pipeline failed", f"Data fetch failed: {exc}")
        sys.exit(1)
    except DataQualityError as exc:
        log_agent_action(
            "main",
            f"Data quality below threshold: score={exc.universe_quality_score:.4f}",
            level="ERROR",
            result="error",
            data_quality_score=exc.universe_quality_score,
        )
        send_alert(
            "Pipeline failed",
            f"Data quality score {exc.universe_quality_score:.4f} below 0.60 threshold",
        )
        sys.exit(1)
    except ValueError as exc:
        log_agent_action("main", f"Validation error: {exc}", level="ERROR", result="error")
        send_alert("Pipeline failed", f"Validation error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
