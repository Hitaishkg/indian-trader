"""Watchlist Builder Agent for the Indian Trader evening pipeline.

Runs at approximately 23:30 IST each evening, after research_agent.py has
completed. Reads the top-5 screener candidates from screener_results and
their completed sentiment reports from research_reports, applies the combined
decision rule (screener rank = BUY intent, LLM sentiment as veto), computes
a partial pre-trade scorecard for human visibility, sends a Telegram+Gmail
checkpoint message listing all PROCEED candidates for human approval, and
writes one row per candidate to the watchlist table.

Also exposes two orchestrator-facing helpers:
  - check_watchlist_timeout()  — called at 07:00 IST next morning
  - record_human_approval()    — called when the human replies via Telegram

This module is a plain Python function. It does NOT use the Python Agent SDK.

Scorecard note: only 4 of the 8 full-scorecard criteria are computable here:
  quality(5) + rank(0|5) + regime(0|2|5) + sentiment(0|1|3|5) = max 20
  (or max 15 when earnings_transcript_unavailable=True).
RSI, MACD, risk-reward, and earnings calendar are scored by signal_agent.py
the next morning. The full threshold of 28/40 is enforced by risk_agent.py.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.config.settings import settings
from src.utils.logger import log_agent_action
from src.utils.notifier import send_alert, send_checkpoint, send_info

# ---------------------------------------------------------------------------
# Timezone constant
# ---------------------------------------------------------------------------

IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

AGENT_NAME: str = "watchlist_agent"

APPROVAL_DEADLINE_HOUR: int = 7    # 07:00 IST
APPROVAL_DEADLINE_MINUTE: int = 0

# Scorecard constants — threshold NOT enforced by this module
SCORECARD_THRESHOLD: int = 28              # enforced by risk_agent on full scorecard
SCORECARD_MAX_FULL: int = 40               # full scorecard max (all 8 criteria)
SCORECARD_MAX_FULL_NO_EARNINGS: int = 35   # full max when earnings_transcript_unavailable
SCORECARD_MAX_WATCHLIST: int = 20          # watchlist-stage max (4 criteria)
SCORECARD_MAX_WATCHLIST_NO_EARNINGS: int = 15  # watchlist-stage max when earnings_transcript_unavailable

# WAL pragmas (applied to every SQLite connection)
_WAL_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA cache_size=-64000;",
    "PRAGMA synchronous=NORMAL;",
)

# DDL for watchlist table — supersedes the preliminary schema in db-schema.md
_CREATE_TABLE_SQL: str = """
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    run_date TEXT NOT NULL,
    combined_decision TEXT NOT NULL,
    scorecard_score INTEGER NOT NULL,
    scorecard_max INTEGER NOT NULL,
    sentiment TEXT NOT NULL,
    confidence REAL NOT NULL,
    rank INTEGER NOT NULL,
    regime TEXT NOT NULL,
    position_size_multiplier REAL NOT NULL,
    human_approved INTEGER NOT NULL DEFAULT 0,
    approval_source TEXT,
    added_at TEXT NOT NULL,
    UNIQUE(symbol, run_date)
);
"""

# Project root for DB path resolution (two levels up from this file)
_PROJECT_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class WatchlistAgentError(Exception):
    """Raised when the Watchlist Agent encounters a fatal, non-recoverable error.

    Attributes:
        message: Human-readable error description.
        phase: Which phase failed.
    """

    def __init__(self, message: str, phase: str) -> None:
        """Initialise with a message and phase identifier.

        Args:
            message: Human-readable error description.
            phase: One of 'db_read', 'db_write', 'notification', 'timeout_check'.
        """
        self.message = message
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchlistCandidate:
    """Intermediate object built during the decision phase.

    Not written to DB directly — used to compute WatchlistEntry objects.

    Attributes:
        symbol: NSE ticker symbol.
        rank: Momentum rank from screener_results (1 = best).
        momentum_score: 12-1 momentum factor score.
        regime: Market regime string.
        position_size_multiplier: 1.0 / 0.5 / 0.0 based on regime.
        sentiment: LLM sentiment from research_reports.
        confidence: LLM confidence score 0.0–1.0.
        earnings_transcript_unavailable: True if earnings reported but transcript not retrievable.
        combined_decision: "PROCEED" or "SKIP".
        skip_reason: None if PROCEED; reason string if SKIP.
        scorecard_score: Partial score (0–20) at watchlist stage.
        scorecard_max: 20 normally; 15 when earnings_transcript_unavailable=True.
    """

    symbol: str
    rank: int
    momentum_score: float
    regime: str                             # "ABOVE_200DMA" / "BELOW_200DMA" / "BELOW_200DMA_10DAYS"
    position_size_multiplier: float         # 1.0 / 0.5 / 0.0
    sentiment: str                          # "Positive" / "Negative" / "Neutral" / "Mixed"
    confidence: float                       # 0.0–1.0
    earnings_transcript_unavailable: bool
    combined_decision: str                  # "PROCEED" or "SKIP"
    skip_reason: str | None                 # None if PROCEED; reason string if SKIP
    scorecard_score: int                    # 0–20 (partial; RSI/MACD/risk-reward scored tomorrow)
    scorecard_max: int                      # 20 normally; 15 when earnings_transcript_unavailable=True


@dataclass(frozen=True)
class WatchlistEntry:
    """Written to the watchlist DB table. One row per candidate (PROCEED and SKIP).

    Attributes:
        symbol: NSE ticker symbol.
        combined_decision: "PROCEED" or "SKIP".
        scorecard_score: Partial score at watchlist stage.
        scorecard_max: Max possible score at watchlist stage.
        sentiment: LLM sentiment.
        confidence: LLM confidence.
        rank: Momentum rank.
        regime: Market regime string.
        position_size_multiplier: Position size multiplier from regime.
        human_approved: False until record_human_approval() called.
        approval_source: None until approved or timed out.
        added_at: IST-aware datetime when row was written.
        run_date: Date this watchlist was built for.
    """

    symbol: str
    combined_decision: str
    scorecard_score: int
    scorecard_max: int
    sentiment: str
    confidence: float
    rank: int
    regime: str
    position_size_multiplier: float
    human_approved: bool                    # False until record_human_approval() called
    approval_source: str | None             # None until approved or timed out
    added_at: datetime.datetime             # IST-aware
    run_date: datetime.date


@dataclass(frozen=True)
class WatchlistAgentResult:
    """Full output of run_watchlist_agent().

    Attributes:
        run_date: Date the watchlist was built for.
        candidates_evaluated: Total rows read from screener_results (quality_passed=1).
        proceed_count: Candidates with combined_decision="PROCEED".
        skipped_count: Candidates with combined_decision="SKIP".
        approved_symbols: Symbols written with human_approved=False (pending approval).
        human_responded: Always False at run time; updated by check_watchlist_timeout().
        completed_at: IST-aware datetime when agent completed.
    """

    run_date: datetime.date
    candidates_evaluated: int
    proceed_count: int
    skipped_count: int
    approved_symbols: list[str]             # PROCEED symbols pending human approval
    human_responded: bool                   # always False at run time
    completed_at: datetime.datetime         # IST-aware


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ist_now() -> datetime.datetime:
    """Return the current time as an IST timezone-aware datetime.

    Returns:
        Current datetime in Asia/Kolkata timezone.
    """
    return datetime.datetime.now(ZoneInfo("Asia/Kolkata"))


def _resolve_db_path() -> str:
    """Resolve the SQLite database file path from settings.

    Returns:
        Absolute path to the SQLite database file.
    """
    url = settings.database_url
    if url.startswith("sqlite:///"):
        remainder = url[len("sqlite:///"):]
    else:
        remainder = url

    if os.path.isabs(remainder):
        return remainder
    return os.path.join(_PROJECT_ROOT, remainder)


def _open_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL pragmas applied.

    Args:
        db_path: Absolute path to the SQLite database file.

    Returns:
        An open sqlite3.Connection with isolation_level=None (autocommit).
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    for pragma in _WAL_PRAGMAS:
        conn.execute(pragma)
    return conn


def _setup_table(db_path: str) -> None:
    """Create watchlist table if it does not exist, then close connection.

    Args:
        db_path: Absolute path to the SQLite database file.

    Raises:
        WatchlistAgentError: If the DDL execution fails.
    """
    conn = _open_connection(db_path)
    try:
        conn.execute(_CREATE_TABLE_SQL)
    except sqlite3.Error as exc:
        conn.close()
        raise WatchlistAgentError(
            message=f"DB setup failed: {exc}",
            phase="db_write",
        ) from exc
    conn.close()


def _compute_scorecard(
    rank: int,
    regime: str,
    sentiment: str,
    earnings_transcript_unavailable: bool,
) -> tuple[int, int]:
    """Compute partial pre-trade scorecard at watchlist stage.

    Scores 4 of the 8 full-scorecard criteria:
      - Quality filter pass (always 5 — prerequisite to be a candidate)
      - Momentum rank in top 3 (5 if rank <= 3, else 0)
      - Regime filter (ABOVE_200DMA=5, BELOW_200DMA=2, BELOW_200DMA_10DAYS=0)
      - LLM sentiment (Positive=5, Neutral=3, Mixed=1, Negative=0)

    RSI, MACD, risk-reward deferred to signal_agent.py (scored next morning).
    Threshold of 28/40 enforced by risk_agent.py, not here.

    Args:
        rank: Momentum rank (1 = best).
        regime: Market regime string.
        sentiment: LLM sentiment string.
        earnings_transcript_unavailable: True if earnings transcript was unavailable.

    Returns:
        Tuple of (scorecard_score, scorecard_max).
        scorecard_max = 20 normally, 15 when earnings_transcript_unavailable=True.
    """
    # Quality criterion: always 5 (quality_passed=1 is a prerequisite to appear here)
    quality_points = 5

    # Rank criterion: top 3 get full 5 points
    rank_points = 5 if rank <= 3 else 0

    # Regime criterion
    if regime == "ABOVE_200DMA":
        regime_points = 5
    elif regime == "BELOW_200DMA":
        regime_points = 2
    else:  # "BELOW_200DMA_10DAYS"
        regime_points = 0

    # Sentiment criterion
    if sentiment == "Positive":
        sentiment_points = 5
    elif sentiment == "Neutral":
        sentiment_points = 3
    elif sentiment == "Mixed":
        sentiment_points = 1
    else:  # "Negative"
        sentiment_points = 0

    score = quality_points + rank_points + regime_points + sentiment_points

    # scorecard_max: 20 normally; 15 when earnings_transcript_unavailable
    # (the "no earnings in next 5 days" criterion reduces full max from 40→35,
    # and watchlist-stage max from 20→15)
    scorecard_max = (
        SCORECARD_MAX_WATCHLIST_NO_EARNINGS
        if earnings_transcript_unavailable
        else SCORECARD_MAX_WATCHLIST
    )

    return score, scorecard_max


def _apply_combined_decision(
    position_size_multiplier: float,
    sentiment: str,
) -> tuple[str, str | None]:
    """Apply the combined decision rule to determine PROCEED or SKIP.

    Evaluation order:
      1. position_size_multiplier == 0.0 → regime_blocked (takes priority)
      2. sentiment == "Negative" → negative_sentiment
      3. All others (Positive, Neutral, Mixed) → PROCEED

    Args:
        position_size_multiplier: Regime-derived multiplier (0.0 / 0.5 / 1.0).
        sentiment: LLM sentiment string.

    Returns:
        Tuple of (combined_decision, skip_reason).
        combined_decision is "PROCEED" or "SKIP".
        skip_reason is None if PROCEED, reason string if SKIP.
    """
    if position_size_multiplier == 0.0:
        return "SKIP", "regime_blocked"
    if sentiment == "Negative":
        return "SKIP", "negative_sentiment"
    return "PROCEED", None


def _build_candidates(
    screener_rows: list[tuple[str, int, float, str, float]],
    research_by_symbol: dict[str, tuple[str, float, bool]],
) -> list[WatchlistCandidate]:
    """Build WatchlistCandidate objects from screener + research data.

    Symbols without completed research are logged as research_incomplete and
    excluded from candidates. Logging is done here via log_agent_action which
    is safe to call outside any BEGIN/COMMIT block.

    Args:
        screener_rows: List of (symbol, rank, momentum_score, regime,
                        position_size_multiplier) tuples from screener_results.
        research_by_symbol: Dict keyed by symbol →
                            (sentiment, confidence, earnings_transcript_unavailable).

    Returns:
        List of WatchlistCandidate objects (one per symbol with completed research).
    """
    candidates: list[WatchlistCandidate] = []

    for symbol, rank, momentum_score, regime, position_size_multiplier in screener_rows:
        if symbol not in research_by_symbol:
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"research_incomplete: {symbol}",
                level="WARNING",
                symbol=symbol,
                result="skipped",
            )
            continue

        sentiment, confidence, earnings_transcript_unavailable = research_by_symbol[symbol]

        combined_decision, skip_reason = _apply_combined_decision(
            position_size_multiplier, sentiment
        )

        scorecard_score, scorecard_max = _compute_scorecard(
            rank, regime, sentiment, earnings_transcript_unavailable
        )

        # Log per-candidate decision and scorecard
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"combined_decision: {symbol} → {combined_decision} reason={skip_reason}",
            level="INFO",
            symbol=symbol,
            result=combined_decision.lower(),
        )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"scorecard: {symbol} score={scorecard_score}/{scorecard_max}",
            level="INFO",
            symbol=symbol,
        )

        candidates.append(
            WatchlistCandidate(
                symbol=symbol,
                rank=rank,
                momentum_score=momentum_score,
                regime=regime,
                position_size_multiplier=position_size_multiplier,
                sentiment=sentiment,
                confidence=confidence,
                earnings_transcript_unavailable=earnings_transcript_unavailable,
                combined_decision=combined_decision,
                skip_reason=skip_reason,
                scorecard_score=scorecard_score,
                scorecard_max=scorecard_max,
            )
        )

    return candidates


def _build_checkpoint_message(
    candidates: list[WatchlistCandidate],
    proceed_candidates: list[WatchlistCandidate],
    skip_candidates: list[WatchlistCandidate],
) -> str:
    """Build the human-readable checkpoint notification message body.

    Args:
        candidates: All evaluated candidates.
        proceed_candidates: Candidates with combined_decision="PROCEED".
        skip_candidates: Candidates with combined_decision="SKIP".

    Returns:
        Formatted message string for the checkpoint notification.
    """
    n_total = len(candidates)
    n_proceed = len(proceed_candidates)
    n_skip = len(skip_candidates)

    lines: list[str] = [
        f"{n_total} candidates evaluated. {n_proceed} proceed, {n_skip} skipped.",
        "",
        "PROCEED candidates (reply to approve):",
        "",
    ]

    for i, cand in enumerate(proceed_candidates, start=1):
        lines.append(
            f"{i}. {cand.symbol} | Sentiment: {cand.sentiment} ({cand.confidence:.2f})"
            f" | Rank: {cand.rank} | Regime: {cand.regime}"
        )
        lines.append(
            f"   Partial score: {cand.scorecard_score}/{cand.scorecard_max}"
            f" (RSI/MACD/risk-reward scored tomorrow)"
        )
        lines.append(f"   Reply: APPROVE {cand.symbol} or SKIP {cand.symbol}")
        lines.append("")

    if skip_candidates:
        skip_parts = [
            f"{c.symbol} ({c.skip_reason})" for c in skip_candidates
        ]
        lines.append(f"Skipped candidates: {', '.join(skip_parts)}")
        lines.append("")

    lines.append("Reply APPROVE ALL to approve all PROCEED candidates.")
    lines.append("Timeout: no response by 07:00 IST → all trades skipped today.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_watchlist_agent(
    run_date: datetime.date | None = None,
) -> WatchlistAgentResult:
    """Run the Watchlist Builder Agent for the given date.

    Reads screener_results and research_reports, applies the combined
    decision rule, computes partial pre-trade scorecard, writes all
    candidates (PROCEED and SKIP) to watchlist table, and sends a
    checkpoint notification for human approval.

    Flow:
      1. DB READ: read screener_results + research_reports (single connection,
         close immediately before any computation).
      2. Compute candidates, combined decisions, scorecards (pure Python, no DB).
      3. DB WRITE: open fresh connection → BEGIN → INSERT OR REPLACE all rows
         into watchlist → COMMIT → PRAGMA wal_checkpoint(PASSIVE) → close.
         All rows written with human_approved=0, approval_source=NULL.
      4. Log all actions (OUTSIDE any BEGIN/COMMIT block).
      5. Send checkpoint notification (PROCEED candidates only, or send_info if none).
      6. Return WatchlistAgentResult.

    The function returns immediately after sending the notification.
    It does NOT block or poll for human response.

    Args:
        run_date: Date to run for. Defaults to today in IST.

    Returns:
        WatchlistAgentResult with run summary.

    Raises:
        WatchlistAgentError: On DB read failure (phase='db_read'),
                             DB write failure (phase='db_write'), or
                             notification failure when both channels fail
                             (phase='notification').
    """
    if run_date is None:
        run_date = _ist_now().date()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"watchlist_run_started: {run_date}",
        level="INFO",
    )

    db_path = _resolve_db_path()
    _setup_table(db_path)

    # --- READ PHASE: single connection, read both tables, close immediately ---
    screener_rows: list[tuple[str, int, float, str, float]] = []
    research_by_symbol: dict[str, tuple[str, float, bool]] = {}

    conn = _open_connection(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT symbol, rank, momentum_score, regime, position_size_multiplier
            FROM screener_results
            WHERE run_date = ?
              AND quality_passed = 1
            ORDER BY rank ASC
            """,
            (run_date.isoformat(),),
        )
        screener_rows = [
            (row[0], int(row[1]), float(row[2]), row[3], float(row[4]))
            for row in cursor.fetchall()
        ]

        # Fetch research for each screener symbol
        for symbol, _, _, _, _ in screener_rows:
            res_cursor = conn.execute(
                """
                SELECT symbol, sentiment, confidence, earnings_transcript_unavailable
                FROM research_reports
                WHERE symbol = ?
                  AND run_date = ?
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (symbol, run_date.isoformat()),
            )
            row = res_cursor.fetchone()
            if row is not None:
                research_by_symbol[row[0]] = (
                    row[1],           # sentiment
                    float(row[2]),    # confidence
                    bool(row[3]),     # earnings_transcript_unavailable
                )

    except sqlite3.Error as exc:
        conn.close()
        raise WatchlistAgentError(
            message=f"DB read failed: {exc}",
            phase="db_read",
        ) from exc

    conn.close()  # Release before any computation or logging

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"screener_results_read: {len(screener_rows)} candidates",
        level="INFO",
    )

    # --- COMPUTE PHASE: pure Python, no DB connection held ---
    candidates = _build_candidates(screener_rows, research_by_symbol)

    proceed_candidates = [c for c in candidates if c.combined_decision == "PROCEED"]
    skip_candidates = [c for c in candidates if c.combined_decision == "SKIP"]

    now_ist = _ist_now()
    added_at_str = now_ist.isoformat()

    # Build WatchlistEntry objects (PROCEED and SKIP — full audit trail)
    entries: list[WatchlistEntry] = [
        WatchlistEntry(
            symbol=cand.symbol,
            combined_decision=cand.combined_decision,
            scorecard_score=cand.scorecard_score,
            scorecard_max=cand.scorecard_max,
            sentiment=cand.sentiment,
            confidence=cand.confidence,
            rank=cand.rank,
            regime=cand.regime,
            position_size_multiplier=cand.position_size_multiplier,
            human_approved=False,
            approval_source=None,
            added_at=now_ist,
            run_date=run_date,
        )
        for cand in candidates
    ]

    # --- Log BEFORE write phase to avoid SQLITE_BUSY_SNAPSHOT ---
    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"watchlist_run_completed: {len(proceed_candidates)} PROCEED, {len(skip_candidates)} SKIP",
        level="INFO",
        result="ok",
    )

    # --- WRITE PHASE: fresh connection, explicit BEGIN/COMMIT ---
    write_conn = _open_connection(db_path)
    try:
        write_conn.execute("BEGIN")
        for entry in entries:
            write_conn.execute(
                """
                INSERT OR REPLACE INTO watchlist (
                    symbol, run_date, combined_decision, scorecard_score,
                    scorecard_max, sentiment, confidence, rank, regime,
                    position_size_multiplier, human_approved, approval_source,
                    added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.symbol,
                    entry.run_date.isoformat(),
                    entry.combined_decision,
                    entry.scorecard_score,
                    entry.scorecard_max,
                    entry.sentiment,
                    entry.confidence,
                    entry.rank,
                    entry.regime,
                    entry.position_size_multiplier,
                    1 if entry.human_approved else 0,
                    entry.approval_source,
                    added_at_str,
                ),
            )
        write_conn.execute("COMMIT")
        write_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except sqlite3.Error as exc:
        try:
            write_conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        write_conn.close()
        raise WatchlistAgentError(
            message=f"DB write failed: {exc}",
            phase="db_write",
        ) from exc

    write_conn.close()

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"watchlist_written: {len(entries)} rows",
        level="INFO",
        result="ok",
    )

    # --- NOTIFICATION PHASE ---
    if len(proceed_candidates) == 0:
        # No tradeable candidates — send informational message only
        log_agent_action(
            agent_name=AGENT_NAME,
            action="no_tradeable_candidates: all skipped",
            level="INFO",
            result="skipped",
        )

        skip_summary = ", ".join(
            f"{c.symbol} ({c.skip_reason})" for c in skip_candidates
        ) if skip_candidates else "no candidates found"

        notify_result = send_info(f"No tradeable candidates today: {skip_summary}")
        if not notify_result.get("telegram", False):
            log_agent_action(
                agent_name=AGENT_NAME,
                action="send_info failed (telegram channel)",
                level="WARNING",
                result="notification_failed",
            )

        return WatchlistAgentResult(
            run_date=run_date,
            candidates_evaluated=len(candidates),
            proceed_count=0,
            skipped_count=len(skip_candidates),
            approved_symbols=[],
            human_responded=False,
            completed_at=now_ist,
        )

    # PROCEED candidates exist — send checkpoint requiring human approval
    subject = (
        f"Watchlist ready — {len(proceed_candidates)} candidates. Approve by 07:00 IST"
    )
    message_body = _build_checkpoint_message(candidates, proceed_candidates, skip_candidates)

    checkpoint_result = send_checkpoint(subject=subject, message=message_body)

    if not checkpoint_result.get("telegram", False) and not checkpoint_result.get("gmail", False):
        raise WatchlistAgentError(
            message="Both notification channels failed for checkpoint",
            phase="notification",
        )

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"checkpoint_sent: {len(proceed_candidates)} PROCEED candidates",
        level="INFO",
        result="ok",
    )

    # Paper trading: auto-approve all PROCEED candidates without waiting for human reply.
    # Notification is still sent above so the human can see what's going in.
    if settings.paper_trading:
        for cand in proceed_candidates:
            record_human_approval(
                symbol=cand.symbol,
                run_date=run_date,
                approved=True,
            )
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"auto_approved: paper_trading_mode — {len(proceed_candidates)} symbols",
            level="INFO",
            result="ok",
        )
        return WatchlistAgentResult(
            run_date=run_date,
            candidates_evaluated=len(candidates),
            proceed_count=len(proceed_candidates),
            skipped_count=len(skip_candidates),
            approved_symbols=[c.symbol for c in proceed_candidates],
            human_responded=True,
            completed_at=_ist_now(),
        )

    return WatchlistAgentResult(
        run_date=run_date,
        candidates_evaluated=len(candidates),
        proceed_count=len(proceed_candidates),
        skipped_count=len(skip_candidates),
        approved_symbols=[c.symbol for c in proceed_candidates],
        human_responded=False,
        completed_at=now_ist,
    )


