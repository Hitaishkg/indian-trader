# Database Schema

## SQLite — data/trading.db

### agent_logs (written by: src/utils/logger.py)

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| logged_at | TEXT | ISO 8601 IST timestamp with timezone (e.g. "2026-03-22T22:15:30+05:30") |
| agent_name | TEXT | Identifies agent or module ("validator", "fetcher", "cleaner", "logger", "notifier", etc.) |
| level | TEXT | Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| action | TEXT | Human-readable description of what happened |
| symbol | TEXT | NSE ticker symbol or NULL for universe-level events |
| result | TEXT | Short outcome string ("ok", "error", "skipped") or NULL |
| data_quality_score | REAL | Float 0.0–1.0 or NULL |

Note: src/data/validator.py also writes to agent_logs directly (legacy schema, Phase 1); will be unified with log_agent_action() in Phase 2+ cleanup.

---

## Phase 1 Tables (written by src/execution/paper_trader.py)

### orders (written by: src/execution/paper_trader.py — place_order() and close_position())
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT | NSE ticker symbol |
| order_type | TEXT | Always 'CNC' (delivery) during Phase 1-5 |
| side | TEXT | 'BUY' or 'SELL' |
| quantity | INTEGER | Shares, must be > 0 |
| entry_price | REAL | Fill price in INR, must be > 0 |
| stop_loss | REAL | Stop-loss trigger price in INR, must be > 0 |
| take_profit | REAL | Take-profit trigger price in INR, must be > 0 |
| order_id | TEXT | Paper trading order ID ("PAPER-{hex12}") |
| gtt_sl_id | TEXT | Paper trading GTT stop-loss ID ("GTT-SL-{hex12}") or NULL for SELL orders |
| gtt_tp_id | TEXT | Paper trading GTT take-profit ID ("GTT-TP-{hex12}") or NULL for SELL orders |
| placed_at | TEXT | ISO 8601 IST timestamp when order written to DB |
| status | TEXT | 'PENDING' initially, updated to 'FILLED' after position/trade written |

### positions (written by: src/execution/paper_trader.py — place_order() BUY, closed by close_position())
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT | NSE ticker symbol, UNIQUE constraint (max 1 position per symbol) |
| quantity | INTEGER | Shares held, must be > 0 |
| entry_price | REAL | Entry price in INR, must be > 0 |
| current_price | REAL | Last updated market price in INR, must be > 0; updated by check_gtts() |
| stop_loss | REAL | Stop-loss level in INR, must be > 0; may be tightened by update_stop_loss() |
| take_profit | REAL | Take-profit level in INR, must be > 0 |
| pnl | REAL | Unrealised P&L in INR; updated by check_gtts() as current_price changes |
| pnl_pct | REAL | Unrealised P&L as percentage; updated by check_gtts() |
| opened_at | TEXT | ISO 8601 IST timestamp when position opened (same as order placed_at) |
| updated_at | TEXT | ISO 8601 IST timestamp of last update (current_price, stop_loss, P&L) |

### trades (written by: src/execution/paper_trader.py — close_position() and place_order() SELL execution)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT | NSE ticker symbol |
| quantity | INTEGER | Shares bought and sold, must be > 0 |
| entry_price | REAL | Entry price in INR, must be > 0 |
| exit_price | REAL | Exit price at stop-loss or take-profit, in INR, must be > 0 |
| pnl | REAL | Realised P&L in INR (exit_price - entry_price) × quantity |
| pnl_pct | REAL | Realised P&L as percentage ((exit_price - entry_price) / entry_price) × 100 |
| exit_reason | TEXT | 'STOP_LOSS', 'TAKE_PROFIT', 'MANUAL_EXIT', or 'REGIME_TIGHTENED' |
| opened_at | TEXT | ISO 8601 IST timestamp when position opened (from positions.opened_at) |
| closed_at | TEXT | ISO 8601 IST timestamp when position closed |

---

## Phase 2 Tables (written by src/data/fundamentals.py — historical additions)

### fundamentals_history (written by: fetch_historical_fundamentals())
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT NOT NULL | NSE ticker symbol |
| fiscal_year | INTEGER NOT NULL | Indian fiscal year (e.g. 2020 = FY2020 = Apr 2019 - Mar 2020) |
| roe | REAL | Return on equity as decimal (0.20 = 20%); NULL if not extractable |
| debt_to_equity | REAL | D/E ratio; NULL if not extractable |
| eps_positive | INTEGER | 1 if annual EPS > 0, 0 if <= 0, NULL if not extractable. NOTE: annual approximation of quarterly check |
| data_source | TEXT NOT NULL | "screener", "yfinance_fallback", or "failed" |
| data_quality | TEXT NOT NULL | "clean", "degraded", or "failed" |
| fetched_at_ist | TEXT NOT NULL | ISO 8601 IST timestamp of when row was fetched |

