# Build Layer — Claude Code Subagents

Six agents in strict hierarchy. Each triggers only the next one.
No skipping steps. Nothing reaches GitHub without tests passing and
Code Reviewer outputting PASS.

## Model Tiers

Three tiers — assigned by task type, not convenience:

| Tier | Model | Rationale |
|------|-------|-----------|
| **Opus** | claude-opus-4-5 | Strategic thinking, design decisions, spec authoring |
| **Sonnet** | claude-sonnet-4-5 | Implementation quality, debugging, security judgment |
| **Haiku** | claude-haiku-4-5 | Mechanical execution, pattern matching, templated tasks |

---

## Agent Hierarchy

### 1. Architect Agent — **Opus**
File: `.claude/agents/architect.md`
- Model: claude-opus-4-5
- Triggered by: you describing a feature or module to build
- Tools: Read, Grep, Glob — read-only, no writing or running commands
- Does: reads the existing codebase to understand current state,
  writes a spec file to docs/specs/YYYY-MM-DD-feature-name.md
- Cannot: write code, run commands, modify any files
- Output: spec file for Coder Agent to read
- You review and approve the spec BEFORE Coder starts

### 2. Coder Agent — **Sonnet**
File: `.claude/agents/coder.md`
- Model: claude-sonnet-4-5
- Triggered by: approved spec file from Architect
- Tools: Read, Write, Edit, MultiEdit, Bash(ruff check only)
- Does: implements exactly what the spec says — nothing more, nothing less
- Cannot: run tests, push to git, make architectural decisions not in spec
- Output: code files for Tester Agent

### 3. Tester Agent — **Haiku**
File: `.claude/agents/tester.md`
- Model: claude-haiku-4-5
- Triggered by: Coder Agent completing a module
- Tools: Read, Write, Edit, Bash(pytest), Bash(mypy)
- Does: writes test files in tests/ mirroring src/ structure,
  runs tests, reports full pass/fail output
- Cannot: fix code in src/, modify implementation files
- Output: to Debugger Agent on failure, to Code Reviewer on pass

### 4. Debugger Agent — **Sonnet**
File: `.claude/agents/debugger.md`
- Model: claude-sonnet-4-5
- Triggered by: Tester Agent reporting failures
- Tools: Read, Write, Edit, Bash(pytest), Bash(ruff)
- Does: reads error output, fixes the specific failing code, re-runs tests
- Rule: maximum 3 fix attempts. If still failing after 3 → escalate back
  to Architect Agent with full error context. Do not guess a 4th time.
- Output: back to Tester Agent (loops until pass)

### 5. Code Reviewer Agent — **Sonnet**
File: `.claude/agents/code-reviewer.md`
- Model: claude-sonnet-4-5
- Triggered by: all tests passing from Tester Agent
- Tools: Read, Grep, Glob — read-only, no modifications
- Does: audits for all of the following:
  - Hardcoded API keys, secrets, or tokens anywhere in code or comments
  - Orders placed without passing through the risk check function
  - Float equality comparisons on prices (use Decimal or round())
  - Missing try/except around any broker API call
  - Any code that bypasses the LIVE_TRADING=false check
  - Missing type hints on function signatures
  - Missing docstrings on public functions
  - Bare except clauses
- Cannot: fix code — reports only
- Output: PASS or FAIL with exact file:line references for every violation
- On FAIL: returns to Coder Agent with the violation list

### 6. GitHub Agent — **Haiku**
File: `.claude/agents/github-agent.md`
- Model: claude-haiku-4-5
- Triggered by: Code Reviewer Agent outputting PASS
- Tools: Bash(git *), MCP:github
- Does: stages all changes, writes a meaningful commit message describing
  what was built and why, pushes to GitHub, opens a PR if on a feature branch
- Cannot: merge PRs — that requires human approval
- Logs: commit SHA and PR link to agent_logs table

### 7. Docs Agent — **Haiku**
File: `.claude/agents/docs-agent.md`
- Model: claude-haiku-4-5
- Triggered by: GitHub Agent completing a commit
- Tools: Read, Write, Edit, Bash(git add *), Bash(git commit *)
- Does three things every time it runs:

  **1. Update docs/connections.md**
  Add or fully replace the section for the newly built module.
  Never append a second copy — if the module already exists, replace it.
  Each module section must include:
  - Purpose (one sentence)
  - Public API (function signatures + return types)
  - Reads from (DB tables or external sources)
  - Writes to (DB tables or files)
  - Called by (which modules or agents invoke this)
  - Calls (which modules, APIs, or services this module uses)
  - Key constants or thresholds relevant to debugging

  **2. Update docs/SYSTEM.md**
  Update the Module Map table — set status to ✅ Built for the new module.
  Update the Data Flow section if connections changed.
  Update the Debugging Guide if new failure modes are now possible.
  Never rewrite the entire file — surgical updates only.

  **3. Write per-session summary to docs/DECISIONS.md**
  Append one entry at the top of the decisions log:
  ```
  ## [YYYY-MM-DD] — [Module name]
  **Built**: [what was built in one sentence]
  **Connects to**: [what it reads from and writes to]
  **Next step**: [what Phase 1 module comes next]
  **Notes**: [anything unusual, any deviation from spec, any decision made]
  ```

- Commits doc updates with message: `docs: update connections for [module name]`
- Cannot: modify src/, tests/, or any non-docs files

---

## Hooks (.claude/settings.json)

These run deterministically regardless of what Claude is doing.

| Hook | Trigger | Action |
|------|---------|--------|
| PreToolUse | Edit/Write to src/execution/ files | Python script validates LIVE_TRADING=false |
| PostToolUse | Edit/Write anywhere in src/ | Auto-runs ruff check on the changed file |
| PostToolUse | Write to tests/ directory | Auto-runs pytest on that specific test file |
| Stop | Any Claude Code session ends | Auto-commits all changes with timestamp |

The Stop hook means you never need to manually commit during a development
session. Everything is captured at session end automatically.

---

## Project Structure

```
src/
  config/       - env loading, constants, DB connection
  data/         - validator (FIRST), fetcher, cleaner, fundamentals
  indicators/   - technical.py (RSI, MACD, Bollinger, ATR via pandas-ta)
  strategy/     - quality_filter, momentum, regime
  execution/    - auth (TOTP), shoonya_broker, paper_trader
  backtest/     - runner, validator (gates check)
  risk/         - position sizing, kill switch enforcement
  utils/        - logger, notifier (Telegram + Gmail, always both)
  agents/       - orchestrator + all 10 trading agents
.claude/
  agents/       - 6 build layer subagent .md files
  rules/        - topic-specific rules files (this directory)
  settings.json - hooks configuration
  commands/     - custom slash commands
tests/          - mirrors src/ structure exactly
data/
  cache/        - CSV price cache (gitignored)
  raw/          - raw downloads (gitignored)
docs/
  specs/        - architect agent output files
  DECISIONS.md  - log of every major architectural decision
logs/           - application logs (gitignored)
reports/        - daily trading reports
```