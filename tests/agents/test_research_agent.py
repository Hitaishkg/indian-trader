"""Tests for src/agents/research_agent.py.

Covers all 10 test hints from docs/specs/2026-03-30-research-agent.md.
All external dependencies mocked: Brave HTTP, Gemini SDK, SQLite, time.sleep.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from unittest.mock import MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest

from src.agents.research_agent import (
    AGENT_NAME,
    BRAVE_NEWS_ENDPOINT,
    BRAVE_RESULTS_COUNT,
    BRAVE_TIMEOUT,
    EARNINGS_AGE_LIMIT_DAYS,
    EARNINGS_KEYWORDS,
    FALLBACK_CONFIDENCE,
    FALLBACK_SENTIMENT,
    GEMINI_MODEL,
    GEMINI_QUOTA_RETRY_DELAY,
    IST,
    MAX_SYMBOLS,
    ResearchAgentError,
    ResearchAgentResult,
    StockResearch,
    SYMBOL_TO_COMPANY,
    TRANSCRIPT_MIN_CHARS,
    VALID_SENTIMENTS,
    run_research_agent,
)

# Test data helpers


def make_brave_article(
    title: str,
    description: str,
    url: str,
    age: str = "2 days ago",
) -> dict:
    """Create a fake Brave article response."""
    return {
        "title": title,
        "description": description,
        "url": url,
        "age": age,
    }


def make_brave_response(articles: list[dict]) -> MagicMock:
    """Create a fake Brave HTTP response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": articles}
    return mock_resp


def make_gemini_response(text: str) -> MagicMock:
    """Create a fake Gemini response object."""
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


