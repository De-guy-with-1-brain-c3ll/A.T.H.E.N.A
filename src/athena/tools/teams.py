"""Read-only Microsoft Teams access through Microsoft Graph.

Authentication uses the device-code flow so it works on a headless Orange Pi.
Set MICROSOFT_CLIENT_ID to an Entra public-client application ID, then run
``athena-teams-login`` once. Tokens are kept under ATHENA_DATA_DIR.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html import unescape
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
import msal

from athena.config import load_local_environment
from athena.paths import data_directory
from athena.tools.models import ToolDefinition, ToolResult
from athena.vision import ImageReader, is_image_name, mime_for, sharepoint_target, vision_model


GRAPH = "https://graph.microsoft.com/v1.0"
# Team.ReadBasic.All is required by /me/joinedTeams and Channel.ReadBasic.All
# is required to resolve a team's channel names before reading its posts.
DEFAULT_SCOPES = [
    "User.Read",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Read.All",
    "EduAssignments.Read",
    # Communication Journal posts are often a photo of the day's notes rather
    # than text, and the image lives in the team's SharePoint library. Without
    # this the post can only be reported as an unreadable attachment.
    "Files.Read.All",
]
ASSIGNMENT_SCOPES = ["User.Read", "EduAssignments.Read"]

_HTML_TAG = re.compile(r"<[^>]+>")
# Teams emits a message for every "X added Y to the channel" style event. They
# carry no subject and no author, and reporting them as posts made the channel
# look empty of anything real.
SYSTEM_EVENT = "systemeventmessage"


def parse_due(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def attachment_note(message: dict[str, Any]) -> str:
    """Describe a post's attachments when there is no text to read.

    A Communication Journal post is often nothing but a photo of the day's notes:
    the body is a bare ``<attachment>`` tag and the content is a file on
    SharePoint. Stripping the tag left an empty string, so those posts looked
    blank and the channel looked broken. Saying what is there is the honest
    answer, and it tells the reader where to look.
    """
    names = [str(item.get("name") or "").strip()
             for item in message.get("attachments") or []]
    names = [name for name in names if name]
    if not names:
        return ""
    images = any(name.casefold().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"))
                 for name in names)
    return f"[{'image' if images else 'file'}: {', '.join(names)}]"


def message_text(message: dict[str, Any]) -> str:
    """Plain text for a channel message, or "" when it is only a system event."""
    body = (message.get("body") or {}).get("content") or ""
    if SYSTEM_EVENT in body.casefold():
        return ""
    text = " ".join(unescape(_HTML_TAG.sub(" ", body)).split())
    return text or attachment_note(message)


def message_links(message: dict[str, Any]) -> list[dict[str, str]]:
    """Attachment names and URLs, so a photo of the notes can still be opened."""
    links = []
    for item in message.get("attachments") or []:
        name = str(item.get("name") or "").strip()
        url = str(item.get("contentUrl") or "").strip()
        if name or url:
            links.append({"name": name or "attachment", "url": url})
    return links


def match_by_name(rows: list[dict[str, Any]], name: str,
                  key: str = "displayName") -> dict[str, Any] | None:
    """Exact match first, then a unique partial one.

    A model asking for "Network Hackers" should not fail because it guessed the
    punctuation, and a partial name is only accepted when it is unambiguous.
    """
    wanted = " ".join(str(name or "").split()).casefold()
    if not wanted:
        return None
    for row in rows:
        if str(row.get(key, "")).casefold() == wanted:
            return row
    partial = [row for row in rows if wanted in str(row.get(key, "")).casefold()]
    return partial[0] if len(partial) == 1 else None


class TeamsAuth:
    def __init__(self) -> None:
        self.client_id = os.environ.get("MICROSOFT_CLIENT_ID", "").strip()
        self.tenant = os.environ.get("MICROSOFT_TENANT_ID", "common").strip() or "common"
        raw = os.environ.get("ATHENA_TEAMS_SCOPES", "").strip()
        self.scopes = raw.split() if raw else DEFAULT_SCOPES
        self.cache_path = data_directory() / "microsoft-token-cache.json"
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.is_file():
            try:
                self.cache.deserialize(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    def _save(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize(), encoding="utf-8")

    def app(self):
        if not self.client_id:
            raise RuntimeError("MICROSOFT_CLIENT_ID is not configured.")
        return msal.PublicClientApplication(
            self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant}",
            token_cache=self.cache,
        )

    def token(self, scopes: list[str] | None = None) -> str:
        app = self.app()
        # MSAL does not reliably choose an account when ``account=None``.
        # Device-code login stores the account in the cache, so explicitly
        # select the first cached account before attempting silent renewal.
        accounts = app.get_accounts()
        account = accounts[0] if accounts else None
        requested = scopes or self.scopes
        result = app.acquire_token_silent(requested, account=account)
        if not result or "access_token" not in result:
            if account is None:
                raise RuntimeError("Teams is not signed in. Run athena-teams-login first.")
            # The usual cause once an admin has approved new permissions: the
            # cached token predates them, so silent renewal cannot satisfy the
            # request. Saying so is far more useful than a bare 403 later.
            raise RuntimeError(
                "Teams sign-in does not cover the required permissions ("
                + ", ".join(requested)
                + "). Run athena-teams-login again to grant them."
            )
        self._save()
        return result["access_token"]

    def login(self) -> None:
        app = self.app()
        print("Requesting permissions: " + ", ".join(self.scopes), flush=True)
        flow = app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise RuntimeError("Could not start Microsoft sign-in.")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(result.get("error_description", "Microsoft sign-in failed."))
        self._save()
        print("Teams sign-in completed.", flush=True)


# A Teams read used to take about four and a half seconds, almost all of it
# overhead: every Graph call opened a brand new TLS connection to Microsoft and
# re-ran MSAL's token acquisition. One reused connection and a cached token cut
# that to well under a second.
TOKEN_REUSE_SECONDS = 45 * 60
# Teams and channel names rarely change, so resolving them once is enough for a
# conversation. Two of the three round trips in a channel read disappear.
RESOLUTION_CACHE_SECONDS = 10 * 60


class TeamsGraph:
    def __init__(self, auth: TeamsAuth | None = None) -> None:
        self.auth = auth or TeamsAuth()
        self._session: aiohttp.ClientSession | None = None
        self._tokens: dict[tuple[str, ...], tuple[str, float]] = {}
        self._token_lock = asyncio.Lock()
        self._teams: list[dict[str, Any]] = []
        self._teams_at = 0.0
        self._channels: dict[str, tuple[list[dict[str, Any]], float]] = {}

    async def _access_token(self, scopes: list[str] | None = None) -> str:
        key = tuple(scopes or self.auth.scopes)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        async with self._token_lock:
            cached = self._tokens.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            refresh = asyncio.create_task(asyncio.to_thread(self.auth.token, scopes))
            try:
                token = await asyncio.shield(refresh)
            except asyncio.CancelledError:
                await asyncio.gather(refresh, return_exceptions=True)
                raise
            self._tokens[key] = (token, time.monotonic() + TOKEN_REUSE_SECONDS)
            return token

    async def _client(self) -> aiohttp.ClientSession:
        """One connection pool for the whole process, so TLS is paid once."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_bytes(self, path: str) -> bytes:
        session = await self._client()
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        async with session.get(GRAPH + path, headers=headers) as response:
            if response.status >= 400:
                if response.status in {401, 403}:
                    raise RuntimeError(
                        "Microsoft denied access to that file. Ask your admin to grant "
                        "Files.Read.All, then run athena-teams-login again so the new "
                        "permission is in the token.")
                raise RuntimeError(f"Microsoft Graph returned HTTP {response.status}.")
            return await response.read()

    async def download_attachment(self, url: str) -> tuple[str, bytes]:
        """Fetch a reference attachment through Graph, since its own URL needs its own token."""
        target = sharepoint_target(url)
        if target is None:
            raise RuntimeError("That attachment is not a SharePoint file I can fetch.")
        host, site_path, file_path = target
        site = await self.get(f"/sites/{host}:{site_path}")
        site_id = site.get("id")
        if not site_id:
            raise RuntimeError("I could not resolve the SharePoint site for that file.")
        quoted = "/".join(quote(part) for part in file_path.split("/"))
        data = await self.get_bytes(f"/sites/{quote(site_id)}/drive/root:{quoted}:/content")
        return file_path.rsplit("/", 1)[-1], data

    async def joined_teams(self) -> list[dict[str, Any]]:
        if self._teams and time.monotonic() - self._teams_at < RESOLUTION_CACHE_SECONDS:
            return self._teams
        self._teams = (await self.get("/me/joinedTeams")).get("value", [])
        self._teams_at = time.monotonic()
        return self._teams

    async def team_channels(self, team_id: str) -> list[dict[str, Any]]:
        cached = self._channels.get(team_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        # $top is rejected here with HTTP 400 ("Query option 'Top' is not allowed").
        found = (await self.get(f"/teams/{quote(team_id)}/channels")).get("value", [])
        self._channels[team_id] = (found, time.monotonic() + RESOLUTION_CACHE_SECONDS)
        return found

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        session = await self._client()
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        async with session.get(GRAPH + path, params=params, headers=headers) as response:
            body = await response.text()
            if response.status >= 400:
                if response.status in {401, 403}:
                    if path == "/me/joinedTeams":
                        detail = "Team.ReadBasic.All"
                    elif "/channels" in path:
                        detail = "Channel.ReadBasic.All (plus ChannelMessage.Read.All for posts)"
                    else:
                        detail = "the delegated Graph permissions"
                    raise RuntimeError(
                        f"Microsoft denied Teams access. Ask your admin to grant {detail}, "
                        "then sign in to Teams again."
                    )
                # A 400 is a request ATHENA built wrongly, not a permission
                # problem, so Graph's own explanation has to be surfaced:
                # "HTTP 400" alone gives no way to tell the two apart.
                explanation = ""
                try:
                    explanation = str(json.loads(body).get("error", {}).get("message") or "")
                except ValueError:
                    pass
                raise RuntimeError(
                    f"Microsoft Graph returned HTTP {response.status} for {path}."
                    + (f" {explanation}" if explanation else "")
                )
            try:
                return json.loads(body)
            except ValueError:
                raise RuntimeError("Microsoft Graph returned invalid data.") from None

    async def assignments(self, limit: int) -> list[dict[str, Any]]:
        # Graph can paginate this collection even when `$top` is supplied.
        # Follow nextLink so older/nearer assignments are not silently omitted.
        rows: list[dict[str, Any]] = []
        url: str | None = "/education/me/assignments"
        params: dict[str, str] | None = {
            "$top": "100",
            "$orderby": "dueDateTime",
            "$select": "id,displayName,dueDateTime,status,classId,assignDateTime,closeDateTime",
        }
        session = await self._client()
        headers = {"Authorization": f"Bearer {await self._access_token(ASSIGNMENT_SCOPES)}"}
        pages = 0
        while url and pages < 20:
            pages += 1
            request_url = GRAPH + url if url.startswith("/") else url
            async with session.get(request_url, params=params, headers=headers) as response:
                body = await response.text()
                if response.status >= 400:
                    if response.status in {401, 403}:
                        raise RuntimeError(
                            "Microsoft denied assignment access. Check EduAssignments.Read "
                            "consent, then sign in to Teams again."
                        )
                    raise RuntimeError(f"Microsoft Graph returned HTTP {response.status}.")
                try:
                    data = json.loads(body)
                except ValueError:
                    raise RuntimeError("Microsoft Graph returned invalid data.") from None
            rows.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = None  # nextLink already contains its query string
        # `$orderby=dueDateTime` is ascending, so taking the first `limit` rows
        # returned the *oldest* assignments on record — a year out of date, and
        # exactly why "what is due" looked broken. Upcoming work comes first, and
        # past work is only used to fill the list when there is little ahead.
        now = datetime.now(timezone.utc)
        ahead: list[tuple[datetime, dict[str, Any]]] = []
        undated: list[dict[str, Any]] = []
        behind: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            due = parse_due(row.get("dueDateTime"))
            if due is None:
                undated.append(row)
            elif due >= now - timedelta(hours=12):
                ahead.append((due, row))
            else:
                behind.append((due, row))
        ahead.sort(key=lambda pair: pair[0])
        behind.sort(key=lambda pair: pair[0], reverse=True)
        # `limit` is the tool's documented maximum, so it caps the whole list —
        # not just the past-due entries, which is how a limit of five could
        # still return a hundred upcoming assignments.
        result = [row for _, row in ahead] + undated + [row for _, row in behind]
        return result[:limit]

    async def posts(self, team: str, channel: str, limit: int) -> list[dict[str, Any]]:
        teams = await self.joined_teams()
        match = match_by_name(teams, team)
        channels: list[dict[str, Any]] = []
        chosen: dict[str, Any] | None = None
        if match is not None:
            channels = await self.team_channels(match["id"])
            chosen = match_by_name(channels, channel)
        else:
            # "General" exists in almost every team, so a channel name alone
            # cannot identify one — but when exactly one team has it, it can.
            # The teams are checked together rather than one after another.
            found_lists = await asyncio.gather(
                *(self.team_channels(candidate["id"]) for candidate in teams),
                return_exceptions=True,
            )
            for candidate, found in zip(teams, found_lists):
                if isinstance(found, BaseException):
                    continue
                picked = match_by_name(found, channel)
                if picked is None:
                    continue
                if chosen is not None:
                    chosen = None
                    break
                match, chosen, channels = candidate, picked, found
            if chosen is None:
                names = ", ".join(str(x.get("displayName")) for x in teams)
                raise RuntimeError(
                    f"I could not find a joined team matching {team!r}. "
                    f"Your teams are: {names}.")
        if chosen is None:
            names = ", ".join(str(x.get("displayName")) for x in channels)
            raise RuntimeError(
                f"I could not find a channel matching {channel!r} in {match.get('displayName')}. "
                f"Its channels are: {names}.")
        raw = (await self.get(f"/teams/{quote(match['id'])}/channels/{quote(chosen['id'])}/messages",
                              {"$top": str(limit)})).get("value", [])
        # Drop system events so "read the channel" reports things people wrote.
        return [row for row in raw if message_text(row) or row.get("subject")]

    async def channels(self) -> list[dict[str, Any]]:
        teams = await self.joined_teams()
        # Eleven teams meant eleven sequential round trips, eight seconds of
        # waiting for work that is entirely independent.
        found = await asyncio.gather(
            *(self.team_channels(team["id"]) for team in teams),
            return_exceptions=True,
        )
        result = []
        for team, channels in zip(teams, found):
            if isinstance(channels, BaseException):
                channels = []
            result.append({"team": team.get("displayName"),
                           "channels": [channel.get("displayName") for channel in channels]})
        return result


class TeamsAssignmentsTool:
    definition = ToolDefinition(
        name="teams_assignments",
        description="Read the user's Microsoft Teams education assignments and due dates. Read-only.",
        parameters={"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}}, "additionalProperties": False},
        timeout_seconds=25,
    )

    def __init__(self, graph=None): self.graph = graph or TeamsGraph()

    async def close(self):
        await self.graph.close()

    async def execute(self, arguments):
        try:
            items = await self.graph.assignments(arguments.get("limit", 50))
            now = datetime.now(timezone.utc)
            rows = []
            for item in items:
                due = parse_due(item.get("dueDateTime"))
                rows.append({
                    "name": item.get("displayName"),
                    "due": item.get("dueDateTime"),
                    "days_until_due": (due - now).days if due else None,
                    "overdue": bool(due and due < now),
                    "status": item.get("status"),
                    "class_id": item.get("classId"),
                    "id": item.get("id"),
                })
            if not rows:
                return ToolResult(True, "You have no Teams assignments right now.",
                                  {"assignments": []})
            upcoming = [row for row in rows if not row["overdue"]]
            # A bare count was useless to speak aloud, so name the next one.
            head = upcoming[0] if upcoming else rows[0]
            due = head["due"][:10] if head["due"] else "no due date"
            state = f"{len(upcoming)} still upcoming" if upcoming else "all past due"
            return ToolResult(
                True,
                f"{len(rows)} Teams assignment(s), {state}. "
                f"Next: {head['name']} due {due}.",
                {"assignments": rows},
            )
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as error:
            return ToolResult(False, str(error))


class TeamsPostsTool:
    definition = ToolDefinition(
        name="teams_channel_posts",
        description=(
            "Read recent posts from a Microsoft Teams channel, including Communication "
            "Journals. Read-only; never sends a message. A post that is a photo of the "
            "notes is transcribed automatically, so the content comes back as text."
        ),
        parameters={"type": "object", "properties": {
            "team": {"type": "string", "minLength": 1, "maxLength": 120},
            "channel": {"type": "string", "minLength": 1, "maxLength": 120},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "read_images": {"type": "boolean", "default": True,
                            "description": "Transcribe image-only posts. Costs a vision call each."},
        }, "required": ["team", "channel"], "additionalProperties": False},
        # Transcribing a photo takes a vision call, so this needs more room than
        # a plain Graph read.
        timeout_seconds=90,
    )

    def __init__(self, graph=None): self.graph = graph or TeamsGraph()

    async def execute(self, arguments):
        where = f"{arguments['team']} / {arguments['channel']}"
        try:
            items = await self.graph.posts(arguments["team"], arguments["channel"],
                                           arguments.get("limit", 20))
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as error:
            return ToolResult(False, str(error))
        if not items:
            return ToolResult(True, f"There are no posts in {where} yet.", {"posts": []})

        read_images = arguments.get("read_images", True)
        rows: list[dict[str, Any]] = []
        problems: list[str] = []
        for item in items:
            body = ((item.get("body") or {}).get("content") or "")
            attachments = message_links(item)
            images = [a for a in attachments if a["url"] and is_image_name(a["name"])]
            text = message_text(item)
            # A body that is only an <attachment> tag carries no words; the notes
            # are in the picture, so the picture has to be read.
            if read_images and images and not _HTML_TAG.sub("", body).strip():
                transcribed, problem = await self._transcribe(images)
                if transcribed:
                    text = transcribed
                elif problem:
                    problems.append(problem)
            rows.append({
                "id": item.get("id"),
                "created": item.get("createdDateTime"),
                "subject": item.get("subject"),
                "text": text[:2000],
                "attachments": attachments,
                "from": ((item.get("from") or {}).get("user") or {}).get("displayName"),
            })

        latest = rows[0]
        preview = (latest["subject"] or latest["text"] or "no text")[:110]
        spoken = (f"{len(rows)} post(s) in {where}. Latest from "
                  f"{latest['from'] or 'someone unknown'}: {preview}")
        if problems:
            # Say why rather than pretending the post was empty.
            spoken += f" One post could not be read: {problems[0][:160]}"
        return ToolResult(True, spoken, {"posts": rows})

    async def _transcribe(self, images: list[dict]) -> tuple[str, str | None]:
        """Turn image-only posts into text. Returns (text, problem)."""
        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            return "", "there is no DashScope key, so images cannot be read"
        reader = self._reader_for(key)
        try:
            parts = []
            for image in images[:3]:
                name, data = await self.graph.download_attachment(image["url"])
                parts.append(await reader.read(data, mime_for(name)))
            return "\n\n".join(part for part in parts if part), None
        except Exception as error:
            return "", str(error)


def create_tools():
    # One Graph for all three tools. Each instance carried its own TLS
    # connection pool, its own MSAL token cache and its own name-resolution
    # cache; sharing one means one handshake, one token, one cache — and the
    # registry's shutdown closes it once via whichever tool runs first
    # (TeamsGraph.close is idempotent).
    graph = TeamsGraph()
    return [TeamsAssignmentsTool(graph), TeamsPostsTool(graph), TeamsChannelsTool(graph)]


class TeamsChannelsTool:
    definition = ToolDefinition(
        name="teams_channels",
        description="List the user's joined Microsoft Teams and channel names so a channel can be selected. Read-only.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        timeout_seconds=25,
    )

    def __init__(self, graph=None): self.graph = graph or TeamsGraph()

    async def execute(self, arguments):
        try:
            rows = await self.graph.channels()
            return ToolResult(True, f"I found {len(rows)} joined team(s).", {"teams": rows})
        except (RuntimeError, aiohttp.ClientError, TimeoutError) as error:
            return ToolResult(False, str(error))


class _Skipped(Exception):
    """A check that could not run because an earlier one failed."""


async def diagnose() -> int:
    """Report which Teams capability works, so an admin change is verifiable.

    Each check names the permission it needs, because "Microsoft denied access"
    on its own does not tell you which consent is still missing.
    """
    load_local_environment()
    if not os.environ.get("MICROSOFT_CLIENT_ID", "").strip():
        print("MICROSOFT_CLIENT_ID is not set, so Teams is switched off entirely.")
        print("Add it to /etc/athena/athena.env on the Pi (or .env on a computer).")
        return 1
    graph = TeamsGraph()
    print("Requested permissions: " + ", ".join(graph.auth.scopes))
    print()
    failures = 0
    skipped = 0
    channels: list[dict] = []

    async def profile():
        data = await graph.get("/me", {"$select": "displayName,userPrincipalName"})
        return data.get("displayName") or data.get("userPrincipalName") or "signed in"

    async def teams():
        data = await graph.get("/me/joinedTeams")
        names = [item.get("displayName") for item in data.get("value", [])]
        return f"{len(names)} team(s): " + ", ".join(filter(None, names)) or "no teams"

    async def channel_list():
        nonlocal channels
        channels = await graph.channels()
        total = sum(len(entry.get("channels") or []) for entry in channels)
        return f"{len(channels)} team(s), {total} channel(s)"

    async def assignments():
        items = await graph.assignments(5)
        return f"{len(items)} assignment(s)"

    async def posts():
        for entry in channels:
            for channel in entry.get("channels") or []:
                found = await graph.posts(entry["team"], channel, 1)
                return f"read {len(found)} post(s) from {entry['team']} / {channel}"
        raise _Skipped("no channel was readable, so messages could not be tested")

    checks = [
        ("signed in", "User.Read", profile),
        ("joined teams", "Team.ReadBasic.All", teams),
        ("channel names", "Channel.ReadBasic.All", channel_list),
        ("assignments", "EduAssignments.Read", assignments),
        ("channel messages", "ChannelMessage.Read.All", posts),
    ]
    try:
        for label, permission, call in checks:
            try:
                summary = await call()
            except _Skipped as reason:
                skipped += 1
                print(f"  skip  {label}  ({reason})")
            except Exception as error:
                failures += 1
                print(f"  FAIL  {label}  (needs {permission})")
                print(f"        {error}")
            else:
                print(f"  ok    {label}  ->  {summary}")
    finally:
        closer = getattr(graph, "close", None)
        if closer:
            await closer()

    print()
    if failures:
        print(f"{failures} check(s) failed. Grant the named permission in Azure, then "
              "run athena-teams-login again: an existing sign-in keeps the old token.")
        return 1
    if skipped:
        print(f"Every permission that could be tested works, but {skipped} check(s) "
              "could not run.")
        return 0
    print("All Teams permissions work. New-message and assignment alerts can be used.")
    return 0


def check() -> int:
    return asyncio.run(diagnose())


def login() -> None:
    TeamsAuth().login()
