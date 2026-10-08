import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from athena.llm.deepseek import DeepSeekLanguageModel
from athena.settings.store import RuntimeSettingsStore
from athena.tools.models import ToolDefinition, ToolResult
from athena.tools.registry import ToolRegistry
from tests.test_search_refinement import Stream, chunk, call


class ModelToolFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_vpn_request_calls_installed_control(self):
        _, requests, tools = await self.run_case('Athena, stop the VPN.',
            [call('manage_vpn', {'action': 'stop'}, 'vpn'), Stream([chunk('The VPN is stopped.')])], ['manage_vpn'])
        self.assertEqual(requests[0]['tool_choice']['function']['name'], 'manage_vpn')
        tools['manage_vpn'].execute.assert_awaited_once_with({'action': 'stop'})

    async def test_music_requests_call_playback_without_search_or_permission(self):
        for text, name in [('Play Back in Black by AC/DC.', 'youtube_audio'),
                           ('Play on YouTube.', 'youtube_audio'),
                           ("Play on Nettie's music.", 'netease_music')]:
            with self.subTest(text=text):
                _, requests, tools = await self.run_case(text,
                    [call(name, {'action': 'play', 'query': 'Back in Black AC/DC'}, 'music'),
                     Stream([chunk('Playback started.')])], ['youtube_audio', 'netease_music'])
                self.assertEqual(requests[0]['tool_choice']['function']['name'], name)
                tools[name].execute.assert_awaited_once()

    async def test_create_and_transfer_keeps_coding_schema_and_repairs_missing_write(self):
        class WaitForUpload(Stream):
            async def __aiter__(self):
                await asyncio.sleep(.05)
                async for item in super().__aiter__(): yield item
        def configure(registry, tools):
            upload = tools['upload_to_pc']
            upload.prepare = AsyncMock(return_value=({}, 'Prepared'))
            upload.execute.return_value = ToolResult(True, 'PC verified the file.')
            upload.notify = None
        _, requests, tools = await self.run_case(
            'Create a program that outputs hello world and transfer it to my computer.',
            [Stream([chunk('Save this code yourself.')]),
             call('coding_workspace', {'action': 'write'}, 'write'),
             call('upload_to_pc', {}, 'send'), WaitForUpload([chunk('Transfer started.')])],
            ['coding_workspace', 'upload_to_pc', 'download_file'], configure=configure)
        self.assertIn('coding_workspace', {t['function']['name'] for t in requests[0]['tools']})
        self.assertEqual(requests[0]['tool_choice']['function']['name'], 'coding_workspace')
        tools['coding_workspace'].execute.assert_awaited_once()
        tools['upload_to_pc'].execute.assert_awaited_once()
        self.assertNotIn('existing saved file', requests[1]['messages'][-1]['content'])

    async def test_homework_calls_real_assignments_on_first_request(self):
        _, requests, tools = await self.run_case('What physics homework do I have?',
            [call('teams_assignments', {'class_name': 'physics'}, 'hw'),
             Stream([chunk('Your physics lab is due Friday.')])], ['teams_assignments'])
        self.assertEqual(requests[0]['tool_choice']['function']['name'], 'teams_assignments')
        tools['teams_assignments'].execute.assert_awaited_once()
    async def test_download_status_uses_live_hub_state_without_model(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            registry._last_download = {'status': 'complete',
                'arguments': {'filename': 'README.rst'}}
            model = DeepSeekLanguageModel('test', 'test', registry,
                RuntimeSettingsStore(Path(directory) / 'settings.json'))
            model._client.chat.completions.create = AsyncMock()
            try:
                reply = ''.join([part async for part in model.stream_reply(
                    uuid4(), 'What is the status of the README.rst download?')])
                self.assertEqual(reply, 'README.rst downloaded successfully.')
                model._client.chat.completions.create.assert_not_awaited()
            finally:
                await model.close()

    async def test_existing_file_transfer_does_not_offer_another_download(self):
        def configure(_registry, tools):
            upload = tools['upload_to_pc']
            upload.prepare = AsyncMock(return_value=({}, 'Prepared'))
            upload.execute.return_value = ToolResult(True, 'PC verified the file.')
            upload.notify = None
        class WaitForUpload(Stream):
            async def __aiter__(self):
                await asyncio.sleep(.05)
                async for item in super().__aiter__():
                    yield item
        _, requests, _ = await self.run_case(
            'Send the README.rst file I just downloaded to my PC inbox.',
            [call('upload_to_pc', {}, 'send'), WaitForUpload([chunk('I need the saved file path.')])],
            ['upload_to_pc', 'download_file', 'pc_transfer_status'], configure=configure)
        offered = {item['function']['name']: item for item in requests[0]['tools']}
        self.assertIn('upload_to_pc', offered)
        self.assertNotIn('download_file', offered)
        selector = offered['select_tools']['function']['parameters']['properties']['names']['items']['enum']
        self.assertNotIn('download_file', selector)

    async def test_weather_schema_is_available_on_first_request(self):
        reply, requests, tools = await self.run_case('What is the weather in Shenzhen?', [
            call('get_weather', {}, 'weather'), Stream([chunk('It is sunny in Shenzhen.')])],
            ['get_weather'])
        self.assertIn('get_weather', {item['function']['name'] for item in requests[0]['tools']})
        tools['get_weather'].execute.assert_awaited_once()

    async def run_case(self, text, streams, names, results=None, configure=None):
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry()
            tools = {}
            for name in names:
                tools[name] = NS(definition=ToolDefinition(name, name, {'type': 'object', 'properties': {}}),
                    execute=AsyncMock(return_value=(results or {}).get(name, ToolResult(True, 'Verified result'))))
                registry.register(tools[name])
            if configure:
                configure(registry, tools)
            model = DeepSeekLanguageModel('test', 'test', registry, RuntimeSettingsStore(Path(directory)/'settings.json'))
            model._client.chat.completions.create = AsyncMock(side_effect=streams)
            try:
                with patch.object(registry, 'handle_user_command', AsyncMock(side_effect=AssertionError('Direct routing used'))), \
                     patch.object(model, '_tool_names_for', side_effect=AssertionError('Keyword filter used')):
                    reply = ''.join([part async for part in model.stream_reply(uuid4(), text)])
                requests = [c.kwargs for c in model._client.chat.completions.create.await_args_list]
                return reply, requests, tools
            finally:
                await model.close()

    async def test_model_selects_tool_without_keyword_filter_or_direct_execution(self):
        reply, requests, tools = await self.run_case('Will I need an umbrella in Shenzhen?', [
            call('get_weather', {}, 'weather'), Stream([chunk('Yes, rain is expected.')])], ['get_weather', 'netease_music'])
        self.assertIn('rain', reply)
        offered = {t['function']['name'] for t in requests[0]['tools']}
        self.assertEqual(offered, {'select_tools', 'check_tool_status', 'get_weather'})
        self.assertIn('netease_music', requests[0]['messages'][2]['content'])
        self.assertEqual(requests[0]['tool_choice']['function']['name'], 'get_weather')
        tools['get_weather'].execute.assert_awaited_once()
        tools['netease_music'].execute.assert_not_awaited()

    async def test_verified_weather_receipt_survives_a_false_model_failure(self):
        reply, _, tools = await self.run_case('What is the weather in Shenzhen?', [
            call('get_weather', {}, 'weather'),
            Stream([chunk("I can't verify that; the weather tool didn't go through.")])],
            ['get_weather'], {'get_weather': ToolResult(True, 'It is 22 degrees in Shenzhen.')})
        self.assertEqual(reply, 'It is 22 degrees in Shenzhen.')
        tools['get_weather'].execute.assert_awaited_once()

    async def test_clock_value_cannot_be_rewritten_by_model(self):
        reply, _, tools = await self.run_case('What time is it?', [
            call('get_local_time', {}, 'clock'), Stream([chunk('It is midnight in New York.')])], ['get_local_time'],
            {'get_local_time': ToolResult(True, 'It is 3:42 PM on Monday, October 5.', {'timezone': 'Asia/Shanghai'})})
        self.assertEqual(reply, 'It is 3:42 PM on Monday, October 5.')
        tools['get_local_time'].execute.assert_awaited_once()

    async def test_plain_yes_is_conversational_when_no_grant_exists(self):
        reply, requests, _ = await self.run_case('yes', [Stream([chunk('Continuing your request.')])], ['get_weather'])
        self.assertEqual(reply, 'Continuing your request.')
        self.assertEqual(len(requests), 1)

    async def test_unsupported_clock_prose_is_repaired_into_measurement(self):
        reply, _, tools = await self.run_case('How late is it?', [
            Stream([chunk('It is 11:00 PM.')]), call('get_local_time', {}, 'clock'),
            Stream([chunk('It is 11:00 PM.')])], ['get_local_time'],
            {'get_local_time': ToolResult(True, 'It is 3:42 PM on Monday, October 5.')})
        self.assertEqual(reply, 'It is 3:42 PM on Monday, October 5.')
        tools['get_local_time'].execute.assert_awaited_once()

    async def test_background_submission_is_not_completion(self):
        reply, _, _ = await self.run_case('Send the file', [
            call('upload_to_pc', {}, 'send'), Stream([chunk("I've sent the file.")]),
            Stream([chunk("I've sent the file.")])], ['upload_to_pc'],
            {'upload_to_pc': ToolResult(True, 'Transfer started.', {'background_started': True})})
        self.assertIn('verified completion', reply)

    async def test_completed_upload_reports_receipt_instead_of_unrelated_download_state(self):
        receipt = ToolResult(True, 'Sent probe.txt to your PC inbox. 32 bytes transferred, 100% complete.')
        def configure(_registry, tools):
            upload = tools['upload_to_pc']
            upload.prepare = AsyncMock(return_value=({}, 'Prepared'))
            upload.execute.return_value = receipt
            upload.notify = None
        class WaitForUpload(Stream):
            async def __aiter__(self):
                await asyncio.sleep(.05)
                async for item in super().__aiter__():
                    yield item
        reply, _, tools = await self.run_case('Create and send a file to my PC', [
            call('upload_to_pc', {}, 'send'),
            WaitForUpload([chunk('I have no download recorded in this session.')])],
            ['upload_to_pc'], configure=configure)
        self.assertIn('32 bytes transferred, 100% complete', reply)
        self.assertNotIn('no download recorded', reply)
        tools['upload_to_pc'].execute.assert_awaited_once()

    async def test_clock_defaults_to_user_timezone_not_execution_host(self):
        from athena.tools.clock import ClockTool
        with patch.dict('os.environ', {'ATHENA_TIMEZONE': 'Asia/Shanghai'}):
            result = await ClockTool().execute({})
        self.assertTrue(result.success)
        self.assertEqual(result.data['timezone'], 'Asia/Shanghai')
        self.assertTrue(result.data['iso'].endswith('+08:00'))

    async def test_selecting_unknown_tool_does_not_execute_it(self):
        reply, requests, tools = await self.run_case('Check something', [
            call('select_tools', {'names': ['imaginary_tool']}, 'select'),
            Stream([chunk('That tool is unavailable.')])], ['get_weather'])
        tools['get_weather'].execute.assert_not_awaited()
        messages = [m for m in requests[-1]['messages'] if m['role'] == 'tool']
        self.assertIn('available tool names', messages[-1]['content'])

    async def test_discovery_is_not_execution_and_does_not_justify_another_permission_question(self):
        reply, _, tools = await self.run_case('Will it rain?', [
            call('select_tools', {'names': ['get_weather']}, 'select'),
            Stream([chunk('Would you like me to check the weather?')]),
            call('get_weather', {}, 'weather'), Stream([chunk('Rain is expected.')])], ['get_weather'])
        self.assertNotIn('Would you like', reply)
        tools['get_weather'].execute.assert_awaited_once()

    async def test_unverified_completion_is_withheld(self):
        reply, _, tools = await self.run_case('Create a file', [
            Stream([chunk("I've created the file.")]), Stream([chunk("I've created the file.")])], ['coding_workspace'])
        self.assertIn('verified completion', reply)
        self.assertNotIn("I've created", reply)
        tools['coding_workspace'].execute.assert_not_awaited()

    async def test_empty_action_promise_is_repaired_before_being_spoken(self):
        reply, _, tools = await self.run_case('Open Google on my PC', [
            Stream([chunk("I'll open Google for you.")]),
            call('pc_browser', {}, 'open'), Stream([chunk('Google opened successfully.')])], ['pc_browser'])
        self.assertNotIn("I'll open", reply)
        tools['pc_browser'].execute.assert_awaited_once()

    async def test_failed_tool_cannot_support_completed_claim(self):
        reply, _, _ = await self.run_case('Open the page', [
            call('pc_browser', {}, 'open'), Stream([chunk("I've opened the page.")]),
            Stream([chunk("I've opened the page.")])], ['pc_browser'],
            {'pc_browser': ToolResult(False, 'The PC cannot be reached.')})
        self.assertIn('verified completion', reply)


if __name__ == '__main__': unittest.main()