# ---------------------------------------------------------------------------
# Orchestrator-facing helpers
# ---------------------------------------------------------------------------


def check_watchlist_timeout(run_date: datetime.date) -> None:
    """Mark unanswered watchlist rows as timed out at 07:00 IST.

    Called by the orchestrator at 07:00 IST the morning after run_watchlist_agent().
    Finds all rows for run_date where human_approved=0 and approval_source IS NULL,
    sets approval_source='timeout_skip', and sends an alert notification.

    If no pending rows are found (all already resolved) → no-op, no alert.

    Args:
        run_date: The date whose pending rows should be timed out.

    Raises:
        WatchlistAgentError: On DB write failure (phase='timeout_check').
    """
    db_path = _resolve_db_path()
    conn = _open_connection(db_path)
    try:
        conn.execute("BEGIN")
        cursor = conn.execute(
            """
            UPDATE watchlist
            SET approval_source = 'timeout_skip'
            WHERE run_date = ?
              AND human_approved = 0
              AND approval_source IS NULL
            """,
            (run_date.isoformat(),),
        )
        rows_updated = cursor.rowcount
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        raise WatchlistAgentError(
            message=f"DB write failed during timeout check: {exc}",
            phase="timeout_check",
        ) from exc

    conn.close()

    if rows_updated > 0:
        log_agent_action(
            agent_name=AGENT_NAME,
            action="watchlist_timeout: no human response by 07:00 IST",
            level="WARNING",
            result="timeout",
        )
        send_alert(
            subject="Watchlist timeout",
            message="No human response by 07:00 IST. All trades skipped today.",
        )


