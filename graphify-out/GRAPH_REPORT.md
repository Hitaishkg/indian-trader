# Graph Report - .  (2026-04-18)

## Corpus Check
- 115 files · ~233,573 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2494 nodes · 5111 edges · 90 communities detected
- Extraction: 65% EXTRACTED · 35% INFERRED · 0% AMBIGUOUS · INFERRED: 1805 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Logging & Audit Layer|Logging & Audit Layer]]
- [[_COMMUNITY_Main Pipeline Entry|Main Pipeline Entry]]
- [[_COMMUNITY_Execution Agent|Execution Agent]]
- [[_COMMUNITY_Data Fetcher & Errors|Data Fetcher & Errors]]
- [[_COMMUNITY_Signal Agent & LLM Calls|Signal Agent & LLM Calls]]
- [[_COMMUNITY_Docs & Context State|Docs & Context State]]
- [[_COMMUNITY_Backtest Runner|Backtest Runner]]
- [[_COMMUNITY_Reporter Agent|Reporter Agent]]
- [[_COMMUNITY_Research Agent|Research Agent]]
- [[_COMMUNITY_Cross-cutting Schemas|Cross-cutting Schemas]]
- [[_COMMUNITY_Risk Agent|Risk Agent]]
- [[_COMMUNITY_Data Cleaner|Data Cleaner]]
- [[_COMMUNITY_Technical Indicator Tests|Technical Indicator Tests]]
- [[_COMMUNITY_Watchlist Agent|Watchlist Agent]]
- [[_COMMUNITY_Momentum Strategy|Momentum Strategy]]
- [[_COMMUNITY_Quality Filter|Quality Filter]]
- [[_COMMUNITY_Fundamentals Tests|Fundamentals Tests]]
- [[_COMMUNITY_Regime Filter|Regime Filter]]
- [[_COMMUNITY_Dashboard Server|Dashboard Server]]
- [[_COMMUNITY_Historical Fundamentals Tests|Historical Fundamentals Tests]]
- [[_COMMUNITY_Fundamentals Scraper|Fundamentals Scraper]]
- [[_COMMUNITY_Main Pipeline Tests|Main Pipeline Tests]]
- [[_COMMUNITY_Screener Agent Tests|Screener Agent Tests]]
- [[_COMMUNITY_Data Validator|Data Validator]]
- [[_COMMUNITY_OHLCV Cache & Fetch|OHLCV Cache & Fetch]]
- [[_COMMUNITY_Backtest Audit Findings|Backtest Audit Findings]]
- [[_COMMUNITY_Technical Indicators|Technical Indicators]]
- [[_COMMUNITY_Integration Test Scenarios|Integration Test Scenarios]]
- [[_COMMUNITY_Tavily News API|Tavily News API]]
- [[_COMMUNITY_Race Condition Prevention|Race Condition Prevention]]
- [[_COMMUNITY_Regime Blocked Logic|Regime Blocked Logic]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Test Rationale Nodes|Test Rationale Nodes]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_Package Init Files|Package Init Files]]
- [[_COMMUNITY_System README|System README]]
- [[_COMMUNITY_DB Schema Docs|DB Schema Docs]]
- [[_COMMUNITY_DB agent_logs Table|DB agent_logs Table]]
- [[_COMMUNITY_DB orders Table|DB orders Table]]
- [[_COMMUNITY_DB positions Table|DB positions Table]]
- [[_COMMUNITY_DB trades Table|DB trades Table]]
- [[_COMMUNITY_DB fundamentals_history Table|DB fundamentals_history Table]]
- [[_COMMUNITY_DB nifty_constituents Table|DB nifty_constituents Table]]
- [[_COMMUNITY_DB screener_results Table|DB screener_results Table]]
- [[_COMMUNITY_DB research_reports Table|DB research_reports Table]]
- [[_COMMUNITY_DB watchlist Table|DB watchlist Table]]
- [[_COMMUNITY_DB signals Table|DB signals Table]]
- [[_COMMUNITY_DB risk_approvals Table|DB risk_approvals Table]]
- [[_COMMUNITY_DB execution_checkpoints Table|DB execution_checkpoints Table]]
- [[_COMMUNITY_DB daily_pnl Table|DB daily_pnl Table]]
- [[_COMMUNITY_DB strategy_perf Table|DB strategy_perf Table]]
- [[_COMMUNITY_DB market_data Table|DB market_data Table]]
- [[_COMMUNITY_DB morning_signals Table|DB morning_signals Table]]
- [[_COMMUNITY_Data Quality Concept|Data Quality Concept]]
- [[_COMMUNITY_Backtest Validator Spec|Backtest Validator Spec]]
- [[_COMMUNITY_Backtest Gates Concept|Backtest Gates Concept]]
- [[_COMMUNITY_Monitor Agent Spec|Monitor Agent Spec]]
- [[_COMMUNITY_Logger Spec|Logger Spec]]
- [[_COMMUNITY_Risk Agent Spec|Risk Agent Spec]]
- [[_COMMUNITY_Peak Equity Rationale|Peak Equity Rationale]]
- [[_COMMUNITY_Orchestrator Spec|Orchestrator Spec]]
- [[_COMMUNITY_Safe Mode Concept|Safe Mode Concept]]
- [[_COMMUNITY_Fundamentals Spec|Fundamentals Spec]]
- [[_COMMUNITY_Screener.in External|Screener.in External]]
- [[_COMMUNITY_Quality Filter Spec|Quality Filter Spec]]
- [[_COMMUNITY_Signal Agent Spec|Signal Agent Spec]]
- [[_COMMUNITY_Backtest Lookahead Audit|Backtest Lookahead Audit]]
- [[_COMMUNITY_Integration Init|Integration Init]]

