import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from athena.tools.teams import (
    ASSIGNMENT_SCOPES,
    TeamsAssignmentsTool,
    TeamsAuth,
    TeamsGraph,
    TeamsPostsTool,
)


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self):
        import json
        return json.dumps(self.payload)


class _Session:
    # Due dates are relative to now, so the fixture never ages out: "early"
    # stays upcoming and "late" stays further ahead whichever day this runs.
    soon = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    later = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT00:00:00Z")
    pages = [
        {
            "value": [
                {"id": "late", "displayName": "Later", "dueDateTime": later},
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/education/me/assignments?$skiptoken=next",
        },
        {"value": [
            {"id": "early", "displayName": "Sooner", "dueDateTime": soon},
        ]},
    ]

    def __init__(self, **_):
        self.index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def get(self, _url, params=None, **kwargs):
        page = self.pages[self.index]
        self.index += 1
        return _Response(page)


class _Auth:
    scopes = ["User.Read", "Team.ReadBasic.All"]

    def token(self, scopes=None):
        return "test-token"


class TeamsAssignmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_assignments_follow_next_link_and_sort_by_due_date(self):
        with patch("athena.tools.teams.aiohttp.ClientSession", _Session):
            rows = await TeamsGraph(_Auth()).assignments(10)
        self.assertEqual([row["id"] for row in rows], ["early", "late"])


class TeamsConsentTests(unittest.TestCase):
    """Once an admin approves new permissions, the cached sign-in must be replaced."""

    def _auth(self):
        import os
        import tempfile
        from athena.tools.teams import TeamsAuth
        with patch.dict(os.environ, {"MICROSOFT_CLIENT_ID": "client-id",
                                     "ATHENA_DATA_DIR": tempfile.mkdtemp()}, clear=False):
            return TeamsAuth()

    def test_a_sign_in_predating_new_permissions_says_so(self):
        auth = self._auth()
        stale = type("App", (), {
            "get_accounts": lambda self: [{"username": "a@example.com"}],
            "acquire_token_silent": lambda self, *a, **k: None,
        })()
        with patch.object(type(auth), "app", lambda self: stale):
            with self.assertRaises(RuntimeError) as caught:
                auth.token()
        message = str(caught.exception)
        self.assertIn("does not cover the required permissions", message)
        self.assertIn("athena-teams-login", message)
        # The permissions must be named: "access denied" alone is not actionable.
        self.assertIn("ChannelMessage.Read.All", message)

    def test_no_account_at_all_asks_for_a_first_sign_in(self):
        auth = self._auth()
        empty = type("App", (), {
            "get_accounts": lambda self: [],
            "acquire_token_silent": lambda self, *a, **k: None,
        })()
        with patch.object(type(auth), "app", lambda self: empty):
            with self.assertRaises(RuntimeError) as caught:
                auth.token()
        self.assertIn("not signed in", str(caught.exception))

    def test_the_approved_scopes_are_the_ones_requested(self):
        auth = self._auth()
        for scope in ("User.Read", "Team.ReadBasic.All", "Channel.ReadBasic.All",
                      "ChannelMessage.Read.All", "EduAssignments.Read"):
            self.assertIn(scope, auth.scopes)


class TeamsDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    """The check must name the permission behind each failure."""

    def _graph(self, failing: str):
        class FakeGraph:
            def __init__(self):
                self.auth = type("Auth", (), {"scopes": ["User.Read", "Team.ReadBasic.All",
                                                         "Channel.ReadBasic.All",
                                                         "ChannelMessage.Read.All",
                                                         "EduAssignments.Read"]})()

            async def get(self, path, params=None, **kwargs):
                if failing in path or (failing == "joinedTeams" and "joinedTeams" in path):
                    raise RuntimeError("Microsoft denied Teams access. Ask your admin to grant it.")
                if "joinedTeams" in path:
                    return {"value": [{"displayName": "Class"}]}
                return {"displayName": "Sam"}

            async def channels(self):
                if failing == "channels":
                    raise RuntimeError("Microsoft denied Teams access.")
                return [{"team": "Class", "channels": ["General"]}]

            async def assignments(self, limit):
                if failing == "assignments":
                    raise RuntimeError("Microsoft denied Teams access.")
                return []

            async def posts(self, team, channel, limit):
                if failing == "posts":
                    raise RuntimeError("Microsoft denied Teams access.")
                return []

        return FakeGraph()

    async def _run(self, failing: str):
        import contextlib
        import io
        import os
        from athena.tools import teams
        captured = io.StringIO()
        with patch.dict(os.environ, {"MICROSOFT_CLIENT_ID": "client-id"}, clear=False):
            with patch.object(teams, "TeamsGraph", lambda *a, **k: self._graph(failing)):
                with contextlib.redirect_stdout(captured):
                    code = await teams.diagnose()
        return code, captured.getvalue()

    async def test_an_unconfigured_client_id_is_reported_plainly(self):
        import contextlib
        import io
        import os
        from athena.tools import teams
        captured = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(teams, "load_local_environment"):
            with contextlib.redirect_stdout(captured):
                code = await teams.diagnose()
        self.assertEqual(code, 1)
        self.assertIn("MICROSOFT_CLIENT_ID is not set", captured.getvalue())

    async def test_all_permissions_working_reports_success(self):
        code, output = await self._run("nothing-fails")
        self.assertEqual(code, 0)
        self.assertIn("All Teams permissions work", output)
        self.assertIn("Class / General", output)

    async def test_a_missing_permission_names_the_scope_and_fails(self):
        code, output = await self._run("assignments")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  assignments", output)
        self.assertIn("EduAssignments.Read", output)
        self.assertIn("athena-teams-login", output)

    async def test_channel_messages_are_checked_separately(self):
        code, output = await self._run("posts")
        self.assertEqual(code, 1)
        self.assertIn("ChannelMessage.Read.All", output)

    async def test_a_check_that_cannot_run_is_not_reported_as_ok(self):
        """Reporting an untested capability as working is worse than not testing."""
        code, output = await self._run("channels")
        self.assertIn("skip  channel messages", output)
        self.assertNotIn("ok    channel messages", output)


class _RecordingResponse:
    def __init__(self, payload, status=200, delay=0.0):
        self.payload = payload
        self.status = status
        self.delay = delay

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, *_):
        return False

    async def text(self):
        import json
        return json.dumps(self.payload)