UNIQUE constraint on (symbol, fiscal_year). Upsert via INSERT OR REPLACE.
Staleness: rows older than 45 days (fetched_at_ist) are refreshed on next fetch.

### nifty_constituents (written by: _populate_nifty_constituents())
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT NOT NULL | NSE ticker symbol |
| year | INTEGER NOT NULL | Calendar year (2010-2023) |
| in_index | INTEGER NOT NULL | 1 if stock was in Nifty 50 that year, 0 otherwise |

UNIQUE constraint on (symbol, year). Populated once from hardcoded list via INSERT OR IGNORE.
Lazy-initialized on first call to get_nifty_universe_for_year().

---

## Future Tables (written by Trading Layer agents — not yet built)

### market_data (written by: Data Collector Agent — Phase 4, step 1)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| date | TEXT | Trading date (ISO 8601, IST) |
| open | REAL | Opening price (INR) |
| high | REAL | High price (INR) |
| low | REAL | Low price (INR) |
| close | REAL | Closing price (INR) |
| volume | REAL | Volume traded |

### screener_results (written by: src/agents/screener_agent.py — ✅ Built)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT NOT NULL | NSE ticker symbol |
| run_date | TEXT NOT NULL | ISO 8601 date for which the screen was run |
| rank | INTEGER NOT NULL | Momentum rank (1 = highest; top 5 candidates) |
| momentum_score | REAL NOT NULL | 12-1 momentum factor score |
| quality_passed | INTEGER NOT NULL | 1 if passed all 5 hard quality filters, 0 otherwise |
| regime | TEXT NOT NULL | "ABOVE_200DMA", "BELOW_200DMA", or "BELOW_200DMA_10DAYS" |
| position_size_multiplier | REAL NOT NULL | 1.0 / 0.5 / 0.0 based on regime |
| screened_at | TEXT NOT NULL | ISO 8601 IST timestamp when this row was computed |

UNIQUE constraint on (symbol, run_date). Upsert via INSERT OR REPLACE.
Re-runs on the same date overwrite prior results — most recent run is always authoritative.

### research_reports (written by: Research Agent — Phase 3/4, step 3)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| sentiment | TEXT | "Positive", "Negative", "Neutral", "Mixed" |
| confidence | REAL | LLM confidence score 0.0–1.0 |
| source_urls | TEXT | JSON list of source URLs (required field) |
| earnings_transcript_unavailable | INTEGER | 1 if earnings reported but transcript not retrievable |
| completed_at | TEXT | ISO 8601 IST timestamp (NULL until fully done, prevents race conditions) |

### watchlist (written by: src/agents/watchlist_agent.py — ✅ Built)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT NOT NULL | NSE ticker symbol |
| run_date | TEXT NOT NULL | ISO 8601 date for which watchlist was built |
| combined_decision | TEXT NOT NULL | "PROCEED" or "SKIP" |
| scorecard_score | INTEGER NOT NULL | Watchlist-stage partial scorecard points (0–20 or 0–15 with earnings) |
| scorecard_max | INTEGER NOT NULL | Max possible points for this candidate (20 or 15 with earnings flag) |
| sentiment | TEXT NOT NULL | "Positive", "Negative", "Neutral", or "Mixed" |
| confidence | REAL NOT NULL | LLM confidence 0.0–1.0 |
| rank | INTEGER NOT NULL | Momentum rank from screener (1=highest) |
| regime | TEXT NOT NULL | "ABOVE_200DMA", "BELOW_200DMA", or "BELOW_200DMA_10DAYS" |
| position_size_multiplier | REAL NOT NULL | 1.0 / 0.5 / 0.0 from regime filter |
| human_approved | INTEGER NOT NULL DEFAULT 0 | 1 if approved by human, 0 if pending/rejected/timed_out |
| approval_source | TEXT | "human_explicit" / "timeout_skip" / NULL if pending |
| added_at | TEXT NOT NULL | ISO 8601 IST timestamp when row inserted |
| UNIQUE(symbol, run_date) | — | No duplicate evaluations per symbol per run_date |

### morning_signals (written by: Morning Validator Agent — Phase 4, step 5)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| overnight_event | TEXT | Reason if removed ("earnings", "circuit_breaker", "trading_halt", etc.) or NULL |
| regime_still_valid | INTEGER | 1 if Nifty 50 still 200-day SMA check valid |
| validated_at | TEXT | ISO 8601 IST timestamp |

