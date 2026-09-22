"""What is allowed to become long-term memory.

The concentrator was told to store "stable information about Benjamin" but nothing
stopped it also storing whatever was topical. In practice the fact table filled up
with its own debugging progress — ``voice_output_status: Working``,
``current_date_confirmed: September 16, 2026``, ``current_task: Pulling the
channel list`` — plus whole essays of conversation content. 107 facts accumulated,
the eight best slots were filled with noise, and the rolling summary was never
written once.

Long-term memory is what ATHENA carries forward. It should describe him, not the
session. This module is the gate.
"""
from __future__ import annotations

import re


# Nothing matching this may ever be stored, whichever path produced it.
SENSITIVE_PATTERN = re.compile(
    r"password|passcode|api[_ -]?key|secret|token|credential|credit[_ -]?card|"
    r"bank|private[_ -]?key|authentication|\bsk-[a-z0-9._-]+",
    re.IGNORECASE,
)


# A fact is a short statement about a person. Anything longer is a note about a
# conversation, and belongs in the summary instead.
MAX_FACT_CHARS = 140
# The table is shown eight facts at a time, so a large one is useless even when
# every row is honest. This is the backstop that keeps it useful.
MAX_FACTS = 60

# Keys that name a moment rather than a trait. Suffix and prefix matching is used
# rather than a keyword search so "works_at" survives while "timer_function_works"
# does not.
SESSION_SUFFIXES = (
    "_status", "_state", "_issue", "_problem", "_error", "_bug",
    "_pending", "_request", "_requested", "_task", "_retry", "_progress",
    "_result", "_works", "_working", "_unset", "_confirmed", "_sequence",
    "_context", "_update", "_note", "_log", "_verified", "_tested",
    "_dislike", "_affirmed", "_visible", "_count", "_length", "_topic",
    "_topics", "_due", "_deadline", "_days", "_left",
)
SESSION_PREFIXES = (
    "current_", "today_", "tonight_", "latest_", "recent_", "now_",
    "session_", "this_", "last_", "upcoming_", "nearest_", "next_",
)
# A durable fact about someone is not usually a progress report.
REPORT_VALUE = re.compile(
    r"^\s*(working|works|resolved|closed|paused|pending|uncertain|unknown|"
    r"as of|no longer|tested|confirmed|not yet|done|fixed|in progress|"
    r"unclear|maybe|probably|seems)\b"
    r"|\b(tested and working|is now working|has been (?:fixed|tested))\b",
    re.IGNORECASE,
)
# Something that only makes sense today.
EPHEMERAL_VALUE = re.compile(
    r"\b(today|tonight|tomorrow|right now|currently|at the moment|this session|"
    r"the latest exchange|just now|this week|next week)\b"
    # A dated fact is a schedule, not a trait, and goes stale within days.
    r"|\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
    re.IGNORECASE,
)
DATE_KEY = re.compile(r"^\d{4}[-_/]\d{1,2}([-_/]\d{1,2})?$")
# Keys are snake_case, so a word boundary never appears inside one. Match on the
# underscore-separated segments instead.
TIME_SEGMENTS = frozenset({
    "timer", "timers", "minute", "minutes", "min", "mins",
    "hour", "hours", "hr", "hrs", "second", "seconds", "sec", "secs",
})
SESSION_SEGMENTS = frozenset({
    "session", "today", "tonight", "current", "latest", "recent",
    "status", "pending", "task", "issue", "request", "now",
})


def rejection_reason(key: str, value: str) -> str | None:
    """Return why a fact must not be stored, or None when it is acceptable.

    One gate decides everything, so no caller can accidentally skip a rule.
    """
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value:
        return "empty"
    if SENSITIVE_PATTERN.search(key) or SENSITIVE_PATTERN.search(value):
        return "it looks like a secret"
    if len(value) > MAX_FACT_CHARS:
        return "the value is a note about a conversation, not a fact"
    if DATE_KEY.match(key):
        return "the key is a date"
    if key.endswith(SESSION_SUFFIXES) or key.startswith(SESSION_PREFIXES):
        return "the key names a moment, not something stable about him"
    if REPORT_VALUE.match(value):
        return "the value is a progress report"
    if EPHEMERAL_VALUE.search(value):
        return "the value only makes sense today"
    segments = set(key.split("_"))
    # A preference about timing is durable ("wants a nudge if he has not started");
    # a record of one specific timer is not.
    if not key.endswith(("_preference", "_habit")):
        if segments & TIME_SEGMENTS:
            return "the key is about a specific timer, not a trait"
        if segments & SESSION_SEGMENTS:
            return "the key names a moment, not something stable about him"
    return None


def is_durable(key: str, value: str) -> bool:
    return rejection_reason(key, value) is None


def audit(facts) -> tuple[list, list]:
    """Split stored facts into (keep, drop) using the same rules."""
    keep, drop = [], []
    for key, value, confidence in facts:
        reason = rejection_reason(key, value)
        (drop if reason else keep).append((key, value, confidence, reason))
    return keep, drop
