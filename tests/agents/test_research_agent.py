"""Tests for src/agents/research_agent.py (LangChain ReAct agent version).

Tests the orchestration logic, DB write patterns, error handling, and
API key validation. The LangChain agent loop is tested via mocking
_run_agent_for_stock so tests stay deterministic.
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
    SentimentResult,
    SYMBOL_TO_COMPANY,
    TAVILY_REQUEST_DELAY,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Temporary SQLite database path for test isolation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield str(db_path)


def _make_sentiment_result(
    sentiment: str = "Neutral",
    confidence: float = 0.5,
    urls: list[str] | None = None,
) -> SentimentResult:
    """Build a SentimentResult for test assertions."""
    return SentimentResult(
        sentiment=sentiment,  # type: ignore[arg-type]
        confidence=confidence,
        source_urls=urls or ["https://example.com/1"],
        earnings_transcript_used=False,
    )


# ---------------------------------------------------------------------------
# Test 1: Missing Tavily API key raises ResearchAgentError
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
def test_missing_tavily_api_key_raises_error(mock_log, mock_settings):
    """ResearchAgentError phase='tavily_search' when TAVILY_API_KEY is absent."""
    mock_settings.tavily_api_key = None
    mock_settings.gemini_api_key = "test-key"

    with pytest.raises(ResearchAgentError) as exc_info:
        run_research_agent(symbols=["TCS"])

    assert exc_info.value.phase == "tavily_search"
    assert "TAVILY_API_KEY" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 2: Missing Gemini API key raises ResearchAgentError
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
def test_missing_gemini_api_key_raises_error(mock_log, mock_settings):
    """ResearchAgentError phase='gemini' when GEMINI_API_KEY is absent."""
    mock_settings.tavily_api_key = "test-tavily-key"
    mock_settings.gemini_api_key = None

    with pytest.raises(ResearchAgentError) as exc_info:
        run_research_agent(symbols=["TCS"])

    assert exc_info.value.phase == "gemini"
    assert "GEMINI_API_KEY" in exc_info.value.message


# ---------------------------------------------------------------------------
# Test 3: Empty symbols list returns empty result without DB writes
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
def test_empty_symbols_returns_empty_result(mock_resolve_db, mock_log, mock_settings, temp_db):
    """Empty symbols list returns ResearchAgentResult with 0 stocks, no DB writes."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db

    result = run_research_agent(symbols=[])

    assert result.stocks_researched == 0
    assert result.results == []
    assert result.skipped_symbols == []


# ---------------------------------------------------------------------------
# Test 4: symbols override bypasses screener_results DB read
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._read_screener_results")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_symbols_override_bypasses_screener_read(
    mock_agent, mock_read_screener, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """Providing symbols= skips the screener_results DB read entirely."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = _make_sentiment_result()

    run_research_agent(symbols=["TCS"])

    mock_read_screener.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Successful agent run writes completed_at to DB
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_successful_agent_sets_completed_at(
    mock_agent, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """completed_at is set in DB when agent succeeds."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = _make_sentiment_result(
        sentiment="Positive", confidence=0.8, urls=["https://example.com/1"]
    )

    result = run_research_agent(symbols=["TCS"])

    assert len(result.results) == 1
    assert result.results[0].symbol == "TCS"
    assert result.results[0].sentiment == "Positive"

    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT completed_at FROM research_reports WHERE symbol = ?", ("TCS",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] is not None  # completed_at must be set


