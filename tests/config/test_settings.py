"""Tests for src/config/settings.py — all 31 acceptance criteria."""

import dataclasses
import os
from collections.abc import Generator
from pathlib import Path

import pytest

from src.config.settings import ConfigurationError, Settings, load_settings


@pytest.fixture
def clean_env() -> Generator[None, None, None]:
    """Clean environment variables before each test to avoid cross-test pollution."""
    # Save originals
    original_env = dict(os.environ)

    # Remove all known config variables
    vars_to_remove = {
        "LIVE_TRADING", "PAPER_TRADING", "MAX_TRADE_AMOUNT", "DATABASE_URL",
        "LOG_LEVEL", "GROQ_API_KEY", "GEMINI_API_KEY", "GITHUB_PAT",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SHOONYA_USER",
        "SHOONYA_PASSWORD", "SHOONYA_TOTP_SECRET", "FYERS_API_KEY",
        "BRAVE_API_KEY", "GMAIL_CREDENTIALS",
    }
    for var in vars_to_remove:
        os.environ.pop(var, None)

    yield

    # Restore
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def valid_env_file(tmp_path: Path, clean_env) -> Path:
    """Create a temporary .env file with all required variables present and valid.

    This is the baseline for testing — phase-gated variables are intentionally absent.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )
    return env_file


# ============================================================================
# Criterion 1: load_settings() returns Settings when all required are valid
# ============================================================================


def test_criterion_1_load_settings_returns_settings_with_valid_env(
    valid_env_file: Path,
) -> None:
    """Criterion 1: load_settings() returns a Settings instance with all valid variables."""
    settings = load_settings(env_path=str(valid_env_file))

    assert isinstance(settings, Settings)
    assert settings.paper_trading is True
    assert settings.max_trade_amount == 10000
    assert settings.database_url == "sqlite:///data/trading.db"
    assert settings.log_level == "INFO"


# ============================================================================
# Criterion 2: ConfigurationError raised when single required variable missing
# ============================================================================


def test_criterion_2_missing_single_required_variable(
    tmp_path: Path, clean_env
) -> None:
    """Criterion 2: ConfigurationError raised when GROQ_API_KEY is missing."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        # Missing GROQ_API_KEY
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError):
        load_settings(env_path=str(env_file))


# ============================================================================
# Criterion 3: ALL missing variables reported together, not just first
# ============================================================================


