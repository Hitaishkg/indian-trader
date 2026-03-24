"""Comprehensive test suite for src/execution/paper_trader.py.

Tests all public methods of PaperTrader class including validation, happy paths,
GTT triggering, and error conditions. Uses in-memory SQLite for test isolation.
"""

from __future__ import annotations

import sqlite3
from unittest import mock

import pytest

from src.execution.paper_trader import PaperTrader


class TestPaperTraderInit:
    """Test PaperTrader.__init__()."""

    def test_init_raises_when_live_trading_true(self, tmp_path):
        """Test that __init__ raises ValueError when LIVE_TRADING=true."""
        db_file = tmp_path / "test.db"
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = True
            with pytest.raises(ValueError, match="LIVE_TRADING=true"):
                PaperTrader(str(db_file))

    def test_init_creates_tables_with_live_trading_false(self, tmp_path):
        """Test that __init__ creates all three tables when LIVE_TRADING=false."""
        db_file = tmp_path / "test.db"
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            pt = PaperTrader(str(db_file))

            # Verify all three tables exist (sqlite_sequence is auto-created for autoincrement)
            cursor = pt._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row[0] for row in cursor.fetchall()} - {"sqlite_sequence"}
            assert tables == {"orders", "trades", "positions"}

    def test_init_applies_wal_pragmas(self, tmp_path):
        """Test that WAL pragmas are applied on connection."""
        db_file = tmp_path / "test.db"
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            pt = PaperTrader(str(db_file))

            journal_mode = pt._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert journal_mode.upper() == "WAL"

    def test_init_derives_db_path_from_settings_when_none(self, tmp_path):
        """Test that db_path is derived from settings.database_url when None."""
        db_file = tmp_path / "test.db"
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            mock_settings.database_url = f"sqlite:///{db_file}"
            pt = PaperTrader(db_path=None)
            assert pt._db_path == str(db_file)

    def test_init_uses_explicit_db_path_when_provided(self, tmp_path):
        """Test that explicit db_path is used over settings.database_url."""
        db_file = tmp_path / "test.db"
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            mock_settings.database_url = "sqlite:///some/other/path.db"
            pt = PaperTrader(str(db_file))
            assert pt._db_path == str(db_file)


