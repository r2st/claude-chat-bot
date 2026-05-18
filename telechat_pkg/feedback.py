"""
Feedback collection and quality evaluation for self-improving system.

Provides:
- User feedback collection (reactions, /rate command, /feedback text)
- Binary quality evaluators (length, error-free, relevance)
- Learnings accumulation (append insights to learnings.md)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import claude_core as cc

log = logging.getLogger(__name__)

# ─── Feedback DB operations ───────────────────────────────────────────────────


def save_feedback(
    platform: str,
    user_id: str,
    rating: int | None = None,
    reaction: str | None = None,
    text_feedback: str | None = None,
    message_ts: float | None = None,
    response_preview: str = "",
) -> None:
    """Save user feedback to the database (non-blocking)."""
    now = time.time()
    cc._enqueue_write(
        """INSERT INTO feedback
           (platform, user_id, rating, reaction, text_feedback, message_ts, response_preview, ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        (platform, user_id, rating, reaction, text_feedback, message_ts or now, response_preview[:500], now),
    )


def get_feedback_stats(platform: str, user_id: str) -> dict:
    """Get feedback statistics for a user."""
    conn = cc._get_conn()
    row = conn.execute(
        """SELECT COUNT(*), AVG(rating), SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END)
           FROM feedback WHERE platform=? AND user_id=? AND rating IS NOT NULL""",
        (platform, user_id),
    ).fetchone()
    total = row[0] or 0
    avg = round(row[1], 2) if row[1] else 0
    positive = row[2] or 0
    return {
        "total_ratings": total,
        "avg_rating": avg,
        "positive_count": positive,
        "satisfaction_pct": round(positive / total * 100, 1) if total > 0 else 0,
    }


def get_recent_feedback(platform: str, user_id: str, limit: int = 10) -> list[dict]:
    """Get recent feedback entries."""
    conn = cc._get_conn()
    rows = conn.execute(
        """SELECT rating, reaction, text_feedback, response_preview, ts
           FROM feedback WHERE platform=? AND user_id=?
           ORDER BY ts DESC LIMIT ?""",
        (platform, user_id, limit),
    ).fetchall()
    return [
        {
            "rating": r[0],
            "reaction": r[1],
            "text_feedback": r[2],
            "response_preview": r[3],
            "ts": r[4],
        }
        for r in rows
    ]


# ─── Binary Quality Evaluators ────────────────────────────────────────────────


def evaluate_response(user_text: str, response: str, stats: dict) -> dict:
    """Run all binary quality evaluators on a response. Returns scores dict."""
    scores = {
        "length_appropriate": _eval_length(user_text, response),
        "error_free": _eval_error_free(response),
        "has_content": _eval_has_content(response),
        "not_truncated": _eval_not_truncated(response),
        "reasonable_cost": _eval_reasonable_cost(stats),
    }
    # Composite score: percentage of passed checks
    passed = sum(1 for v in scores.values() if v)
    scores["composite"] = round(passed / len(scores), 2) if scores else 0
    return scores


def save_quality_score(
    platform: str,
    user_id: str,
    evaluator: str,
    score: float,
    response_preview: str = "",
    metadata: str = "",
) -> None:
    """Save a quality evaluation score."""
    cc._enqueue_write(
        """INSERT INTO quality_scores
           (platform, user_id, evaluator, score, response_preview, metadata, ts)
           VALUES (?,?,?,?,?,?,?)""",
        (platform, user_id, evaluator, score, response_preview[:500], metadata, time.time()),
    )


def get_quality_trend(platform: str, user_id: str, evaluator: str = "composite", limit: int = 50) -> list[float]:
    """Get recent quality scores for trend analysis."""
    conn = cc._get_conn()
    rows = conn.execute(
        """SELECT score FROM quality_scores
           WHERE platform=? AND user_id=? AND evaluator=?
           ORDER BY ts DESC LIMIT ?""",
        (platform, user_id, evaluator, limit),
    ).fetchall()
    return [r[0] for r in reversed(rows)]


# ─── Individual evaluator functions ──────────────────────────────────────────


def _eval_length(user_text: str, response: str) -> bool:
    """Check if response length is appropriate for the query."""
    if not response:
        return False
    user_len = len(user_text)
    resp_len = len(response)

    # Very short queries shouldn't get massive responses
    if user_len < 20 and resp_len > 5000:
        return False
    # Non-trivial queries should get substantive responses
    if user_len > 50 and resp_len < 20:
        return False
    return True


def _eval_error_free(response: str) -> bool:
    """Check if response doesn't contain error indicators."""
    error_markers = [
        "[Claude error]",
        "[Timeout]",
        "[Error]",
        "[SDK Error]",
        "rate limit",
        "overloaded",
    ]
    response_lower = response.lower()
    return not any(marker.lower() in response_lower for marker in error_markers)


def _eval_has_content(response: str) -> bool:
    """Check if response has meaningful content."""
    if not response or not response.strip():
        return False
    if response.strip() in ("(no response)", "(empty response)"):
        return False
    # Must have at least some substance
    return len(response.strip()) > 10


def _eval_not_truncated(response: str) -> bool:
    """Check if response doesn't appear truncated."""
    truncation_markers = [
        "…(truncated)",
        "... (cut off)",
        "(response cut",
    ]
    return not any(m in response for m in truncation_markers)


def _eval_reasonable_cost(stats: dict) -> bool:
    """Check if the response cost is reasonable."""
    if not stats:
        return True  # No stats = assume OK
    cost = stats.get("cost_usd", 0)
    # Flag if a single response costs more than $1
    if cost > 1.0:
        return False
    return True


# ─── Learnings accumulation ──────────────────────────────────────────────────

LEARNINGS_PATH = Path(cc.CLAUDE_WORK_DIR) / "learnings.md"


def append_learning(insight: str, source: str = "auto", category: str = "general") -> None:
    """Append a learning insight to the learnings file."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not LEARNINGS_PATH.exists():
        LEARNINGS_PATH.write_text(
            "# Telechat Learnings\n\n"
            "Auto-accumulated insights from user interactions.\n\n"
            "---\n\n"
        )

    entry = f"## [{category}] {timestamp} (via {source})\n\n{insight}\n\n---\n\n"
    with open(LEARNINGS_PATH, "a") as f:
        f.write(entry)
    log.info("Appended learning: %s", insight[:80])


def get_learnings_summary() -> str:
    """Get the current learnings content for system prompt injection."""
    if not LEARNINGS_PATH.exists():
        return ""
    content = LEARNINGS_PATH.read_text()
    # Return last 2000 chars to keep system prompt reasonable
    if len(content) > 2000:
        return "...\n" + content[-2000:]
    return content
