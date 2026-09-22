"""Long-term memory must describe Benjamin, not the session."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from athena.coordinator import VoiceCoordinator
from athena.memory.apply import apply_result, prune
from athena.memory.database import MemoryDatabase
from athena.memory.quality import MAX_FACTS, MAX_FACT_CHARS, is_durable, rejection_reason


# The keys that were actually sitting in the live database.
REAL_JUNK = [
    ("voice_output_status", "Working; Benjamin can hear ATHENA speak"),
    ("music_playback_status", "Working; music is playing successfully"),
    ("current_date_confirmed", "September 16, 2026, per Benjamin"),
    ("current_task", "Pulling the channel list for the Chinese team"),
    ("timer_function_works", "Timer setting and expiry confirmation tested and working"),
    ("teams_channel_retry_pending", "ATHENA asked whether to retry; he has not answered"),
    ("alarm_persistence_issue", "Resolved: a 5-minute alarm fired correctly"),
    ("web_search_verified", "Web search functionality tested and working"),
    ("upcoming_assignments", "FRQ Unit 2 on December 2, Unit 3 review on December 4"),
    ("rest_timer_ten_minutes", "Benjamin set a ten-minute rest timer after his seminar"),
    ("study_session_not_nap", "Benjamin's rest period is a study session, not a nap"),
]

# Facts that are genuinely about him and must survive.
REAL_FACTS = [
    ("location", "Shenzhen"),
    ("music_taste", "Enjoys rock: Nirvana, AC/DC, Guns N' Roses"),
    ("communication_tone", "Prefers playful tone but not sarcasm for serious questions"),
    ("hobby_drones", "Interested in drones and FPV, including flight controllers"),
    ("humor_style", "Uses dry humour and expects ATHENA to recognise jokes"),
    ("athena_tts_voice", "Uses the Dolce TTS voice, not the default Neil"),
]


class FactGateTests(unittest.TestCase):
    def test_session_state_is_refused(self):
        for key, value in REAL_JUNK:
            with self.subTest(key=key):
                self.assertFalse(is_durable(key, value),
                                 f"{key} would have been stored as a durable fact")
                self.assertTrue(rejection_reason(key, value))

    def test_real_facts_survive(self):
        for key, value in REAL_FACTS:
            with self.subTest(key=key):
                self.assertTrue(is_durable(key, value),
                                f"{key} was wrongly rejected: {rejection_reason(key, value)}")

    def test_a_conversation_note_is_refused(self):
        essay = ("Third stance: commercial cause = Mongol securing of Silk Road routes; "
                 "political cause = dynastic ambition and Chinggisid legitimacy; "
                 "source limitation = most accounts from merchants")
        self.assertFalse(is_durable("debate_stances_third_topic", essay))
        self.assertIn("note about a conversation", rejection_reason("debate_stances_third_topic", essay))

    def test_a_secret_is_never_durable(self):
        self.assertTrue(rejection_reason("api_key", "sk-abcdefghijklmnop"))
        self.assertTrue(rejection_reason("note", "my password is hunter2"))

    def test_a_date_key_is_refused(self):
        self.assertTrue(rejection_reason("2026-09-16", "something happened"))

    def test_short_honest_facts_are_not_over_rejected(self):
        for key, value in [("works_at", "A school in Shenzhen"),
                           ("speaks", "Mandarin and English"),
                           ("birthday_month", "March")]:
            with self.subTest(key=key):
                self.assertTrue(is_durable(key, value), rejection_reason(key, value))


class ApplyResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = MemoryDatabase(Path(self.directory.name) / "athena.db")
        self.database.initialize()

    async def test_a_junk_fact_never_reaches_the_table(self):
        applied = await apply_result(self.database, {
            "summary": "A day.",
            "facts": [
                {"key": "voice_output_status", "value": "Working", "confidence": 0.9},
                {"key": "location", "value": "Shenzhen", "confidence": 0.9},
            ],
            "forget_keys": [],
        }, uuid4())
        self.assertEqual(applied.written, 1)
        self.assertEqual([k for k, _, _ in self.database.facts()], ["location"])

    async def test_the_table_is_capped(self):
        for index in range(MAX_FACTS + 12):
            self.database.upsert_fact(f"fact_{index:03d}", f"value {index}", 0.9, None)
        await apply_result(self.database, {
            "summary": "s", "facts": [], "forget_keys": [],
        }, uuid4())
        self.assertLessEqual(self.database.fact_count(), MAX_FACTS)

    async def test_a_long_note_is_refused(self):
        applied = await apply_result(self.database, {
            "summary": "s",
            "facts": [{"key": "some_topic", "value": "x" * (MAX_FACT_CHARS + 1),
                       "confidence": 0.9}],
            "forget_keys": [],
        }, uuid4())
        self.assertEqual(applied.written, 0)

    async def test_existing_junk_is_cleared_on_the_next_pass(self):
        self.database.upsert_fact("voice_output_status", "Working", 0.9, None)
        self.database.upsert_fact("location", "Shenzhen", 0.9, None)
        applied = await apply_result(self.database, {
            "summary": "s", "facts": [], "forget_keys": [],
        }, uuid4())
        self.assertEqual([k for k, _, _ in self.database.facts()], ["location"])
        self.assertIn("voice_output_status", applied.rejected)

    async def test_a_dry_run_changes_nothing(self):
        self.database.upsert_fact("voice_output_status", "Working", 0.9, None)
        await apply_result(self.database, {"summary": "s", "facts": [], "forget_keys": []},
                           uuid4(), dry_run=True)
        self.assertEqual(self.database.fact_count(), 1)

    async def test_prune_removes_only_the_junk(self):
        for key, value in REAL_JUNK:
            self.database.upsert_fact(key, value, 0.9, None)
        for key, value in REAL_FACTS:
            self.database.upsert_fact(key, value, 0.9, None)
        removed = await prune(self.database)
        self.assertEqual(removed, len(REAL_JUNK))
        self.assertEqual(sorted(k for k, _, _ in self.database.facts()),
                         sorted(k for k, _ in REAL_FACTS))

    async def test_prune_can_be_dry_run(self):
        self.database.upsert_fact("voice_output_status", "Working", 0.9, None)
        self.assertEqual(await prune(self.database, dry_run=True), 1)
        self.assertEqual(self.database.fact_count(), 1)


class SleepCommandTests(unittest.TestCase):
    def test_the_ways_he_would_say_it(self):
        for phrase in ("go to sleep", "sleep mode", "sleep on it", "remember today",
                       "consolidate memory", "go to sleep please"):
            with self.subTest(phrase=phrase):
                self.assertTrue(VoiceCoordinator.is_sleep_command(phrase))

    def test_ordinary_requests_are_not_sleep_commands(self):
        for phrase in ("set a timer", "what is due", "shut down", "stop athena",
                       "read the channel"):
            with self.subTest(phrase=phrase):
                self.assertFalse(VoiceCoordinator.is_sleep_command(phrase))

    def test_shutting_down_is_not_sleeping(self):
        """Sleep must not be mistaken for powering off, or ATHENA would stop."""
        from athena.tools.registry import ToolRegistry
        self.assertFalse(ToolRegistry.is_shutdown_command("go to sleep"))
        self.assertTrue(ToolRegistry.is_shutdown_command("shut down"))