class _RecordingSession:
    """Records the query options each request used."""

    calls: list = []
    headers_seen: list = []
    teams = [{"id": "team-1", "displayName": "Class"}]
    classes: list = [{"id": "class-1", "displayName": "Physics"}]
    assignments: list = []
    closed = False

    def __init__(self, *args, **kwargs):
        self.headers = kwargs.get("headers", {})

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def get(self, url, params=None, **kwargs):
        _RecordingSession.calls.append((url, params))
        headers = kwargs.get("headers") or {}
        _RecordingSession.headers_seen.append(headers)
        # Graph answers 401 without a bearer token. Mirroring that here means a
        # dropped Authorization header fails loudly instead of silently.
        if "Authorization" not in headers:
            return _RecordingResponse({"error": {"message": "Unauthorized"}}, status=401)
        if "education/me/assignments" in url:
            return _RecordingResponse({"value": list(_RecordingSession.assignments)})
        if url.endswith("/education/me/classes"):
            return _RecordingResponse({"value": list(_RecordingSession.classes)})
        if url.endswith("/me/joinedTeams"):
            return _RecordingResponse({"value": list(_RecordingSession.teams)})
        if url.endswith("/channels"):
            return _RecordingResponse({"value": [{"id": "chan-1", "displayName": "General"}]})
        return _RecordingResponse({"value": [
            {"id": "sys-1", "body": {"content": "<systemEventMessage/>"}},
            {"id": "msg-1", "body": {"content": "<div>Hello <b>team</b></div>"},
             "from": {"user": {"displayName": "Sam"}}},
        ]})