def record_human_approval(
    symbol: str,
    run_date: datetime.date,
    approved: bool,
) -> None:
    """Record a human approval or rejection for a watchlist symbol.

    Called by the orchestrator when it parses a Telegram reply.
    Updates the watchlist row for (symbol, run_date):
      - human_approved = 1 if approved else 0
      - approval_source = "human_explicit"

    If no row exists for (symbol, run_date), logs a warning and returns
    without error (no-op). Does NOT raise.

    Args:
        symbol: NSE ticker symbol to approve/reject.
        run_date: The watchlist run date this approval applies to.
        approved: True to approve, False to reject.
    """
    db_path = _resolve_db_path()
    try:
        conn = _open_connection(db_path)
        try:
            # First check if the row exists
            cursor = conn.execute(
                "SELECT id FROM watchlist WHERE symbol = ? AND run_date = ?",
                (symbol, run_date.isoformat()),
            )
            row = cursor.fetchone()

            if row is None:
                conn.close()
                log_agent_action(
                    agent_name=AGENT_NAME,
                    action=f"record_human_approval: no row found for {symbol} on {run_date}",
                    level="WARNING",
                    symbol=symbol,
                    result="not_found",
                )
                return

            conn.execute("BEGIN")
            conn.execute(
                """
                UPDATE watchlist
                SET human_approved = ?,
                    approval_source = 'human_explicit'
                WHERE symbol = ?
                  AND run_date = ?
                """,
                (1 if approved else 0, symbol, run_date.isoformat()),
            )
            conn.execute("COMMIT")
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            log_agent_action(
                agent_name=AGENT_NAME,
                action=f"record_human_approval: DB error for {symbol} on {run_date}: {exc}",
                level="WARNING",
                symbol=symbol,
                result="error",
            )
            return

        conn.close()

    except sqlite3.Error as exc:
        log_agent_action(
            agent_name=AGENT_NAME,
            action=f"record_human_approval: connection failed for {symbol}: {exc}",
            level="WARNING",
            symbol=symbol,
            result="error",
        )
        return

    log_agent_action(
        agent_name=AGENT_NAME,
        action=f"human_approval_recorded: {symbol} approved={approved}",
        level="INFO",
        symbol=symbol,
        result="ok",
    )
