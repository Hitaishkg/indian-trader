# Data Sources and Validation

## Source Hierarchy

| Data type | Primary source | Fallback | Cost |
|-----------|---------------|---------|------|
| Historical OHLCV (backtest) | jugaad-data (NSE direct, 2010-2023) | yfinance (.NS suffix) | Free |
| Live prices + daily OHLCV | Fyers API WebSocket | nsepython spot checks | Free |
| Fundamentals (ROE, D/E, EPS) | Screener.in scraping | yfinance on 3-strike failure | Free |
| Nifty 50 official P/E, P/B | NSE India website directly | No fallback needed | Free |
| Factor research validation | IIM Ahmedabad FF-Momentum library | faculty.iima.ac.in/iffm/ | Free |
| Market news (nightly) | Brave Search MCP | NSE announcements page | Free tier |
| Earnings transcripts | Brave Search MCP | Flag + fall back to standard news | Free tier |
| FII/DII flow data | Brave Search MCP query | NSE website direct | Free |

---

## Screener.in — Usage Rules

Scrape with 2–5 second delays between requests. Cache all results locally.
Data is updated quarterly for fundamentals — do not fetch more than necessary.

Cache expiry: any cached fundamentals older than 45 days are flagged as
fundamentals_stale. Treat stale entries as if they failed the quality filter.
Do not trade on fundamentals that are 45+ days old.

3-strike fallback rule: if Screener.in returns errors on 3 consecutive
requests for the same stock → automatically fall back to yfinance fundamentals.
Log the failure as screener_fallback with timestamp. Log data_quality: degraded
against every trade decision made using yfinance fallback data. This creates an
audit trail so you can later evaluate whether degraded-data days produced
different outcomes than clean-data days.

Cross-validation rule: for every stock, cross-check one Screener.in field
against yfinance before using the data. If P/E ratio deviates by more than 20%
between the two sources → flag as stale_data, skip this stock entirely rather
than trading on potentially corrupt fundamentals.

For Nifty 50 stocks specifically: NSE India publishes official P/E and P/B
directly on their website. Use this as the primary source for Nifty 50
fundamentals — no ToS concerns, official data.

---

## Data Validator (src/data/validator.py)

This is the FIRST module built in Phase 1. It runs on actual live data,
not mocks, and checks real data quality before any strategy logic runs.

Required checks:
- ROE values are plausible: between -50% and 200%
  (values outside this range indicate data corruption)
- D/E present for at least 80% of the Nifty 50 universe
  (if missing for more than 20%, flag as data_coverage_low)
- OHLCV data complete with no gaps longer than 5 consecutive trading days
  (gaps indicate missing data that will corrupt momentum calculations)

Output: a data_quality_score logged alongside every trade decision in agent_logs.
This makes it possible to audit whether a bad trade came from bad data.

---

## LLM Providers

| Provider | Model | Used for | Rate limit (free) | Fallback |
|---------|-------|---------|-------------------|---------|
| Gemini | 2.5 Flash free tier | Nightly research synthesis | 250 RPD | Groq |
| Groq | Llama 3.3 70B free tier | Morning signal confirmation | 1,000 RPD | Gemini |
| Ollama (local) | Llama 3.2 3B | Local fallback if both above fail | Unlimited | None |

Free tier risk mitigation: both Gemini and Groq are VC-funded services.
Free tiers can change without notice. Ollama running locally is the permanent
fallback. If both cloud free tiers fail simultaneously → use Ollama for
morning signal confirmation. For nightly research synthesis, Ollama is slower
but sufficient for a batch job run at 22:00.

---

## Notifications — Always Both Channels

Telegram AND Gmail must both receive every notification. Never send to only one.

This is not redundancy for its own sake. Telegram's delivery in India is
generally reliable but not guaranteed. If Telegram fails and the system
proceeds without human approval → the human checkpoint is eliminated.
If Telegram fails and the system enters safe mode → you lose a trading day
without knowing why.

Email via Gmail API is the permanent fallback. Both must confirm delivery.
If both channels fail → default to safe mode automatically, log the failure.

---

## Environment Variables

All secrets in .env only. Never in code. Never in comments. Never in logs.

Required variables:
```
LIVE_TRADING=false
PAPER_TRADING=true
LOG_LEVEL=INFO
MAX_TRADE_AMOUNT=10000
DATABASE_URL=sqlite:///data/trading.db
SHOONYA_USER=your_shoonya_user_id
SHOONYA_PASSWORD=your_shoonya_password
SHOONYA_TOTP_SECRET=totp_secret_from_shoonya_setup
FYERS_API_KEY=your_fyers_api_key
GROQ_API_KEY=your_groq_key
GEMINI_API_KEY=your_google_ai_studio_key
GITHUB_PAT=your_github_personal_access_token
BRAVE_API_KEY=your_brave_search_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
GMAIL_CREDENTIALS=path_to_gmail_oauth_credentials.json
```