## God Nodes (most connected - your core abstractions)
1. `Settings` - 252 edges
2. `FetchError` - 167 edges
3. `PaperTrader` - 135 edges
4. `SQLiteHandler` - 118 edges
5. `FundamentalsError` - 79 edges
6. `BacktestResult` - 66 edges
7. `RiskAgentResult` - 64 edges
8. `SignalAgentResult` - 59 edges
9. `ScreenerAgentError` - 50 edges
10. `AgentStepResult` - 42 edges

## Surprising Connections (you probably didn't know these)
- `Safe Mode Design Pattern (no new positions + monitor rule-based only)` --semantically_similar_to--> `Safe Mode — orchestrator halts execution on kill switch or late start`  [INFERRED] [semantically similar]
  docs/SYSTEM.md → src/agents/orchestrator.py
- `Daily Report 2026-04-14: P&L -42.23, Drawdown 0.4%, 0 open positions` --implements--> `_run_report_session()`  [INFERRED]
  reports/2026-04-14.md → src/agents/orchestrator.py
- `Decisions Log: signal_agent.py — Groq advisory-only, both-LLM-fail keeps BUY` --references--> `Safe Mode — orchestrator halts execution on kill switch or late start`  [INFERRED]
  docs/context/decisions-log.md → src/agents/orchestrator.py
- `SYSTEM.md Data Flow (evening/morning/market sessions)` --references--> `_run_evening_session()`  [EXTRACTED]
  docs/SYSTEM.md → src/agents/orchestrator.py
- `SYSTEM.md Data Flow (evening/morning/market sessions)` --references--> `_run_morning_session()`  [EXTRACTED]
  docs/SYSTEM.md → src/agents/orchestrator.py

