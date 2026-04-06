"""Tests for src/agents/watchlist_agent.py.

Covers all 15 scenarios from the spec at docs/specs/2026-04-05-watchlist-agent.md.
"""

from __future__ import annotations

import datetime
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.agents.watchlist_agent import (
    run_watchlist_agent,
    check_watchlist_timeout,
    record_human_approval,
    WatchlistAgentError,
)
from src.utils.logger import setup_logging

# ---------------------------------------------------------------------------
# Timezone constant for test use
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

# Fixed run date used across most tests
RUN_DATE = datetime.date(2026, 4, 5)

# WAL pragmas (same as in module)
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)


# ---------------------------------------------------------------------------
# Test fixture: temporary SQLite database with required tables
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database file and clean up after test."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".db", delete=False) as f:
        db_path = f.name

    yield db_path

    # Cleanup
    Path(db_path).unlink(missing_ok=True)
    Path(f"{db_path}-wal").unlink(missing_ok=True)
    Path(f"{db_path}-shm").unlink(missing_ok=True)


@pytest.fixture
def seeded_db(temp_db_path, monkeypatch):
    """Set up a temporary database with screener_results and research_reports tables,
    and monkeypatch settings.database_url to point to it.
    """
    conn = sqlite3.connect(temp_db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    # Create screener_results table
    conn.execute("""
        CREATE TABLE screener_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            momentum_score REAL NOT NULL,
            quality_passed INTEGER NOT NULL,
            regime TEXT NOT NULL,
            position_size_multiplier REAL NOT NULL,
            screened_at TEXT NOT NULL,
            UNIQUE(symbol, run_date)
        )
    """)

    # Create research_reports table (exact columns from research_agent.py)
    conn.execute("""
        CREATE TABLE research_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_urls TEXT NOT NULL,
            earnings_transcript_unavailable INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            raw_response TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Create watchlist table
    conn.execute("""
        CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            run_date TEXT NOT NULL,
            combined_decision TEXT NOT NULL,
            scorecard_score INTEGER NOT NULL,
            scorecard_max INTEGER NOT NULL,
            sentiment TEXT NOT NULL,
            confidence REAL NOT NULL,
            rank INTEGER NOT NULL,
            regime TEXT NOT NULL,
            position_size_multiplier REAL NOT NULL,
            human_approved INTEGER NOT NULL DEFAULT 0,
            approval_source TEXT,
            added_at TEXT NOT NULL,
            UNIQUE(symbol, run_date)
        )
    """)

    # Create agent_logs table for logging
    conn.execute("""
        CREATE TABLE agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            level TEXT NOT NULL,
            action TEXT NOT NULL,
            symbol TEXT,
            result TEXT,
            data_quality_score REAL
        )
    """)

    conn.commit()
    conn.close()

    # Patch _resolve_db_path in watchlist_agent to return temp database path
    import src.agents.watchlist_agent as wa_module
    monkeypatch.setattr(wa_module, "_resolve_db_path", lambda: temp_db_path)

    # Initialize logging with the temp database
    setup_logging(temp_db_path)

    yield temp_db_path


# ---------------------------------------------------------------------------
# Helper functions to seed data
# ---------------------------------------------------------------------------


def insert_screener_result(
    db_path: str,
    symbol: str,
    rank: int,
    momentum_score: float,
    regime: str,
    position_size_multiplier: float,
    run_date: datetime.date = RUN_DATE,
    quality_passed: int = 1,
) -> None:
    """Insert a row into screener_results."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    conn.execute(
        """
        INSERT INTO screener_results
            (symbol, run_date, rank, momentum_score, quality_passed,
             regime, position_size_multiplier, screened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            rank,
            momentum_score,
            quality_passed,
            regime,
            position_size_multiplier,
            datetime.datetime.now(tz=IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def insert_research_report(
    db_path: str,
    symbol: str,
    sentiment: str,
    confidence: float,
    source_urls: list[str],
    earnings_transcript_unavailable: bool = False,
    completed_at: datetime.datetime | None = None,
    run_date: datetime.date = RUN_DATE,
) -> None:
    """Insert a row into research_reports."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    import json

    completed_at_str = (
        completed_at.isoformat() if completed_at is not None else None
    )

    conn.execute(
        """
        INSERT INTO research_reports
            (symbol, run_date, sentiment, confidence, source_urls,
             earnings_transcript_unavailable, completed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            run_date.isoformat(),
            sentiment,
            confidence,
            json.dumps(source_urls),
            1 if earnings_transcript_unavailable else 0,
            completed_at_str,
            datetime.datetime.now(tz=IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def read_watchlist_row(db_path: str, symbol: str, run_date: datetime.date):
    """Read a row from the watchlist table."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    cursor = conn.execute(
        """
        SELECT symbol, combined_decision, scorecard_score, scorecard_max,
               sentiment, confidence, rank, regime, position_size_multiplier,
               human_approved, approval_source
        FROM watchlist
        WHERE symbol = ? AND run_date = ?
        """,
        (symbol, run_date.isoformat()),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def count_watchlist_rows(db_path: str, run_date: datetime.date) -> int:
    """Count rows in watchlist for a given run_date."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    cursor = conn.execute(
        "SELECT COUNT(*) FROM watchlist WHERE run_date = ?",
        (run_date.isoformat(),),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Tests: 15 scenarios from spec Section 14
# ---------------------------------------------------------------------------


def test_01_happy_path_3_proceed_all_approved(seeded_db, monkeypatch):
    """Scenario 1: 3 PROCEED candidates, human approves all."""
    db_path = seeded_db

    # Seed: 3 screener results with Positive sentiment research
    now = datetime.datetime.now(tz=IST)
    for i, symbol in enumerate(["HDFC", "TCS", "INFY"], start=1):
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )
        insert_research_report(
            db_path,
            symbol,
            sentiment="Positive",
            confidence=0.9,
            source_urls=["https://example.com/1"],
            completed_at=now,
        )

    # Patch notifiers
    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        result = run_watchlist_agent(run_date=RUN_DATE)

    # Assertions
    assert result.proceed_count == 3
    assert result.skipped_count == 0
    assert result.candidates_evaluated == 3
    assert set(result.approved_symbols) == {"HDFC", "TCS", "INFY"}
    assert result.human_responded is False

    # All 3 rows written with human_approved=0, approval_source=NULL
    for symbol in ["HDFC", "TCS", "INFY"]:
        row = read_watchlist_row(db_path, symbol, RUN_DATE)
        assert row is not None
        assert row[1] == "PROCEED"  # combined_decision
        assert row[9] == 0  # human_approved = 0

    # Now human approves all three
    for symbol in ["HDFC", "TCS", "INFY"]:
        record_human_approval(symbol, RUN_DATE, approved=True)

    # Verify records updated
    for symbol in ["HDFC", "TCS", "INFY"]:
        row = read_watchlist_row(db_path, symbol, RUN_DATE)
        assert row[9] == 1  # human_approved = 1
        assert row[10] == "human_explicit"  # approval_source


def test_02_negative_sentiment_blocks_one(seeded_db, monkeypatch):
    """Scenario 2: 2 Positive + 1 Negative → proceed_count=2, Negative blocked."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)
    symbols = ["HDFC", "TCS", "INFY"]
    sentiments = ["Positive", "Positive", "Negative"]

    for i, (symbol, sentiment) in enumerate(zip(symbols, sentiments), start=1):
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )
        insert_research_report(
            db_path,
            symbol,
            sentiment=sentiment,
            confidence=0.9 if sentiment == "Positive" else 0.8,
            source_urls=["https://example.com/1"],
            completed_at=now,
        )

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        result = run_watchlist_agent(run_date=RUN_DATE)

    assert result.proceed_count == 2
    assert result.skipped_count == 1
    assert result.candidates_evaluated == 3

    # INFY should be SKIP with reason "negative_sentiment"
    row = read_watchlist_row(db_path, "INFY", RUN_DATE)
    assert row[1] == "SKIP"  # combined_decision


