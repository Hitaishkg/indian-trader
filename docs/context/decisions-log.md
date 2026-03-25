# Decisions Log — Fast Reference

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
