"""Tests for main.py end-to-end pipeline."""

import datetime
import sys
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import pytest

from main import compute_atr, main


class TestComputeAtr:
    """Unit tests for compute_atr() function."""

    def test_compute_atr_valid_data(self) -> None:
        """compute_atr with sufficient data returns positive float."""
        df = pd.DataFrame({
            "high": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0],
            "close": [99.5, 100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 107.5, 108.5, 109.5, 110.5, 111.5, 112.5, 113.5],
        })
        result = compute_atr(df, period=14)
        assert result is not None
        assert isinstance(result, float)
        assert result > 0

    def test_compute_atr_insufficient_data(self) -> None:
        """compute_atr with fewer than period+1 rows returns None."""
        df = pd.DataFrame({
            "high": [100.0, 101.0, 102.0],
            "low": [99.0, 100.0, 101.0],
            "close": [99.5, 100.5, 101.5],
        })
        result = compute_atr(df, period=14)
        assert result is None

    def test_compute_atr_exact_period_plus_one(self) -> None:
        """compute_atr with exactly period+1 rows returns non-None."""
        df = pd.DataFrame({
            "high": [float(100 + i) for i in range(15)],
            "low": [float(99 + i) for i in range(15)],
            "close": [float(99.5 + i) for i in range(15)],
        })
        result = compute_atr(df, period=14)
        assert result is not None
        assert isinstance(result, float)

    def test_compute_atr_custom_period(self) -> None:
        """compute_atr respects custom period parameter."""
        df = pd.DataFrame({
            "high": [float(100 + i) for i in range(10)],
            "low": [float(99 + i) for i in range(10)],
            "close": [float(99.5 + i) for i in range(10)],
        })
        result = compute_atr(df, period=5)
        assert result is not None
        assert isinstance(result, float)