def test_03_research_incomplete(seeded_db, monkeypatch):
    """Scenario 3: 2 screener rows but only 1 matching research → 1 candidate."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    # Insert 2 screener results
    for i, symbol in enumerate(["HDFC", "TCS"], start=1):
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )

    # Insert research for only the first
    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        result = run_watchlist_agent(run_date=RUN_DATE)

    # Only 1 candidate evaluated (TCS skipped due to missing research)
    assert result.candidates_evaluated == 1
    assert result.proceed_count == 1

    # Check that TCS is NOT in watchlist
    row = read_watchlist_row(db_path, "TCS", RUN_DATE)
    assert row is None


def test_04_regime_blocked(seeded_db, monkeypatch):
    """Scenario 4: position_size_multiplier=0.0 → SKIP even with Positive sentiment."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    # Insert screener result with regime_blocked (multiplier=0.0)
    insert_screener_result(
        db_path,
        "HDFC",
        rank=1,
        momentum_score=10.0,
        regime="BELOW_200DMA_10DAYS",
        position_size_multiplier=0.0,
    )

    # Insert Positive research
    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint, \
         patch("src.agents.watchlist_agent.send_info") as mock_info:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}
        mock_info.return_value = {"telegram": True, "gmail": False}

        result = run_watchlist_agent(run_date=RUN_DATE)

    # No PROCEED candidates, send_info called instead
    assert result.proceed_count == 0
    assert result.skipped_count == 1
    mock_info.assert_called_once()

    # Verify watchlist row is SKIP
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[1] == "SKIP"  # combined_decision


