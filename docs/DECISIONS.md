# Decisions and Build Log

> Maintained by the Docs Agent. New entries added at the TOP after every build session.
> Most recent session is always first.
> For system overview, see `docs/SYSTEM.md`.
> For API details, see `docs/connections.md`.

---

## [2026-04-05] — src/agents/watchlist_agent.py
**Built**: Combined decision agent: screener rank + LLM sentiment → PROCEED/SKIP. Human approval checkpoint via Telegram+Gmail. Partial pre-trade scorecard (max 20pts). Non-blocking design — run_watchlist_agent() returns after sending checkpoint; orchestrator calls check_watchlist_timeout() at 07:00 IST.
**Connects to**: reads screener_results + research_reports; writes watchlist table; calls send_checkpoint/send_alert/send_info
**Next step**: Phase 4 — src/execution/auth.py (TOTP auto-login for Shoonya)
**Notes**: research_reports filtered by run_date column (not DATE(completed_at)). Mixed → PROCEED with 1 scorecard point. SKIP candidates written to watchlist for audit trail. record_human_approval() never raises. SCORECARD_THRESHOLD=28 documented but NOT enforced here — enforced by risk_agent on full scorecard.

---

## [2026-04-05] — src/agents/screener_agent.py
**Built**: 3-step weekly stock selection pipeline (quality filter → momentum → regime). Writes top 5 ranked candidates to screener_results table. Standalone-callable for Phase 4 emergency rescreens.
**Connects to**: reads fetch_ohlcv/fetch_sector_indices/fundamentals (no DB reads); writes screener_results table; calls apply_quality_filter, compute_momentum, apply_regime_filter
**Next step**: src/agents/watchlist_agent.py (Phase 3, Step 4) — reads screener_results + research_reports, applies combined decision rule, produces final watchlist
**Notes**: regime_blocked (BELOW_200DMA_10DAYS) still writes top5 with position_size_multiplier=0.0 — Watchlist Builder decides; screener_agent does not suppress. thin_universe (< 3 pass quality) returns early with empty top5 and send_alert. INSERT OR REPLACE on UNIQUE(symbol, run_date) — most recent run always authoritative.

---

## [2026-04-05] — Intraweek emergency rescreen (design decision)
**Decision**: Add intraweek emergency rescreen trigger to Phase 4 monitor_agent.py
**Detail**: monitor_agent checks Nifty 50 close-to-close daily at 15:35 IST. If drop > 3% → re-run full screener_agent pipeline immediately, update screener_results table, send Telegram alert: "Emergency rescreen triggered: Nifty dropped X% today. Watchlist updated." Open positions are NOT automatically closed — existing GTT stop-losses handle position protection. Monday rescreen still runs regardless of how many intraday rescreens happened that week. Threshold is close-vs-prev-close only (not intraday high-low range).
**Implementation notes**: Phase 4 feature (monitor_agent.py). screener_agent must be callable standalone (not only from the scheduled evening pipeline). Flag monitor_agent as a known caller in screener_agent spec.
**Next step**: src/agents/screener_agent.py (Phase 3, Step 3) — build with standalone callable design

---

## [2026-04-05] — signal_agent.py
**Built**: Morning signal confirmation agent reading screener_results, computing RSI/MACD/Bollinger/ATR, applying combined decision rule, Groq advisory LLM check.
**Connects to**: reads screener_results + research_reports; writes signals table; calls fetch_ohlcv, add_indicators, Groq API (requests.post), Gemini fallback (google-genai SDK)
**Next step**: src/agents/screener_agent.py (Phase 3, Step 3)
**Notes**: Both-LLM-failure behavior overridden from "skip all trades" to "keep rule-based BUY, groq_confidence=-1.0". RSI_BUY_THRESHOLD=40.0 is intentionally conservative — most days produce 0 BUY signals, which is correct. Hard deadline at 08:50 IST triggers late_start=True and empty result with no DB writes.

---

## [2026-04-01] — research_agent.py Tavily migration