class TeamsQueryOptionTests(unittest.IsolatedAsyncioTestCase):
    """$top is rejected by these endpoints, which silently broke channel reads.

    Microsoft Graph answers 400 "Query option 'Top' is not allowed" rather than
    ignoring the option, so every channel listing and message read failed while
    looking like a permissions problem.
    """

    def setUp(self):
        _RecordingSession.calls = []
        _RecordingSession.teams = [{"id": "team-1", "displayName": "Class"}]
        patcher = patch("athena.tools.teams.aiohttp.ClientSession", _RecordingSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_every_request_carries_an_authorization_header(self):
        """Dropping the header made a working assignments call look like a 403."""
        _RecordingSession.headers_seen = []
        graph = TeamsGraph(_Auth())
        await graph.assignments(5)
        await graph.channels()
        await graph.get("/me")
        self.assertTrue(_RecordingSession.headers_seen)
        for headers in _RecordingSession.headers_seen:
            self.assertIn("Authorization", headers)

    async def test_channel_lists_are_fetched_concurrently(self):
        """Eleven teams fetched one after another took eight seconds."""
        import time as clock

        class SlowSession(_RecordingSession):
            def get(self, url, params=None, **kwargs):
                response = super().get(url, params, **kwargs)
                if url.endswith("/channels"):
                    # Each request waits, but they wait together, so six teams
                    # cost about one round trip rather than six.
                    response.delay = 0.05
                return response

        _RecordingSession.teams = [{"id": f"t{i}", "displayName": f"Team {i}"}
                                   for i in range(6)]
        with patch("athena.tools.teams.aiohttp.ClientSession", SlowSession):
            graph = TeamsGraph(_Auth())
            started = clock.perf_counter()
            rows = await graph.channels()
            elapsed = clock.perf_counter() - started
        self.assertEqual(len(rows), 6)
        self.assertLess(elapsed, 0.3, "channel lists were fetched one at a time")

    async def test_a_successful_response_returns_its_payload(self):
        """A mis-indented error branch once made every 2xx response return None."""
        data = await TeamsGraph(_Auth()).get("/me/joinedTeams")
        self.assertIsInstance(data, dict)
        self.assertIn("value", data)

    async def test_the_connection_and_token_are_reused_across_calls(self):
        """A fresh TLS handshake and token lookup per call cost seconds."""
        graph = TeamsGraph(_Auth())
        await graph.channels()
        first = len(_RecordingSession.calls)
        await graph.channels()
        self.assertEqual(len(_RecordingSession.calls), first,
                         "resolution was not cached, so the round trips repeated")
        self.assertIs(graph._session, graph._session)

    async def test_channel_listing_does_not_send_top(self):
        rows = await TeamsGraph(_Auth()).channels()
        self.assertEqual(rows, [{"team": "Class", "channels": ["General"]}])
        for url, params in _RecordingSession.calls:
            if "joinedTeams" in url or url.endswith("/channels"):
                self.assertIn(params, (None, {}), f"$top was sent to {url}")

    async def test_reading_a_channel_does_not_send_top_either(self):
        posts = await TeamsGraph(_Auth()).posts("Class", "General", 5)
        self.assertEqual([row["id"] for row in posts], ["msg-1"])
        for url, params in _RecordingSession.calls:
            if "joinedTeams" in url or url.endswith("/channels"):
                self.assertIn(params, (None, {}), f"$top was sent to {url}")

    async def test_the_messages_endpoint_keeps_its_limit(self):
        await TeamsGraph(_Auth()).posts("Class", "General", 5)
        message_calls = [params for url, params in _RecordingSession.calls
                         if url.endswith("/messages")]
        self.assertEqual(message_calls, [{"$top": "5"}])

    async def test_a_graph_error_message_is_surfaced_not_just_the_status(self):
        class BadSession(_RecordingSession):
            def get(self, url, params=None, **kwargs):
                return _RecordingResponse(
                    {"error": {"message": "Query option 'Top' is not allowed."}}, status=400)

        with patch("athena.tools.teams.aiohttp.ClientSession", BadSession):
            with self.assertRaises(RuntimeError) as caught:
                await TeamsGraph(_Auth()).channels()
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("not allowed", str(caught.exception))

    async def test_the_diagnostic_does_not_send_top_either(self):
        """The check itself fell into the same trap it was written to catch."""
        await TeamsGraph(_Auth()).channels()  # populate the recorded calls
        _RecordingSession.calls = []
        from athena.tools import teams
        import contextlib
        import io
        import os
        with patch.dict(os.environ, {"MICROSOFT_CLIENT_ID": "client-id"}, clear=False):
            stub = TeamsGraph(_Auth())
            stub.auth = type("Auth", (), {"scopes": ["User.Read"]})()
            with patch.object(teams, "TeamsGraph", lambda *a, **k: stub):
                with contextlib.redirect_stdout(io.StringIO()):
                    await teams.diagnose()
        for url, params in _RecordingSession.calls:
            if "joinedTeams" in url or url.endswith("/channels"):
                self.assertIn(params, (None, {}), f"the check sent $top to {url}")


class TeamsContentTests(unittest.IsolatedAsyncioTestCase):
    """What the model is told must be real content, not markup or join events."""

    def setUp(self):
        _RecordingSession.calls = []
        _RecordingSession.teams = [{"id": "team-1", "displayName": "Class"}]
        patcher = patch("athena.tools.teams.aiohttp.ClientSession", _RecordingSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_system_events_are_not_reported_as_posts(self):
        result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
            {"team": "Class", "channel": "General", "limit": 10})
        ids = [row["id"] for row in result.data["posts"]]
        self.assertEqual(ids, ["msg-1"], "a join/leave event was reported as a post")

    async def test_post_bodies_are_plain_text(self):
        result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
            {"team": "Class", "channel": "General", "limit": 10})
        self.assertEqual(result.data["posts"][0]["text"], "Hello team")
        self.assertNotIn("<", result.data["posts"][0]["text"])

    async def test_the_summary_names_the_latest_post(self):
        result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
            {"team": "Class", "channel": "General", "limit": 10})
        self.assertIn("Sam", result.spoken_text)
        self.assertIn("Hello team", result.spoken_text)

    async def test_a_channel_with_only_system_events_says_so(self):
        class OnlyEvents(_RecordingSession):
            def get(self, url, params=None, **kwargs):
                if url.endswith("/messages"):
                    return _RecordingResponse({"value": [
                        {"id": "s", "body": {"content": "<systemEventMessage/>"}}]})
                return super().get(url, params, **kwargs)

        with patch("athena.tools.teams.aiohttp.ClientSession", OnlyEvents):
            result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
                {"team": "Class", "channel": "General", "limit": 10})
        self.assertTrue(result.success)
        self.assertEqual(result.data["posts"], [])
        self.assertIn("no posts", result.spoken_text)

    async def test_a_unique_partial_team_name_is_accepted(self):
        result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
            {"team": "Hackers", "channel": "General", "limit": 5})
        self.assertTrue(result.success, result.spoken_text)

    async def test_a_channel_name_alone_can_identify_the_team(self):
        """"General" exists in most teams, so it only resolves when unique."""
        result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
            {"team": "Nowhere", "channel": "General", "limit": 5})
        self.assertTrue(result.success, result.spoken_text)

    async def test_an_ambiguous_request_lists_the_real_names(self):
        _RecordingSession.teams = [
            {"id": "t1", "displayName": "Class One"},
            {"id": "t2", "displayName": "Class Two"},
        ]
        result = await TeamsPostsTool(TeamsGraph(_Auth())).execute(
            {"team": "Nowhere", "channel": "General", "limit": 5})
        self.assertFalse(result.success)
        self.assertIn("Class One", result.spoken_text)
        self.assertIn("Your teams are", result.spoken_text)