class TestPlaceOrderBUY:
    """Test PaperTrader.place_order() for BUY orders."""

    @pytest.fixture
    def paper_trader(self, tmp_path):
        """Create a PaperTrader with tmp_path database."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            yield PaperTrader(str(tmp_path / "test.db"))

    def test_place_order_buy_happy_path(self, paper_trader):
        """Test BUY order happy path: order written PENDING, position created, marked FILLED."""
        order_id = paper_trader.place_order(
            symbol="RELIANCE",
            side="BUY",
            quantity=4,
            entry_price=2000.0,
            stop_loss=1900.0,
            take_profit=2200.0,
        )

        # Verify order in orders table
        order_row = paper_trader._conn.execute(
            "SELECT status, symbol, side, quantity FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        assert order_row["status"] == "FILLED"
        assert order_row["symbol"] == "RELIANCE"
        assert order_row["side"] == "BUY"
        assert order_row["quantity"] == 4

        # Verify position created
        pos_row = paper_trader._conn.execute(
            "SELECT symbol, quantity, entry_price, stop_loss, take_profit FROM positions WHERE symbol = ?",
            ("RELIANCE",),
        ).fetchone()
        assert pos_row is not None
        assert pos_row["quantity"] == 4
        assert pos_row["entry_price"] == 2000.0
        assert pos_row["stop_loss"] == 1900.0
        assert pos_row["take_profit"] == 2200.0

    def test_place_order_buy_returns_order_id(self, paper_trader):
        """Test that place_order returns the order ID."""
        order_id = paper_trader.place_order(
            symbol="INFY",
            side="BUY",
            quantity=5,
            entry_price=1600.0,
            stop_loss=1520.0,
            take_profit=1700.0,
        )
        assert isinstance(order_id, int)
        assert order_id > 0

    def test_place_order_buy_duplicate_symbol_raises(self, paper_trader):
        """Test that duplicate BUY for same symbol raises ValueError."""
        paper_trader.place_order(
            symbol="HDFC",
            side="BUY",
            quantity=3,
            entry_price=1800.0,
            stop_loss=1700.0,
            take_profit=1950.0,
        )
        with pytest.raises(ValueError, match="Position already open"):
            paper_trader.place_order(
                symbol="HDFC",
                side="BUY",
                quantity=2,
                entry_price=1850.0,
                stop_loss=1750.0,
                take_profit=2000.0,
            )

    def test_place_order_buy_stop_loss_ge_entry_raises(self, paper_trader):
        """Test that stop_loss >= entry_price for BUY raises ValueError."""
        with pytest.raises(ValueError, match="stop_loss.*must be below"):
            paper_trader.place_order(
                symbol="TCS",
                side="BUY",
                quantity=2,
                entry_price=3000.0,
                stop_loss=3100.0,  # Greater than entry
                take_profit=3200.0,
            )

    def test_place_order_buy_take_profit_le_entry_raises(self, paper_trader):
        """Test that take_profit <= entry_price for BUY raises ValueError."""
        with pytest.raises(ValueError, match="take_profit.*must be above"):
            paper_trader.place_order(
                symbol="WIPRO",
                side="BUY",
                quantity=6,
                entry_price=400.0,
                stop_loss=380.0,
                take_profit=400.0,  # Equal to entry
            )

    def test_place_order_max_trade_amount_exceeded_raises(self, paper_trader):
        """Test that order value exceeding MAX_TRADE_AMOUNT raises ValueError."""
        with pytest.raises(ValueError, match="exceeds MAX_TRADE_AMOUNT"):
            paper_trader.place_order(
                symbol="RELIANCE",
                side="BUY",
                quantity=100,
                entry_price=2500.0,  # 100 * 2500 = 250000 > 10000
                stop_loss=2400.0,
                take_profit=2700.0,
            )

    def test_place_order_invalid_side_raises(self, paper_trader):
        """Test that invalid side raises ValueError."""
        with pytest.raises(ValueError, match="side must be 'BUY' or 'SELL'"):
            paper_trader.place_order(
                symbol="MARUTI",
                side="INVALID",
                quantity=5,
                entry_price=9000.0,
                stop_loss=8500.0,
                take_profit=9500.0,
            )

    def test_place_order_empty_symbol_raises(self, paper_trader):
        """Test that empty symbol raises ValueError."""
        with pytest.raises(ValueError, match="symbol must be a non-empty string"):
            paper_trader.place_order(
                symbol="",
                side="BUY",
                quantity=5,
                entry_price=1500.0,
                stop_loss=1400.0,
                take_profit=1600.0,
            )

    def test_place_order_quantity_le_zero_raises(self, paper_trader):
        """Test that quantity <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="quantity must be a positive integer"):
            paper_trader.place_order(
                symbol="AXISBANK",
                side="BUY",
                quantity=0,
                entry_price=1000.0,
                stop_loss=950.0,
                take_profit=1100.0,
            )


