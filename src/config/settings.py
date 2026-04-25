"""Configuration loader for the Indian Trader pipeline.

This module is the single source of truth for all environment-based configuration.
It loads variables from .env at import time, validates them, coerces them to typed
Python values, and exposes a frozen Settings dataclass as a module-level singleton.

Every other module imports `settings` from here instead of reading os.environ directly.
Secret values are never exposed in string representations, logs, or error messages.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass

import dotenv

# ---------------------------------------------------------------------------
# Secret fields — never included in __repr__, __str__, or error messages
# ---------------------------------------------------------------------------

_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "shoonya_user",
        "shoonya_password",
        "shoonya_totp_secret",
        "fyers_api_key",
        "groq_api_key",
        "gemini_api_key",
        "github_pat",
        "brave_api_key",
        "tavily_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
        "gmail_credentials",
    }
)

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class ConfigurationError(Exception):
    """Raised when environment configuration is missing or invalid.

    Attributes:
        errors: List of all validation error messages found.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        joined = "\n  - ".join(errors)
        super().__init__(
            f"Configuration validation failed with {len(errors)} error(s):\n  - {joined}"
        )


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Immutable configuration loaded from .env at startup.

    All secret fields are masked in __repr__ and __str__.
    """

    # --- Trading controls ---
    live_trading: bool
    paper_trading: bool
    max_trade_amount: int

    # --- Database ---
    database_url: str

    # --- Logging ---
    log_level: str

    # --- Broker credentials (Shoonya) — phase-gated: None until Phase 4 ---
    shoonya_user: str | None
    shoonya_password: str | None
    shoonya_totp_secret: str | None

    # --- API keys ---
    fyers_api_key: str | None  # phase-gated: None until Phase 3/4
    groq_api_key: str
    gemini_api_key: str
    github_pat: str
    brave_api_key: str | None  # phase-gated: None until Phase 3
    tavily_api_key: str | None  # phase-gated: None until Phase 3

    # --- Notification ---
    telegram_bot_token: str
    telegram_chat_id: str
    gmail_credentials: str | None  # phase-gated: None until Phase 4

    # --- Universe selection ---
    nifty_universe: str = "nifty200"  # nifty50 | nifty200 | nifty500

    def __repr__(self) -> str:
        """Return a string representation with all secret fields masked as '***'.

        Phase-gated fields that are None are shown as None, not '***'.
        """
        parts: list[str] = []
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name in _SECRET_FIELDS and value is not None:
                parts.append(f"{field.name}='***'")
            else:
                parts.append(f"{field.name}={value!r}")
        return f"Settings({', '.join(parts)})"

    def __str__(self) -> str:
        """Return the same masked representation as __repr__."""
        return self.__repr__()


# ---------------------------------------------------------------------------
# Public loader function
# ---------------------------------------------------------------------------


def load_settings(env_path: str | None = None) -> Settings:
    """Load and validate all configuration from .env, returning a Settings instance.

    Reads the .env file using python-dotenv, validates every required variable
    is present, validates value constraints, coerces types, and returns a
    frozen Settings dataclass.

    Args:
        env_path: Optional path to .env file. When None, python-dotenv
                  searches from the current working directory upward.
                  Useful for testing with alternate .env files.

    Returns:
        A fully validated, immutable Settings instance.

    Raises:
        ConfigurationError: If any required variable is missing, or if any
                            variable fails its validation rule. The error
                            message lists ALL problems found, not just the
                            first one.
    """
    dotenv.load_dotenv(dotenv_path=env_path, override=True)

    errors: list[str] = []

    # -----------------------------------------------------------------------
    # LIVE_TRADING — optional with safety default of False
    # -----------------------------------------------------------------------
    live_trading: bool = False
    live_trading_raw = os.environ.get("LIVE_TRADING", "").strip()
    if live_trading_raw != "":
        if live_trading_raw.lower() not in ("true", "false"):
            errors.append(
                f"LIVE_TRADING must be 'true' or 'false', got: '{live_trading_raw}'"
            )
        else:
            live_trading = live_trading_raw.lower() == "true"

    # -----------------------------------------------------------------------
    # PAPER_TRADING — always required
    # -----------------------------------------------------------------------
    paper_trading: bool = False
    paper_trading_raw = os.environ.get("PAPER_TRADING", "").strip()
    if not paper_trading_raw:
        errors.append("Missing required variable: PAPER_TRADING")
    elif paper_trading_raw.lower() not in ("true", "false"):
        errors.append(
            f"PAPER_TRADING must be 'true' or 'false', got: '{paper_trading_raw}'"
        )
    else:
        paper_trading = paper_trading_raw.lower() == "true"

    # -----------------------------------------------------------------------
    # MAX_TRADE_AMOUNT — always required, positive int <= 10000
    # -----------------------------------------------------------------------
    max_trade_amount: int = 0
    max_trade_amount_raw = os.environ.get("MAX_TRADE_AMOUNT", "").strip()
    if not max_trade_amount_raw:
        errors.append("Missing required variable: MAX_TRADE_AMOUNT")
    else:
        try:
            max_trade_amount_parsed = int(max_trade_amount_raw)
            if max_trade_amount_parsed <= 0 or max_trade_amount_parsed > 10000:
                errors.append(
                    f"MAX_TRADE_AMOUNT must be a positive integer <= 10000, "
                    f"got: '{max_trade_amount_raw}'"
                )
            else:
                max_trade_amount = max_trade_amount_parsed
        except ValueError:
            errors.append(
                f"MAX_TRADE_AMOUNT must be a positive integer <= 10000, "
                f"got: '{max_trade_amount_raw}'"
            )

    # -----------------------------------------------------------------------
    # DATABASE_URL — always required, must start with sqlite:///
    # -----------------------------------------------------------------------
    database_url: str = ""
    database_url_raw = os.environ.get("DATABASE_URL", "").strip()
    if not database_url_raw:
        errors.append("Missing required variable: DATABASE_URL")
    elif not database_url_raw.startswith("sqlite:///"):
        errors.append(
            f"DATABASE_URL must start with 'sqlite:///', got: '{database_url_raw}'"
        )
    else:
        database_url = database_url_raw

    # -----------------------------------------------------------------------
    # LOG_LEVEL — always required, one of DEBUG/INFO/WARNING/ERROR
    # -----------------------------------------------------------------------
    log_level: str = ""
    log_level_raw = os.environ.get("LOG_LEVEL", "").strip()
    if not log_level_raw:
        errors.append("Missing required variable: LOG_LEVEL")
    elif log_level_raw.upper() not in _VALID_LOG_LEVELS:
        errors.append(
            f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR, got: '{log_level_raw}'"
        )
    else:
        log_level = log_level_raw.upper()

    # -----------------------------------------------------------------------
    # Always-required string API keys — non-empty check only
    # -----------------------------------------------------------------------
    groq_api_key: str = ""
    groq_api_key_raw = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_api_key_raw:
        errors.append("Missing required variable: GROQ_API_KEY")
    else:
        groq_api_key = groq_api_key_raw

    gemini_api_key: str = ""
    gemini_api_key_raw = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_api_key_raw:
        errors.append("Missing required variable: GEMINI_API_KEY")
    else:
        gemini_api_key = gemini_api_key_raw

    github_pat: str = ""
    github_pat_raw = os.environ.get("GITHUB_PAT", "").strip()
    if not github_pat_raw:
        errors.append("Missing required variable: GITHUB_PAT")
    else:
        github_pat = github_pat_raw

    telegram_bot_token: str = ""
    telegram_bot_token_raw = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not telegram_bot_token_raw:
        errors.append("Missing required variable: TELEGRAM_BOT_TOKEN")
    else:
        telegram_bot_token = telegram_bot_token_raw

    telegram_chat_id: str = ""
    telegram_chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not telegram_chat_id_raw:
        errors.append("Missing required variable: TELEGRAM_CHAT_ID")
    else:
        telegram_chat_id = telegram_chat_id_raw

    # -----------------------------------------------------------------------
    # Phase-gated variables — absent or empty string → None, no error
    # -----------------------------------------------------------------------
    def _phase_gated(var_name: str) -> str | None:
        """Return stripped value if non-empty, else None. No errors raised."""
        raw = os.environ.get(var_name, "").strip()
        return raw if raw else None

    shoonya_user = _phase_gated("SHOONYA_USER")
    shoonya_password = _phase_gated("SHOONYA_PASSWORD")
    shoonya_totp_secret = _phase_gated("SHOONYA_TOTP_SECRET")
    fyers_api_key = _phase_gated("FYERS_API_KEY")
    brave_api_key = _phase_gated("BRAVE_API_KEY")
    tavily_api_key = _phase_gated("TAVILY_API_KEY")
    gmail_credentials = _phase_gated("GMAIL_CREDENTIALS")

    # -----------------------------------------------------------------------
    # NIFTY_UNIVERSE — optional, defaults to nifty200
    # -----------------------------------------------------------------------
    _VALID_UNIVERSES: frozenset[str] = frozenset({"nifty50", "nifty200", "nifty500"})
    nifty_universe_raw = os.environ.get("NIFTY_UNIVERSE", "nifty200").strip().lower()
    if nifty_universe_raw not in _VALID_UNIVERSES:
        errors.append(
            f"NIFTY_UNIVERSE must be one of nifty50/nifty200/nifty500, got: '{nifty_universe_raw}'"
        )
        nifty_universe = "nifty200"
    else:
        nifty_universe = nifty_universe_raw

    # -----------------------------------------------------------------------
    # Raise if any individual errors were collected
    # -----------------------------------------------------------------------
    if errors:
        raise ConfigurationError(errors)

    # -----------------------------------------------------------------------
    # Safety interlock: live_trading and paper_trading cannot both be True
    # -----------------------------------------------------------------------
    if live_trading and paper_trading:
        raise ConfigurationError(
            ["LIVE_TRADING and PAPER_TRADING cannot both be true"]
        )

    return Settings(
        live_trading=live_trading,
        paper_trading=paper_trading,
        max_trade_amount=max_trade_amount,
        database_url=database_url,
        log_level=log_level,
        shoonya_user=shoonya_user,
        shoonya_password=shoonya_password,
        shoonya_totp_secret=shoonya_totp_secret,
        fyers_api_key=fyers_api_key,
        groq_api_key=groq_api_key,
        gemini_api_key=gemini_api_key,
        github_pat=github_pat,
        brave_api_key=brave_api_key,
        tavily_api_key=tavily_api_key,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        gmail_credentials=gmail_credentials,
        nifty_universe=nifty_universe,
    )


# ---------------------------------------------------------------------------
# Module-level singleton — loaded at import time
# ---------------------------------------------------------------------------

settings: Settings = load_settings()
