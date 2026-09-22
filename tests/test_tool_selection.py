"""Tool selection: the model must have the tool before it can use it.

The assistant kept telling Benjamin it had no Teams tool, which was true and
useless: tools were chosen by matching literal keywords in the transcript, so
"pull them", "go get the rest" and "do some other cjs" selected nothing at all.
"""
import unittest
from unittest.mock import MagicMock

from athena.llm.deepseek import DeepSeekLanguageModel


def model_with(tools):
    model = DeepSeekLanguageModel.__new__(DeepSeekLanguageModel)
    registry = MagicMock()
    registry.names.return_value = list(tools)
    model._tools = registry
    model._last_tools = set()
    return model


TEAMS = {"teams_channel_posts", "teams_channels", "teams_assignments", "set_alarm",
         "get_weather", "search_web"}
SLEEP = {"sleep_mode", "sleep_status", "get_weather", "teams_channel_posts"}


class TeamsToolSelectionTests(unittest.TestCase):
    def setUp(self):
        self.model = model_with(TEAMS)

    def _selected(self, text):
        return self.model._tool_names_for(text)

    def test_the_phrases_he_actually_used(self):
        for phrase in ("pull all the CJs", "the CJ channels", "communication journal",
                       "what is due", "check my channels", "the briefing"):
            with self.subTest(phrase=phrase):
                self.assertIn("teams_channel_posts", self._selected(phrase),
                              f"{phrase!r} would leave the model with no channel reader")

    def test_a_keyword_free_follow_up_inherits_the_previous_turn(self):
        # "pull all the CJs" selects the Teams tools...
        self._selected("pull all the cjs")
        # ...so the follow-up still has them, instead of nothing.
        for follow_up in ("pull them", "go get the rest", "do some other cjs",
                          "are you pulling them", "yes"):
            with self.subTest(phrase=follow_up):
                self.assertIn("teams_channel_posts", self._selected(follow_up),
                              f"{follow_up!r} left the model with no tools")

    def test_a_new_topic_replaces_the_previous_turn(self):
        self._selected("pull all the cjs")
        self.assertEqual(self._selected("what is the weather"), {"get_weather"})

    def test_an_unrelated_question_still_gets_nothing_extra(self):
        self.assertEqual(self._selected("tell me a joke"), set())

    def test_an_explicit_retry_still_offers_everything(self):
        self.assertEqual(self._selected("try again"), TEAMS)

    def test_a_tool_that_does_not_exist_is_never_offered(self):
        model = model_with({"get_weather"})
        self.assertEqual(model._tool_names_for("pull the cjs"), set())


class SleepToolSelectionTests(unittest.TestCase):
    """The model can only use the sleep tools if it is handed them.

    "Did you consolidate today" reached the model with no tools at all, so it
    answered about its own memory from the conversation — which is exactly the
    thing it cannot know.
    """

    def setUp(self):
        self.model = model_with(SLEEP)

    def _selected(self, text):
        return self.model._tool_names_for(text)

    def test_the_phrases_he_uses_for_sleep_mode(self):
        for phrase in ("go to sleep", "sleep on it", "consolidate your memory",
                       "remember today", "sleep"):
            with self.subTest(phrase=phrase):
                self.assertIn("sleep_mode", self._selected(phrase))

    def test_the_phrases_he_uses_to_check_on_it(self):
        for phrase in ("when did you last save your memory", "how did consolidation go",
                       "did you consolidate today", "is your memory up to date",
                       "consolidation status"):
            with self.subTest(phrase=phrase):
                self.assertIn("sleep_status", self._selected(phrase),
                              f"{phrase!r} would leave the model guessing")

    def test_both_sleep_tools_are_offered_together(self):
        # Asking to start a pass and asking how it went are one topic; splitting
        # them makes the model pick the wrong half.
        self.assertEqual(self._selected("go to sleep"),
                         self._selected("did you consolidate"))

    def test_an_unrelated_question_still_gets_nothing_sleep_related(self):
        self.assertEqual(self._selected("what is the weather"), {"get_weather"})


class CarryOverIsolationTests(unittest.TestCase):
    def test_a_fork_does_not_inherit_the_parent_carry_over(self):
        model = model_with(TEAMS)
        model._selected = None
        model._tool_names_for("pull all the cjs")
        self.assertTrue(model._last_tools)
        fork = model.fork()
        self.assertEqual(fork._last_tools, set())