@pytest.fixture
def in_memory_db():
    """Create an in-memory SQLite database with research_reports table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA cache_size=-64000;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    # Create research_reports table
    sql = """
    CREATE TABLE IF NOT EXISTS research_reports (
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
    );
    """
    conn.execute(sql)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date "
        "ON research_reports(symbol, run_date);"
    )
    conn.commit()

    yield conn

    conn.close()


@pytest.fixture
def mock_settings():
    """Mock settings with required fields."""
    mock_set = MagicMock()
    mock_set.brave_api_key = "test-brave-key"
    mock_set.gemini_api_key = "test-gemini-key"
    mock_set.database_url = "sqlite:///data/trading.db"
    return mock_set


@pytest.fixture(autouse=True)
def patch_log_agent_action():
    """Mock log_agent_action to avoid DB dependency."""
    with patch("src.agents.research_agent.log_agent_action") as mock_log:
        yield mock_log


# ============================================================================
# Test 1: Valid sentiment values only
# ============================================================================


def test_invalid_sentiment_retry_fallback(mock_settings, in_memory_db):
    """Test 1: Invalid sentiment triggers retry, then fallback to Neutral/0.3."""
    run_date = datetime.date(2026, 3, 30)

    # Mock articles without earnings keywords to avoid 4th Brave query
    articles = [
        make_brave_article(
            title="TCS performance article",
            description="Strong performance reported",
            url="https://example.com/tcs-article",
            age="2 days ago",
        )
    ]

    # First Gemini call returns invalid sentiment "Bullish"
    gemini_invalid_response = '{"sentiment": "Bullish", "confidence": 0.8, "source_urls": []}'
    # Second Gemini call (retry) returns valid
    gemini_valid_response = '{"sentiment": "Neutral", "confidence": 0.5, "source_urls": []}'

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # Mock Brave responses (3 queries, no earnings so no 4th)
        mock_get.side_effect = [
            make_brave_response(articles),
            make_brave_response([]),
            make_brave_response([]),
        ]

        # Mock Gemini client
        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini

        # First generate_content returns invalid sentiment
        # Second returns valid JSON
        response1 = make_gemini_response(gemini_invalid_response)
        response2 = make_gemini_response(gemini_valid_response)
        mock_gemini.models.generate_content.side_effect = [response1, response2]

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        assert result.stocks_researched == 1
        assert len(result.results) == 1
        assert result.results[0].sentiment == "Neutral"
        assert result.results[0].confidence == 0.5


# ============================================================================
# Test 2: completed_at is NULL on Gemini failure
# ============================================================================


def test_completed_at_null_on_gemini_failure(mock_settings):
    """Test 2: Gemini failure leaves completed_at=NULL and symbol in skipped_symbols."""
    run_date = datetime.date(2026, 3, 30)

    # Create test DB
    test_db = sqlite3.connect(":memory:")
    test_db.execute("PRAGMA journal_mode=WAL;")
    test_db.execute("PRAGMA busy_timeout=30000;")
    test_db.execute("PRAGMA cache_size=-64000;")
    test_db.execute("PRAGMA synchronous=NORMAL;")
    sql = """
    CREATE TABLE IF NOT EXISTS research_reports (
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
    );
    """
    test_db.execute(sql)
    test_db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date "
        "ON research_reports(symbol, run_date);"
    )
    test_db.commit()

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=test_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # Mock Brave responses (3 queries succeed)
        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        # Mock Gemini to raise exception both times
        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.side_effect = RuntimeError("Network Error")

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        # Stock should be in skipped_symbols
        assert result.stocks_researched == 0
        assert "TCS" in result.skipped_symbols


# ============================================================================
# Test 3: Earnings detection triggers transcript branch
# ============================================================================


def test_earnings_detection_triggers_transcript_query(mock_settings, in_memory_db):
    """Test 3: Earnings keywords in recent articles trigger 4th Brave query."""
    run_date = datetime.date(2026, 3, 30)

    # Article with earnings keyword and recent date
    earnings_article = make_brave_article(
        title="TCS Q3 results quarterly profit announcement",
        description="TCS posts strong Q3 earnings",
        url="https://example.com/tcs-q3",
        age="3 days ago",
    )

    transcript_article = make_brave_article(
        title="TCS Q3 Earnings Transcript",
        description="Earnings call transcript with 500+ characters of content. " * 5,
        url="https://example.com/transcript",
        age="2 days ago",
    )

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep") as mock_sleep:

        # Mock 4 Brave requests: 3 standard + 1 transcript
        mock_get.side_effect = [
            make_brave_response([earnings_article]),
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([transcript_article]),  # 4th request for transcript
        ]

        # Mock Gemini
        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            '{"sentiment": "Positive", "confidence": 0.9, "source_urls": []}'
        )

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        # Should have 4 Brave requests
        assert mock_get.call_count == 4

        # Should have 4 sleep calls (before each request)
        assert mock_sleep.call_count == 4


# ============================================================================
# Test 4: Earnings transcript unavailable fallback
# ============================================================================


def test_earnings_transcript_unavailable_fallback(mock_settings):
    """Test 4: Transcript with <200 chars sets earnings_transcript_unavailable=True."""
    run_date = datetime.date(2026, 3, 30)

    earnings_article = make_brave_article(
        title="TCS Q3 results",
        description="Quarterly earnings profit",
        url="https://example.com/tcs",
        age="3 days ago",
    )

    # Transcript with insufficient content (less than 200 chars)
    short_transcript = make_brave_article(
        title="Transcript",
        description="Short",
        url="https://example.com/short",
        age="2 days ago",
    )

    # Create test DB
    test_db = sqlite3.connect(":memory:")
    test_db.execute("PRAGMA journal_mode=WAL;")
    test_db.execute("PRAGMA busy_timeout=30000;")
    test_db.execute("PRAGMA cache_size=-64000;")
    test_db.execute("PRAGMA synchronous=NORMAL;")
    sql = """
    CREATE TABLE IF NOT EXISTS research_reports (
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
    );
    """
    test_db.execute(sql)
    test_db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date "
        "ON research_reports(symbol, run_date);"
    )
    test_db.commit()

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=test_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # 4 Brave requests: 3 standard (with earnings) + 1 transcript (short)
        mock_get.side_effect = [
            make_brave_response([earnings_article]),
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([short_transcript]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            '{"sentiment": "Neutral", "confidence": 0.5, "source_urls": []}'
        )

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        assert result.stocks_researched == 1
        assert result.results[0].earnings_transcript_unavailable is True


# ============================================================================
# Test 5: Rate limiting delay between Brave calls
# ============================================================================


def test_rate_limiting_sleep_between_brave_calls(mock_settings, in_memory_db):
    """Test 5: Verify BRAVE_REQUEST_DELAY sleeps between each Brave request."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep") as mock_sleep:

        # 3 standard Brave responses
        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            '{"sentiment": "Neutral", "confidence": 0.3, "source_urls": []}'
        )

        run_research_agent(run_date=run_date, symbols=["TCS"])

        # For 1 stock with no earnings: 3 Brave queries + 3 sleeps (before each)
        assert mock_sleep.call_count == 3
        # Each sleep should be BRAVE_REQUEST_DELAY (1.1 seconds)
        for call_obj in mock_sleep.call_args_list:
            assert call_obj[0][0] == 1.1