## Hyperedges (group relationships)
- **Three-Step Stock Selection Pipeline (quality filter -> momentum -> regime)** — momentum_module, regime_module, screener_agent_module [EXTRACTED 1.00]
- **Evening Research Pipeline (screener -> research -> watchlist)** — screener_agent_module, research_agent_module, watchlist_agent_module [EXTRACTED 1.00]
- **Phase 1 Foundation Pipeline (fetcher -> cleaner -> validator -> paper_trader)** — fetcher_module, cleaner_module, validator_module, paper_trader_module [EXTRACTED 1.00]
- **Integration Tests: 10 Scenarios Cover Full Evening + Morning Pipeline** — test_full_pipeline_TestScenario1EveningHappyPath, test_full_pipeline_TestScenario2KillSwitchFires, test_full_pipeline_TestScenario3ThinUniverse, test_full_pipeline_TestScenario4RegimeBlocked, test_full_pipeline_TestScenario5ResearchIncomplete, test_full_pipeline_part2_TestScenario6GTTReconciliation, test_full_pipeline_part2_TestScenario7OvernightEventRemoval, test_full_pipeline_part2_TestScenario8LateSignalDeadlineMiss, test_full_pipeline_part2_TestScenario9FullWeekSimulation, test_full_pipeline_part2_TestScenario10WatchlistTimeout [EXTRACTED 1.00]
- **Backtest Audit + Walk-Forward Results inform Phase 2 Gate Decision** — backtest_audit_transaction_costs, walkforward_phase2_verdict, walkforward_train_result, walkforward_test_result, backtest_eval_test_2019_2023 [EXTRACTED 0.95]
- **Orchestrator Dispatches to 4 Session Runners (evening/morning/monitor/report)** — orchestrator_run_orchestrator, orchestrator_run_evening_session, orchestrator_run_morning_session, orchestrator_run_monitor_session, orchestrator_run_report_session [EXTRACTED 1.00]

## Communities

### Community 0 - "Logging & Audit Layer"
Cohesion: 0.02
Nodes (229): get_logger(), _ISTFormatter, log_agent_action(), Structured logging module for the Indian Trader pipeline.  Configures Python's s, Initialise the handler with the path to the SQLite database.          Args:, Open and configure the SQLite connection if not already open.          Returns t, Write a log record to the agent_logs table.          Extracts fields from the Lo, Write a structured row directly to agent_logs, bypassing LogRecord.          Pub (+221 more)

### Community 1 - "Main Pipeline Entry"
Cohesion: 0.02
Nodes (228): Enum, compute_atr(), main(), Compute Average True Range for a single symbol's OHLCV data.      Uses the stand, Run the Phase 1 end-to-end dry-run pipeline., _check_kill_switches(), _compute_peak_equity(), _fetch_atr() (+220 more)

### Community 2 - "Execution Agent"
Cohesion: 0.02
Nodes (154): _build_checkpoint_message(), _checkpoint_file_path(), ExecutionAgentError, ExecutionResult, _fetch_atr_from_signals(), _fetch_current_price(), _ist_now(), _open_connection() (+146 more)

### Community 3 - "Data Fetcher & Errors"
Cohesion: 0.02
Nodes (167): Exception, FetchError, Raised when OHLCV data cannot be fetched from any source.      Attributes:, FundamentalsError, Raised when fundamentals data fetch or DB operation fails., Monitor Agent — position monitoring during market hours for the Indian Trader pi, Resolve the SQLite database path from override or settings.      Args:         d, Open a SQLite connection with WAL pragmas.      Args:         db_path: Path to S (+159 more)

### Community 4 - "Signal Agent & LLM Calls"
Cohesion: 0.03
Nodes (122): _build_llm_prompt(), _call_gemini_fallback(), _call_groq(), _compute_bollinger_position(), _ensure_table(), _extract_latest_indicators(), _open_connection(), _parse_llm_confidence() (+114 more)

### Community 5 - "Docs & Context State"
Cohesion: 0.03
Nodes (123): Current State: Phase 4 Complete, Phase 5 not started, Daily Report 2026-04-14: P&L -42.23, Drawdown 0.4%, 0 open positions, Decisions Log: monitor_agent.py — MARKET_CLOSE_MINUTE=45, stop monotonic guard, Decisions Log: orchestrator.py — kill_switch skip + dashboard auto-start, Decisions Log: signal_agent.py — Groq advisory-only, both-LLM-fail keeps BUY, AgentStepResult Dataclass, OrchestratorError Exception, OrchestratorResult Dataclass (+115 more)

### Community 6 - "Backtest Runner"
Cohesion: 0.04
Nodes (119): BacktestError, BacktestResult, Raised when the backtest runner encounters a fatal error.      Attributes:, Complete output of a backtest run.      All percentage fields are stored as posi, make_fundamentals_df(), make_nifty_above_200dma(), make_nifty_below_200dma_10days(), make_nifty_with_crossover() (+111 more)

