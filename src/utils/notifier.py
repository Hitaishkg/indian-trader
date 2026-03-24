"""Dual-channel notification module for the Indian Trader pipeline.

Sends notifications via Telegram Bot API and Gmail API. Three notification
types exist: ALERT (both channels), CHECKPOINT (both channels), and INFO
(Telegram only). Every send attempt, success, failure, and skip is logged
to the agent_logs table via log_agent_action(). The module degrades
gracefully when credentials are not configured -- it logs the skip and
continues without raising.

Importing this module triggers no network calls and no OAuth flow.
Side effects occur only when a public send function is called.
"""

from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText
from enum import Enum
from typing import Any

import requests

from src.config.settings import settings
from src.utils.logger import log_agent_action

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.send"]

_TELEGRAM_API_BASE: str = "https://api.telegram.org/bot"

_TELEGRAM_TIMEOUT: int = 10  # seconds

_TELEGRAM_MAX_LENGTH: int = 4096  # Telegram sendMessage character limit

_AGENT_NAME: str = "notifier"  # used in all log_agent_action() calls

_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

_TOKEN_FILE: str = "token.json"  # stored in _PROJECT_ROOT

# ---------------------------------------------------------------------------
# Module-level mutable state — Gmail service and address cache
# ---------------------------------------------------------------------------

_gmail_service_cache: Any = None
_gmail_address_cache: str | None = None


# ---------------------------------------------------------------------------
# Notification type enum
# ---------------------------------------------------------------------------


class NotificationType(Enum):
    """Notification severity/routing type."""

    ALERT = "ALERT"
    CHECKPOINT = "CHECKPOINT"
    INFO = "INFO"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_alert(subject: str, message: str) -> dict[str, bool]:
    """Send an ALERT notification via both Telegram and Gmail.

    Args:
        subject: Short subject line used as Telegram message prefix and
                 Gmail email subject (e.g. "Kill switch fired: drawdown > 15%").
        message: Full notification body as plain text.

    Returns:
        {"telegram": bool, "gmail": bool} -- True if that channel sent
        successfully, False if it failed or was skipped.
    """
    telegram_result = _send_telegram(NotificationType.ALERT, subject, message)
    gmail_result = _send_gmail(NotificationType.ALERT, subject, message)

    if not telegram_result and not gmail_result:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action="ALERT notification could not be sent: both channels failed or unconfigured",
            level="CRITICAL",
            result="error",
        )

    return {"telegram": telegram_result, "gmail": gmail_result}


def send_checkpoint(subject: str, message: str) -> dict[str, bool]:
    """Send a CHECKPOINT notification via both Telegram and Gmail.

    Args:
        subject: Short subject line (e.g. "Trade approval needed: HDFC Bank BUY").
        message: Full notification body with trade details as plain text.

    Returns:
        {"telegram": bool, "gmail": bool} -- True if that channel sent
        successfully, False if it failed or was skipped.
    """
    telegram_result = _send_telegram(NotificationType.CHECKPOINT, subject, message)
    gmail_result = _send_gmail(NotificationType.CHECKPOINT, subject, message)

    if not telegram_result and not gmail_result:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action="CHECKPOINT notification could not be sent: both channels failed or unconfigured",
            level="CRITICAL",
            result="error",
        )

    return {"telegram": telegram_result, "gmail": gmail_result}


def send_info(message: str) -> dict[str, bool]:
    """Send an INFO notification via Telegram only. Gmail is never attempted.

    Args:
        message: Notification body. No separate subject line; the entire
                 message is sent as the Telegram text.

    Returns:
        {"telegram": bool, "gmail": False} -- gmail is always False (not
        attempted, not logged as skipped).
    """
    telegram_result = _send_telegram(NotificationType.INFO, "", message)
    gmail_result = False

    return {"telegram": telegram_result, "gmail": gmail_result}


# ---------------------------------------------------------------------------
# Private functions
# ---------------------------------------------------------------------------


def _send_telegram(
    notification_type: NotificationType, subject: str, message: str
) -> bool:
    """Send a single message via the Telegram Bot API.

    Args:
        notification_type: Used in the log action string.
        subject: Prepended to the message with a newline if non-empty.
        message: The body text.

    Returns:
        True if the HTTP POST returned status 200 and the response JSON has
        "ok": true. False otherwise.
    """
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    # Defensive credential check -- handles None or empty string
    if (
        not token
        or not token.strip()
        or not chat_id
        or not chat_id.strip()
    ):
        log_agent_action(
            agent_name=_AGENT_NAME,
            action="Telegram skipped: credentials not configured",
            level="WARNING",
            result="skipped",
        )
        return False

    # Construct the message text
    if subject:
        text = f"<b>[{notification_type.value}] {subject}</b>\n\n{message}"
    else:
        text = message

    # Enforce Telegram's 4096-character limit
    if len(text) > _TELEGRAM_MAX_LENGTH:
        text = text[:4093] + "..."

    try:
        response = requests.post(
            f"{_TELEGRAM_API_BASE}{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=_TELEGRAM_TIMEOUT,
        )
    except requests.RequestException as exc:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Telegram {notification_type.value} failed: {exc}",
            level="ERROR",
            result="error",
        )
        return False

    # Check response
    if response.status_code != 200:
        error_detail = response.text[:200]
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Telegram {notification_type.value} failed: HTTP {response.status_code} {error_detail}",
            level="ERROR",
            result="error",
        )
        return False

    try:
        resp_json = response.json()
    except Exception as exc:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Telegram {notification_type.value} failed: could not parse response JSON: {exc}",
            level="ERROR",
            result="error",
        )
        return False

    if not resp_json.get("ok"):
        error_detail = str(resp_json)[:200]
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Telegram {notification_type.value} failed: {error_detail}",
            level="ERROR",
            result="error",
        )
        return False

    # Success
    if subject:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Telegram {notification_type.value} sent: {subject}",
            level="INFO",
            result="ok",
        )
    else:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Telegram {notification_type.value} sent",
            level="INFO",
            result="ok",
        )
    return True


