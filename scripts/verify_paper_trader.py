"""Self-verification script for PaperTrader — runs after tests and code review pass."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

results: list[str] = []
verdict = "PASS"

def log(msg: str) -> None:
    results.append(msg)
    print(msg)

def fail(msg: str) -> None:
    global verdict
    verdict = "FAIL"
    results.append(f"FAIL: {msg}")
    print(f"FAIL: {msg}")

# Use a temp DB so we don't pollute the real trading.db
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
DB_PATH = tmp.name

mock_settings = MagicMock()
mock_settings.live_trading = False
mock_settings.max_trade_amount = 10000
mock_settings.database_url = f"sqlite:///{DB_PATH}"

with patch("src.execution.paper_trader.settings", mock_settings), \
     patch("src.execution.paper_trader.log_agent_action"):

    from src.execution.paper_trader import PaperTrader

    log("=== PaperTrader Self-Verification ===\n")

    # Step 1 — create instance
    try:
        pt = PaperTrader(db_path=DB_PATH)
        log("STEP 1: PaperTrader instantiated OK")
    except Exception as e:
        fail(f"STEP 1: instantiation failed: {e}")
        sys.exit(1)

    # Step 2 — place 2 orders
    try:
        oid1 = pt.place_order("RELIANCE", "BUY", 3, 2450.0, 2390.0, 2570.0)
        log(f"STEP 2a: RELIANCE BUY placed — order_id={oid1}")
    except Exception as e:
        fail(f"STEP 2a: RELIANCE BUY failed: {e}")

    try:
        oid2 = pt.place_order("TCS", "BUY", 2, 3800.0, 3710.0, 3990.0)
        log(f"STEP 2b: TCS BUY placed — order_id={oid2}")
    except Exception as e:
        fail(f"STEP 2b: TCS BUY failed: {e}")

    # Step 3 — get_positions
    try:
        positions = pt.get_positions()
        symbols = [p["symbol"] for p in positions]
        if "RELIANCE" in symbols and "TCS" in symbols:
            log(f"STEP 3: get_positions() — {len(positions)} open: {symbols} OK")
        else:
            fail(f"STEP 3: expected RELIANCE+TCS, got {symbols}")
        for p in positions:
            log(f"  {p['symbol']}: qty={p['quantity']} entry={p['entry_price']} SL={p['stop_loss']} TP={p['take_profit']} pnl={p['pnl']:.2f}")
    except Exception as e:
        fail(f"STEP 3: get_positions failed: {e}")

    # Step 4 — trigger SL on RELIANCE (2385 < SL 2390)
    try:
        triggered = pt.check_gtts({"RELIANCE": 2385.0, "TCS": 3820.0})
        if len(triggered) == 1 and triggered[0]["symbol"] == "RELIANCE" and triggered[0]["exit_reason"] == "STOP_LOSS":
            log(f"STEP 4: SL triggered on RELIANCE at {triggered[0]['exit_price']} — trade_id={triggered[0]['trade_id']} OK")
        else:
            fail(f"STEP 4: expected RELIANCE STOP_LOSS, got {triggered}")
    except Exception as e:
        fail(f"STEP 4: check_gtts SL failed: {e}")

    # Step 5 — trigger TP on TCS (3995 > TP 3990)
    try:
        triggered = pt.check_gtts({"RELIANCE": 2385.0, "TCS": 3995.0})
        if len(triggered) == 1 and triggered[0]["symbol"] == "TCS" and triggered[0]["exit_reason"] == "TAKE_PROFIT":
            log(f"STEP 5: TP triggered on TCS at {triggered[0]['exit_price']} — trade_id={triggered[0]['trade_id']} OK")
        else:
            fail(f"STEP 5: expected TCS TAKE_PROFIT, got {triggered}")
    except Exception as e:
        fail(f"STEP 5: check_gtts TP failed: {e}")

    # Step 6 — get_pnl
    try:
        pnl = pt.get_pnl()
        log(f"\nSTEP 6: get_pnl() result:")
        log(f"  realized_pnl:   ₹{pnl['realized_pnl']:.2f}")
        log(f"  unrealized_pnl: ₹{pnl['unrealized_pnl']:.2f}")
        log(f"  total_pnl:      ₹{pnl['total_pnl']:.2f}")
        log(f"  trade_count:    {pnl['trade_count']}")
        log(f"  win_count:      {pnl['win_count']}")
        log(f"  loss_count:     {pnl['loss_count']}")

        # Expected:
        # RELIANCE: (2390-2450)*3 = -180 (STOP_LOSS)
        # TCS: (3990-3800)*2 = +380 (TAKE_PROFIT)
        expected_realized = (2390.0 - 2450.0) * 3 + (3990.0 - 3800.0) * 2  # -180 + 380 = 200
        if abs(pnl["realized_pnl"] - expected_realized) < 0.01:
            log(f"  P&L math check: PASS (expected ₹{expected_realized:.2f})")
        else:
            fail(f"  P&L math check: expected ₹{expected_realized:.2f}, got ₹{pnl['realized_pnl']:.2f}")
    except Exception as e:
        fail(f"STEP 6: get_pnl failed: {e}")

# Step 7 & 8 — query the temp DB directly
log("\nSTEP 7: orders table (last 5):")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT id, symbol, side, quantity, entry_price, status, placed_at FROM orders ORDER BY id DESC LIMIT 5"):
    log(f"  id={row['id']} {row['symbol']} {row['side']} qty={row['quantity']} price={row['entry_price']} status={row['status']}")

log("\nSTEP 8: trades table (last 5):")
for row in conn.execute("SELECT id, symbol, quantity, entry_price, exit_price, pnl, exit_reason, closed_at FROM trades ORDER BY id DESC LIMIT 5"):
    log(f"  id={row['id']} {row['symbol']} qty={row['quantity']} entry={row['entry_price']} exit={row['exit_price']} pnl={row['pnl']:.2f} reason={row['exit_reason']}")
conn.close()

os.unlink(DB_PATH)

log(f"\n=== VERDICT: {verdict} ===")

# Write results to a file for the notifier
with open("/tmp/verify_results.txt", "w") as f:
    f.write("\n".join(results))

print(f"\nVerdict: {verdict}")
sys.exit(0 if verdict == "PASS" else 1)
