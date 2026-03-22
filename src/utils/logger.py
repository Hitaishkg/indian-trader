"""Structured logging module for the Indian Trader pipeline.

Configures Python's standard logging framework to emit log records to two
destinations simultaneously: the console (stderr) for developer visibility,
and the SQLite agent_logs table for persistent audit trails.

Every agent action, data quality score, and trade decision flows through this
module. Existing modules that already call logging.getLogger(__name__)
(fetcher, cleaner, fundamentals) continue to work without modification --
setup_logging() configures the root logger so all child loggers inherit the
SQLite handler automatically.
"""

from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import sys
import threading
from zoneinfo import ZoneInfo

from src.config.settings import settings

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

_CREATE_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS agent_logs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at          TEXT    NOT NULL,
    agent_name         TEXT    NOT NULL,
    level              TEXT    NOT NULL,
    action             TEXT    NOT NULL,
    symbol             TEXT,
    result             TEXT,
    data_quality_score REAL
);
"""

_INSERT_SQL: str = (
    "INSERT INTO agent_logs "
    "(logged_at, agent_name, level, action, symbol, result, data_quality_score) "
    "VALUES (?, ?, ?, ?, ?, ?, ?);"
)

_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)


# ---------------------------------------------------------------------------
# Private IST-aware formatter
# ---------------------------------------------------------------------------


class _ISTFormatter(logging.Formatter):
    """Formatter that forces IST (Asia/Kolkata) timestamps in console output."""

    _IST = ZoneInfo("Asia/Kolkata")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Return the log record's creation time formatted in IST.

        Args:
            record: The log record whose timestamp to format.
            datefmt: Optional strftime format string.

        Returns:
            IST-formatted timestamp string.
        """
        dt = datetime.datetime.fromtimestamp(record.created, tz=self._IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# SQLiteHandler
# ---------------------------------------------------------------------------


class SQLiteHandler(logging.Handler):
    """A logging.Handler subclass that writes each LogRecord to agent_logs.

    Thread-safe: every emit() and close() call acquires an instance-level
    threading.Lock. The database connection is opened lazily on the first
    emit() call to prevent constructor errors from blocking setup_logging().
    """

    def __init__(self, db_path: str) -> None:
        """Initialise the handler with the path to the SQLite database.

        Args:
            db_path: Absolute path to the SQLite database file.
        """
        super().__init__()
        self._db_path: str = db_path
        self._lock: threading.Lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    def _ensure_connection(self) -> sqlite3.Connection | None:
        """Open and configure the SQLite connection if not already open.

        Returns the existing connection if already open. On first call, opens
        sqlite3.connect(self._db_path), applies WAL pragmas, and creates the
        agent_logs table. Returns None and prints to stderr if any step fails.

        Returns:
            An open sqlite3.Connection, or None if the connection could not
            be established.
        """
        if self._connection is not None:
            return self._connection
        try:
            conn = sqlite3.connect(self._db_path)
            for pragma in _WAL_PRAGMAS:
                conn.execute(pragma)
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
            self._connection = conn
            return self._connection
        except Exception as exc:
            print(
                f"[logger] SQLiteHandler._ensure_connection failed: {exc}",
                file=sys.stderr,
            )
            self._connection = None
            return None

    def emit(self, record: logging.LogRecord) -> None:
        """Write a log record to the agent_logs table.

        Extracts fields from the LogRecord and inserts one row. Silently
        returns if the database connection cannot be established. Never raises.

        Args:
            record: The log record to persist.
        """
        with self._lock:
            conn = self._ensure_connection()
            if conn is None:
                return

            logged_at = datetime.datetime.now(_IST).isoformat(timespec="seconds")
            agent_name = record.name
            level = record.levelname
            try:
                action = self.format(record)
            except Exception:
                action = record.getMessage()
            symbol: str | None = getattr(record, "symbol", None)
            result: str | None = getattr(record, "result", None)
            data_quality_score: float | None = getattr(
                record, "data_quality_score", None
            )

            try:
                conn.execute(
                    _INSERT_SQL,
                    (logged_at, agent_name, level, action, symbol, result, data_quality_score),
                )
                conn.commit()
            except Exception as exc:
                print(
                    f"[logger] SQLiteHandler.emit failed: {exc}",
                    file=sys.stderr,
                )

    def write_row(
        self,
        logged_at: str,
        agent_name: str,
        level: str,
        action: str,
        symbol: str | None,
        result: str | None,
        data_quality_score: float | None,
    ) -> None:
        """Write a structured row directly to agent_logs, bypassing LogRecord.

        Public method used by log_agent_action() to avoid accessing private
        attributes. Acquires the instance lock, ensures the connection, and
        inserts one row. Never raises.

        Args:
            logged_at: IST ISO 8601 timestamp string (e.g. '2026-03-22T22:15:30+05:30').
            agent_name: Identifies the agent or module writing the row.
            level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL.
            action: Human-readable description of what happened.
            symbol: NSE ticker symbol or None.
            result: Short outcome string (e.g. 'ok', 'error') or None.
            data_quality_score: Float between 0.0 and 1.0, or None.
        """
        with self._lock:
            conn = self._ensure_connection()
            if conn is None:
                return
            try:
                conn.execute(
                    _INSERT_SQL,
                    (logged_at, agent_name, level, action, symbol, result, data_quality_score),
                )
                conn.commit()
            except Exception as exc:
                print(
                    f"[logger] SQLiteHandler.write_row failed: {exc}",
                    file=sys.stderr,
                )

    def close(self) -> None:
        """Close the SQLite connection and release handler resources.

        Acquires the instance lock, closes the connection if open, then calls
        the parent close(). Prints to stderr if the close fails. Never raises.
        """
        with self._lock:
            try:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
            except Exception as exc:
                print(
                    f"[logger] SQLiteHandler.close failed: {exc}",
                    file=sys.stderr,
                )
            super().close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(db_path: str | None = None) -> None:
    """Configure the Python root logger with a StreamHandler and SQLiteHandler.

    Attaches two handlers to the root logger: a StreamHandler writing to
    stderr with IST-formatted timestamps, and a SQLiteHandler writing to the
    agent_logs table in the SQLite database. Idempotent -- safe to call
    multiple times; subsequent calls are no-ops.

    If the database path cannot be resolved or the database cannot be opened,
    a warning is printed to stderr and only the StreamHandler is attached.
    Never raises.

    Args:
        db_path: Path to the SQLite database file. When None, the path is
                 derived from settings.database_url by stripping the
                 'sqlite:///' prefix. A relative path is resolved relative
                 to the project root. An absolute path is used as-is.
    """
    root = logging.getLogger()

    # Idempotency check: if a SQLiteHandler is already attached, do nothing.
    for handler in root.handlers:
        if isinstance(handler, SQLiteHandler):
            return

    # --- 6.1. Database path resolution ---
    resolved_db_path: str | None = None
    try:
        if db_path is not None:
            if os.path.isabs(db_path):
                resolved_db_path = db_path
            else:
                resolved_db_path = os.path.join(_PROJECT_ROOT, db_path)
        else:
            url = settings.database_url
            if not url.startswith("sqlite:///"):
                print(
                    "[logger] Could not resolve database path, SQLite logging disabled",
                    file=sys.stderr,
                )
                resolved_db_path = None
            else:
                remainder = url[len("sqlite:///"):]
                if os.path.isabs(remainder):
                    resolved_db_path = remainder
                else:
                    resolved_db_path = os.path.join(_PROJECT_ROOT, remainder)

        if resolved_db_path is not None:
            parent_dir = os.path.dirname(resolved_db_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
    except Exception as exc:
        print(
            f"[logger] Could not resolve database path, SQLite logging disabled: {exc}",
            file=sys.stderr,
        )
        resolved_db_path = None

    # --- 6.3. Handler attachment ---

    # Remove any pre-existing StreamHandlers (added by ad-hoc module setup).
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]

    # Set root logger level from settings.
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))

    # Attach the IST-aware StreamHandler.
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.DEBUG)
    formatter = _ISTFormatter(
        "[%(asctime)s] [%(levelname)s] %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # Attach the SQLiteHandler if we have a resolved path.
    if resolved_db_path is not None:
        sqlite_handler = SQLiteHandler(resolved_db_path)
        sqlite_handler.setLevel(logging.DEBUG)
        # No formatter on SQLiteHandler -- action column stores plain message text.
        root.addHandler(sqlite_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits handlers from the root logger.

    Thin convenience wrapper around logging.getLogger(name). The returned
    logger inherits both the StreamHandler and SQLiteHandler that
    setup_logging() attached to the root logger.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        A logging.Logger instance.
    """
    return logging.getLogger(name)


def log_agent_action(
    agent_name: str,
    action: str,
    level: str = "INFO",
    symbol: str | None = None,
    result: str | None = None,
    data_quality_score: float | None = None,
) -> None:
    """Write a structured row to agent_logs, bypassing the logging pipeline.

    Designed for trading agents that need precise control over every column
    in the agent_logs row. Finds the active SQLiteHandler on the root logger
    and calls its write_row() method. Never raises.

    Args:
        agent_name: Identifies the agent or module (e.g. 'screener_agent').
        action: Human-readable description of what happened.
        level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL. Defaults to INFO.
               Invalid values are coerced to INFO without error.
        symbol: NSE ticker symbol or None.
        result: Short outcome string (e.g. 'ok', 'error', 'skipped') or None.
        data_quality_score: Float between 0.0 and 1.0, or None.
    """
    _VALID_LEVELS: frozenset[str] = frozenset(
        {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    )

    logged_at = datetime.datetime.now(_IST).isoformat(timespec="seconds")

    if level not in _VALID_LEVELS:
        level = "INFO"

    handler: SQLiteHandler | None = None
    for h in logging.getLogger().handlers:
        if isinstance(h, SQLiteHandler):
            handler = h
            break

    if handler is None:
        print(
            "[logger] log_agent_action called but no SQLiteHandler configured. "
            "Call setup_logging() first.",
            file=sys.stderr,
        )
        return

    try:
        handler.write_row(
            logged_at, agent_name, level, action, symbol, result, data_quality_score
        )
    except Exception as exc:
        print(
            f"[logger] log_agent_action failed: {exc}",
            file=sys.stderr,
        )