class TestPlaceOrderSELL:
    """Test PaperTrader.place_order() for SELL orders."""

    @pytest.fixture
    def paper_trader(self, tmp_path):
        """Create a PaperTrader with tmp_path database."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            yield PaperTrader(str(tmp_path / "test.db"))

    def test_place_order_sell_with_open_position(self, paper_trader):
        """Test SELL order closes existing position correctly."""
        # Place BUY first (qty*price = 4 * 2000 = 8000 < 10000)
        paper_trader.place_order(
            symbol="RELIANCE",
            side="BUY",
            quantity=4,
            entry_price=2000.0,
            stop_loss=1900.0,
            take_profit=2200.0,
        )

        # Now SELL
        paper_trader.place_order(
            symbol="RELIANCE",
            side="SELL",
            quantity=4,
            entry_price=2100.0,  # Exit at profit
            stop_loss=2200.0,
            take_profit=1900.0,
        )

        # Verify position is closed
        pos = paper_trader._conn.execute(
            "SELECT * FROM positions WHERE symbol = ?", ("RELIANCE",)
        ).fetchone()
        assert pos is None

        # Verify trade is recorded
        trade_row = paper_trader._conn.execute(
            "SELECT pnl, exit_reason FROM trades WHERE symbol = ?",
            ("RELIANCE",),
        ).fetchone()
        assert trade_row["pnl"] == 400.0  # (2100 - 2000) * 4
        assert trade_row["exit_reason"] == "MANUAL_EXIT"

    def test_place_order_sell_no_position_raises(self, paper_trader):
        """Test SELL order with no open position raises ValueError."""
        with pytest.raises(ValueError, match="No open position"):
            paper_trader.place_order(
                symbol="NIFTYBEES",
                side="SELL",
                quantity=10,
                entry_price=500.0,
                stop_loss=510.0,
                take_profit=490.0,
            )


class TestClosePosition:
    """Test PaperTrader.close_position()."""

    @pytest.fixture
    def paper_trader_with_open_position(self, tmp_path):
        """Create a PaperTrader with one open BUY position."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            pt = PaperTrader(str(tmp_path / "test.db"))
            pt.place_order(
                symbol="INFY",
                side="BUY",
                quantity=5,
                entry_price=1200.0,
                stop_loss=1140.0,
                take_profit=1320.0,
            )
            yield pt

    def test_close_position_happy_path(self, paper_trader_with_open_position):
        """Test close_position happy path: trade written, position removed, P&L correct."""
        pt = paper_trader_with_open_position
        trade_id = pt.close_position(
            symbol="INFY",
            exit_price=1260.0,
            exit_reason="STOP_LOSS",
        )

        # Verify trade written
        trade_row = pt._conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        assert trade_row is not None
        assert trade_row["pnl"] == 300.0  # (1260 - 1200) * 5
        assert trade_row["pnl_pct"] == 5.0  # (60 / 1200) * 100
        assert trade_row["exit_reason"] == "STOP_LOSS"

        # Verify position removed
        pos = pt._conn.execute(
            "SELECT * FROM positions WHERE symbol = ?", ("INFY",)
        ).fetchone()
        assert pos is None

    def test_close_position_invalid_exit_reason_raises(
        self, paper_trader_with_open_position
    ):
        """Test that invalid exit_reason raises ValueError."""
        pt = paper_trader_with_open_position
        with pytest.raises(ValueError, match="exit_reason must be one of"):
            pt.close_position(
                symbol="INFY",
                exit_price=1650.0,
                exit_reason="INVALID_REASON",
            )

    def test_close_position_no_position_raises(self, paper_trader_with_open_position):
        """Test that closing non-existent position raises ValueError."""
        pt = paper_trader_with_open_position
        with pytest.raises(ValueError, match="No open position"):
            pt.close_position(
                symbol="RELIANCE",
                exit_price=2500.0,
                exit_reason="MANUAL_EXIT",
            )

    def test_close_position_all_exit_reasons(self, paper_trader_with_open_position):
        """Test that all valid exit_reasons are accepted."""
        pt = paper_trader_with_open_position
        reasons = ["STOP_LOSS", "TAKE_PROFIT", "MANUAL_EXIT", "REGIME_TIGHTENED"]

        for i, reason in enumerate(reasons):
            # Place a new position for each test
            pt.place_order(
                symbol=f"TEST{i}",
                side="BUY",
                quantity=1,
                entry_price=100.0,
                stop_loss=90.0,
                take_profit=110.0,
            )
            # Close it with the reason
            pt.close_position(
                symbol=f"TEST{i}",
                exit_price=105.0,
                exit_reason=reason,
            )
            # Verify the reason was stored
            trade_row = pt._conn.execute(
                "SELECT exit_reason FROM trades WHERE symbol = ?", (f"TEST{i}",)
            ).fetchone()
            assert trade_row["exit_reason"] == reason


