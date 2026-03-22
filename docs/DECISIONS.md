# Decisions and Build Log

> Maintained by the Docs Agent. New entries added at the TOP after every build session.
> Most recent session is always first.
> For system overview, see `docs/SYSTEM.md`.
> For API details, see `docs/connections.md`.

---

## [2026-03-22] — Data Validator (src/data/validator.py)
**Built**: Data quality gate module that validates OHLCV and fundamentals DataFrames for ROE plausibility, D/E coverage, and OHLCV gap continuity before any strategy logic runs.
**Connects to**: Writes to agent_logs table in data/trading.db. Reads from DataFrames passed by caller — no direct data fetching.
**Next step**: src/config/settings.py — env loading and validation at startup (Phase 1, step 2 of 9)
**Notes**: Module is the mandatory first build in Phase 1. No mocks — validates real data. DataQualityError halts the pipeline if universe_quality_score < 0.6. Scoring: ROE plausibility 0.40 weight, ROE present 0.10, OHLCV gaps 0.50. D/E coverage below 80% deducts 0.10 from all per-stock scores.

<!-- Docs Agent: prepend new session entries above this line -->