# ============================================================================
# Test 6: Gemini invalid JSON retry then fallback
# ============================================================================


def test_gemini_invalid_json_retry_then_fallback(mock_settings, in_memory_db):
    """Test 6: Invalid JSON from Gemini retries once, then uses fallback."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini

        # First response: not valid JSON
        # Second response: still not valid JSON
        mock_gemini.models.generate_content.side_effect = [
            make_gemini_response("I think positive"),
            make_gemini_response("still not json"),
        ]

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        assert result.stocks_researched == 1
        assert result.results[0].sentiment == FALLBACK_SENTIMENT
        assert result.results[0].confidence == FALLBACK_CONFIDENCE
        assert result.results[0].source_urls == []


# ============================================================================
# Test 7: symbols override bypasses screener_results
# ============================================================================


def test_symbols_override_bypasses_screener_results(mock_settings, in_memory_db):
    """Test 7: Passing symbols parameter skips DB read of screener_results."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results") as mock_read, \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # Mock Brave responses
        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.side_effect = [
            make_gemini_response('{"sentiment": "Neutral", "confidence": 0.3, "source_urls": []}'),
            make_gemini_response('{"sentiment": "Neutral", "confidence": 0.3, "source_urls": []}'),
        ]

        result = run_research_agent(run_date=run_date, symbols=["TCS", "INFY"])

        # _read_screener_results should NOT be called
        mock_read.assert_not_called()

        # Both symbols should be researched
        assert result.stocks_researched == 2
        assert len(result.results) == 2


# ============================================================================
# Test 8: Empty screener results
# ============================================================================


