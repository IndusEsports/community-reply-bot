"""Deterministic safety rules, applied BEFORE any comment reaches the LLM.

These are hard-coded on purpose: refund/fix-date/reward/link holds, the 60-minute
age cutoff, "ignore anything that pre-dates bot startup", dedupe, and the daily
cap must never depend on a model's judgment call.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MAX_COMMENT_AGE_MINUTES = 60
MIN_REPLY_DELAY_MINUTES = 1.5
MAX_REPLY_DELAY_MINUTES = 6.0
HARD_MAX_REPLY_DELAY_MINUTES = 9.0

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_HOLD_PATTERNS = re.compile(
    r"\b(refund|compensat\w*|giveaway|free\s+(robux|v-?bucks|gems|skin)|"
    r"when\s+will\s+(you|it)\s+be\s+fixed|fix\s+date|reward\b)",
    re.IGNORECASE,
)


@dataclass
class PolicyResult:
    decision: str   # "proceed" | "ignore" | "hold"
    reason: str | None = None


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(
    *,
    comment_text: str,
    comment_created_at: str,
    startup_time: str | None,
    already_seen: bool,
    is_own_comment: bool,
    today_count: int,
    daily_cap: int,
) -> PolicyResult:
    if already_seen:
        return PolicyResult("ignore", "duplicate comment")

    if is_own_comment:
        return PolicyResult("ignore", "comment is from the account's own user")

    created = _parse_iso(comment_created_at)
    now = datetime.now(timezone.utc)

    if startup_time is not None and created < _parse_iso(startup_time):
        return PolicyResult("ignore", "comment pre-dates bot startup")

    age_minutes = (now - created).total_seconds() / 60.0
    if age_minutes > MAX_COMMENT_AGE_MINUTES:
        return PolicyResult("ignore", f"comment is {age_minutes:.0f} min old (> {MAX_COMMENT_AGE_MINUTES} cutoff)")

    if today_count >= daily_cap:
        return PolicyResult("hold", "daily reply cap reached")

    if _URL_RE.search(comment_text):
        return PolicyResult("hold", "comment contains a link")

    if _HOLD_PATTERNS.search(comment_text):
        return PolicyResult("hold", "comment mentions refund/fix-date/reward")

    return PolicyResult("proceed")


def pick_send_delay_minutes() -> float:
    """Random human-feeling delay for an accepted reply, 1.5-6 min, never past 9."""
    return round(random.uniform(MIN_REPLY_DELAY_MINUTES, MAX_REPLY_DELAY_MINUTES), 2)


def scheduled_send_at(comment_created_at: str) -> str:
    created = _parse_iso(comment_created_at)
    delay = pick_send_delay_minutes()
    send_at = created + timedelta(minutes=delay)
    # Safety clamp: never schedule past the hard 9-minute ceiling from comment time.
    hard_ceiling = created + timedelta(minutes=HARD_MAX_REPLY_DELAY_MINUTES)
    if send_at > hard_ceiling:
        send_at = hard_ceiling
    return send_at.isoformat()
