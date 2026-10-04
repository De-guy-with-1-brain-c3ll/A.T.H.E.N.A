import unittest
from unittest.mock import patch, AsyncMock
from athena.coordinator import VoiceCoordinator
from athena.prompts import interaction_examples, interaction_style, read_voice_prompt


class InteractionStyleTests(unittest.TestCase):
    def test_all_approved_scenarios_are_packaged_with_context(self):
        cases=interaction_examples()
        self.assertEqual([case['id'] for case in cases],list(range(1,31)))
        for case in cases:
            self.assertTrue(case['context'])
            self.assertTrue(case['user'])
            self.assertIn('expected',case)
        self.assertEqual(sum(case['humour'] for case in cases),2)

    def test_reference_facts_are_not_in_runtime_style(self):
        style=interaction_style()
        self.assertLess(len(style),1800)
        self.assertIn('Most replies contain no joke',style)
        self.assertIn('not memories',style)
        for fact in ['Shenzhen', '28 C', '4.9', '2.9 MiB', 'typhoon']:
            self.assertNotIn(fact,style)

    def test_custom_system_prompt_is_preserved(self):
        with patch('athena.prompts.read_prompt',return_value='My custom policy'):
            self.assertEqual(read_voice_prompt(),'My custom policy')


class InteractionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_returns_to_wake_detection_without_shutdown(self):
        coordinator=VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator._active_until=100.0
        coordinator._speak_text=AsyncMock()
        self.assertTrue(await coordinator._handle_quiet_request("That's enough for now."))
        self.assertEqual(coordinator._active_until,0.0)
        coordinator._speak_text.assert_awaited_once_with('Very well.')

    async def test_ordinary_text_does_not_close_window(self):
        coordinator=VoiceCoordinator.__new__(VoiceCoordinator)
        coordinator._active_until=100.0
        coordinator._speak_text=AsyncMock()
        self.assertFalse(await coordinator._handle_quiet_request('Explain what standby means'))
        self.assertEqual(coordinator._active_until,100.0)
        coordinator._speak_text.assert_not_awaited()
