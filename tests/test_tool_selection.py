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


class WebSearchOnItsOwnTests(unittest.TestCase):
    """ATHENA has to look things up without being told to.

    Tools were chosen by matching topic nouns — "web", "news", "latest" — so a
    plain question reached the model with no way to check anything. It either
    refused or answered from memory that might be years out of date, and the
    only way to make it search was to say "look it up" yourself. Needing to be
    told is the failure being fixed here.
    """

    def setUp(self):
        self.model = model_with(TEAMS)

    def _selected(self, text):
        return self.model._tool_names_for(text)

    def test_a_question_with_no_web_word_still_gets_the_web(self):
        for question in ("who won the world cup in 2022",
                         "how tall is mount fuji",
                         "what is the capital of mongolia",
                         "when was the iphone 17 released",
                         "who is the prime minister of japan",
                         "how far is mars from earth",
                         "why is the sky blue",
                         "is mount everest still growing",
                         "who wrote hamlet"):
            with self.subTest(question=question):
                self.assertIn("search_web", self._selected(question),
                              f"{question!r} left the model unable to check")

    def test_a_question_with_no_verb_at_all_still_gets_the_web(self):
        # Spoken questions often arrive as bare statements: no question word, no
        # verb, no question mark. These used to select nothing whatsoever.
        for question in ("distance to mars", "the iphone 17 release date"):
            with self.subTest(question=question):
                self.assertIn("search_web", self._selected(question),
                              f"{question!r} read as a command, not a question")

    def test_asking_for_research_explicitly_still_works(self):
        for phrase in ("look it up", "google that", "find out for me",
                       "search for it", "research that", "check that online"):
            with self.subTest(phrase=phrase):
                self.assertIn("search_web", self._selected(phrase))

    def test_what_athena_can_answer_itself_does_not_go_to_the_web(self):
        # Searching for the time or the weather is not just wasted, it is slower
        # and less accurate than reading the clock that is already injected.
        for local in ("what time is it", "what is the weather"):
            with self.subTest(phrase=local):
                self.assertNotIn("search_web", self._selected(local))

    def test_its_own_commands_are_not_read_as_questions(self):
        for command in ("set an alarm for 7", "play some music", "go to sleep",
                        "tell me a joke", "stop", "cancel that"):
            with self.subTest(command=command):
                self.assertNotIn("search_web", self._selected(command),
                                 f"{command!r} was mistaken for a question")

    def test_chit_chat_is_not_read_as_a_question(self):
        # "Hello" is a short utterance with no verb, which is exactly the shape
        # of "distance to mars". The difference is that a greeting names nothing.
        for greeting in ("hello", "hi", "hey", "good morning", "how are you",
                         "thanks", "never mind", "nothing", "okay"):
            with self.subTest(greeting=greeting):
                self.assertNotIn("search_web", self._selected(greeting),
                                 f"{greeting!r} was mistaken for a question")

    def test_a_question_about_itself_is_not_a_question_about_the_world(self):
        # "What tools do you have" is answered from a built-in list without any
        # model call, so handing it a web search would be slower and worse.
        for question in ("what tools do you have", "what can you do",
                         "who are you", "what are your abilities"):
            with self.subTest(question=question):
                self.assertNotIn("search_web", self._selected(question))

    def test_offering_the_web_never_cancels_the_previous_turn(self):
        # The web tools ride along with whatever the turn is about. If they
        # became a *topic*, a bare "yes" would look like a new subject and drop
        # the channel reader the follow-up still needed.
        self._selected("pull all the cjs")
        for follow_up in ("yes", "pull them", "are you pulling them", "that"):
            with self.subTest(follow_up=follow_up):
                self.assertIn("teams_channel_posts", self._selected(follow_up),
                              f"{follow_up!r} lost the previous turn's tools")

    def test_the_web_tools_are_not_carried_into_the_next_turn(self):
        # They are a capability for the turn that needs them, not a topic, so
        # they must not linger and make every later turn look web-shaped.
        self._selected("who won the world cup in 2022")
        self.assertEqual(self._selected("set an alarm for 7"), {"set_alarm"})