def test_05_no_completed_research_today(seeded_db, monkeypatch):
    """Scenario 5: All research_reports.completed_at are NULL → 0 candidates."""
    db_path = seeded_db

    # Insert screener results
    for i, symbol in enumerate(["HDFC", "TCS"], start=1):
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )

    # Insert research reports but with completed_at=NULL
    for symbol in ["HDFC", "TCS"]:
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        for pragma in _WAL_PRAGMAS:
            conn.execute(pragma)

        import json

        conn.execute(
            """
            INSERT INTO research_reports
                (symbol, run_date, sentiment, confidence, source_urls,
                 earnings_transcript_unavailable, completed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                RUN_DATE.isoformat(),
                "Positive",
                0.9,
                json.dumps(["https://example.com/1"]),
                0,
                None,  # completed_at IS NULL
                datetime.datetime.now(tz=IST).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    with patch("src.agents.watchlist_agent.send_info") as mock_info:
        mock_info.return_value = {"telegram": True, "gmail": False}

        result = run_watchlist_agent(run_date=RUN_DATE)

    # No candidates (research not completed)
    assert result.candidates_evaluated == 0
    assert result.proceed_count == 0
    mock_info.assert_called_once()


def test_06_check_watchlist_timeout_no_response(seeded_db, monkeypatch):
    """Scenario 6: 2 PROCEED rows written, check_watchlist_timeout marks both timed out."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    # Insert screener + research
    for i, symbol in enumerate(["HDFC", "TCS"], start=1):
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )
        insert_research_report(
            db_path,
            symbol,
            sentiment="Positive",
            confidence=0.9,
            source_urls=["https://example.com/1"],
            completed_at=now,
        )

    # Run watchlist agent to write rows
    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        result = run_watchlist_agent(run_date=RUN_DATE)

    assert result.proceed_count == 2

    # Now call check_watchlist_timeout
    with patch("src.agents.watchlist_agent.send_alert") as mock_alert:
        check_watchlist_timeout(RUN_DATE)

    # Verify both rows updated to approval_source='timeout_skip'
    for symbol in ["HDFC", "TCS"]:
        row = read_watchlist_row(db_path, symbol, RUN_DATE)
        assert row[10] == "timeout_skip"  # approval_source

    # send_alert should have been called
    mock_alert.assert_called_once()


