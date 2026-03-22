# Decisions and Build Log

> Maintained by the Docs Agent. New entries added at the TOP after every build session.
> Most recent session is always first.
> For system overview, see `docs/SYSTEM.md`.
> For API details, see `docs/connections.md`.

---

## [2026-03-22] — Config Settings (src/config/settings.py)
**Built**: Environment loading and validation module — frozen Settings dataclass singleton with all 16 config variables, three-tier variable categorisation (always-required / optional-with-default / phase-gated), and startup-time ConfigurationError reporting all problems at once.
**Connects to**: Reads from .env via python-dotenv. Writes nothing. All other Phase 1+ modules will import the `settings` singleton from here.
**Next step**: src/data/fetcher.py — yfinance + jugaad-data OHLCV fetcher with CSV caching (Phase 1, step 3 of 9)
**Notes**: Phase-gated variables (SHOONYA_*, FYERS_API_KEY, BRAVE_API_KEY, GMAIL_CREDENTIALS) return None when absent — no startup error. Required from Phase 3/4 respectively. Safety interlock prevents LIVE_TRADING and PAPER_TRADING from both being True. 31/31 acceptance criteria tests passing. Code Reviewer noted interlock error is raised separately (not accumulated) — this is by design since the interlock is a post-validation safety check.

## [2026-03-22] — Data Validator (src/data/validator.py)
**Built**: Data quality gate module that validates OHLCV and fundamentals DataFrames for ROE plausibility, D/E coverage, and OHLCV gap continuity before any strategy logic runs.
**Connects to**: Writes to agent_logs table in data/trading.db. Reads from DataFrames passed by caller — no direct data fetching.
**Next step**: src/config/settings.py — env loading and validation at startup (Phase 1, step 2 of 9)
**Notes**: Module is the mandatory first build in Phase 1. No mocks — validates real data. DataQualityError halts the pipeline if universe_quality_score < 0.6. Scoring: ROE plausibility 0.40 weight, ROE present 0.10, OHLCV gaps 0.50. D/E coverage below 80% deducts 0.10 from all per-stock scores.

<!-- Docs Agent: prepend new session entries above this line -->