### Community 7 - "Reporter Agent"
Cohesion: 0.04
Nodes (103): _build_markdown_report(), _build_notification_message(), _build_open_positions_table(), _compute_consecutive_losses(), _compute_drawdown_pct(), _compute_kill_switch_status(), _compute_peak_equity(), _compute_profit_factor() (+95 more)

### Community 8 - "Research Agent"
Cohesion: 0.04
Nodes (97): _detect_earnings(), _ensure_table(), _fetch_tavily_news(), _fetch_transcript(), _format_articles_for_prompt(), _insert_placeholder_row(), _open_connection(), _parse_gemini_response() (+89 more)

### Community 9 - "Cross-cutting Schemas"
Cohesion: 0.03
Nodes (91): add_indicators() function, agent_logs DB Table, BacktestError Exception, BacktestResult Dataclass, src/backtest/runner.py, Backtest Runner Spec, src/data/cleaner.py, Data Cleaner Spec (+83 more)

### Community 10 - "Risk Agent"
Cohesion: 0.07
Nodes (80): _build_summary_message(), _check_consecutive_losses(), _compute_drawdown_pct(), _compute_peak_equity(), _compute_portfolio_equity(), _compute_sharpe(), _compute_win_rate_pct(), _fetch_entry_price() (+72 more)

### Community 11 - "Data Cleaner"
Cohesion: 0.04
Nodes (80): clean_ohlcv(), CleaningReport, _fill_missing_values(), _flag_consistency(), _flag_negative_prices(), _flag_price_floor(), OHLCV data cleaning layer for the Indian Trader pipeline.  Sits between fetcher., Check that df has all required columns and timezone-aware dates.      Mirrors th (+72 more)

### Community 12 - "Technical Indicator Tests"
Cohesion: 0.03
Nodes (55): multi_symbol_combined(), Tests for src/indicators/technical.py — Technical Indicator Calculations.  Test, Single symbol with exactly 26 rows — at MINIMUM_LOOKBACK., Single symbol with 1 row — minimal edge case., Combined DataFrame: Symbol A (50 rows), Symbol B (50 rows), Symbol C (50 rows)., Tests 1-3: Core functionality — happy path scenarios., Test 1: All 8 indicator columns present in output (50 rows × 3 symbols)., Test 2: Original 7 columns unchanged after add_indicators(). (+47 more)

### Community 13 - "Watchlist Agent"
Cohesion: 0.05
Nodes (75): count_watchlist_rows(), insert_research_report(), insert_screener_result(), Tests for src/agents/watchlist_agent.py.  Covers all 15 scenarios from the spec, Insert a row into screener_results., Insert a row into research_reports., Read a row from the watchlist table., Count rows in watchlist for a given run_date. (+67 more)

### Community 14 - "Momentum Strategy"
Cohesion: 0.05
Nodes (72): _apply_tiebreaker(), compute_momentum(), _ist_now(), MomentumReport, 12-1 momentum factor computation and top-N candidate selection.  Formula: moment, Validate all inputs; raise ValueError on any violation., Single adjacent-pair pass tiebreaker. Mutates scores in place.      For each adj, Return current IST time as ISO 8601 string. (+64 more)

### Community 15 - "Quality Filter"
Cohesion: 0.06
Nodes (70): apply_quality_filter(), _compute_ohlcv_metrics(), FilterReport, _ist_now(), Quality filter for the Indian Trader stock selection pipeline.  Implements the f, Compute OHLCV-derived metrics for a single symbol.      Args:         symbol: NS, Apply all five hard quality filters to the stock universe.      Filters:     1., Summary of quality filtering results.      Attributes:         universe_size: To (+62 more)

### Community 16 - "Fundamentals Tests"
Cohesion: 0.04
Nodes (69): make_screener_html(), mock_yfinance_response(), Tests for src/data/fundamentals.py — covering all 31 acceptance criteria., Criterion 2: Cache older than 45 days triggers fresh fetch., Criterion 3: Stale cache returned with fundamentals_stale when fresh fetch fails, Criterion 4: force_refresh=True bypasses cache for all symbols., Criterion 5: Corrupt cache file is deleted and data is refetched., Create a temporary cache directory and return its path. (+61 more)

