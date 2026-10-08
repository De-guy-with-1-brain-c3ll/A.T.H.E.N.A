import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from athena.memory.database import MemoryDatabase, StoredTurn
from athena.prompts import packaged_prompt_path, read_prompt, write_prompt
from athena.web import (
    VOICE_SERVICE,
    WEB_RESTART_DELAY,
    WEB_SERVICE,
    DashboardState,
    _authenticated,
    _is_local,
    _schedule_web_restart,
    _service_action,
    _systemctl,
    index,
)
from athena.web_auth import COOKIE, SessionAuth, auth_disabled
from uuid import uuid4


class WebDashboardTests(unittest.TestCase):
    def test_control_panel_has_a_dedicated_athena_restart_button(self):
        page = (Path(__file__).parents[1] / "src" / "athena" / "web_static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="restartAthena"', page)
        self.assertIn('data-service="restart"', page)
        self.assertIn(">Restart ATHENA<", page)

    def test_dashboard_accepts_only_local_addresses(self):
        self.assertTrue(_is_local("127.0.0.1"))
        self.assertTrue(_is_local("192.168.31.10"))
        self.assertFalse(_is_local("8.8.8.8"))
        self.assertFalse(_is_local(None))

    def test_signed_session_expires_and_csrf_is_bound_to_it(self):
        state = DashboardState.__new__(DashboardState)
        state.auth = SessionAuth("a long enough test password",
                                 b"a sufficiently long dashboard test secret")
        token = state.issue_session()
        self.assertTrue(state.valid_session(token))
        self.assertFalse(state.valid_session(token + "changed"))
        self.assertNotEqual(state.csrf(token), state.csrf(token + "changed"))

    def test_prompt_override_is_persistent_and_packaged_default_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ATHENA_DATA_DIR": directory}, clear=False):
                self.assertEqual(read_prompt("system"),
                                 packaged_prompt_path("system").read_text(encoding="utf-8").strip())
                target = write_prompt("system", "Be concise and precise.")
                self.assertEqual(target, Path(directory) / "prompts" / "system_prompt.txt")
                self.assertEqual(read_prompt("system"), "Be concise and precise.")
                with self.assertRaises(ValueError):
                    write_prompt("system", "")

    def test_recent_conversations_include_time_and_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MemoryDatabase(Path(directory) / "athena.db")
            database.initialize()
            first, second = uuid4(), uuid4()
            database.save_turn(StoredTurn(first, "one", "first"))
            database.save_turn(StoredTurn(second, "two", "second"))
            rows = database.recent_conversations()
            self.assertEqual(rows[0].turn_id, second)
            self.assertTrue(rows[0].started_at)


class _FakeRequest:
    """Enough of a request for the auth helpers to run without a server."""

    def __init__(self, state, cookie: str | None = None) -> None:
        self.app = {"state": state}
        self.cookies = {} if cookie is None else {COOKIE: cookie}


def _disabled_state() -> DashboardState:
    state = DashboardState.__new__(DashboardState)
    state.auth = SessionAuth("", b"a sufficiently long dashboard test secret",
                             required=False)
    return state


class DashboardPasswordDisabledTests(unittest.TestCase):
    """The password can be switched off, and what that does and does not give up.

    Turning it off is a deliberate choice, so the tests pin the two halves that
    matter: it really does stop asking, and it does not also throw away the CSRF
    token — which is the only thing left stopping a page on another site from
    driving the dashboard through the browser of whoever is sitting there.
    """

    def test_the_switch_is_an_explicit_value_not_a_missing_one(self):
        for value in ("off", "OFF", "none", "disabled", "no", "0"):
            with patch.dict(os.environ, {"ATHENA_WEB_AUTH": value}):
                self.assertTrue(auth_disabled(), value)

    def test_a_typo_leaves_the_password_in_force(self):
        # Failing towards "still protected" is the only safe direction here.
        for value in ("", "ofl", "false", "yes", "1"):
            with patch.dict(os.environ, {"ATHENA_WEB_AUTH": value}):
                self.assertFalse(auth_disabled(), value)

    def test_a_short_password_is_still_refused_when_switched_on(self):
        with self.assertRaises(ValueError):
            SessionAuth("1234", b"a sufficiently long dashboard test secret")

    def test_a_short_password_is_allowed_when_switched_off(self):
        # The point of the switch: no password prompt at all.
        auth = SessionAuth("", b"a sufficiently long dashboard test secret",
                           required=False)
        self.assertTrue(auth.disabled)

    def test_the_secret_is_still_required_when_switched_off(self):
        # The secret signs the session and the CSRF token, so it is not part of
        # what switching the password off gives up.
        with self.assertRaises(ValueError):
            SessionAuth("", b"too short", required=False)

    def test_a_secret_is_generated_when_there_is_none_to_read(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                    "ATHENA_WEB_AUTH": "off",
                    "ATHENA_WEB_ENV_FILE": str(Path(directory) / "absent.env")},
                    clear=False):
                os.environ.pop("ATHENA_WEB_SECRET", None)
                os.environ.pop("ATHENA_WEB_PASSWORD", None)
                auth = SessionAuth.from_environment()
        self.assertTrue(auth.disabled)
        self.assertGreaterEqual(len(auth.secret), 32)

    def test_a_request_with_no_cookie_is_accepted_when_switched_off(self):
        self.assertTrue(_authenticated(_FakeRequest(_disabled_state())))

    def test_a_request_with_no_cookie_is_refused_when_switched_on(self):
        state = DashboardState.__new__(DashboardState)
        state.auth = SessionAuth("a long enough test password",
                                 b"a sufficiently long dashboard test secret")
        self.assertFalse(_authenticated(_FakeRequest(state)))

    def test_csrf_still_differs_per_session_when_switched_off(self):
        state = _disabled_state()
        first, second = state.issue_session(), state.issue_session()
        self.assertNotEqual(state.csrf(first), state.csrf(second))

    def test_sessions_are_still_signed_when_switched_off(self):
        state = _disabled_state()
        token = state.issue_session()
        self.assertTrue(state.valid_session(token))
        self.assertFalse(state.valid_session(token + "changed"))


