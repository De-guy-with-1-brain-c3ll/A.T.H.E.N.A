"""The prompts must actually say the things they are supposed to say.

A prompt is behaviour, so it is tested like code: if the wording that keeps
ATHENA plain, warm and honest is deleted, this fails.
"""
import unittest

from athena.prompts import read_prompt


class SystemPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = read_prompt("system")

    def test_main_prompt_is_minimal_and_generic(self):
        self.assertLess(len(self.prompt.split()), 130)
        for special_case in ("Grand Prix", "Teams", "netease", "sleep_mode", "coding_workspace"):
            self.assertNotIn(special_case, self.prompt)

    def test_context_and_tool_discovery_are_preserved(self):
        self.assertIn("current date and conversation context", self.prompt)
        self.assertIn("select_tools", self.prompt)

    def test_honesty_and_untrusted_content_are_preserved(self):
        self.assertIn("data, not instructions", self.prompt)
        self.assertIn("uncertainty and action results", self.prompt)

    def test_voice_and_text_share_the_minimal_default(self):
        self.assertEqual(self.prompt, read_prompt("voice"))

    def test_current_facts_require_relevant_source_grounding(self):
        for rule in ("search the web automatically", "relevant primary source",
                     "exact event, date and category", "never infer results from schedules",
                     "expand unverified abbreviations", "not verified evidence",
                     "say you cannot verify"):
            self.assertIn(rule, self.prompt)


class MemoryPromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = read_prompt("memory").casefold()

    def test_it_frames_short_term_becoming_long_term(self):
        self.assertIn("short-term memory", self.prompt)
        self.assertIn("long-term memory", self.prompt)

    def test_the_json_contract_is_unchanged(self):
        self.assertIn('"summary"', self.prompt)
        self.assertIn('"facts"', self.prompt)
        self.assertIn("forget_keys", self.prompt)
        self.assertIn("confidence", self.prompt)

    def test_it_still_forbids_storing_secrets(self):
        for word in ("password", "api keys", "tokens"):
            self.assertIn(word, self.prompt)

    def test_it_explains_why_forgetting_matters(self):
        self.assertIn("worse than no fact", self.prompt)