### Community 17 - "Regime Filter"
Cohesion: 0.06
Nodes (66): apply_regime_filter(), compute_200dma(), count_consecutive_days_below_200dma(), _ist_now(), Nifty 50 200-day SMA regime filter.  Determines the current market regime by com, Compute the 200-day simple moving average of Nifty 50 close prices.      Args:, Count consecutive trading days the Nifty 50 close has been below the rolling 200, Validate nifty_ohlcv_df inputs; raise ValueError on violation. (+58 more)

### Community 18 - "Dashboard Server"
Cohesion: 0.06
Nodes (48): BaseHTTPRequestHandler, _build_pnl_chart(), _build_response(), _compute_kill_switches(), DashboardHandler, _db_connect(), _fetch_agent_activity(), _fetch_agent_summary() (+40 more)

### Community 19 - "Historical Fundamentals Tests"
Cohesion: 0.04
Nodes (49): mock_settings(), Tests for historical fundamentals support in src/data/fundamentals.py  Tests cov, Test fiscal year selection for June (month <= 6 -> year-1)., Test fiscal year selection for July (month >= 7 -> year)., Test April boundary (month <= 6 -> year-1, NOT year, preventing lookahead bias)., Test October boundary (month >= 7 -> year)., Test that May 2015 query returns FY2014, not FY2015 (no lookahead bias)., Test that missing DB row returns data_quality='missing' with NaN financials. (+41 more)

### Community 20 - "Fundamentals Scraper"
Cohesion: 0.08
Nodes (40): _cache_path(), _cache_to_row(), _cross_validate_pe(), fetch_fundamentals(), fetch_historical_fundamentals(), _fetch_yfinance_fundamentals(), _FundamentalsCache, get_cache_age_days() (+32 more)

### Community 21 - "Main Pipeline Tests"
Cohesion: 0.06
Nodes (22): Tests for main.py end-to-end pipeline., Unit tests for compute_atr() function., compute_atr with sufficient data returns positive float., Settings import failure → sys.exit(1)., If settings raises ConfigurationError on import, main.py catches and exits., FetchError → sys.exit(1), send_alert() called., compute_atr with fewer than period+1 rows returns None., DataQualityError → sys.exit(1), send_alert() called. (+14 more)

### Community 22 - "Screener Agent Tests"
Cohesion: 0.38
Nodes (21): _make_fundamentals_df(), _make_momentum_result(), _make_ohlcv_df(), _make_quality_filter_result(), _make_regime_result(), _make_sector_df(), temp_db(), test_all_stocks_fail_quality_filter() (+13 more)

### Community 23 - "Data Validator"
Cohesion: 0.15
Nodes (18): _check_ohlcv_gaps(), _check_roe(), _compute_stock_score(), _now_ist(), _open_db(), Return the current datetime in IST., Validate a single stock's ROE value.      Returns (passed: bool, detail: str)., Detect gaps longer than 5 consecutive trading days in a sorted date series. (+10 more)

### Community 24 - "OHLCV Cache & Fetch"
Cohesion: 0.16
Nodes (17): _cache_path(), _fetch_jugaad(), fetch_nifty50_symbols(), fetch_ohlcv(), fetch_sector_indices(), _fetch_yfinance(), OHLCV data acquisition layer for the Indian Trader pipeline.  Fetches historical, Return the absolute file path for a cache CSV file.      Args:         symbol: N (+9 more)

### Community 25 - "Backtest Audit Findings"
Cohesion: 0.18
Nodes (13): Backtest Audit: Position sizing consistency — NONE, correctly implemented, Backtest Audit: Regime filter whipsaw validation gap (no per-year breakdown) — MEDIUM, Backtest Audit: Partial survivorship bias in NIFTY_CONSTITUENTS_BY_SYMBOL — MEDIUM, Backtest Audit: Zero transaction costs — HIGH severity finding, Backtest Evaluation Report: Test 2019–2023, Score 74/100, Deploy, BacktestError Exception, BacktestResult Dataclass, _PortfolioTracker — multi-stock simulation class inside backtesting.py wrapper (+5 more)

