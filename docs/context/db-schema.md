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

### screener_results (written by: Screener Agent — Phase 4, step 2)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| roe | REAL | Return on equity (as decimal, e.g. 0.20 = 20%) |
| debt_to_equity | REAL | Debt-to-equity ratio |
| momentum_12_1 | REAL | 12-month return minus 1-month return |
| quality_passed | INTEGER | 1 if passed all 5 filters, 0 otherwise |
| regime_above_200dma | INTEGER | 1 if Nifty 50 above 200-day SMA |
| rank | INTEGER | Momentum rank (1-5 for top 5 stocks) |
| screened_at | TEXT | ISO 8601 IST timestamp |

### research_reports (written by: Research Agent — Phase 3/4, step 3)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| sentiment | TEXT | "Positive", "Negative", "Neutral", "Mixed" |
| confidence | REAL | LLM confidence score 0.0–1.0 |
| source_urls | TEXT | JSON list of source URLs (required field) |
| earnings_transcript_unavailable | INTEGER | 1 if earnings reported but transcript not retrievable |
| completed_at | TEXT | ISO 8601 IST timestamp (NULL until fully done, prevents race conditions) |

### watchlist (written by: Watchlist Builder Agent — Phase 4, step 4)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| trade_type | TEXT | "LONG" or "SHORT" |
| thesis | TEXT | Rationale (quality filter + momentum + sentiment) |
| approved_by_human | INTEGER | 1 if user approved by 08:00 IST, 0 if default accepted |
| approved_at | TEXT | ISO 8601 IST timestamp of approval |
| built_at | TEXT | ISO 8601 IST timestamp when watchlist was built |

### morning_signals (written by: Morning Validator Agent — Phase 4, step 5)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| overnight_event | TEXT | Reason if removed ("earnings", "circuit_breaker", "trading_halt", etc.) or NULL |
| regime_still_valid | INTEGER | 1 if Nifty 50 still 200-day SMA check valid |
| validated_at | TEXT | ISO 8601 IST timestamp |

### signals (written by: Signal Agent — Phase 4, step 6)
| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT | NSE ticker symbol |
| rsi | REAL | RSI value (0-100) |
| macd_signal | TEXT | "BUY" or "HOLD" |
| bollinger_position | TEXT | "ABOVE", "MIDDLE", "BELOW" |
| atr | REAL | Average True Range (INR) |
| groq_confidence | REAL | Groq signal confidence 0.0–1.0 |
| signal_type | TEXT | "BUY", "HOLD", "SELL" |
| signalled_at | TEXT | ISO 8601 IST timestamp |

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