### signals (written by: Signal Agent — Phase 3, signal_agent.py — ✅ Built)
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Auto-incrementing row ID |
| symbol | TEXT NOT NULL | NSE ticker symbol |
| run_date | TEXT NOT NULL | ISO 8601 date for which the signal was generated |
| rsi | REAL NOT NULL | RSI value (0–100) |
| macd_signal | TEXT NOT NULL | "BUY" or "HOLD" |
| bollinger_position | TEXT NOT NULL | "ABOVE", "MIDDLE", or "BELOW" |
| atr | REAL NOT NULL | Average True Range in INR |
| groq_confidence | REAL NOT NULL | Groq advisory confidence 0.0–1.0; -1.0 sentinel when both LLMs unavailable |
| signal_type | TEXT NOT NULL | "BUY" or "HOLD" |
| skip_reason | TEXT | NULL on BUY; populated on HOLD with reason (e.g. "negative_sentiment", "groq_low_confidence") |
| signalled_at | TEXT NOT NULL | ISO 8601 IST timestamp |

Index: `idx_signals_symbol_date` on (symbol, run_date).
groq_confidence = -1.0 means both Groq and Gemini were unavailable; rule-based BUY is kept (not skipped).

### risk_approvals (written by: Risk Agent — Phase 4, step 7)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| position_size | INTEGER | Shares to buy (rounded down) |
| entry_price | REAL | Entry price (INR) |
| stop_loss | REAL | Stop-loss price (INR, 2× ATR) |
| take_profit | REAL | Take-profit price (INR, 1:2 risk-reward min) |
| approval_status | TEXT | "APPROVED" or "REJECTED" |
| rejection_reason | TEXT | Reason if rejected ("drawdown_kill_switch", "max_positions", etc.) or NULL |
| approved_at | TEXT | ISO 8601 IST timestamp |

### orders (written by: Execution Agent — Phase 4, step 8)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| order_type | TEXT | "CNC" (delivery) or "INTRADAY" |
| side | TEXT | "BUY" or "SELL" |
| quantity | INTEGER | Shares |
| entry_price | REAL | Execution price (INR) |
| stop_loss | REAL | Stop-loss price (INR) |
| take_profit | REAL | Take-profit price (INR) |
| order_id | TEXT | Shoonya broker order ID |
| gtt_sl_id | TEXT | Shoonya GTT order ID for stop-loss |
| gtt_tp_id | TEXT | Shoonya GTT order ID for take-profit |
| placed_at | TEXT | ISO 8601 IST timestamp |
| status | TEXT | "PENDING", "FILLED", "REJECTED" |

### positions (written by: Monitor Agent — Phase 4, step 9)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| quantity | INTEGER | Shares held |
| entry_price | REAL | Entry price (INR) |
| current_price | REAL | Current market price (INR) |
| stop_loss | REAL | Current stop-loss level (may be tightened on regime filter) |
| take_profit | REAL | Take-profit level |
| pnl | REAL | Unrealised P&L (INR) |
| pnl_pct | REAL | Unrealised P&L (%) |
| opened_at | TEXT | ISO 8601 IST timestamp when position opened |
| updated_at | TEXT | ISO 8601 IST timestamp when position last updated |

### trades (written by: Monitor Agent at exit — Phase 4, step 9)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| quantity | INTEGER | Shares bought |
| entry_price | REAL | Entry price (INR) |
| exit_price | REAL | Exit price at stop-loss or take-profit (INR) |
| pnl | REAL | Realised P&L (INR) |
| pnl_pct | REAL | Realised P&L (%) |
| exit_reason | TEXT | "STOP_LOSS", "TAKE_PROFIT", "MANUAL_EXIT" |
| opened_at | TEXT | ISO 8601 IST timestamp |
| closed_at | TEXT | ISO 8601 IST timestamp |

### daily_pnl (written by: Reporter Agent — Phase 4, step 10)
| Column | Type | Notes |
|--------|------|-------|
| date | TEXT | Trading date (ISO 8601) |
| pnl | REAL | Daily P&L (INR) |
| win_count | INTEGER | Number of winning trades |
| loss_count | INTEGER | Number of losing trades |
| win_rate | REAL | Win rate (%) |
| sharpe | REAL | Rolling Sharpe ratio |
| max_drawdown | REAL | Drawdown (%) from equity peak |
| reported_at | TEXT | ISO 8601 IST timestamp (15:45 IST) |

### strategy_perf (written by: Reporter Agent — Phase 4, step 10)
| Column | Type | Notes |
|--------|------|-------|
| date | TEXT | Trading date (ISO 8601) |
| equity | REAL | Account equity at end of day (INR) |
| cumulative_pnl | REAL | Cumulative P&L since start (INR) |
| sharpe_8w | REAL | 8-week rolling Sharpe ratio |
| reported_at | TEXT | ISO 8601 IST timestamp |