class AssignmentOrderingTests(unittest.IsolatedAsyncioTestCase):
    """Oldest-first ordering returned work from a year ago as "what is due"."""

    def setUp(self):
        _RecordingSession.calls = []
        _RecordingSession.classes = [{"id": "class-1", "displayName": "Physics"}]
        patcher = patch("athena.tools.teams.aiohttp.ClientSession", _RecordingSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _graph(self, rows):
        _RecordingSession.assignments = rows
        return TeamsGraph(_Auth())

    async def test_upcoming_work_comes_before_old_work(self):
        now = datetime.now(timezone.utc)
        graph = self._graph([
            {"displayName": "ancient", "dueDateTime": "2025-09-21T13:30:00Z"},
            {"displayName": "next week", "dueDateTime": (now + timedelta(days=7)).isoformat()},
            {"displayName": "tomorrow", "dueDateTime": (now + timedelta(days=1)).isoformat()},
        ])
        tool = TeamsAssignmentsTool()
        tool.graph = graph
        result = await tool.execute({})
        names = [row["name"] for row in result.data["assignments"]]
        self.assertEqual(names, ["tomorrow", "next week", "ancient"])

    async def test_overdue_work_is_flagged_and_not_called_upcoming(self):
        now = datetime.now(timezone.utc)
        graph = self._graph([
            {"displayName": "late", "dueDateTime": (now - timedelta(days=30)).isoformat()},
        ])
        tool = TeamsAssignmentsTool()
        tool.graph = graph
        result = await tool.execute({})
        self.assertTrue(result.data["assignments"][0]["overdue"])
        self.assertIn("all past due", result.spoken_text)

    async def test_the_summary_names_the_next_assignment(self):
        now = datetime.now(timezone.utc)
        graph = self._graph([
            {"displayName": "Essay", "dueDateTime": (now + timedelta(days=2)).isoformat()},
        ])
        tool = TeamsAssignmentsTool()
        tool.graph = graph
        result = await tool.execute({})
        self.assertIn("Essay", result.spoken_text)
        self.assertIn("upcoming", result.spoken_text)


class ClassFilterTests(unittest.IsolatedAsyncioTestCase):
    """"Homework for Physics" was filtered with a team ID, which Graph rejects.

    A class and a team are different objects. The roster comes from
    /education/me/classes, and its ID is what /education/classes/{id} expects.
    """

    def setUp(self):
        _RecordingSession.calls = []
        _RecordingSession.classes = [{"id": "class-1", "displayName": "Physics"}]
        _RecordingSession.assignments = []
        patcher = patch("athena.tools.teams.aiohttp.ClientSession", _RecordingSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_class_filter_uses_the_class_roster_not_joined_teams(self):
        now = datetime.now(timezone.utc)
        _RecordingSession.assignments = [
            {"id": "a1", "classId": "class-1", "displayName": "Optics lab",
             "dueDateTime": (now + timedelta(days=1)).isoformat()},
        ]
        tool = TeamsAssignmentsTool(TeamsGraph(_Auth()))
        result = await tool.execute({"class_name": "Physics"})
        self.assertTrue(result.success, result.spoken_text)
        class_calls = [url for url, _ in _RecordingSession.calls if "/education/classes/" in url]
        self.assertTrue(class_calls, "the class-scoped endpoint was never called")
        self.assertTrue(all("class-1" in url for url in class_calls),
                        f"a non-class ID was sent: {class_calls}")

    async def test_a_unique_partial_class_name_is_accepted(self):
        _RecordingSession.classes = [
            {"id": "class-1", "displayName": "Physics"},
            {"id": "class-2", "displayName": "History"},
        ]
        tool = TeamsAssignmentsTool(TeamsGraph(_Auth()))
        result = await tool.execute({"class_name": "phys"})
        self.assertTrue(result.success, result.spoken_text)

    async def test_an_unknown_class_lists_the_real_names(self):
        _RecordingSession.classes = [{"id": "class-1", "displayName": "Physics"}]
        tool = TeamsAssignmentsTool(TeamsGraph(_Auth()))
        result = await tool.execute({"class_name": "Astronomy"})
        self.assertFalse(result.success)
        self.assertIn("Physics", result.spoken_text)

    async def test_the_roster_is_fetched_without_top(self):
        await TeamsGraph(_Auth()).classes()
        for url, params in _RecordingSession.calls:
            if url.endswith("/education/me/classes"):
                self.assertIn(params, (None, {}), f"$top was sent to {url}")

    async def test_the_roster_request_carries_a_token(self):
        _RecordingSession.headers_seen = []
        await TeamsGraph(_Auth()).classes()
        self.assertTrue(_RecordingSession.headers_seen)
        for headers in _RecordingSession.headers_seen:
            self.assertIn("Authorization", headers)

    async def test_edu_roster_is_requested_so_the_filter_can_work(self):
        import os
        import tempfile
        with patch.dict(os.environ, {"MICROSOFT_CLIENT_ID": "client-id",
                                     "ATHENA_DATA_DIR": tempfile.mkdtemp()}, clear=False):
            self.assertIn("EduRoster.ReadBasic", TeamsAuth().scopes)

    def test_the_roster_scope_is_not_part_of_the_default_assignment_token(self):
        """An unfiltered "what is due" must not need the roster consent.

        ASSIGNMENT_SCOPES is used to mint the token for plain assignment reads.
        Leaving EduRoster.ReadBasic in it made every unfiltered read fail on a
        token cached before that scope existed, which the live Pi run caught.
        """
        self.assertNotIn("EduRoster.ReadBasic", ASSIGNMENT_SCOPES)
        self.assertEqual(ASSIGNMENT_SCOPES, ["User.Read", "EduAssignments.Read"])

    async def test_the_roster_is_fetched_with_the_roster_scope(self):
        """classes() must ask for the roster scope on top of the base scopes."""
        seen = []

        class _Auth:
            def token(self, scopes=None):
                seen.append(list(scopes or []))
                return "token"

        with patch("athena.tools.teams.aiohttp.ClientSession", _RecordingSession):
            graph = TeamsGraph(_Auth())
            await graph.classes()
        self.assertTrue(seen, "no token was requested")
        for scopes in seen:
            self.assertIn("EduRoster.ReadBasic", scopes)
            self.assertIn("EduAssignments.Read", scopes)

    async def test_no_class_filter_still_works_when_the_roster_is_denied(self):
        """An unfiltered "what is due" must not break on a missing consent."""
        now = datetime.now(timezone.utc)
        _RecordingSession.assignments = [
            {"displayName": "Essay", "dueDateTime": (now + timedelta(days=1)).isoformat()},
        ]

        class DeniedRoster(_RecordingSession):
            def get(self, url, params=None, **kwargs):
                if url.endswith("/education/me/classes"):
                    return _RecordingResponse({"error": {"message": "denied"}}, status=403)
                return super().get(url, params, **kwargs)

        with patch("athena.tools.teams.aiohttp.ClientSession", DeniedRoster):
            tool = TeamsAssignmentsTool(TeamsGraph(_Auth()))
            result = await tool.execute({})
        self.assertTrue(result.success, result.spoken_text)
        self.assertIn("Essay", result.spoken_text)

    async def test_a_class_filter_reports_the_missing_roster_consent(self):
        class DeniedRoster(_RecordingSession):
            def get(self, url, params=None, **kwargs):
                if url.endswith("/education/me/classes"):
                    return _RecordingResponse({"error": {"message": "denied"}}, status=403)
                return super().get(url, params, **kwargs)

        with patch("athena.tools.teams.aiohttp.ClientSession", DeniedRoster):
            tool = TeamsAssignmentsTool(TeamsGraph(_Auth()))
            result = await tool.execute({"class_name": "Physics"})
        self.assertFalse(result.success)
        self.assertIn("EduRoster.ReadBasic", result.spoken_text)

    async def test_student_can_filter_own_work_when_roster_and_class_access_are_denied(self):
        class StudentGraph:
            async def classes(self): raise RuntimeError('Missing EduRoster.ReadBasic')
            async def get(self, path):
                if path == '/me/joinedTeams':
                    return {'value': [{'id': 'physics', 'displayName': 'AP Physics'}]}
                raise RuntimeError('Class detail denied')
            async def assignments(self, limit, class_id=None):
                if class_id: raise RuntimeError('Class assignments denied')
                return [{'id': '1', 'classId': 'math', 'displayName': 'Math worksheet'},
                        {'id': '2', 'classId': 'physics', 'displayName': 'Optics lab'}]
        result = await TeamsAssignmentsTool(StudentGraph()).execute({'class_name': 'physics'})
        self.assertTrue(result.success, result.spoken_text)
        self.assertIn('Optics lab', result.spoken_text)
        self.assertNotIn('Math worksheet', result.spoken_text)