def test_empty_screener_results(mock_settings, in_memory_db):
    """Test 8: No screener results returns empty results without error."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=[]):

        result = run_research_agent(run_date=run_date)

        assert result.stocks_researched == 0
        assert result.results == []
        assert result.skipped_symbols == []


# ============================================================================
# Test 9: DB write order -- completed_at is last
# ============================================================================


def test_db_write_order_completed_at_last(mock_settings):
    """Test 9: completed_at is NULL after INSERT, NOT NULL after UPDATE."""
    run_date = datetime.date(2026, 3, 30)

    # Create test DB
    test_db = sqlite3.connect(":memory:")
    test_db.execute("PRAGMA journal_mode=WAL;")
    test_db.execute("PRAGMA busy_timeout=30000;")
    test_db.execute("PRAGMA cache_size=-64000;")
    test_db.execute("PRAGMA synchronous=NORMAL;")
    sql = """
    CREATE TABLE IF NOT EXISTS research_reports (
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
    );
    """
    test_db.execute(sql)
    test_db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date "
        "ON research_reports(symbol, run_date);"
    )
    test_db.commit()

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=test_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            '{"sentiment": "Positive", "confidence": 0.8, "source_urls": []}'
        )

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        # Verify StockResearch has completed_at set
        assert result.stocks_researched == 1
        assert result.results[0].completed_at is not None


# ============================================================================
# Test 10: source_urls JSON round-trip
# ============================================================================


def test_source_urls_json_round_trip(mock_settings):
    """Test 10: source_urls stored as JSON in DB, returned as list in StockResearch."""
    run_date = datetime.date(2026, 3, 30)

    urls = ["https://a.com", "https://b.com"]

    # Create test DB
    test_db = sqlite3.connect(":memory:")
    test_db.execute("PRAGMA journal_mode=WAL;")
    test_db.execute("PRAGMA busy_timeout=30000;")
    test_db.execute("PRAGMA cache_size=-64000;")
    test_db.execute("PRAGMA synchronous=NORMAL;")
    sql = """
    CREATE TABLE IF NOT EXISTS research_reports (
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
    );
    """
    test_db.execute(sql)
    test_db.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date "
        "ON research_reports(symbol, run_date);"
    )
    test_db.commit()

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=test_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # Mock Brave responses (3 queries, no earnings)
        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        gemini_response_json = json.dumps(
            {
                "sentiment": "Positive",
                "confidence": 0.8,
                "source_urls": urls,
            }
        )
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            gemini_response_json
        )

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        # StockResearch should have list of URLs
        assert result.results[0].source_urls == urls


# ============================================================================
# Additional tests: Brave API key missing, Gemini quota retry, confidence clamping, MAX_SYMBOLS
# ============================================================================


def test_brave_api_key_missing(mock_settings):
    """Test: BRAVE_API_KEY missing raises ResearchAgentError."""
    mock_settings.brave_api_key = None

    with patch("src.agents.research_agent.settings", mock_settings):
        with pytest.raises(ResearchAgentError) as exc_info:
            run_research_agent(symbols=["TCS"])

        assert exc_info.value.phase == "brave_search"
        assert "BRAVE_API_KEY" in exc_info.value.message


def test_gemini_quota_retry(mock_settings, in_memory_db):
    """Test 12: Gemini 429/quota error sleeps 60s, retries, succeeds."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep") as mock_sleep:

        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini

        # First call: raise 429 error
        # Second call (after sleep): succeed
        mock_gemini.models.generate_content.side_effect = [
            Exception("HTTP 429 ResourceExhausted"),
            make_gemini_response('{"sentiment": "Positive", "confidence": 0.8, "source_urls": []}'),
        ]

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        assert result.stocks_researched == 1
        assert result.results[0].sentiment == "Positive"

        # sleep should be called with GEMINI_QUOTA_RETRY_DELAY (60 seconds)
        # Note: 3 Brave sleeps + 1 Gemini sleep
        gemini_sleep_call = [c for c in mock_sleep.call_args_list if c[0][0] == 60]
        assert len(gemini_sleep_call) == 1


def test_confidence_clamping(mock_settings, in_memory_db):
    """Test 13: Confidence > 1.0 is clamped to 1.0."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=["TCS"]), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        mock_get.side_effect = [
            make_brave_response([]),
            make_brave_response([]),
            make_brave_response([]),
        ]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            '{"sentiment": "Positive", "confidence": 1.5, "source_urls": []}'
        )

        result = run_research_agent(run_date=run_date, symbols=["TCS"])

        # Confidence should be clamped to 1.0
        assert result.results[0].confidence == 1.0


def test_max_symbols_cap(mock_settings, in_memory_db):
    """Test 14: Only first 5 symbols are processed even if more provided."""
    run_date = datetime.date(2026, 3, 30)

    symbols = ["TCS", "INFY", "HDFCBANK", "RELIANCE", "SBIN", "ITC", "M&M"]

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # 5 symbols * 3 Brave queries = 15 mocked responses
        mock_get.side_effect = [make_brave_response([]) for _ in range(15)]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.return_value = make_gemini_response(
            '{"sentiment": "Neutral", "confidence": 0.3, "source_urls": []}'
        )

        result = run_research_agent(run_date=run_date, symbols=symbols)

        # Only 5 symbols should be researched
        assert result.stocks_researched == 5


# ============================================================================
# Edge case tests
# ============================================================================


def test_empty_symbols_list(mock_settings, in_memory_db):
    """Test: Empty symbols list returns empty result."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db):

        result = run_research_agent(run_date=run_date, symbols=[])

        assert result.stocks_researched == 0
        assert result.results == []