def _send_gmail(
    notification_type: NotificationType, subject: str, message: str
) -> bool:
    """Send a single email via the Gmail API.

    Args:
        notification_type: Used in email subject prefix and log action string.
        subject: The subject line content.
        message: The email body (plain text).

    Returns:
        True if the Gmail API call succeeded. False otherwise.
    """
    global _gmail_service_cache

    credentials_path = settings.gmail_credentials

    # Defensive credential check -- handles None or empty string
    if not credentials_path or not credentials_path.strip():
        log_agent_action(
            agent_name=_AGENT_NAME,
            action="Gmail skipped: credentials not configured",
            level="WARNING",
            result="skipped",
        )
        return False

    service = _build_gmail_service()
    if service is None:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Gmail {notification_type.value} failed: could not build Gmail service",
            level="ERROR",
            result="error",
        )
        return False

    address = _get_gmail_address(service)
    mime_message = _build_mime_message(notification_type, subject, message, address)

    raw_bytes = mime_message.as_bytes()
    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    try:
        service.users().messages().send(
            userId="me",
            body={"raw": encoded},
        ).execute()
    except Exception as exc:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Gmail {notification_type.value} failed: {exc}",
            level="ERROR",
            result="error",
        )
        _gmail_service_cache = None
        return False

    log_agent_action(
        agent_name=_AGENT_NAME,
        action=f"Gmail {notification_type.value} sent: {subject}",
        level="INFO",
        result="ok",
    )
    return True


def _build_gmail_service() -> Any | None:
    """Build and return a Gmail API service object, handling OAuth token management.

    Returns the cached service on subsequent calls without re-reading token.json.
    Runs the OAuth flow on the first call when no token.json exists. Returns
    None if the service could not be built. Never raises.

    Returns:
        A googleapiclient.discovery.Resource for the Gmail API, or None.
    """
    global _gmail_service_cache

    if _gmail_service_cache is not None:
        return _gmail_service_cache

    # Lazy imports -- only here, never at module level
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = os.path.join(_PROJECT_ROOT, _TOKEN_FILE)

    try:
        creds: Credentials | None = None

        # Load existing token if present
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, _GMAIL_SCOPES)

        # Refresh or re-authorize if needed
        if creds is None or not creds.valid:
            if creds is not None and creds.expired and creds.refresh_token is not None:
                try:
                    creds.refresh(GoogleAuthRequest())
                except Exception as exc:
                    log_agent_action(
                        agent_name=_AGENT_NAME,
                        action=f"Gmail OAuth token refresh failed: {exc}",
                        level="ERROR",
                        result="error",
                    )
                    # Delete the stale token and fall through to full OAuth flow
                    try:
                        os.remove(token_path)
                    except OSError:
                        pass
                    creds = None

            if creds is None:
                credentials_path = settings.gmail_credentials
                if not credentials_path or not os.path.isfile(credentials_path):
                    log_agent_action(
                        agent_name=_AGENT_NAME,
                        action=f"Gmail OAuth credentials file not found: {credentials_path}",
                        level="ERROR",
                        result="error",
                    )
                    return None

                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentials_path, _GMAIL_SCOPES
                    )
                    creds = flow.run_local_server(port=0)
                except Exception as exc:
                    log_agent_action(
                        agent_name=_AGENT_NAME,
                        action=f"Gmail OAuth flow failed: {exc}",
                        level="ERROR",
                        result="error",
                    )
                    return None

        # Persist the credentials to token.json
        try:
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except OSError as exc:
            log_agent_action(
                agent_name=_AGENT_NAME,
                action=f"Gmail OAuth token could not be saved: {exc}",
                level="WARNING",
                result="error",
            )
            # Continue -- creds are still valid in memory

        service = build("gmail", "v1", credentials=creds)
        _gmail_service_cache = service
        return service

    except Exception as exc:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Gmail service build failed: {exc}",
            level="ERROR",
            result="error",
        )
        return None


def _get_gmail_address(service: Any) -> str | None:
    """Fetch and cache the authenticated Gmail account address.

    Calls service.users().getProfile(userId="me") on first invocation and
    caches the result in _gmail_address_cache for all subsequent calls.

    Args:
        service: A built Gmail API service object.

    Returns:
        The email address string (e.g. "user@gmail.com"), or None on failure.
    """
    global _gmail_address_cache

    if _gmail_address_cache is not None:
        return _gmail_address_cache

    try:
        profile = service.users().getProfile(userId="me").execute()
        address: str = profile["emailAddress"]
        _gmail_address_cache = address
        return address
    except Exception as exc:
        log_agent_action(
            agent_name=_AGENT_NAME,
            action=f"Gmail getProfile failed: {exc}",
            level="ERROR",
            result="error",
        )
        return None


def _build_mime_message(
    notification_type: NotificationType, subject: str, message: str, address: str | None
) -> MIMEText:
    """Construct a MIME email message ready for base64url encoding.

    Args:
        notification_type: Used in the subject prefix.
        subject: The subject line content.
        message: The email body (plain text).
        address: The authenticated Gmail address used for both From and To
                 headers. Falls back to an empty string if None (should not
                 happen in normal operation but prevents a header error).

    Returns:
        A MIMEText object with Subject, From, and To headers set.
    """
    addr = address or ""
    msg = MIMEText(message, "plain")
    msg["Subject"] = f"[Indian Trader] {notification_type.value}: {subject}"
    msg["From"] = addr
    msg["To"] = addr
    return msg