def test_criterion_3_all_missing_variables_reported(
    tmp_path: Path, clean_env
) -> None:
    """Criterion 3: ConfigurationError lists ALL missing variables at once."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        # Missing PAPER_TRADING
        # Missing MAX_TRADE_AMOUNT
        # Missing DATABASE_URL
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    # Verify at least 3 errors are reported
    assert len(exc_info.value.errors) >= 3


# ============================================================================
# Criterion 4: ConfigurationError.errors attribute contains exact error list
# ============================================================================


def test_criterion_4_configuration_error_errors_attribute(
    tmp_path: Path, clean_env
) -> None:
    """Criterion 4: ConfigurationError.errors contains exact list of error strings."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        # Missing TELEGRAM_CHAT_ID
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert hasattr(exc_info.value, "errors")
    assert isinstance(exc_info.value.errors, list)
    assert all(isinstance(err, str) for err in exc_info.value.errors)
    assert any("TELEGRAM_CHAT_ID" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 5: LIVE_TRADING defaults to False when absent
# ============================================================================


def test_criterion_5_live_trading_defaults_false(tmp_path: Path, clean_env) -> None:
    """Criterion 5: LIVE_TRADING defaults to False when absent from .env."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        # LIVE_TRADING absent
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    settings = load_settings(env_path=str(env_file))
    assert settings.live_trading is False


# ============================================================================
# Criterion 6: LIVE_TRADING="true" coerces to bool True (case-insensitive)
# ============================================================================


def test_criterion_6_live_trading_true_case_insensitive(
    valid_env_file: Path, tmp_path: Path
) -> None:
    """Criterion 6: LIVE_TRADING='True', 'TRUE' all coerce to bool True."""
    for value in ["true", "True", "TRUE"]:
        env_file = tmp_path / f".env_{value}"
        content = f"""LIVE_TRADING={value}
PAPER_TRADING=false
MAX_TRADE_AMOUNT=10000
DATABASE_URL=sqlite:///data/trading.db
LOG_LEVEL=INFO
GROQ_API_KEY=test-groq-key
GEMINI_API_KEY=test-gemini-key
GITHUB_PAT=test-github-pat
TELEGRAM_BOT_TOKEN=test-telegram-token
TELEGRAM_CHAT_ID=test-chat-id
"""
        env_file.write_text(content)

        settings = load_settings(env_path=str(env_file))
        assert settings.live_trading is True, f"Failed for LIVE_TRADING={value}"


# ============================================================================
# Criterion 7: LIVE_TRADING="false" coerces to bool False
# ============================================================================


def test_criterion_7_live_trading_false(tmp_path: Path, clean_env) -> None:
    """Criterion 7: LIVE_TRADING='false' coerces to bool False."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    settings = load_settings(env_path=str(env_file))
    assert settings.live_trading is False


# ============================================================================
# Criterion 8: LIVE_TRADING="yes" raises ConfigurationError
# ============================================================================


def test_criterion_8_live_trading_invalid_value(tmp_path: Path, clean_env) -> None:
    """Criterion 8: LIVE_TRADING='yes' raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=yes\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("LIVE_TRADING" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 9: MAX_TRADE_AMOUNT="10000" coerces to int 10000
# ============================================================================


def test_criterion_9_max_trade_amount_10000(valid_env_file: Path) -> None:
    """Criterion 9: MAX_TRADE_AMOUNT='10000' coerces to int 10000."""
    settings = load_settings(env_path=str(valid_env_file))
    assert settings.max_trade_amount == 10000
    assert isinstance(settings.max_trade_amount, int)


# ============================================================================
# Criterion 10: MAX_TRADE_AMOUNT="10001" raises ConfigurationError
# ============================================================================


def test_criterion_10_max_trade_amount_exceeds_cap(tmp_path: Path, clean_env) -> None:
    """Criterion 10: MAX_TRADE_AMOUNT='10001' raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10001\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("MAX_TRADE_AMOUNT" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 11: MAX_TRADE_AMOUNT="0" raises ConfigurationError
# ============================================================================


def test_criterion_11_max_trade_amount_zero(tmp_path: Path, clean_env) -> None:
    """Criterion 11: MAX_TRADE_AMOUNT='0' raises ConfigurationError (must be positive)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=0\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("MAX_TRADE_AMOUNT" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 12: MAX_TRADE_AMOUNT="abc" raises ConfigurationError
# ============================================================================


def test_criterion_12_max_trade_amount_not_integer(tmp_path: Path, clean_env) -> None:
    """Criterion 12: MAX_TRADE_AMOUNT='abc' raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=abc\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("MAX_TRADE_AMOUNT" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 13: DATABASE_URL="sqlite:///data/trading.db" passes validation
# ============================================================================


def test_criterion_13_database_url_sqlite_valid(valid_env_file: Path) -> None:
    """Criterion 13: DATABASE_URL='sqlite:///data/trading.db' passes validation."""
    settings = load_settings(env_path=str(valid_env_file))
    assert settings.database_url == "sqlite:///data/trading.db"


# ============================================================================
# Criterion 14: DATABASE_URL="postgres://..." raises ConfigurationError
# ============================================================================


def test_criterion_14_database_url_postgres_invalid(tmp_path: Path, clean_env) -> None:
    """Criterion 14: DATABASE_URL='postgres://localhost/db' raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=postgres://localhost/db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("DATABASE_URL" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 15: LOG_LEVEL="DEBUG" passes and is stored uppercase
# ============================================================================


def test_criterion_15_log_level_debug(tmp_path: Path, clean_env) -> None:
    """Criterion 15: LOG_LEVEL='DEBUG' passes and is stored as 'DEBUG'."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=DEBUG\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    settings = load_settings(env_path=str(env_file))
    assert settings.log_level == "DEBUG"


# ============================================================================
# Criterion 16: LOG_LEVEL="info" passes and is stored as "INFO" (uppercased)
# ============================================================================


def test_criterion_16_log_level_lowercase_uppercased(tmp_path: Path, clean_env) -> None:
    """Criterion 16: LOG_LEVEL='info' passes and is stored as 'INFO'."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=info\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    settings = load_settings(env_path=str(env_file))
    assert settings.log_level == "INFO"


# ============================================================================
# Criterion 17: LOG_LEVEL="TRACE" raises ConfigurationError
# ============================================================================


def test_criterion_17_log_level_invalid(tmp_path: Path, clean_env) -> None:
    """Criterion 17: LOG_LEVEL='TRACE' raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=TRACE\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("LOG_LEVEL" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 18: LOG_LEVEL missing raises ConfigurationError
# ============================================================================


def test_criterion_18_log_level_missing(tmp_path: Path, clean_env) -> None:
    """Criterion 18: LOG_LEVEL absent from .env raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        # LOG_LEVEL absent
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("LOG_LEVEL" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 19: Settings is frozen — cannot modify after construction
# ============================================================================


def test_criterion_19_settings_frozen(valid_env_file: Path) -> None:
    """Criterion 19: Settings is frozen — assigning to any field raises FrozenInstanceError."""
    settings = load_settings(env_path=str(valid_env_file))

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.live_trading = True  # type: ignore


# ============================================================================
# Criterion 20: repr(settings) masks all secret fields
# ============================================================================


def test_criterion_20_repr_masks_secrets(valid_env_file: Path) -> None:
    """Criterion 20: repr(settings) does not contain actual secret values."""
    settings = load_settings(env_path=str(valid_env_file))
    repr_str = repr(settings)

    # Should NOT contain actual values
    assert "test-groq-key" not in repr_str
    assert "test-gemini-key" not in repr_str
    assert "test-github-pat" not in repr_str
    assert "test-telegram-token" not in repr_str

    # Should contain masked values
    assert "groq_api_key='***'" in repr_str
    assert "gemini_api_key='***'" in repr_str
    assert "github_pat='***'" in repr_str
    assert "telegram_bot_token='***'" in repr_str
    assert "telegram_chat_id='***'" in repr_str


# ============================================================================
# Criterion 21: str(settings) masks all secret fields
# ============================================================================


def test_criterion_21_str_masks_secrets(valid_env_file: Path) -> None:
    """Criterion 21: str(settings) does not contain actual secret values."""
    settings = load_settings(env_path=str(valid_env_file))
    str_repr = str(settings)

    # Should NOT contain actual values
    assert "test-groq-key" not in str_repr
    assert "test-gemini-key" not in str_repr
    assert "test-github-pat" not in str_repr
    assert "test-telegram-token" not in str_repr

    # Should contain masked values
    assert "groq_api_key='***'" in str_repr
    assert "gemini_api_key='***'" in str_repr
    assert "github_pat='***'" in str_repr
    assert "telegram_bot_token='***'" in str_repr


# ============================================================================
# Criterion 22: live_trading=True AND paper_trading=True raises ConfigurationError
# ============================================================================


def test_criterion_22_both_trading_modes_true(tmp_path: Path) -> None:
    """Criterion 22: live_trading=True and paper_trading=True simultaneously raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=true\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("cannot both be true" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 23: load_settings(env_path=...) reads from specified file
# ============================================================================


def test_criterion_23_env_path_parameter(tmp_path: Path) -> None:
    """Criterion 23: load_settings(env_path='/path/to/test.env') reads from specified file."""
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=5000\n"
        "DATABASE_URL=sqlite:///custom.db\n"
        "LOG_LEVEL=WARNING\n"
        "GROQ_API_KEY=custom-groq-key\n"
        "GEMINI_API_KEY=custom-gemini-key\n"
        "GITHUB_PAT=custom-github-pat\n"
        "TELEGRAM_BOT_TOKEN=custom-telegram-token\n"
        "TELEGRAM_CHAT_ID=custom-chat-id\n"
    )

    settings = load_settings(env_path=str(env_file))

    assert settings.max_trade_amount == 5000
    assert settings.database_url == "sqlite:///custom.db"
    assert settings.log_level == "WARNING"


# ============================================================================
# Criterion 24: Empty string for required variable treated as missing
# ============================================================================


def test_criterion_24_empty_string_required_variable(tmp_path: Path) -> None:
    """Criterion 24: Empty string values for required variables raise ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        'GROQ_API_KEY=""\n'
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("GROQ_API_KEY" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 25: Whitespace-only for required variable treated as missing
# ============================================================================


def test_criterion_25_whitespace_only_required_variable(tmp_path: Path) -> None:
    """Criterion 25: Whitespace-only values for required variables raise ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        'GROQ_API_KEY="   "\n'
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("GROQ_API_KEY" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 26: MAX_TRADE_AMOUNT="-5" raises ConfigurationError
# ============================================================================


def test_criterion_26_max_trade_amount_negative(tmp_path: Path) -> None:
    """Criterion 26: MAX_TRADE_AMOUNT='-5' raises ConfigurationError."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=-5\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
    )

    with pytest.raises(ConfigurationError) as exc_info:
        load_settings(env_path=str(env_file))

    assert any("MAX_TRADE_AMOUNT" in err for err in exc_info.value.errors)


# ============================================================================
# Criterion 27: Phase-gated var absent → None (no ConfigurationError)
# ============================================================================


def test_criterion_27_phase_gated_absent_results_in_none(
    valid_env_file: Path,
) -> None:
    """Criterion 27: Phase-gated variable absent results in None — no error."""
    settings = load_settings(env_path=str(valid_env_file))

    assert settings.shoonya_user is None
    assert settings.shoonya_password is None
    assert settings.shoonya_totp_secret is None
    assert settings.fyers_api_key is None
    assert settings.brave_api_key is None
    assert settings.gmail_credentials is None


# ============================================================================
# Criterion 28: Phase-gated var empty string → None (not error)
# ============================================================================


def test_criterion_28_phase_gated_empty_string_results_in_none(tmp_path: Path) -> None:
    """Criterion 28: Phase-gated variable set to empty string results in None."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
        'SHOONYA_PASSWORD=""\n'
        'BRAVE_API_KEY=""\n'
    )

    settings = load_settings(env_path=str(env_file))

    assert settings.shoonya_password is None
    assert settings.brave_api_key is None


# ============================================================================
# Criterion 29: Phase-gated var with valid value → stored as string
# ============================================================================


def test_criterion_29_phase_gated_valid_value_stored(tmp_path: Path) -> None:
    """Criterion 29: Phase-gated variable with valid value stored as string."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TRADING=false\n"
        "PAPER_TRADING=true\n"
        "MAX_TRADE_AMOUNT=10000\n"
        "DATABASE_URL=sqlite:///data/trading.db\n"
        "LOG_LEVEL=INFO\n"
        "GROQ_API_KEY=test-groq-key\n"
        "GEMINI_API_KEY=test-gemini-key\n"
        "GITHUB_PAT=test-github-pat\n"
        "TELEGRAM_BOT_TOKEN=test-telegram-token\n"
        "TELEGRAM_CHAT_ID=test-chat-id\n"
        "BRAVE_API_KEY=abc123\n"
        "SHOONYA_USER=test_user\n"
    )

    settings = load_settings(env_path=str(env_file))

    assert settings.brave_api_key == "abc123"
    assert settings.shoonya_user == "test_user"


# ============================================================================
# Criterion 30: mypy passes with --ignore-missing-imports
# ============================================================================


def test_criterion_30_mypy_passes(tmp_path: Path) -> None:
    """Criterion 30: mypy passes on settings.py with --ignore-missing-imports."""
    import subprocess

    result = subprocess.run(
        [
            "python", "-m", "mypy",
            "src/config/settings.py",
            "--ignore-missing-imports",
        ],
        cwd="/home/hitaish/projects/indian-trader",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"mypy failed:\n{result.stdout}\n{result.stderr}"


# ============================================================================
# Criterion 31: ruff check passes
# ============================================================================


def test_criterion_31_ruff_check_passes(tmp_path: Path) -> None:
    """Criterion 31: ruff check passes on settings.py."""
    import subprocess

    result = subprocess.run(
        [
            "python", "-m", "ruff",
            "check",
            "src/config/settings.py",
        ],
        cwd="/home/hitaish/projects/indian-trader",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"ruff check failed:\n{result.stdout}\n{result.stderr}"