def test_run_date_defaults_to_today_ist(mock_settings, in_memory_db):
    """Test: run_date defaults to today in IST when not provided."""
    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent._read_screener_results", return_value=[]):

        result = run_research_agent()

        # Should return today's date in IST
        today_ist = datetime.datetime.now(tz=IST).date()
        assert result.run_date == today_ist


def test_sentiment_values_valid(mock_settings):
    """Test: All four valid sentiments are accepted."""
    run_date = datetime.date(2026, 3, 30)

    sentiments = ["Positive", "Negative", "Neutral", "Mixed"]

    for i, sentiment in enumerate(sentiments):
        # Create a fresh in-memory DB for each iteration
        test_db = sqlite3.connect(":memory:")
        test_db.execute("PRAGMA journal_mode=WAL;")
        test_db.execute("PRAGMA busy_timeout=30000;")
        test_db.execute("PRAGMA cache_size=-64000;")
        test_db.execute("PRAGMA synchronous=NORMAL;")
        sql = """
        CREATE TABLE IF NOT EXISTS research_reports (
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
        );
        """
        test_db.execute(sql)
        test_db.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_reports_symbol_date "
            "ON research_reports(symbol, run_date);"
        )
        test_db.commit()

        with patch("src.agents.research_agent.settings", mock_settings), \
             patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
             patch("src.agents.research_agent._open_connection", return_value=test_db), \
             patch("src.agents.research_agent._read_screener_results", return_value=[f"STOCK{i}"]), \
             patch("src.agents.research_agent.requests.get") as mock_get, \
             patch("src.agents.research_agent.genai.Client") as mock_client_class, \
             patch("src.agents.research_agent.time.sleep"):

            mock_get.side_effect = [
                make_brave_response([]),
                make_brave_response([]),
                make_brave_response([]),
            ]

            mock_gemini = MagicMock()
            mock_client_class.return_value = mock_gemini
            mock_gemini.models.generate_content.return_value = make_gemini_response(
                json.dumps({
                    "sentiment": sentiment,
                    "confidence": 0.7,
                    "source_urls": []
                })
            )

            result = run_research_agent(run_date=run_date, symbols=[f"STOCK{i}"])

            assert result.results[0].sentiment == sentiment

        test_db.close()


def test_multiple_stocks_sequential(mock_settings, in_memory_db):
    """Test: Multiple stocks are processed sequentially."""
    run_date = datetime.date(2026, 3, 30)

    with patch("src.agents.research_agent.settings", mock_settings), \
         patch("src.agents.research_agent._resolve_db_path", return_value=":memory:"), \
         patch("src.agents.research_agent._open_connection", return_value=in_memory_db), \
         patch("src.agents.research_agent.requests.get") as mock_get, \
         patch("src.agents.research_agent.genai.Client") as mock_client_class, \
         patch("src.agents.research_agent.time.sleep"):

        # 2 stocks * 3 Brave queries = 6 responses
        mock_get.side_effect = [make_brave_response([]) for _ in range(6)]

        mock_gemini = MagicMock()
        mock_client_class.return_value = mock_gemini
        mock_gemini.models.generate_content.side_effect = [
            make_gemini_response('{"sentiment": "Positive", "confidence": 0.8, "source_urls": []}'),
            make_gemini_response('{"sentiment": "Negative", "confidence": 0.7, "source_urls": []}'),
        ]

        result = run_research_agent(run_date=run_date, symbols=["TCS", "INFY"])

        assert result.stocks_researched == 2
        assert result.results[0].symbol == "TCS"
        assert result.results[0].sentiment == "Positive"
        assert result.results[1].symbol == "INFY"
        assert result.results[1].sentiment == "Negative"
