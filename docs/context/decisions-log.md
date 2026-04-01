# Decisions Log — Fast Reference
[2026-04-01] research_agent.py — switched Brave→Tavily; NewsData.io rejected (12h free-tier delay); Tavily published_date ISO string replaces Brave age-string heuristics for earnings detection
[2026-03-30] research_agent.py — direct HTTP to Brave (not MCP) for testability; two-step DB write (INSERT then UPDATE completed_at) prevents Watchlist Builder race condition; google-genai>=1.0.0 (not deprecated google-generativeai)
[2026-03-29] backtest/validator.py — gates_passed=True set only here via dataclasses.replace(); strict inequalities for all gates except total_trades (>=); float('inf') passes profit_factor gate naturally
[2026-03-29] backtest/runner.py — uses Nifty 50 index as dummy instrument for backtesting.py (single-symbol lib); all multi-stock logic in _PortfolioTracker class. Weekly rebalance on (iso_year, iso_week) tuple not iso_week alone to handle Diwali week and multi-day holiday blocks.
[2026-03-25] fundamentals.py (historical) — fiscal year cutoff is month >= 7 (July), NOT month >= 4 (April). Indian FY results published 2–3 months after March year-end; using April creates lookahead bias. July is the safe cutoff.
[2026-03-25] regime.py — pure computation module; does NOT call update_stop_loss(); only returns stop_tighten_symbols; caller executes stop updates
[2026-03-25] regime.py — nifty_ohlcv_df requires only date+close (no symbol); use fetch_sector_indices() not fetch_ohlcv() to get Nifty 50 data (confirmed 270 rows available)
[2026-03-25] regime.py — close == 200 SMA → ABOVE_200DMA (uses >=); avoids unnecessary position reduction at boundary
[2026-03-25] fetcher.py- fetcher cache validates file age only, not date range coverage.Workaround: use cache_expiry_hours=0 when longer history needed.Fix planned for Phase 3/4 cache layer.
[2026-03-25] momentum.py — tiebreaker is a single adjacent-pair pass (not a full re-sort); only swaps if rel_diff < 2%; lower pct_from_52w_high wins (closer to 52w high)
[2026-03-25] momentum.py — test_tiebreaker and test_negative_scores required close[-21]=close[-1] to zero out 1m return; otherwise 12m and 1m returns cancel and score=0
[2026-03-25] quality_filter.py — stale/failed fundamentals auto-fail all 5 filters before column checks to prevent trading on corrupt data
[2026-03-25] quality_filter.py — all 5 filters always evaluated (no short-circuit) to produce accurate filter_failure_counts for audit trail
[2026-03-24] technical.py — pandas 3.0 groupby.apply drops key column; fixed by iterating groupby groups explicitly with for-loop
[2026-03-24] technical.py — bbands column names vary by pandas-ta version (BBL_20_2.0 vs other formats); fixed by prefix matching (BBL_, BBM_, BBU_)
[2026-03-24] paper_trader.py — update_stop_loss() added (not in original requirements) — needed for regime tightening (2× ATR → 1× ATR) and LLM sentiment tightening (confidence > 0.8 Negative); REGIME_TIGHTENED kept as own exit_reason distinct from MANUAL_EXIT (audit trail); caller provides prices (paper_trader is pure execution simulator only, not a price fetcher — same interface works for paper and live trading).

[2026-03-22] validator.py — Module is mandatory first build in Phase 1. No mocks — validates real data. DataQualityError halts the pipeline if universe_quality_score < 0.6.

[2026-03-22] settings.py — Phase-gated variables (SHOONYA_*, FYERS_API_KEY, BRAVE_API_KEY, GMAIL_CREDENTIALS) return None when absent — no startup error. Safety interlock prevents LIVE_TRADING and PAPER_TRADING from both being True.

[2026-03-22] fetcher.py — jugaad-data now returns DATE/OPEN/HIGH/LOW/CLOSE/VOLUME/SYMBOL columns. No jugaad fallback for sector indices — yfinance only. HDFCBANK symbol verified (no space). Cache expiry default 24 hours.

[2026-03-22] cleaner.py — Deliberately does not import validator.py's _validate_ohlcv_df — duplicates schema check to avoid private API coupling. Price floor of 1.0 INR is data sanity only (not the strategy's 50 INR filter). Anomalous rows are never dropped — only flagged.

[2026-03-22] fundamentals.py — JSON cache (not CSV) chosen for sparse structured data. _read_stale_cache separate from _read_cache enables graceful degradation when both sources fail. Cross-validation excluded for yfinance fallback (would be self-referential). yfinance eps_positive_4q uses trailingEps > 0 — documented approximation.

[2026-03-22] logger.py — Schema conflict with validator.py's legacy agent_logs table documented. Future task will migrate validator.py to use log_agent_action(). log_agent_action() calls handler.write_row() (public) — does not access private handler attributes. Database path resolved from settings.database_url; idempotent setup.

[2026-03-22] notifier.py — Gmail OAuth requires one-time browser authorization on first run. token.json and gmail_credentials.json are gitignored. Google library imports are lazy (inside _build_gmail_service only) — Telegram works even if Google packages not installed. _gmail_service_cache caches the built service object in memory across calls.