def test_07_check_watchlist_timeout_partial_response(seeded_db, monkeypatch):
    """Scenario 7: 2 PROCEED rows, approve first, then check_watchlist_timeout."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    # Insert 2 screener + research
    for i, symbol in enumerate(["HDFC", "TCS"], start=1):
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )
        insert_research_report(
            db_path,
            symbol,
            sentiment="Positive",
            confidence=0.9,
            source_urls=["https://example.com/1"],
            completed_at=now,
        )

    # Run watchlist agent
    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        run_watchlist_agent(run_date=RUN_DATE)

    # Approve first symbol
    record_human_approval("HDFC", RUN_DATE, approved=True)

    # Verify first approved
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[10] == "human_explicit"

    # Call check_watchlist_timeout
    with patch("src.agents.watchlist_agent.send_alert"), \
         patch("src.agents.watchlist_agent.log_agent_action"):
        check_watchlist_timeout(RUN_DATE)

    # Second row should be timeout_skip
    row = read_watchlist_row(db_path, "TCS", RUN_DATE)
    assert row[10] == "timeout_skip"

    # First should remain human_explicit
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[10] == "human_explicit"


def test_08_record_human_approval_single_symbol(seeded_db, monkeypatch):
    """Scenario 8: record_human_approval sets human_approved=1, approval_source='human_explicit'."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    insert_screener_result(
        db_path,
        "HDFC",
        rank=1,
        momentum_score=10.0,
        regime="ABOVE_200DMA",
        position_size_multiplier=1.0,
    )
    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    # Run watchlist agent
    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        run_watchlist_agent(run_date=RUN_DATE)

    # Approve the symbol
    with patch("src.agents.watchlist_agent.log_agent_action"):
        record_human_approval("HDFC", RUN_DATE, approved=True)

    # Verify
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[9] == 1  # human_approved
    assert row[10] == "human_explicit"  # approval_source


def test_09_record_human_approval_symbol_not_found(seeded_db, monkeypatch):
    """Scenario 9: record_human_approval with non-existent symbol → no error, WARNING logged."""

    # Call record_human_approval for symbol not in watchlist
    with patch("src.agents.watchlist_agent.log_agent_action") as mock_log:
        record_human_approval("NONEXISTENT", RUN_DATE, approved=True)

    # Verify no exception raised, WARNING logged
    assert mock_log.call_count >= 1
    # Check that a WARNING was logged
    warning_calls = [
        call for call in mock_log.call_args_list
        if call.kwargs.get("level") == "WARNING"
    ]
    assert len(warning_calls) > 0


def test_10_scorecard_rank_points(seeded_db, monkeypatch):
    """Scenario 10: rank=2 → 5 points; rank=4 → 0 points."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    # Insert two screener results with different ranks
    insert_screener_result(
        db_path,
        "HDFC",
        rank=2,
        momentum_score=9.0,
        regime="ABOVE_200DMA",
        position_size_multiplier=1.0,
    )
    insert_screener_result(
        db_path,
        "TCS",
        rank=4,
        momentum_score=7.0,
        regime="ABOVE_200DMA",
        position_size_multiplier=1.0,
    )

    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )
    insert_research_report(
        db_path,
        "TCS",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        run_watchlist_agent(run_date=RUN_DATE)

    # HDFC: rank 2 → quality(5) + rank(5) + regime(5) + sentiment(5) = 20
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[2] == 20  # scorecard_score

    # TCS: rank 4 → quality(5) + rank(0) + regime(5) + sentiment(5) = 15
    row = read_watchlist_row(db_path, "TCS", RUN_DATE)
    assert row[2] == 15  # scorecard_score


def test_11_scorecard_earnings_flag_reduces_max(seeded_db, monkeypatch):
    """Scenario 11: earnings_transcript_unavailable=True → scorecard_max=15."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    insert_screener_result(
        db_path,
        "HDFC",
        rank=1,
        momentum_score=10.0,
        regime="ABOVE_200DMA",
        position_size_multiplier=1.0,
    )

    # Insert research with earnings_transcript_unavailable=True
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)

    import json

    conn.execute(
        """
        INSERT INTO research_reports
            (symbol, run_date, sentiment, confidence, source_urls,
             earnings_transcript_unavailable, completed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "HDFC",
            RUN_DATE.isoformat(),
            "Positive",
            0.9,
            json.dumps(["https://example.com/1"]),
            1,  # earnings_transcript_unavailable = True
            now.isoformat(),
            datetime.datetime.now(tz=IST).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        run_watchlist_agent(run_date=RUN_DATE)

    # scorecard_max should be 15 (not 20)
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[3] == 15  # scorecard_max


def test_12_scorecard_below_200dma_regime(seeded_db, monkeypatch):
    """Scenario 12: regime="BELOW_200DMA" → regime points = 2."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    insert_screener_result(
        db_path,
        "HDFC",
        rank=4,  # 0 rank points
        momentum_score=7.0,
        regime="BELOW_200DMA",
        position_size_multiplier=0.5,
    )
    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        run_watchlist_agent(run_date=RUN_DATE)

    # Scorecard: quality(5) + rank(0) + regime(2) + sentiment(5) = 12
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[2] == 12  # scorecard_score


