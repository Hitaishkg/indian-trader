"""Temporary parameter sensitivity test. NOT production code. Delete after use."""
from __future__ import annotations
import sys
import json
import datetime
import pandas as pd


def run_sensitivity(rsi_threshold: float | None, roe_threshold: float) -> dict:
    import src.strategy.quality_filter as qf_mod
    import src.backtest.runner as runner_mod

    # --- patch ROE ---
    original_roe = qf_mod.ROE_THRESHOLD
    qf_mod.ROE_THRESHOLD = roe_threshold

    # --- patch RSI (wrap apply_quality_filter in runner namespace) ---
    original_apply_qf = runner_mod.apply_quality_filter

    if rsi_threshold is not None:
        from src.indicators.technical import add_indicators

        def rsi_filtered_apply(fundamentals_df, ohlcv_df):
            result_df, report = original_apply_qf(fundamentals_df, ohlcv_df)
            if result_df.empty:
                return result_df, report
            passing = []
            for sym in result_df["symbol"]:
                sym_ohlcv = (
                    ohlcv_df[ohlcv_df["symbol"] == sym].copy().sort_values("date")
                )
                if len(sym_ohlcv) < 15:
                    continue
                try:
                    ind_df = add_indicators(sym_ohlcv)
                    latest_rsi = ind_df["rsi"].iloc[-1]
                    if pd.isna(latest_rsi) or float(latest_rsi) < rsi_threshold:
                        passing.append(sym)
                except Exception:
                    pass
            return result_df[result_df["symbol"].isin(passing)], report

        runner_mod.apply_quality_filter = rsi_filtered_apply

    try:
        from src.backtest.validator import validate_backtest

        result = runner_mod.run_backtest(
            start_date=datetime.date(2014, 1, 1),
            end_date=datetime.date(2023, 12, 31),
        )
        v = validate_backtest(result)
        return {
            "rsi_threshold": rsi_threshold,
            "roe_threshold": roe_threshold,
            "sharpe": round(result.sharpe_ratio, 3),
            "max_drawdown_pct": round(result.max_drawdown_pct, 2),
            "win_rate_pct": round(result.win_rate_pct, 2),
            "total_trades": result.total_trades,
            "profit_factor": round(result.profit_factor, 3),
            "total_return_pct": round(result.total_return_pct, 2),
            "gates_passed": sum(1 for g in v.gate_results if g.passed),
        }
    finally:
        qf_mod.ROE_THRESHOLD = original_roe
        runner_mod.apply_quality_filter = original_apply_qf


if __name__ == "__main__":
    rsi_arg = sys.argv[1]
    roe_arg = float(sys.argv[2])
    rsi = None if rsi_arg == "None" else float(rsi_arg)
    result = run_sensitivity(rsi, roe_arg)
    print(json.dumps(result))