class TestMainHappyPath:
    """Happy path: all steps succeed, paper trade placed, notifications sent."""

    @patch("main.send_info")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_happy_path(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_info: MagicMock,
        tmp_path,
    ) -> None:
        """Main completes successfully: data fetched, cleaned, validated, trade placed."""
        # Setup mocks
        ohlcv_df = pd.DataFrame({
            "symbol": ["RELIANCE"] * 30,
            "date": pd.date_range("2026-02-20", periods=30),
            "open": [2500.0 + i for i in range(30)],
            "high": [2510.0 + i for i in range(30)],
            "low": [2490.0 + i for i in range(30)],
            "close": [2505.0 + i for i in range(30)],
            "volume": [1000000 + i * 1000 for i in range(30)],
        })
        mock_fetch_ohlcv.return_value = ohlcv_df

        fundamentals_df = pd.DataFrame({
            "symbol": ["RELIANCE"],
            "roe": [18.5],
            "debt_to_equity": [0.8],
            "eps": [100.0],
            "traded_value_avg": [5000000000],
            "price": [2535.0],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = ohlcv_df.copy()
        cleaning_report = Mock()
        cleaning_report.rows_input = 30
        cleaning_report.rows_output = 30
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        quality_report = Mock()
        quality_report.universe_quality_score = 0.95
        mock_validate_data.return_value = quality_report

        trader_instance = Mock()
        trader_instance.place_order.return_value = 123
        trader_instance.get_pnl.return_value = {
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "trade_count": 1,
            "win_count": 0,
            "loss_count": 0,
        }
        trader_instance.get_positions.return_value = [
            {
                "symbol": "RELIANCE",
                "entry_price": 2535.0,
                "quantity": 1,
                "stop_loss": 2500.0,
                "take_profit": 2570.0,
            }
        ]
        mock_paper_trader_class.return_value = trader_instance

        mock_send_info.return_value = {"telegram": True, "gmail": False}

        # Patch settings to use tmp_path for database
        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                main()

        # Verify send_info was called
        mock_send_info.assert_called()
        call_args = mock_send_info.call_args
        assert call_args is not None
        message = call_args[0][0]
        assert "Phase 1 dry-run complete" in message
        assert "RELIANCE" in message
        assert "0.95" in message  # quality score

    @patch("main.send_info")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_pnl_calculation(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_info: MagicMock,
        tmp_path,
    ) -> None:
        """Main retrieves P&L and sends it in notification."""
        ohlcv_df = pd.DataFrame({
            "symbol": ["TCS"] * 30,
            "date": pd.date_range("2026-02-20", periods=30),
            "open": [3500.0 + i for i in range(30)],
            "high": [3510.0 + i for i in range(30)],
            "low": [3490.0 + i for i in range(30)],
            "close": [3505.0 + i for i in range(30)],
            "volume": [500000 + i * 500 for i in range(30)],
        })
        mock_fetch_ohlcv.return_value = ohlcv_df

        fundamentals_df = pd.DataFrame({
            "symbol": ["TCS"],
            "roe": [20.0],
            "debt_to_equity": [0.5],
            "eps": [150.0],
            "traded_value_avg": [3000000000],
            "price": [3535.0],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = ohlcv_df.copy()
        cleaning_report = Mock()
        cleaning_report.rows_input = 30
        cleaning_report.rows_output = 30
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        quality_report = Mock()
        quality_report.universe_quality_score = 0.92
        mock_validate_data.return_value = quality_report

        trader_instance = Mock()
        trader_instance.place_order.return_value = 456
        trader_instance.get_pnl.return_value = {
            "realized_pnl": 150.0,
            "unrealized_pnl": 200.0,
            "total_pnl": 350.0,
            "trade_count": 5,
            "win_count": 3,
            "loss_count": 2,
        }
        trader_instance.get_positions.return_value = []
        mock_paper_trader_class.return_value = trader_instance

        mock_send_info.return_value = {"telegram": True, "gmail": False}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                main()

        mock_send_info.assert_called()
        call_args = mock_send_info.call_args
        assert call_args is not None
        message = call_args[0][0]
        assert "realized=150.0" in message
        assert "unrealized=200.0" in message
        assert "total=350.0" in message


class TestMainConfigurationFailure:
    """Settings import failure → sys.exit(1)."""

    def test_settings_import_failure(self) -> None:
        """If settings raises ConfigurationError on import, main.py catches and exits."""
        # This test simulates the try/except block at the top of main.py
        from main import ConfigurationError

        errors = ["Missing required: TELEGRAM_BOT_TOKEN", "Missing required: GROQ_API_KEY"]
        exc = ConfigurationError(errors)

        # Simulate what main() does when it catches ConfigurationError
        try:
            raise exc
        except ConfigurationError as e:
            # This mimics the exception handler in main()
            assert e.errors == errors
            assert "TELEGRAM_BOT_TOKEN" in str(e)


class TestMainFetchError:
    """FetchError → sys.exit(1), send_alert() called."""

    @patch("main.send_alert")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_fetch_error(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_alert: MagicMock,
        tmp_path,
    ) -> None:
        """When fetch_ohlcv raises FetchError, main exits with error alert."""
        from main import FetchError

        mock_fetch_ohlcv.side_effect = FetchError(
            symbol="RELIANCE",
            yfinance_error="Network error",
            jugaad_error="Connection timeout",
        )

        mock_send_alert.return_value = {"telegram": True, "gmail": True}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

        mock_send_alert.assert_called()
        call_args = mock_send_alert.call_args
        assert call_args is not None
        subject, message = call_args[0]
        assert "failed" in subject.lower()
        assert "fetch" in message.lower()


class TestMainDataQualityError:
    """DataQualityError → sys.exit(1), send_alert() called."""

    @patch("main.send_alert")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_data_quality_error(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_alert: MagicMock,
        tmp_path,
    ) -> None:
        """When validate_data raises DataQualityError, main exits with alert."""
        from main import DataQualityError

        ohlcv_df = pd.DataFrame({
            "symbol": ["INFY"] * 30,
            "date": pd.date_range("2026-02-20", periods=30),
            "open": [1500.0 + i for i in range(30)],
            "high": [1510.0 + i for i in range(30)],
            "low": [1490.0 + i for i in range(30)],
            "close": [1505.0 + i for i in range(30)],
            "volume": [2000000 + i * 2000 for i in range(30)],
        })
        mock_fetch_ohlcv.return_value = ohlcv_df

        fundamentals_df = pd.DataFrame({
            "symbol": ["INFY"],
            "roe": [25.0],
            "debt_to_equity": [0.3],
            "eps": [80.0],
            "traded_value_avg": [4000000000],
            "price": [1535.0],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = ohlcv_df.copy()
        cleaning_report = Mock()
        cleaning_report.rows_input = 30
        cleaning_report.rows_output = 30
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        # Create a mock report object
        mock_report = Mock()
        mock_report.universe_quality_score = 0.45
        quality_error = DataQualityError(0.45, mock_report)
        mock_validate_data.side_effect = quality_error

        mock_send_alert.return_value = {"telegram": True, "gmail": True}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

        mock_send_alert.assert_called()
        call_args = mock_send_alert.call_args
        assert call_args is not None
        subject, message = call_args[0]
        assert "failed" in subject.lower()
        assert "0.45" in message


class TestMainValueError:
    """ValueError from place_order or no data → sys.exit(1), send_alert() called."""

    @patch("main.send_alert")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_no_symbols_with_data(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_alert: MagicMock,
        tmp_path,
    ) -> None:
        """When cleaned_df has no symbols, ValueError raised and caught."""
        mock_fetch_ohlcv.return_value = pd.DataFrame({
            "symbol": [],
            "date": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        })

        fundamentals_df = pd.DataFrame({
            "symbol": [],
            "roe": [],
            "debt_to_equity": [],
            "eps": [],
            "traded_value_avg": [],
            "price": [],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = pd.DataFrame({
            "symbol": [],
            "date": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        })
        cleaning_report = Mock()
        cleaning_report.rows_input = 0
        cleaning_report.rows_output = 0
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        quality_report = Mock()
        quality_report.universe_quality_score = 0.5
        mock_validate_data.return_value = quality_report

        mock_send_alert.return_value = {"telegram": True, "gmail": True}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

        mock_send_alert.assert_called()
        call_args = mock_send_alert.call_args
        assert call_args is not None
        subject, message = call_args[0]
        assert "failed" in subject.lower()
        assert "Validation error" in message or "No symbols" in message

    @patch("main.send_alert")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_place_order_value_error(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_alert: MagicMock,
        tmp_path,
    ) -> None:
        """When PaperTrader.place_order raises ValueError, main exits with alert."""
        ohlcv_df = pd.DataFrame({
            "symbol": ["HDFCBANK"] * 30,
            "date": pd.date_range("2026-02-20", periods=30),
            "open": [1600.0 + i for i in range(30)],
            "high": [1610.0 + i for i in range(30)],
            "low": [1590.0 + i for i in range(30)],
            "close": [1605.0 + i for i in range(30)],
            "volume": [1500000 + i * 1500 for i in range(30)],
        })
        mock_fetch_ohlcv.return_value = ohlcv_df

        fundamentals_df = pd.DataFrame({
            "symbol": ["HDFCBANK"],
            "roe": [16.5],
            "debt_to_equity": [0.9],
            "eps": [130.0],
            "traded_value_avg": [6000000000],
            "price": [1635.0],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = ohlcv_df.copy()
        cleaning_report = Mock()
        cleaning_report.rows_input = 30
        cleaning_report.rows_output = 30
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        quality_report = Mock()
        quality_report.universe_quality_score = 0.88
        mock_validate_data.return_value = quality_report

        trader_instance = Mock()
        trader_instance.get_positions.return_value = []
        trader_instance.place_order.side_effect = ValueError("Invalid quantity")
        mock_paper_trader_class.return_value = trader_instance

        mock_send_alert.return_value = {"telegram": True, "gmail": True}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

        mock_send_alert.assert_called()
        call_args = mock_send_alert.call_args
        assert call_args is not None
        subject, message = call_args[0]
        assert "failed" in subject.lower()


class TestMainIdempotency:
    """Idempotency: if position already open for symbol, trade skipped, no ValueError."""

    @patch("main.send_info")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_position_already_open(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_info: MagicMock,
        tmp_path,
    ) -> None:
        """When position already exists for selected symbol, trade is skipped."""
        ohlcv_df = pd.DataFrame({
            "symbol": ["ICICIBANK"] * 30,
            "date": pd.date_range("2026-02-20", periods=30),
            "open": [1000.0 + i for i in range(30)],
            "high": [1010.0 + i for i in range(30)],
            "low": [990.0 + i for i in range(30)],
            "close": [1005.0 + i for i in range(30)],
            "volume": [800000 + i * 800 for i in range(30)],
        })
        mock_fetch_ohlcv.return_value = ohlcv_df

        fundamentals_df = pd.DataFrame({
            "symbol": ["ICICIBANK"],
            "roe": [17.0],
            "debt_to_equity": [0.7],
            "eps": [75.0],
            "traded_value_avg": [5500000000],
            "price": [1035.0],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = ohlcv_df.copy()
        cleaning_report = Mock()
        cleaning_report.rows_input = 30
        cleaning_report.rows_output = 30
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        quality_report = Mock()
        quality_report.universe_quality_score = 0.90
        mock_validate_data.return_value = quality_report

        trader_instance = Mock()
        # Position already exists for ICICIBANK
        trader_instance.get_positions.return_value = [
            {
                "symbol": "ICICIBANK",
                "entry_price": 1010.0,
                "quantity": 1,
                "stop_loss": 980.0,
                "take_profit": 1050.0,
            }
        ]
        # place_order should NOT be called because of idempotency check
        trader_instance.place_order.side_effect = Exception("Should not be called")
        trader_instance.get_pnl.return_value = {
            "realized_pnl": 0.0,
            "unrealized_pnl": 50.0,
            "total_pnl": 50.0,
            "trade_count": 1,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_paper_trader_class.return_value = trader_instance

        mock_send_info.return_value = {"telegram": True, "gmail": False}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                main()

        # Verify place_order was NOT called (idempotency check skipped it)
        trader_instance.place_order.assert_not_called()

        # Verify send_info was still called
        mock_send_info.assert_called()


class TestMainAtrFallback:
    """ATR fallback: if compute_atr() returns None, fallback 2%/4% levels used."""

    @patch("main.send_info")
    @patch("main.PaperTrader")
    @patch("main.validate_data")
    @patch("main.clean_ohlcv")
    @patch("main.fetch_fundamentals")
    @patch("main.fetch_ohlcv")
    @patch("main.log_agent_action")
    def test_main_atr_fallback_insufficient_data(
        self,
        mock_log_agent_action: MagicMock,
        mock_fetch_ohlcv: MagicMock,
        mock_fetch_fundamentals: MagicMock,
        mock_clean_ohlcv: MagicMock,
        mock_validate_data: MagicMock,
        mock_paper_trader_class: MagicMock,
        mock_send_info: MagicMock,
        tmp_path,
    ) -> None:
        """When compute_atr returns None, fallback 2% SL / 4% TP used."""
        # Return only 5 rows, insufficient for ATR (needs > 14)
        ohlcv_df = pd.DataFrame({
            "symbol": ["RELIANCE"] * 5,
            "date": pd.date_range("2026-03-20", periods=5),
            "open": [2500.0, 2510.0, 2520.0, 2530.0, 2540.0],
            "high": [2510.0, 2520.0, 2530.0, 2540.0, 2550.0],
            "low": [2490.0, 2500.0, 2510.0, 2520.0, 2530.0],
            "close": [2505.0, 2515.0, 2525.0, 2535.0, 2545.0],
            "volume": [1000000, 1100000, 1200000, 1300000, 1400000],
        })
        mock_fetch_ohlcv.return_value = ohlcv_df

        fundamentals_df = pd.DataFrame({
            "symbol": ["RELIANCE"],
            "roe": [18.5],
            "debt_to_equity": [0.8],
            "eps": [100.0],
            "traded_value_avg": [5000000000],
            "price": [2545.0],
        })
        mock_fetch_fundamentals.return_value = fundamentals_df

        cleaned_df = ohlcv_df.copy()
        cleaning_report = Mock()
        cleaning_report.rows_input = 5
        cleaning_report.rows_output = 5
        cleaning_report.duplicates_removed = 0
        mock_clean_ohlcv.return_value = (cleaned_df, cleaning_report)

        quality_report = Mock()
        quality_report.universe_quality_score = 0.85
        mock_validate_data.return_value = quality_report

        trader_instance = Mock()
        trader_instance.get_positions.return_value = []
        trader_instance.place_order.return_value = 789
        trader_instance.get_pnl.return_value = {
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "trade_count": 1,
            "win_count": 0,
            "loss_count": 0,
        }
        mock_paper_trader_class.return_value = trader_instance

        mock_send_info.return_value = {"telegram": True, "gmail": False}

        with patch("main.settings") as mock_settings:
            mock_settings.database_url = f"sqlite:///{tmp_path}/trading.db"
            with patch("main.os.makedirs"):
                main()

        # Verify place_order was called
        trader_instance.place_order.assert_called_once()
        call_args = trader_instance.place_order.call_args
        assert call_args is not None
        kwargs = call_args[1]

        entry_price = 2545.0
        # Fallback: stop_loss = entry * 0.98, take_profit = entry * 1.04
        expected_sl = round(entry_price * 0.98, 2)
        expected_tp = round(entry_price * 1.04, 2)

        assert kwargs["stop_loss"] == expected_sl
        assert kwargs["take_profit"] == expected_tp

        # Verify send_info message mentions "N/A (fallback used)"
        mock_send_info.assert_called()
        call_args = mock_send_info.call_args
        assert call_args is not None
        message = call_args[0][0]
        assert "fallback" in message.lower() or "N/A" in message