**Built**: Switched news source from Brave Search to Tavily SDK
**Connects to**: reads screener_results table, writes research_reports table (same two-step INSERT+UPDATE pattern); calls Tavily Search API + Gemini 2.5 Flash API
**Next step**: src/agents/signal_agent.py — Groq morning confirmation
**Notes**: NewsData.io rejected due to 12-hour free-tier article delay (earnings announcements post-15:30 IST would be missed). Tavily: real-time news, topic=news/finance, published_date per result as ISO 8601 string. Earnings detection now parses published_date instead of fragile age-string heuristics. TAVILY_REQUEST_DELAY=0.5s (was BRAVE_REQUEST_DELAY=1.1s).

## [2026-03-30] — src/agents/research_agent.py

**Built**: Evening pipeline agent: Brave Search news fetch + Gemini 2.5 Flash synthesis per top-5 screener candidates
**Connects to**: reads screener_results table; writes research_reports table (two-step: INSERT then UPDATE completed_at); calls Brave Search API + Gemini 2.5 Flash API
**Next step**: src/agents/signal_agent.py — Groq morning confirmation with Gemini fallback
**Notes**: Two-step DB write (INSERT then UPDATE completed_at) prevents Watchlist Builder from reading incomplete rows. Direct HTTP to Brave (not MCP) for testability. google-genai 1.69.0 (new unified SDK, not deprecated google-generativeai). Earnings branch falls back to standard synthesis if transcript not retrievable (earnings_transcript_unavailable flag). Confidence is 0.0–1.0 per Gemini output. Source URLs always required (non-empty list).

## [2026-03-29] — src/backtest/validator.py

**Built**: Pure computation gate checker; evaluates BacktestResult against 5 backtest gates from risk.md
**Connects to**: reads BacktestResult from runner.py (in-memory); writes nothing to DB except agent_logs via log_agent_action
**Next step**: Phase 2 milestone — run backtest over 2010–2023, all 5 gates must pass before Phase 3 begins
**Notes**: Only module that sets gates_passed=True (via dataclasses.replace). Strict inequalities on all gates except total_trades which uses >=. float('inf') profit_factor passes naturally. Phase 2 build complete.

## [2026-03-29] — src/backtest/runner.py

**Built**: backtesting.py wrapper for full strategy validation over 2010–2023 historical data with multi-symbol portfolio tracking.
**Connects to**: reads fundamentals_history + nifty_constituents tables (via fundamentals.py); calls quality_filter, momentum, regime, technical modules; writes nothing to DB.
**Next step**: src/backtest/validator.py — gate checker that reads BacktestResult and sets gates_passed=True only if all 5 gates pass (Sharpe ≥ 1.0, drawdown < 15%, win rate > 40%, min 100 trades, profit factor > 1.3).
**Notes**: backtesting.py is single-symbol; solved by feeding Nifty 50 index as dummy instrument and running all trade logic through _PortfolioTracker class. Weekly rebalance uses (iso_year, iso_week) tuple to handle Diwali week and other multi-day holiday blocks. Warm-up period: no trades for first 400 calendar days (approx Feb 2011 for 2010 start). Weekend bar guard prevents processing Saturday NSE non-trading sessions.

## [2026-03-25] — src/data/fundamentals.py (historical additions)

**Built**: Point-in-time historical fundamentals (Screener.in scraping, SQLite storage) + Nifty 50 constituent universe for 2010–2023.
**Connects to**: Writes to fundamentals_history + nifty_constituents tables; called by backtest/runner.py.
**Next step**: src/backtest/runner.py spec update (now unblocked)
**Notes**: Fiscal year cutoff is month>=7 (July), NOT April — FY results not safely published until ~July. EPS approximation: annual EPS>0 maps to eps_positive_4q column for schema compat. FundamentalsError is local (not imported from validator.py — incompatible signature).

## [2026-03-25] — src/strategy/regime.py

**Built**: Nifty 50 200 DMA regime filter with three regimes, position size multipliers, and stop-loss tightening signals.
**Connects to**: Reads ranked_df + nifty_ohlcv_df + open_positions in memory; writes nothing; passes filtered_df to backtest/runner.py.
**Next step**: src/backtest/runner.py — Phase 2 Step 5
**Notes**: Pure computation — does NOT execute stop updates. Use fetch_sector_indices() for Nifty data (not fetch_ohlcv). close==SMA → ABOVE_200DMA. Integration confirmed: momentum test passed on real NSE data (6 stocks scored, top 5 selected).

