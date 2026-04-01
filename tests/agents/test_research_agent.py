"""Tests for src/agents/research_agent.py (Tavily version).

Tests the Research Agent's Tavily integration, Gemini synthesis,
earnings detection, and database write patterns.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from src.agents.research_agent import (
    run_research_agent,
    ResearchAgentError,
    StockResearch,
    ResearchAgentResult,
    _detect_earnings,
    _fetch_tavily_news,
    _format_articles_for_prompt,
    _parse_gemini_response,
    SYMBOL_TO_COMPANY,
    TAVILY_REQUEST_DELAY,
)


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


def make_tavily_result(
    title: str = "Test Article",
    url: str = "https://example.com/article-1",
    content: str = "Some news content about the stock.",
    score: float = 0.9,
    published_date: str = "2026-03-30",
) -> dict:
    """Create a mock Tavily search result dict."""
    return {
        "title": title,
        "url": url,
        "content": content,
        "score": score,
        "published_date": published_date,
    }


def make_tavily_response(results: list[dict]) -> dict:
    """Create a mock Tavily search response dict."""
    return {
        "results": results,
        "query": "test",
        "response_time": 0.5,
    }


@pytest.fixture
def temp_db():
    """Create a temporary in-memory SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield str(db_path)


@pytest.fixture
def mock_settings():
    """Create a mock settings object."""
    settings = MagicMock()
    settings.tavily_api_key = "test-tavily-key-12345"
    settings.gemini_api_key = "test-gemini-key-12345"
    settings.database_url = "sqlite:///data/trading.db"
    return settings


@pytest.fixture
def mock_gemini_client():
    """Create a mock Gemini client."""
    client = MagicMock()
    return client


@pytest.fixture
def mock_tavily_client():
    """Create a mock Tavily client."""
    client = MagicMock()
    return client