# ---------------------------------------------------------------------------
# Test 6: Agent failure — stock in skipped_symbols, completed_at=NULL
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_agent_failure_leaves_completed_at_null(
    mock_agent, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """When agent returns None, stock goes to skipped_symbols and completed_at stays NULL."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = None  # agent failed

    result = run_research_agent(symbols=["TCS"])

    assert "TCS" in result.skipped_symbols
    assert result.stocks_researched == 0

    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT completed_at FROM research_reports WHERE symbol = ?", ("TCS",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] is None  # placeholder row, never updated


# ---------------------------------------------------------------------------
# Test 7: Multiple stocks — each processed independently
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_multiple_stocks_processed_independently(
    mock_agent, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """Two stocks both succeed and both appear in results."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = _make_sentiment_result()

    result = run_research_agent(symbols=["TCS", "INFY"])

    assert result.stocks_researched == 2
    symbols = {r.symbol for r in result.results}
    assert symbols == {"TCS", "INFY"}


# ---------------------------------------------------------------------------
# Test 8: Partial failure — one success, one skip
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_partial_failure_one_success_one_skip(
    mock_agent, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """First stock succeeds, second fails: one in results, one in skipped_symbols."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.side_effect = [_make_sentiment_result(), None]

    result = run_research_agent(symbols=["TCS", "INFY"])

    assert result.stocks_researched == 1
    assert result.results[0].symbol == "TCS"
    assert "INFY" in result.skipped_symbols


# ---------------------------------------------------------------------------
# Test 9: SentimentResult fields map correctly to StockResearch
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_sentiment_result_fields_mapped_to_stock_research(
    mock_agent, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """SentimentResult sentiment/confidence/source_urls appear in StockResearch."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = _make_sentiment_result(
        sentiment="Negative",
        confidence=0.9,
        urls=["https://example.com/a", "https://example.com/b"],
    )

    result = run_research_agent(symbols=["RELIANCE"])

    r = result.results[0]
    assert r.sentiment == "Negative"
    assert r.confidence == 0.9
    assert r.source_urls == ["https://example.com/a", "https://example.com/b"]


# ---------------------------------------------------------------------------
# Test 10: DB row source_urls stored as JSON
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_source_urls_stored_as_json_in_db(
    mock_agent, mock_resolve_db, mock_log, mock_settings, temp_db
):
    """source_urls are stored as JSON array in the research_reports table."""
    mock_settings.tavily_api_key = "test-key"
    mock_settings.gemini_api_key = "test-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    urls = ["https://example.com/1", "https://example.com/2"]
    mock_agent.return_value = _make_sentiment_result(urls=urls)

    run_research_agent(symbols=["TCS"])

    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT source_urls FROM research_reports WHERE symbol = ?", ("TCS",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert json.loads(row[0]) == urls


# ---------------------------------------------------------------------------
# Test 11: SYMBOL_TO_COMPANY mapping is populated
# ---------------------------------------------------------------------------


def test_symbol_to_company_mapping():
    """Verify SYMBOL_TO_COMPANY contains key NSE stocks."""
    assert "TCS" in SYMBOL_TO_COMPANY
    assert SYMBOL_TO_COMPANY["TCS"] == "Tata Consultancy Services"
    assert "HDFCBANK" in SYMBOL_TO_COMPANY
    assert "HEROMOTOCO" in SYMBOL_TO_COMPANY


# ---------------------------------------------------------------------------
# Test 12: ChatGoogleGenerativeAI instantiated with correct model + key
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.ChatGoogleGenerativeAI")
@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_llm_created_with_correct_model_and_key(
    mock_agent, mock_resolve_db, mock_log, mock_settings,
    mock_tavily_cls, mock_llm_cls, temp_db
):
    """ChatGoogleGenerativeAI is instantiated with gemini-2.5-flash and correct API key."""
    mock_settings.tavily_api_key = "test-tavily-key"
    mock_settings.gemini_api_key = "test-gemini-key-xyz"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = _make_sentiment_result()

    run_research_agent(symbols=["TCS"])

    mock_llm_cls.assert_called_once()
    call_kwargs = mock_llm_cls.call_args.kwargs
    assert call_kwargs["model"] == "gemini-2.5-flash"
    assert call_kwargs["google_api_key"] == "test-gemini-key-xyz"


# ---------------------------------------------------------------------------
# Test 13: TavilyClient instantiated with correct API key
# ---------------------------------------------------------------------------


@patch("src.agents.research_agent.ChatGoogleGenerativeAI")
@patch("src.agents.research_agent.TavilyClient")
@patch("src.agents.research_agent.settings")
@patch("src.agents.research_agent.log_agent_action")
@patch("src.agents.research_agent._resolve_db_path")
@patch("src.agents.research_agent._run_agent_for_stock")
def test_tavily_client_created_with_api_key(
    mock_agent, mock_resolve_db, mock_log, mock_settings,
    mock_tavily_cls, mock_llm_cls, temp_db
):
    """TavilyClient is instantiated with the Tavily API key from settings."""
    mock_settings.tavily_api_key = "test-tavily-key-abc"
    mock_settings.gemini_api_key = "test-gemini-key"
    mock_settings.database_url = f"sqlite:///{temp_db}"
    mock_resolve_db.return_value = temp_db
    mock_agent.return_value = _make_sentiment_result()

    run_research_agent(symbols=["TCS"])

    mock_tavily_cls.assert_called_once_with(api_key="test-tavily-key-abc")
