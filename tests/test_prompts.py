"""The prompts must actually say the things they are supposed to say.

A prompt is behaviour, so it is tested like code: if the wording that makes
ATHENA speak in the JARVIS register — competent, courteous, dry — is deleted,
this fails.
"""
import unittest

from athena.prompts import read_prompt


class SystemPromptTests(unittest.TestCase):
    def setUp(self):
        # Prompts are hard-wrapped, so a phrase can span a line break. Compare
        # against collapsed whitespace or these tests fail on formatting.
        self.prompt = " ".join(read_prompt("system").casefold().split())

    def test_both_kinds_of_memory_are_explained(self):
        self.assertIn("short-term memory", self.prompt)
        self.assertIn("long-term memory", self.prompt)
        self.assertIn("outranks", self.prompt)

    def test_recent_conversation_is_said_to_beat_older_memory(self):
        self.assertIn("beats long-term memory", self.prompt)

    def test_low_confidence_facts_are_not_to_be_trusted(self):
        self.assertIn("confidence", self.prompt)
        self.assertIn("guess", self.prompt)

    def test_it_forbids_asking_permission_and_narrating_itself(self):
        self.assertIn("never ask permission", self.prompt)
        self.assertIn("never narrate your own process", self.prompt)

    def test_it_asks_for_the_jarvis_register(self):
        self.assertIn("jarvis", self.prompt)
        self.assertIn("sir", self.prompt)
        self.assertIn("warn once", self.prompt)
        self.assertIn("anticipate", self.prompt)
        self.assertIn("no flattery", self.prompt)
        self.assertIn("calm", self.prompt)
        self.assertIn("have a view", self.prompt)
        self.assertIn("disagree", self.prompt)

    def test_it_keeps_the_spoken_output_rules(self):
        for rule in ("spoken aloud", "no markdown", "bullets", "emoji"):
            self.assertIn(rule, self.prompt)

    def test_it_keeps_the_honesty_rules(self):
        self.assertIn("never claim to remember", self.prompt)
        self.assertIn("never invent a detail", self.prompt)

    def test_it_keeps_the_garbled_transcript_rule(self):
        self.assertIn("garbled", self.prompt)

    def test_it_caps_the_length_of_a_spoken_answer(self):
        """Speech is billed per 10,000 characters, so length is a cost."""
        self.assertIn("one or two sentences", self.prompt)
        self.assertIn("three is the ceiling", self.prompt)
        self.assertIn("costs money by the character", self.prompt)

    def test_it_forbids_padding(self):
        self.assertIn("never pad", self.prompt)
        self.assertIn("no preamble", self.prompt)

    def test_it_tells_athena_how_to_enter_sleep_mode(self):
        self.assertIn("sleep_mode", self.prompt)


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