### Community 26 - "Technical Indicators"
Cohesion: 0.29
Nodes (7): add_indicators(), compute_atr_series(), _compute_for_symbol(), Technical indicator calculations for the Indian Trader pipeline.  Pure calculati, Compute technical indicators per symbol and append columns to the DataFrame., Compute ATR series for a single symbol's OHLCV data using Wilder smoothing., Compute all indicators for a single symbol group.      This private helper is ca

### Community 27 - "Integration Test Scenarios"
Cohesion: 0.4
Nodes (5): Scenario 1: Evening Happy Path Test, Scenario 2: Kill Switch Drawdown Test, Scenario 3: Thin Universe Test, Integration Test DB Fixture (fresh SQLite per test), Integration Test Data Seeding Helpers

### Community 28 - "Tavily News API"
Cohesion: 1.0
Nodes (2): Tavily Search API, Spec: Research Agent Tavily Migration

### Community 29 - "Race Condition Prevention"
Cohesion: 1.0
Nodes (2): Race Condition Prevention: completed_at flag gates watchlist reads, Scenario 5: Research Incomplete Race Condition Test

### Community 30 - "Regime Blocked Logic"
Cohesion: 1.0
Nodes (2): Design Decision: regime_blocked still writes top5 with multiplier=0.0, Scenario 4: Regime Blocked (Below 200 DMA 10+ days) Test

### Community 31 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): Main completes successfully: data fetched, cleaned, validated, trade placed.

### Community 32 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): Main retrieves P&L and sends it in notification.

### Community 33 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): When fetch_ohlcv raises FetchError, main exits with error alert.

### Community 34 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): When validate_data raises DataQualityError, main exits with alert.

### Community 35 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): When cleaned_df has no symbols, ValueError raised and caught.

### Community 36 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): When PaperTrader.place_order raises ValueError, main exits with alert.

### Community 37 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): When position already exists for selected symbol, trade is skipped.

### Community 38 - "Test Rationale Nodes"
Cohesion: 1.0
Nodes (1): When compute_atr returns None, fallback 2% SL / 4% TP used.

### Community 39 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 40 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 41 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 42 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 43 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 44 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 45 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 46 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 47 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 48 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 49 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 50 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 51 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 52 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 53 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 54 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 55 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 56 - "Package Init Files"
Cohesion: 1.0
Nodes (0): 

### Community 57 - "System README"
Cohesion: 1.0
Nodes (1): Indian Trader — Agentic Trading System

### Community 58 - "DB Schema Docs"
Cohesion: 1.0
Nodes (1): Database Schema Documentation

### Community 59 - "DB agent_logs Table"
Cohesion: 1.0
Nodes (1): DB Table: agent_logs

### Community 60 - "DB orders Table"
Cohesion: 1.0
Nodes (1): DB Table: orders

### Community 61 - "DB positions Table"
Cohesion: 1.0
Nodes (1): DB Table: positions

### Community 62 - "DB trades Table"
Cohesion: 1.0
Nodes (1): DB Table: trades

### Community 63 - "DB fundamentals_history Table"
Cohesion: 1.0
Nodes (1): DB Table: fundamentals_history

### Community 64 - "DB nifty_constituents Table"
Cohesion: 1.0
Nodes (1): DB Table: nifty_constituents

### Community 65 - "DB screener_results Table"
Cohesion: 1.0
Nodes (1): DB Table: screener_results

### Community 66 - "DB research_reports Table"
Cohesion: 1.0
Nodes (1): DB Table: research_reports

### Community 67 - "DB watchlist Table"
Cohesion: 1.0
Nodes (1): DB Table: watchlist

### Community 68 - "DB signals Table"
Cohesion: 1.0
Nodes (1): DB Table: signals

### Community 69 - "DB risk_approvals Table"
Cohesion: 1.0
Nodes (1): DB Table: risk_approvals

### Community 70 - "DB execution_checkpoints Table"
Cohesion: 1.0
Nodes (1): DB Table: execution_checkpoints

### Community 71 - "DB daily_pnl Table"
Cohesion: 1.0
Nodes (1): DB Table: daily_pnl

### Community 72 - "DB strategy_perf Table"
Cohesion: 1.0
Nodes (1): DB Table: strategy_perf