class TestGetPositions:
    """Test PaperTrader.get_positions()."""

    @pytest.fixture
    def paper_trader(self, tmp_path):
        """Create a PaperTrader with tmp_path database."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            yield PaperTrader(str(tmp_path / "test.db"))

    def test_get_positions_empty_when_no_positions(self, paper_trader):
        """Test that get_positions returns empty list when no positions."""
        positions = paper_trader.get_positions()
        assert positions == []

    def test_get_positions_returns_correct_fields(self, paper_trader):
        """Test that get_positions returns all correct fields."""
        paper_trader.place_order(
            symbol="HDFC",
            side="BUY",
            quantity=3,
            entry_price=1800.0,
            stop_loss=1700.0,
            take_profit=1950.0,
        )

        positions = paper_trader.get_positions()
        assert len(positions) == 1

        pos = positions[0]
        assert "symbol" in pos
        assert "quantity" in pos
        assert "entry_price" in pos
        assert "current_price" in pos
        assert "stop_loss" in pos
        assert "take_profit" in pos
        assert "pnl" in pos
        assert "pnl_pct" in pos
        assert "opened_at" in pos
        assert "updated_at" in pos

        assert pos["symbol"] == "HDFC"
        assert pos["quantity"] == 3
        assert pos["entry_price"] == 1800.0


class TestGetPNL:
    """Test PaperTrader.get_pnl()."""

    @pytest.fixture
    def paper_trader(self, tmp_path):
        """Create a PaperTrader with tmp_path database."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            yield PaperTrader(str(tmp_path / "test.db"))

    def test_get_pnl_zeros_when_no_trades(self, paper_trader):
        """Test that get_pnl returns zero-valued dict when trades and positions tables are empty.

        SQL SUM() returns NULL on an empty table. COALESCE is used to convert
        those NULLs to 0 so callers always receive a valid numeric result.
        """
        pnl = paper_trader.get_pnl()
        assert pnl == {
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
        }

    def test_get_pnl_correct_after_close(self, paper_trader):
        """Test that get_pnl returns correct realized P&L after closing a trade."""
        # Place and close a winning trade (qty*price = 4 * 2000 = 8000 < 10000)
        paper_trader.place_order(
            symbol="RELIANCE",
            side="BUY",
            quantity=4,
            entry_price=2000.0,
            stop_loss=1900.0,
            take_profit=2200.0,
        )
        paper_trader.close_position(
            symbol="RELIANCE",
            exit_price=2100.0,
            exit_reason="TAKE_PROFIT",
        )

        pnl = paper_trader.get_pnl()
        assert pnl["realized_pnl"] == 400.0  # (2100 - 2000) * 4
        assert pnl["trade_count"] == 1
        assert pnl["win_count"] == 1
        assert pnl["loss_count"] == 0
        assert pnl["total_pnl"] == 400.0

    def test_get_pnl_win_loss_counts(self, paper_trader):
        """Test that win/loss counts are correct."""
        # Winning trade (4 * 1600 = 6400 < 10000)
        paper_trader.place_order(
            symbol="INFY",
            side="BUY",
            quantity=4,
            entry_price=1600.0,
            stop_loss=1500.0,
            take_profit=1700.0,
        )
        paper_trader.close_position(
            symbol="INFY",
            exit_price=1700.0,
            exit_reason="TAKE_PROFIT",
        )

        # Losing trade (3 * 3000 = 9000 < 10000)
        paper_trader.place_order(
            symbol="TCS",
            side="BUY",
            quantity=3,
            entry_price=3000.0,
            stop_loss=2800.0,
            take_profit=3200.0,
        )
        paper_trader.close_position(
            symbol="TCS",
            exit_price=2900.0,
            exit_reason="STOP_LOSS",
        )

        pnl = paper_trader.get_pnl()
        assert pnl["trade_count"] == 2
        assert pnl["win_count"] == 1
        assert pnl["loss_count"] == 1


