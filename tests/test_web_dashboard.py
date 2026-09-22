import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from athena.memory.database import MemoryDatabase, StoredTurn
from athena.prompts import packaged_prompt_path, read_prompt, write_prompt
from athena.web import DashboardState, _authenticated, _is_local, index
from athena.web_auth import COOKIE, SessionAuth, auth_disabled
from uuid import uuid4


class WebDashboardTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
