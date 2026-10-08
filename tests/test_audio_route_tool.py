import unittest
from unittest.mock import AsyncMock, patch

from athena.tools.audio_route import AudioRouteTool


class AudioRouteToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_switch_uses_coordinator_without_self_ipc(self):
        tool = AudioRouteTool()
        coordinator = AsyncMock()
        coordinator._switch_audio.return_value = {'target': 'computer', 'computer_ready': True}
        tool.coordinator = coordinator
        with patch('athena.tools.audio_route.request_audio_route',
                   side_effect=AssertionError('self IPC would deadlock')):
            result = await tool.execute({'target': 'computer'})
        self.assertTrue(result.success)
        coordinator._switch_audio.assert_awaited_once_with('computer')

    async def test_offline_computer_is_reported_as_failure(self):
        tool = AudioRouteTool()
        coordinator = AsyncMock()
        coordinator._switch_audio.side_effect = RuntimeError('Computer audio is not connected.')
        tool.coordinator = coordinator
        result = await tool.execute({'target': 'computer'})
        self.assertFalse(result.success)
        self.assertIn('not connected', result.spoken_text)