class TestCheckGTTs:
    """Test PaperTrader.check_gtts()."""

    @pytest.fixture
    def paper_trader(self, tmp_path):
        """Create a PaperTrader with tmp_path database."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            yield PaperTrader(str(tmp_path / "test.db"))

    def test_check_gtts_stop_loss_trigger(self, paper_trader):
        """Test that stop-loss trigger closes position and returns triggered list."""
        paper_trader.place_order(
            symbol="RELIANCE",
            side="BUY",
            quantity=4,
            entry_price=2500.0,
            stop_loss=2400.0,
            take_profit=2700.0,
        )

        triggered = paper_trader.check_gtts({"RELIANCE": 2350.0})

        assert len(triggered) == 1
        assert triggered[0]["symbol"] == "RELIANCE"
        assert triggered[0]["exit_price"] == 2400.0
        assert triggered[0]["exit_reason"] == "STOP_LOSS"
        assert "trade_id" in triggered[0]

        # Verify position is closed
        positions = paper_trader.get_positions()
        assert len(positions) == 0

    def test_check_gtts_take_profit_trigger(self, paper_trader):
        """Test that take-profit trigger closes position and returns triggered list."""
        paper_trader.place_order(
            symbol="INFY",
            side="BUY",
            quantity=5,
            entry_price=1600.0,
            stop_loss=1500.0,
            take_profit=1750.0,
        )

        triggered = paper_trader.check_gtts({"INFY": 1800.0})

        assert len(triggered) == 1
        assert triggered[0]["symbol"] == "INFY"
        assert triggered[0]["exit_price"] == 1750.0
        assert triggered[0]["exit_reason"] == "TAKE_PROFIT"

        positions = paper_trader.get_positions()
        assert len(positions) == 0

    def test_check_gtts_no_trigger_updates_pnl(self, paper_trader):
        """Test that no-trigger updates current_price and unrealized P&L."""
        paper_trader.place_order(
            symbol="TCS",
            side="BUY",
            quantity=3,
            entry_price=3000.0,
            stop_loss=2800.0,
            take_profit=3200.0,
        )

        triggered = paper_trader.check_gtts({"TCS": 3100.0})

        assert len(triggered) == 0

        # Verify position updated with new P&L
        positions = paper_trader.get_positions()
        assert len(positions) == 1
        assert positions[0]["current_price"] == 3100.0
        assert positions[0]["pnl"] == 300.0  # (3100 - 3000) * 3

    def test_check_gtts_missing_symbol_skipped(self, paper_trader):
        """Test that missing symbol in current_prices is skipped (no raise)."""
        paper_trader.place_order(
            symbol="HDFC",
            side="BUY",
            quantity=3,
            entry_price=1800.0,
            stop_loss=1700.0,
            take_profit=1950.0,
        )

        # Pass empty prices dict
        triggered = paper_trader.check_gtts({})

        # Should not raise, just skip
        assert len(triggered) == 0

        # Position should still be open
        positions = paper_trader.get_positions()
        assert len(positions) == 1

    def test_check_gtts_stop_loss_priority_over_take_profit(self, paper_trader):
        """Test that SL takes priority when both trigger simultaneously."""
        paper_trader.place_order(
            symbol="WIPRO",
            side="BUY",
            quantity=10,
            entry_price=400.0,
            stop_loss=380.0,
            take_profit=420.0,
        )

        # Price below SL should trigger SL, not TP
        triggered = paper_trader.check_gtts({"WIPRO": 370.0})

        assert len(triggered) == 1
        assert triggered[0]["exit_reason"] == "STOP_LOSS"

    def test_check_gtts_multiple_positions(self, paper_trader):
        """Test check_gtts with multiple open positions."""
        paper_trader.place_order(
            symbol="RELIANCE",
            side="BUY",
            quantity=4,
            entry_price=2500.0,
            stop_loss=2400.0,
            take_profit=2700.0,
        )
        paper_trader.place_order(
            symbol="INFY",
            side="BUY",
            quantity=5,
            entry_price=1600.0,
            stop_loss=1500.0,
            take_profit=1750.0,
        )

        # Trigger SL on RELIANCE, no trigger on INFY
        triggered = paper_trader.check_gtts({
            "RELIANCE": 2350.0,
            "INFY": 1650.0,
        })

        assert len(triggered) == 1
        assert triggered[0]["symbol"] == "RELIANCE"

        # Verify INFY position still open with updated P&L
        positions = paper_trader.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "INFY"


class TestUpdateStopLoss:
    """Test PaperTrader.update_stop_loss()."""

    @pytest.fixture
    def paper_trader_with_position(self, tmp_path):
        """Create a PaperTrader with one open position."""
        with mock.patch("src.execution.paper_trader.settings") as mock_settings:
            mock_settings.live_trading = False
            mock_settings.max_trade_amount = 10000
            pt = PaperTrader(str(tmp_path / "test.db"))
            pt.place_order(
                symbol="RELIANCE",
                side="BUY",
                quantity=4,
                entry_price=2500.0,
                stop_loss=2400.0,
                take_profit=2700.0,
            )
            yield pt

    def test_update_stop_loss_happy_path(self, paper_trader_with_position):
        """Test update_stop_loss happy path: positions table updated."""
        pt = paper_trader_with_position
        pt.update_stop_loss("RELIANCE", 2450.0)

        # Verify update
        pos_row = pt._conn.execute(
            "SELECT stop_loss FROM positions WHERE symbol = ?",
            ("RELIANCE",),
        ).fetchone()
        assert pos_row["stop_loss"] == 2450.0

    def test_update_stop_loss_no_position_raises(self, paper_trader_with_position):
        """Test that updating SL for non-existent position raises ValueError."""
        pt = paper_trader_with_position
        with pytest.raises(ValueError, match="No open position"):
            pt.update_stop_loss("INFY", 1500.0)

    def test_update_stop_loss_ge_entry_price_raises(self, paper_trader_with_position):
        """Test that new_stop_loss >= entry_price raises ValueError."""
        pt = paper_trader_with_position
        with pytest.raises(ValueError, match="must be below entry_price"):
            pt.update_stop_loss("RELIANCE", 2500.0)

    def test_update_stop_loss_le_zero_raises(self, paper_trader_with_position):
        """Test that new_stop_loss <= 0 raises ValueError."""
        pt = paper_trader_with_position
        with pytest.raises(ValueError, match="must be positive"):
            pt.update_stop_loss("RELIANCE", 0.0)