### Community 73 - "DB market_data Table"
Cohesion: 1.0
Nodes (1): DB Table: market_data

### Community 74 - "DB morning_signals Table"
Cohesion: 1.0
Nodes (1): DB Table: morning_signals

### Community 75 - "Data Quality Concept"
Cohesion: 1.0
Nodes (1): Data Quality Score (0.0-1.0)

### Community 76 - "Backtest Validator Spec"
Cohesion: 1.0
Nodes (1): Spec: Backtest Validator

### Community 77 - "Backtest Gates Concept"
Cohesion: 1.0
Nodes (1): 5 Backtest Gates

### Community 78 - "Monitor Agent Spec"
Cohesion: 1.0
Nodes (1): Spec: Monitor Agent

### Community 79 - "Logger Spec"
Cohesion: 1.0
Nodes (1): Spec: Logger

### Community 80 - "Risk Agent Spec"
Cohesion: 1.0
Nodes (1): Spec: Risk Agent

### Community 81 - "Peak Equity Rationale"
Cohesion: 1.0
Nodes (1): Rationale: Peak equity from trades table (reporter not built)

### Community 82 - "Orchestrator Spec"
Cohesion: 1.0
Nodes (1): Spec: Orchestrator

### Community 83 - "Safe Mode Concept"
Cohesion: 1.0
Nodes (1): Safe Mode (no new positions)

### Community 84 - "Fundamentals Spec"
Cohesion: 1.0
Nodes (1): Spec: Fundamentals Fetcher

### Community 85 - "Screener.in External"
Cohesion: 1.0
Nodes (1): Screener.in Scraper

### Community 86 - "Quality Filter Spec"
Cohesion: 1.0
Nodes (1): Spec: Quality Filter

### Community 87 - "Signal Agent Spec"
Cohesion: 1.0
Nodes (1): Spec: Signal Agent

### Community 88 - "Backtest Lookahead Audit"
Cohesion: 1.0
Nodes (1): Backtest Audit: Lookahead bias — LOW, well-controlled

### Community 89 - "Integration Init"
Cohesion: 1.0
Nodes (1): Integration Tests __init__.py (empty marker)

## Ambiguous Edges - Review These
- `src/strategy/momentum.py` → `src/agents/screener_agent.py`  [AMBIGUOUS]
  docs/specs/2026-04-05-screener-agent.md · relation: semantically_similar_to