def test_13_db_write_failure_raises_error(seeded_db, monkeypatch):
    """Scenario 13: DB write failure → WatchlistAgentError(phase='db_write')."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    insert_screener_result(
        db_path,
        "HDFC",
        rank=1,
        momentum_score=10.0,
        regime="ABOVE_200DMA",
        position_size_multiplier=1.0,
    )
    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Positive",
        confidence=0.9,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    # Create a wrapper class to intercept execute calls
    class FailingConnection:
        def __init__(self, real_conn):
            self._conn = real_conn
            self._in_write_phase = False

        def execute(self, sql, *args, **kwargs):
            # Mark write phase when we see BEGIN
            if sql.strip() == "BEGIN":
                self._in_write_phase = True
            # Fail on INSERT into watchlist during write phase
            if self._in_write_phase and "INSERT OR REPLACE INTO watchlist" in sql:
                raise sqlite3.Error("Simulated DB write error")
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    import src.agents.watchlist_agent as wa_module
    original_open_connection = wa_module._open_connection

    def mock_open_connection(db_path_arg):
        real_conn = original_open_connection(db_path_arg)
        return FailingConnection(real_conn)

    with patch.object(wa_module, "_open_connection", mock_open_connection):
        with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
            mock_checkpoint.return_value = {"telegram": True, "gmail": True}

            with pytest.raises(WatchlistAgentError) as exc_info:
                run_watchlist_agent(run_date=RUN_DATE)

            assert exc_info.value.phase == "db_write"


def test_14_mixed_sentiment_proceeds_with_1_point(seeded_db, monkeypatch):
    """Scenario 14: sentiment='Mixed' → PROCEED with 1 scorecard point."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    insert_screener_result(
        db_path,
        "HDFC",
        rank=1,
        momentum_score=10.0,
        regime="ABOVE_200DMA",
        position_size_multiplier=1.0,
    )
    insert_research_report(
        db_path,
        "HDFC",
        sentiment="Mixed",
        confidence=0.5,
        source_urls=["https://example.com/1"],
        completed_at=now,
    )

    with patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint:
        mock_checkpoint.return_value = {"telegram": True, "gmail": True}

        result = run_watchlist_agent(run_date=RUN_DATE)

    assert result.proceed_count == 1

    # Scorecard: quality(5) + rank(5) + regime(5) + sentiment(1) = 16
    row = read_watchlist_row(db_path, "HDFC", RUN_DATE)
    assert row[1] == "PROCEED"  # combined_decision
    assert row[2] == 16  # scorecard_score


def test_15_all_candidates_skip_no_checkpoint_sent(seeded_db, monkeypatch):
    """Scenario 15: All 5 candidates SKIP → proceed_count=0, send_info called."""
    db_path = seeded_db

    now = datetime.datetime.now(tz=IST)

    # Insert 5 screener + negative research
    for i in range(1, 6):
        symbol = f"SYM{i}"
        insert_screener_result(
            db_path,
            symbol,
            rank=i,
            momentum_score=10.0 - i,
            regime="ABOVE_200DMA",
            position_size_multiplier=1.0,
        )
        insert_research_report(
            db_path,
            symbol,
            sentiment="Negative",
            confidence=0.85,
            source_urls=["https://example.com/1"],
            completed_at=now,
        )

    with patch("src.agents.watchlist_agent.send_info") as mock_info, \
         patch("src.agents.watchlist_agent.send_checkpoint") as mock_checkpoint, \
         patch("src.agents.watchlist_agent.log_agent_action"):
        mock_info.return_value = {"telegram": True, "gmail": False}

        result = run_watchlist_agent(run_date=RUN_DATE)

    # No PROCEED candidates
    assert result.proceed_count == 0
    assert result.skipped_count == 5
    assert result.candidates_evaluated == 5

    # send_info called, send_checkpoint NOT called
    mock_info.assert_called_once()
    mock_checkpoint.assert_not_called()

    # WatchlistAgentError should NOT be raised
    # (test passes if no exception)
