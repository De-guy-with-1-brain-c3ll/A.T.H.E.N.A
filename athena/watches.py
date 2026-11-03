"""Recurring background checks that let ATHENA speak up without being asked.

Each watcher inspects one watch record and returns the lines to announce. They
run locally and never call the language model, so a new Teams message or a rain
warning costs no tokens. Watchers stay silent when they fail: a broken watch must
never talk over the user.
"""
from __future__ import annotations

from datetime import datetime
import os
import re
from zoneinfo import ZoneInfo

from athena.alerts import Watcher


TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
MAX_MESSAGE_CHARS = 220


def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("ATHENA_TIMEZONE", "Asia/Shanghai"))
    except Exception:
        return ZoneInfo("UTC")


def plain_text(value: object) -> str:
    """Turn a Graph HTML message body into one short spoken line."""
    text = TAG.sub(" ", str(value or ""))
    for entity, character in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                              ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, character)
    return SPACE.sub(" ", text).strip()


async def teams_messages(watch: dict, services: dict) -> list[str]:
    """Announce channel posts that appeared since the previous check."""
    graph = services.get("teams_graph")
    params = watch.get("params") or {}
    team, channel = str(params.get("team") or "").strip(), str(params.get("channel") or "").strip()
    if graph is None or not team or not channel:
        return []
    try:
        posts = await graph.posts(team, channel, 15)
    except Exception:
        return []
    ordered = sorted((post for post in posts if post.get("id")),
                     key=lambda post: str(post.get("createdDateTime") or ""))
    state = watch.setdefault("state", {})
    if not state.get("baseline"):
        # The first look records what already exists instead of replaying history
        # at the user the moment they ask for the watch.
        state["baseline"] = True
        state["seen"] = [str(post["id"]) for post in ordered][-40:]
        return []
    seen = {str(item) for item in (state.get("seen") or [])}
    fresh = [post for post in ordered if str(post["id"]) not in seen]
    state["seen"] = ([str(post["id"]) for post in ordered] + sorted(seen))[-40:]
    lines = []
    for post in fresh[-3:]:
        author = plain_text(((post.get("from") or {}).get("user") or {}).get("displayName")) or "someone"
        body = plain_text((post.get("body") or {}).get("content"))
        if not body:
            continue
        lines.append(f"New Teams message from {author} in {channel}: {body[:MAX_MESSAGE_CHARS]}")
    if len(fresh) > 3:
        lines.append(f"Plus {len(fresh) - 3} more new Teams messages in {channel}.")
    return lines


async def weather(watch: dict, services: dict) -> list[str]:
    """Announce a forecast, either on a daily schedule or on a rain threshold."""
    registry = services.get("registry")
    params = watch.get("params") or {}
    location = str(params.get("location") or "").strip()
    if registry is None or not location:
        return []
    zone = local_zone()
    now = datetime.now(zone)
    state = watch.setdefault("state", {})
    scheduled_at = str(params.get("at") or "").strip()
    if scheduled_at:
        try:
            hour, minute = (int(part) for part in scheduled_at.split(":", 1))
        except ValueError:
            hour, minute = 7, 30
        if state.get("last_date") == now.date().isoformat():
            return []
        if (now.hour, now.minute) < (hour, minute):
            return []
        state["last_date"] = now.date().isoformat()
    result = await registry.execute("get_weather", {"location": location, "days": 1})
    if not result.success:
        return []
    data = result.data or {}
    daily = data.get("daily") or {}
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_probability_max") or []
    parts = []
    if highs and lows:
        parts.append(f"{round(highs[0])} high, {round(lows[0])} low")
    if rain:
        parts.append(f"{round(rain[0])} percent chance of precipitation")
    if not parts:
        return []
    label = data.get("location") or location
    line = f"Weather for {label}: " + ", ".join(parts) + "."
    threshold = params.get("alert_if_rain_over")
    if threshold is not None and rain and float(rain[0]) >= float(threshold):
        line = f"Heads up, take an umbrella. {line}"
    return [line]


# Communication Journal channels are where teachers post the day's work. Their
# names are inconsistent between teams, so they are found rather than listed.
JOURNAL_WORDS = ("cj", "communication journal", "journal")


def is_journal_channel(name: str) -> bool:
    lowered = str(name or "").strip().casefold()
    if lowered in JOURNAL_WORDS:
        return True
    return any(word in lowered for word in ("communication journal", "journal"))


async def cj_schedule(watch: dict, services: dict) -> list[str]:
    """Read every Communication Journal channel and prepare the day's schedule.

    Stays silent. The schedule is saved as a brief, which ATHENA offers the next
    time Benjamin is actually there to hear it, rather than talking to an empty
    room at four o'clock.
    """
    graph = services.get("teams_graph")
    scheduler = services.get("alert_scheduler")
    if graph is None or scheduler is None:
        return []

    from athena.tools.teams import TeamsChannelsTool, TeamsPostsTool

    params = watch.get("params") or {}
    per_channel = int(params.get("posts_per_channel", 3) or 3)
    listing = await TeamsChannelsTool(graph).execute({})
    teams = (listing.data or {}).get("teams", [])

    sections: list[str] = []
    unreadable: list[str] = []
    for entry in teams:
        team = entry.get("team")
        for channel in entry.get("channels") or []:
            if not is_journal_channel(channel):
                continue
            result = await TeamsPostsTool(graph).execute(
                {"team": team, "channel": channel, "limit": per_channel})
            if not result.success:
                continue
            posts = (result.data or {}).get("posts", [])
            if not posts:
                continue
            lines = [f"{team} / {channel}"]
            for post in posts:
                when = str(post.get("created") or "")[:10]
                body = plain_text(post.get("text"))
                if body.startswith("[image:") or body.startswith("[file:"):
                    unreadable.append(f"{team} / {channel}")
                    continue
                if body:
                    lines.append(f"  {when}: {body[:400]}")
            if len(lines) > 1:
                sections.append("\n".join(lines))

    if not sections and not unreadable:
        return []

    today = datetime.now(local_zone()).strftime("%A %d %B")
    parts = [f"Communication Journal schedule for {today}."]
    parts.extend(sections)
    if unreadable:
        names = ", ".join(sorted(set(unreadable)))
        parts.append(f"Posted as images, which I cannot read yet: {names}.")
    scheduler.save_brief("cj_schedule", f"CJ schedule for {today}", "\n\n".join(parts))
    return []


def build_watchers() -> dict[str, Watcher]:
    return {
        "teams_messages": teams_messages,
        "weather": weather,
        "cj_schedule": cj_schedule,
    }