## [2026-03-25] — src/strategy/momentum.py

**Built**: 12-1 momentum factor scoring with top-N selection and 2% adjacent-pair tiebreaker (lower pct_from_52w_high wins).
**Connects to**: Reads quality_df + ohlcv_df in memory; writes nothing; passes ranked_df to regime.py.
**Next step**: src/strategy/regime.py — Phase 2 Step 4
**Notes**: Tiebreaker is single-pass (not full re-sort). Tests required close[-21]=close[-1] to zero out 1m return and isolate 12m return for readable score values.

## [2026-03-25] — src/strategy/quality_filter.py

**Built**: Five hard quality filters (ROE, D/E, EPS, volume, price) with FilterReport dataclass and thin_universe detection.
**Connects to**: Reads fundamentals_df + ohlcv_df; writes nothing; called by screener_agent.py and backtest/runner.py.
**Next step**: src/strategy/momentum.py — Phase 2 Step 3
**Notes**: Stale fundamentals auto-fail all filters. No short-circuit evaluation (accurate failure counts). pct_from_52w_high passed downstream for momentum tiebreaker.

## [2026-03-24] — src/indicators/technical.py

**Built**: Pure pandas-ta indicator calculation module: RSI(14), MACD(12/26/9), Bollinger Bands(20,2.0), ATR(14 Wilder) per symbol, with per-symbol isolation via explicit for-loop and NaN-safe minimum lookback (26 rows).
**Connects to**: Reads cleaned OHLCV DataFrame from cleaner.py; writes nothing; called by signal_agent.py (Phase 3) and main.py for position sizing.
**Next step**: src/strategy/quality_filter.py — Phase 2 Step 2
**Notes**: pandas 3.0 broke groupby.apply (drops key column); fixed by explicit for-loop over groups. Bollinger Bands column names vary by pandas-ta version; fixed by prefix matching. ATR uses Wilder smoothing via pandas-ta RMA (SMA init for first N periods, not pure ewm from start). Symbols with < 26 rows get all-NaN indicators (conservative, prevents unreliable values).

## [2026-03-24] — src/execution/paper_trader.py

**Built**: CNC swing trade execution simulator with SQLite-backed orders, positions, trades tables and in-memory GTT simulation.
**Connects to**: Reads/writes orders, positions, trades tables in SQLite; logs to agent_logs via logger.log_agent_action(); reads settings.live_trading (gate), settings.max_trade_amount (cap), settings.database_url (DB path).
**Next step**: main.py — Phase 1 milestone, runs full pipeline in dry-run mode end-to-end (Phase 1, step 9 of 9).
**Notes**: update_stop_loss() added vs original requirements (regime + LLM sentiment tightening both need it); REGIME_TIGHTENED kept as distinct exit_reason (audit trail); caller provides fill prices (paper_trader is pure execution simulator, not a data fetcher — same interface works for paper and live trading); every order written to orders table BEFORE position/trade execution (never after); WAL mode pragmas applied at init time; all prices INR float, all quantities int (no fractional shares); all timestamps IST ISO 8601 with timezone offset.

## [2026-03-22] — src/utils/notifier.py

**Built**: Dual-channel Telegram + Gmail notification module with three types: ALERT and CHECKPOINT go to both channels, INFO goes to Telegram only.
**Connects to**: Reads settings.telegram_bot_token, telegram_chat_id, gmail_credentials; writes to agent_logs via log_agent_action(); writes token.json on first Gmail OAuth; calls Telegram Bot API and Gmail API.
**Next step**: src/execution/paper_trader.py — simulated order execution with realistic P&L tracking (Phase 1, step 8 of 9)
**Notes**: Gmail OAuth requires one-time browser authorization on first run. token.json and gmail_credentials.json are gitignored. Google library imports are lazy (inside _build_gmail_service only) — Telegram works even if Google packages not installed. _gmail_service_cache caches the built service object in memory across calls.