class DashboardPasswordOffPageLoadTests(unittest.IsolatedAsyncioTestCase):
    """Loading the page with no password must still hand out a session.

    Without a login step there is nothing else to issue one, and the CSRF token
    is derived from it — so skipping this would give a dashboard that loads and
    then rejects every write with "refresh the page", which is worse than a
    password prompt because it looks broken.
    """

    async def test_the_page_issues_a_session_when_the_password_is_off(self):
        response = await index(_FakeRequest(_disabled_state()))
        token = response.cookies.get(COOKIE)
        self.assertIsNotNone(token.value if token else None)
        self.assertTrue(_disabled_state().valid_session(token.value))

    async def test_the_page_does_not_reissue_for_a_valid_session(self):
        state = _disabled_state()
        existing = state.issue_session()
        response = await index(_FakeRequest(state, existing))
        self.assertIsNone(response.cookies.get(COOKIE))

    async def test_the_page_issues_nothing_when_the_password_is_on(self):
        state = DashboardState.__new__(DashboardState)
        state.auth = SessionAuth("a long enough test password",
                                 b"a sufficiently long dashboard test secret")
        response = await index(_FakeRequest(state))
        self.assertIsNone(response.cookies.get(COOKIE))


class ServiceControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transport=patch('athena.web.WINDOWS_HOST',False)
        transport.start();self.addCleanup(transport.stop)
    """Restarting "everything" must also restart the dashboard that asks.

    athena-web cannot survive the request that restarts it, so the web half has
    to be detached and delayed. These tests pin that down: the voice half is
    awaited, and the web half is deliberately not.
    """

    async def test_a_plain_restart_only_touches_voice(self):
        calls = []

        async def fake_systemctl(*arguments, timeout=None):
            calls.append(list(arguments))
            return ""

        async def fake_status(service=VOICE_SERVICE):
            return "active"

        with patch("athena.web._systemctl", fake_systemctl), \
             patch("athena.web._service_status", fake_status):
            status, label = await _service_action("restart")

        self.assertEqual(calls, [["restart", VOICE_SERVICE]])
        self.assertEqual(status, "active")
        self.assertIn("running", label)

    async def test_restart_all_restarts_voice_then_detaches_the_dashboard(self):
        calls = []
        created = []

        async def fake_systemctl(*arguments, timeout=None):
            calls.append(list(arguments))
            return ""

        async def fake_status(service=VOICE_SERVICE):
            return "active"

        def fake_create_task(coroutine):
            created.append(coroutine)
            coroutine.close()
            return None

        with patch("athena.web._systemctl", fake_systemctl), \
             patch("athena.web._service_status", fake_status), \
             patch("athena.web.asyncio.create_task", fake_create_task):
            status, label = await _service_action("restart", everything=True)

        self.assertEqual(calls, [["restart", VOICE_SERVICE], ['restart', 'athena-feishu.service']])
        self.assertEqual(len(created), 1, "the web restart must be detached, not awaited")
        self.assertIn("restarting", label.lower())

    async def test_the_detached_web_restart_uses_no_block(self):
        calls = []
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def fake_exec(*arguments, **kwargs):
            calls.append(list(arguments))

            class Done:
                returncode = 0

            return Done()

        with patch("athena.web.asyncio.sleep", fake_sleep), \
             patch("athena.web.asyncio.create_subprocess_exec", fake_exec):
            await _schedule_web_restart()

        self.assertEqual(delays, [WEB_RESTART_DELAY])
        self.assertEqual(
            calls, [["sudo", "-n", "/usr/bin/systemctl", "restart", "--no-block", WEB_SERVICE]]
        )

    async def test_a_failing_systemctl_surfaces_its_stderr(self):
        class Failed:
            returncode = 1

            async def communicate(self):
                return b"", b"sudo: a password is required"

        async def fake_exec(*arguments, **kwargs):
            return Failed()

        with patch("athena.web.asyncio.create_subprocess_exec", fake_exec):
            with self.assertRaises(RuntimeError) as caught:
                await _systemctl("restart", VOICE_SERVICE)

        self.assertIn("password is required", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