## Knowledge Gaps
- **445 isolated node(s):** `Tests for main.py end-to-end pipeline.`, `Unit tests for compute_atr() function.`, `compute_atr with sufficient data returns positive float.`, `compute_atr with fewer than period+1 rows returns None.`, `compute_atr with exactly period+1 rows returns non-None.` (+440 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Tavily News API`** (2 nodes): `Tavily Search API`, `Spec: Research Agent Tavily Migration`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Race Condition Prevention`** (2 nodes): `Race Condition Prevention: completed_at flag gates watchlist reads`, `Scenario 5: Research Incomplete Race Condition Test`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Regime Blocked Logic`** (2 nodes): `Design Decision: regime_blocked still writes top5 with multiplier=0.0`, `Scenario 4: Regime Blocked (Below 200 DMA 10+ days) Test`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `Main completes successfully: data fetched, cleaned, validated, trade placed.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `Main retrieves P&L and sends it in notification.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `When fetch_ohlcv raises FetchError, main exits with error alert.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `When validate_data raises DataQualityError, main exits with alert.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `When cleaned_df has no symbols, ValueError raised and caught.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `When PaperTrader.place_order raises ValueError, main exits with alert.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `When position already exists for selected symbol, trade is skipped.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Test Rationale Nodes`** (1 nodes): `When compute_atr returns None, fallback 2% SL / 4% TP used.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Package Init Files`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `System README`** (1 nodes): `Indian Trader — Agentic Trading System`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB Schema Docs`** (1 nodes): `Database Schema Documentation`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB agent_logs Table`** (1 nodes): `DB Table: agent_logs`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB orders Table`** (1 nodes): `DB Table: orders`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB positions Table`** (1 nodes): `DB Table: positions`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB trades Table`** (1 nodes): `DB Table: trades`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB fundamentals_history Table`** (1 nodes): `DB Table: fundamentals_history`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB nifty_constituents Table`** (1 nodes): `DB Table: nifty_constituents`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB screener_results Table`** (1 nodes): `DB Table: screener_results`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB research_reports Table`** (1 nodes): `DB Table: research_reports`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB watchlist Table`** (1 nodes): `DB Table: watchlist`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB signals Table`** (1 nodes): `DB Table: signals`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB risk_approvals Table`** (1 nodes): `DB Table: risk_approvals`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB execution_checkpoints Table`** (1 nodes): `DB Table: execution_checkpoints`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB daily_pnl Table`** (1 nodes): `DB Table: daily_pnl`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB strategy_perf Table`** (1 nodes): `DB Table: strategy_perf`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB market_data Table`** (1 nodes): `DB Table: market_data`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `DB morning_signals Table`** (1 nodes): `DB Table: morning_signals`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Data Quality Concept`** (1 nodes): `Data Quality Score (0.0-1.0)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Backtest Validator Spec`** (1 nodes): `Spec: Backtest Validator`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Backtest Gates Concept`** (1 nodes): `5 Backtest Gates`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Monitor Agent Spec`** (1 nodes): `Spec: Monitor Agent`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Logger Spec`** (1 nodes): `Spec: Logger`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Risk Agent Spec`** (1 nodes): `Spec: Risk Agent`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Peak Equity Rationale`** (1 nodes): `Rationale: Peak equity from trades table (reporter not built)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Orchestrator Spec`** (1 nodes): `Spec: Orchestrator`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Safe Mode Concept`** (1 nodes): `Safe Mode (no new positions)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Fundamentals Spec`** (1 nodes): `Spec: Fundamentals Fetcher`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Screener.in External`** (1 nodes): `Screener.in Scraper`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Quality Filter Spec`** (1 nodes): `Spec: Quality Filter`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Signal Agent Spec`** (1 nodes): `Spec: Signal Agent`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Backtest Lookahead Audit`** (1 nodes): `Backtest Audit: Lookahead bias — LOW, well-controlled`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Integration Init`** (1 nodes): `Integration Tests __init__.py (empty marker)`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `src/strategy/momentum.py` and `src/agents/screener_agent.py`?**
  _Edge tagged AMBIGUOUS (relation: semantically_similar_to) - confidence is low._
- **Why does `Settings` connect `Main Pipeline Entry` to `Logging & Audit Layer`, `Execution Agent`, `Signal Agent & LLM Calls`, `Reporter Agent`, `Risk Agent`?**
  _High betweenness centrality (0.137) - this node is a cross-community bridge._
- **Why does `FetchError` connect `Data Fetcher & Errors` to `Main Pipeline Entry`, `Execution Agent`, `Signal Agent & LLM Calls`, `Risk Agent`, `OHLCV Cache & Fetch`?**
  _High betweenness centrality (0.118) - this node is a cross-community bridge._
- **Why does `PaperTrader` connect `Execution Agent` to `Main Pipeline Entry`, `Risk Agent`, `Data Fetcher & Errors`, `Reporter Agent`?**
  _High betweenness centrality (0.084) - this node is a cross-community bridge._
- **Are the 247 inferred relationships involving `Settings` (e.g. with `TestScenario6GTTReconciliation` and `TestScenario7OvernightEventRemoval`) actually correct?**
  _`Settings` has 247 INFERRED edges - model-reasoned connections that need verification._
- **Are the 161 inferred relationships involving `FetchError` (e.g. with `Compute Average True Range for a single symbol's OHLCV data.      Uses the stand` and `Run the Phase 1 end-to-end dry-run pipeline.`) actually correct?**
  _`FetchError` has 161 INFERRED edges - model-reasoned connections that need verification._
- **Are the 126 inferred relationships involving `PaperTrader` (e.g. with `Compute Average True Range for a single symbol's OHLCV data.      Uses the stand` and `Run the Phase 1 end-to-end dry-run pipeline.`) actually correct?**
  _`PaperTrader` has 126 INFERRED edges - model-reasoned connections that need verification._