## [2026-03-22] — src/utils/logger.py

**Built**: SQLite-backed structured logging module; configures root logger with dual output (stderr + agent_logs table), thread-safe SQLiteHandler with public write_row() method.
**Connects to**: Writes to agent_logs table; reads settings.database_url and settings.log_level; all existing src.data.* modules flow through it automatically after setup_logging() is called.
**Next step**: src/utils/notifier.py — Telegram + Gmail dual-channel notifications (Phase 1, step 7 of 9)
**Notes**: Schema conflict with validator.py's legacy agent_logs table documented in spec Section 5. Future task will migrate validator.py to use log_agent_action(). log_agent_action() calls handler.write_row() (public) — does not access private handler attributes.

## [2026-03-22] — Fundamentals Fetcher (src/data/fundamentals.py)
**Built**: Screener.in scraper with 45-day JSON cache, 3-strike yfinance fallback, and P/E cross-validation between sources. Returns fundamentals DataFrame matching validator.py Section 5.2 contract plus quality metadata columns.
**Connects to**: Reads from Screener.in and yfinance APIs. Writes JSON cache to data/cache/. Imports settings singleton for log_level. Output consumed by validator.py (roe + debt_to_equity) and quality_filter.py (Phase 2, all fields).
**Next step**: src/utils/logger.py — structured logging to SQLite agent_logs table (Phase 1, step 6 of 9)
**Notes**: JSON cache (not CSV) chosen for sparse structured data. _read_stale_cache separate from _read_cache enables graceful degradation when both sources fail (returns stale data flagged as fundamentals_stale rather than crashing). Cross-validation excluded for yfinance fallback to avoid self-referential comparison. yfinance eps_positive_4q uses trailingEps > 0 — documented approximation since per-quarter data unavailable from yfinance. Strike counter is loop-local (resets per symbol per call). new deps: requests>=2.31.0, beautifulsoup4>=4.12.0. 31/31 tests passing.

## [2026-03-22] — Data Fetcher (src/data/fetcher.py)
**Built**: OHLCV data acquisition layer with yfinance primary source, jugaad-data fallback, and CSV cache in data/cache/. Returns DataFrames matching the validator.py Section 5.1 contract exactly.
**Connects to**: Reads from yfinance API and jugaad-data NSE API. Writes CSV cache files to data/cache/ (gitignored). Imports settings singleton for log_level. Output consumed by cleaner.py (Phase 1 step 4).
**Next step**: src/data/cleaner.py — missing value handling and anomaly flagging (Phase 1, step 4 of 9)
**Notes**: Architect confirmed jugaad-data now returns DATE/OPEN/HIGH/LOW/CLOSE/VOLUME/SYMBOL columns (not the older CH_* names). No jugaad fallback for sector indices — index API unreliable. HDFCBANK symbol corrected (spec initially had "HDFC BANK" with a space). 31/31 tests passing. Code Reviewer flagged one test fragility (hardcoded relative path in bare-except test) — fixed before commit.

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

## [2026-03-22] — Data Cleaner (src/data/cleaner.py)
**Built**: Best-effort OHLCV repair module — forward-fills missing prices (per-symbol, no cross-symbol bleed), removes duplicate dates (keep last), and flags anomalies (negative prices, high < low, price floor) without ever dropping rows.
**Connects to**: Receives DataFrame from fetcher.py. Returns cleaned DataFrame + CleaningReport to caller. No DB writes, no external calls. Imports settings singleton for log_level only.
**Next step**: src/data/fundamentals.py — Screener.in scraper with 45-day cache and yfinance fallback (Phase 1, step 5 of 9)
**Notes**: Deliberately does not import _validate_ohlcv_df from validator.py — duplicates the schema check to avoid private API coupling. Price floor of 1.0 INR is data sanity only (not the strategy's 50 INR filter). Code Reviewer noted clean_ohlcv does not re-sort output — acceptable since fetcher output is always pre-sorted. 30/30 tests passing.

## [2026-03-22] — Data Fetcher (src/data/fetcher.py)