# ---------------------------------------------------------------------------
# Test 1: Tavily client created with correct API key
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
def test_tavily_client_created_with_api_key(
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify TavilyClient is instantiated with the correct API key from settings."""
    # Setup
    mock_settings.tavily_api_key = "test-tavily-key-12345"
    mock_settings.gemini_api_key = "test-gemini-key-12345"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    mock_genai_response.text = json.dumps({
        "sentiment": "Neutral",
        "confidence": 0.3,
        "source_urls": []
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute
    run_research_agent(symbols=["TCS"])

    # Verify TavilyClient was created with the correct API key
    mock_tavily_cls.assert_called_once_with(api_key="test-tavily-key-12345")


# ---------------------------------------------------------------------------
# Test 2: Three Tavily calls per stock with correct parameters
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent.time.sleep")
def test_three_tavily_calls_with_correct_parameters(
    mock_sleep,
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify 3 Tavily queries per stock with correct topic/time_range/max_results."""
    # Setup
    mock_settings.tavily_api_key = "test-tavily-key-12345"
    mock_settings.gemini_api_key = "test-gemini-key-12345"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([
        make_tavily_result(title="News 1", url="https://example.com/1")
    ])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    mock_genai_response.text = json.dumps({
        "sentiment": "Neutral",
        "confidence": 0.5,
        "source_urls": ["https://example.com/1"]
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute
    run_research_agent(symbols=["TCS"])

    # Verify 3 calls to search with correct parameters
    assert mock_tavily_instance.search.call_count >= 3

    calls = mock_tavily_instance.search.call_args_list

    # Query 1: stock news
    assert calls[0][1]["query"] == "TCS stock news India"
    assert calls[0][1]["topic"] == "news"
    assert calls[0][1]["time_range"] == "week"
    assert calls[0][1]["max_results"] == 10

    # Query 2: earnings/results
    assert calls[1][1]["query"] == "TCS NSE quarterly results earnings"
    assert calls[1][1]["topic"] == "finance"
    assert calls[1][1]["time_range"] == "week"
    assert calls[1][1]["max_results"] == 10

    # Query 3: business outlook
    assert calls[2][1]["query"] == "Tata Consultancy Services business outlook"
    assert calls[2][1]["topic"] == "news"
    assert calls[2][1]["time_range"] == "week"
    assert calls[2][1]["max_results"] == 10


# ---------------------------------------------------------------------------
# Test 3: published_date not earnings trigger (4 days old)
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.datetime")
def test_published_date_not_earnings_trigger(mock_datetime):
    """Verify published_date 4 days old is within limit but earnings detection works."""
    # Setup: today = 2026-04-01, article = 2026-03-28 (4 days old)
    mock_datetime.date.today.return_value = datetime.date(2026, 4, 1)
    mock_datetime.date.side_effect = lambda *args, **kw: datetime.date(*args, **kw)
    mock_datetime.datetime = datetime.datetime

    articles = [
        {
            "title": "Quarterly Results Released",
            "content": "The quarterly earnings were strong",
            "published_date": "2026-03-28",
        }
    ]

    # Execute
    result = _detect_earnings(articles)

    # Verify: within 5-day window AND has keyword -> detected
    assert result is True


# ---------------------------------------------------------------------------
# Test 4: published_date edge case (exactly 5 days, inclusive boundary)
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.datetime")
def test_published_date_edge_case_exactly_5_days(mock_datetime):
    """Verify published_date exactly EARNINGS_AGE_LIMIT_DAYS old is still detected."""
    # Setup: today = 2026-04-01, article = 2026-03-27 (exactly 5 days old)
    mock_datetime.date.today.return_value = datetime.date(2026, 4, 1)
    mock_datetime.date.side_effect = lambda *args, **kw: datetime.date(*args, **kw)
    mock_datetime.datetime = datetime.datetime

    articles = [
        {
            "title": "Quarterly Results Announced",
            "content": "Company earnings call transcript",
            "published_date": "2026-03-27",
        }
    ]

    # Execute
    result = _detect_earnings(articles)

    # Verify: exactly 5 days is inclusive -> detected
    assert result is True


# ---------------------------------------------------------------------------
# Test 5: published_date missing (treated as not recent)
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.datetime")
def test_published_date_missing_treated_as_not_recent(mock_datetime):
    """Verify articles with missing published_date are skipped in earnings detection."""
    # Setup
    mock_datetime.date.today.return_value = datetime.date(2026, 4, 1)
    mock_datetime.date.side_effect = lambda *args, **kw: datetime.date(*args, **kw)
    mock_datetime.datetime = datetime.datetime

    articles = [
        {
            "title": "Quarterly Results",
            "content": "Earnings announcement",
            "published_date": "",  # Missing
        },
        {
            "title": "Another Quarterly Update",
            "content": "Q3 Results",
            # published_date key missing entirely
        }
    ]

    # Execute
    result = _detect_earnings(articles)

    # Verify: articles without valid date are skipped, no crash
    assert result is False


# ---------------------------------------------------------------------------
# Test 6: Tavily raises exception -> empty articles, continues to Gemini
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent.time.sleep")
def test_tavily_exception_continues_to_gemini(
    mock_sleep,
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify Tavily exception results in empty articles, Gemini still called with fallback."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    # Raise exception on all search calls
    mock_tavily_instance.search.side_effect = Exception("Tavily API error")

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    # Gemini gets "No recent news articles found." prompt
    mock_genai_response.text = json.dumps({
        "sentiment": "Neutral",
        "confidence": 0.3,
        "source_urls": []
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute
    result = run_research_agent(symbols=["TCS"])

    # Verify: Gemini was still called (not skipped entirely)
    assert mock_genai_instance.models.generate_content.called

    # Verify: stock was researched (not in skipped_symbols due to Tavily failure)
    # because Gemini fallback is applied
    assert len(result.results) > 0 or len(result.skipped_symbols) == 0


# ---------------------------------------------------------------------------
# Test 7: 0.5s sleep between Tavily calls
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent.time.sleep")
def test_tavily_0_5s_sleep_between_calls(
    mock_sleep,
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify time.sleep(0.5) is called between Tavily queries."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([
        make_tavily_result(title="News", url="https://example.com/1")
    ])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    mock_genai_response.text = json.dumps({
        "sentiment": "Neutral",
        "confidence": 0.5,
        "source_urls": ["https://example.com/1"]
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute (1 stock = 3 Tavily queries, no earnings = no transcript query)
    run_research_agent(symbols=["TCS"])

    # Verify: sleep(0.5) called at least 3 times
    sleep_calls = [c for c in mock_sleep.call_args_list if c[0] == (0.5,)]
    assert len(sleep_calls) >= 3


# ---------------------------------------------------------------------------
# Test 8: symbols override bypasses screener_results DB read
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._read_screener_results")
def test_symbols_override_bypasses_screener_read(
    mock_read_screener,
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify providing symbols parameter skips screener_results DB read."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([
        make_tavily_result(title="News", url="https://example.com/1")
    ])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    mock_genai_response.text = json.dumps({
        "sentiment": "Neutral",
        "confidence": 0.5,
        "source_urls": ["https://example.com/1"]
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute with symbols override
    run_research_agent(symbols=["TCS", "INFY"])

    # Verify: _read_screener_results was never called
    mock_read_screener.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: completed_at=NULL when Gemini fails (stock in skipped_symbols)
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
def test_completed_at_null_when_gemini_fails(
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify completed_at stays NULL when Gemini fails fatally."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([
        make_tavily_result(title="News", url="https://example.com/1")
    ])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    # Gemini raises non-quota exception (will be retried, then fail)
    mock_genai_instance.models.generate_content.side_effect = Exception("Gemini API error")

    # Execute
    result = run_research_agent(symbols=["TCS"])

    # Verify: TCS is in skipped_symbols
    assert "TCS" in result.skipped_symbols

    # Verify: DB row has completed_at = NULL
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT completed_at FROM research_reports WHERE symbol = ?",
        ("TCS",)
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] is None  # completed_at should be NULL


# ---------------------------------------------------------------------------
# Test 10: source_urls extracted from Tavily result url field
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
def test_source_urls_extracted_from_tavily_url(
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify source_urls are extracted from Tavily result url field and stored in DB."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    tavily_urls = [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([
        make_tavily_result(title="News 1", url=tavily_urls[0]),
        make_tavily_result(title="News 2", url=tavily_urls[1]),
    ])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    # Gemini returns the URLs as source_urls
    mock_genai_response.text = json.dumps({
        "sentiment": "Positive",
        "confidence": 0.8,
        "source_urls": tavily_urls
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute
    result = run_research_agent(symbols=["TCS"])

    # Verify: result contains the URLs
    assert len(result.results) > 0
    research = result.results[0]
    assert research.source_urls == tavily_urls

    # Verify: DB row contains the URLs as JSON
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT source_urls FROM research_reports WHERE symbol = ?",
        ("TCS",)
    )
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    stored_urls = json.loads(row[0])
    assert stored_urls == tavily_urls


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------


def test_format_articles_for_prompt_empty_list():
    """Verify empty articles list produces the default message."""
    result = _format_articles_for_prompt([])
    assert "No recent news articles found" in result


def test_format_articles_for_prompt_with_articles():
    """Verify articles are formatted correctly with Tavily fields."""
    articles = [
        {
            "title": "Stock News",
            "url": "https://example.com/1",
            "content": "Content summary",
        }
    ]
    result = _format_articles_for_prompt(articles)
    assert "Stock News" in result
    assert "https://example.com/1" in result
    assert "Content summary" in result


def test_parse_gemini_response_valid_json():
    """Verify valid Gemini response is parsed correctly."""
    response = json.dumps({
        "sentiment": "Positive",
        "confidence": 0.85,
        "source_urls": ["https://example.com/1"]
    })
    result = _parse_gemini_response(response)
    assert result is not None
    sentiment, confidence, urls = result
    assert sentiment == "Positive"
    assert confidence == 0.85
    assert urls == ["https://example.com/1"]


def test_parse_gemini_response_with_markdown_fences():
    """Verify Gemini response with markdown fences is parsed."""
    response = """```json
{
    "sentiment": "Neutral",
    "confidence": 0.5,
    "source_urls": []
}
```"""
    result = _parse_gemini_response(response)
    assert result is not None
    sentiment, confidence, urls = result
    assert sentiment == "Neutral"
    assert confidence == 0.5


def test_parse_gemini_response_invalid_json():
    """Verify invalid JSON returns None."""
    result = _parse_gemini_response("not json at all")
    assert result is None


def test_parse_gemini_response_missing_sentiment():
    """Verify response missing sentiment field returns None."""
    response = json.dumps({
        "confidence": 0.5,
        "source_urls": []
    })
    result = _parse_gemini_response(response)
    assert result is None


def test_parse_gemini_response_invalid_sentiment():
    """Verify response with invalid sentiment value returns None."""
    response = json.dumps({
        "sentiment": "InvalidSentiment",
        "confidence": 0.5,
        "source_urls": []
    })
    result = _parse_gemini_response(response)
    assert result is None


def test_parse_gemini_response_confidence_clamped():
    """Verify confidence is clamped to [0.0, 1.0]."""
    response = json.dumps({
        "sentiment": "Positive",
        "confidence": 1.5,
        "source_urls": []
    })
    result = _parse_gemini_response(response)
    assert result is not None
    _, confidence, _ = result
    assert confidence == 1.0

    response2 = json.dumps({
        "sentiment": "Negative",
        "confidence": -0.5,
        "source_urls": []
    })
    result2 = _parse_gemini_response(response2)
    assert result2 is not None
    _, confidence2, _ = result2
    assert confidence2 == 0.0


@patch("src.agents.research_agent.settings")
def test_missing_tavily_api_key_raises_error(mock_settings):
    """Verify ResearchAgentError is raised when TAVILY_API_KEY is missing."""
    mock_settings.tavily_api_key = None
    mock_settings.gemini_api_key = "test-key"

    with patch("src.agents.research_agent.log_agent_action"):
        with pytest.raises(ResearchAgentError) as exc_info:
            run_research_agent(symbols=["TCS"])

        assert exc_info.value.phase == "tavily_search"
        assert "TAVILY_API_KEY" in exc_info.value.message


def test_earnings_detection_with_multiple_keywords():
    """Verify earnings detection finds any matching keyword."""
    with patch("src.agents.research_agent.datetime") as mock_dt:
        mock_dt.date.today.return_value = datetime.date(2026, 4, 1)
        mock_dt.date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
        mock_dt.datetime = datetime.datetime

        articles = [
            {
                "title": "Q3 Results",
                "content": "The company announced Q3 profits",
                "published_date": "2026-03-31"
            }
        ]

        result = _detect_earnings(articles)
        assert result is True


def test_earnings_detection_case_insensitive():
    """Verify earnings keyword detection is case-insensitive."""
    with patch("src.agents.research_agent.datetime") as mock_dt:
        mock_dt.date.today.return_value = datetime.date(2026, 4, 1)
        mock_dt.date.side_effect = lambda *a, **kw: datetime.date(*a, **kw)
        mock_dt.datetime = datetime.datetime

        articles = [
            {
                "title": "QUARTERLY EARNINGS",
                "content": "Results in CAPS",
                "published_date": "2026-03-31"
            }
        ]

        result = _detect_earnings(articles)
        assert result is True


def test_symbol_to_company_mapping():
    """Verify SYMBOL_TO_COMPANY mapping is populated."""
    assert "TCS" in SYMBOL_TO_COMPANY
    assert SYMBOL_TO_COMPANY["TCS"] == "Tata Consultancy Services"
    assert "HDFC" not in SYMBOL_TO_COMPANY or "HDFCBANK" in SYMBOL_TO_COMPANY


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
def test_multiple_stocks_researched(
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify multiple stocks are researched in a single run."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_instance = MagicMock()
    mock_tavily_cls.return_value = mock_tavily_instance
    mock_tavily_instance.search.return_value = make_tavily_response([
        make_tavily_result(title="News", url="https://example.com/1")
    ])

    mock_genai_instance = MagicMock()
    mock_genai_cls.return_value = mock_genai_instance
    mock_genai_response = MagicMock()
    mock_genai_response.text = json.dumps({
        "sentiment": "Neutral",
        "confidence": 0.5,
        "source_urls": ["https://example.com/1"]
    })
    mock_genai_instance.models.generate_content.return_value = mock_genai_response

    # Execute with 2 stocks
    result = run_research_agent(symbols=["TCS", "INFY"])

    # Verify: both stocks are in results
    assert len(result.results) == 2
    symbols = {r.symbol for r in result.results}
    assert symbols == {"TCS", "INFY"}


@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.genai.Client")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
def test_empty_symbols_list(
    mock_resolve_db,
    mock_log,
    mock_settings,
    mock_genai_cls,
    mock_tavily_cls,
    temp_db,
):
    """Verify empty symbols list results in empty research run."""
    # Setup
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    mock_tavily_cls.return_value = MagicMock()
    mock_genai_cls.return_value = MagicMock()

    # Execute with empty symbols
    result = run_research_agent(symbols=[])

    # Verify: no stocks researched
    assert len(result.results) == 0
    assert result.stocks_researched == 0
