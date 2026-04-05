# Indian Trader — Agentic Trading System

Two-layer system: Claude Code subagents build the codebase. Python Agent SDK
runs the trading pipeline. Both share one SQLite database.
Language: Python 3.12. Package manager: uv.

---

## Hard rules — enforced every session, no exceptions

- NEVER place live orders without LIVE_TRADING=true in .env AND explicit human
  confirmation in chat
- NEVER store API keys, secrets, or tokens in code, comments, or logs — .env only
- NEVER modify .env directly — tell the user what to add, let them do it
- All prices in INR as float. All quantities as int — always round DOWN, never up
- Every order must be written to the orders table BEFORE execution, never after
- All timestamps in IST (Asia/Kolkata). Market hours: 09:15–15:30 IST weekdays
- No bare except clauses — always catch specific exceptions
- Type hints on every function. Docstring on every public function
- MAX_TRADE_AMOUNT=10000 is a hard cap — never exceed in any single trade
- Strategies go live ONLY after passing ALL backtest gates AND 8-week paper gate
- data/validator.py must be the FIRST module built in Phase 1 — before everything else

---

## OUTPUT DISCIPLINE — enforced every response, no exceptions

- No preamble. Start with the answer directly.
- No filler: "I'll now...", "Let me...", "Great question"
- No restating the task before doing it.
- No summary at the end repeating what was just done.
- Explain only when explanation adds information not already obvious from context.
- Telegram messages: only what the human needs to act on.
- Specs: state decisions, not reasoning behind obvious ones.
- Test results: numbers and pass/fail, narrative only if something failed and needs diagnosis.
- Every sentence must earn its place. If removing it loses nothing, remove it.

---

## Verification commands — run after every change

```bash
python -m pytest tests/ -v
python -m mypy src/ --ignore-missing-imports
python -m ruff check src/
python main.py
```

---

## Environment setup

```bash
uv sync
source .venv/bin/activate
cp .env.example .env
```

---

## Workflow for every new feature

1. /plan before any multi-file change
2. Invoke architect agent — reviews codebase, writes spec to docs/specs/
3. Review spec — approve or reject in writing before any code is written
4. Build pipeline: architect → coder → tester → debugger → code-reviewer → github-agent
5. Update docs/DECISIONS.md with what was built and why
6. Docs Agent updates docs/context/ after every completed module
---

## Rules files (loaded automatically)

@.claude/rules/strategy.md
@.claude/rules/risk.md
@.claude/rules/agents-build.md
@.claude/rules/agents-trading.md
@.claude/rules/data.md
@.claude/rules/phases.md