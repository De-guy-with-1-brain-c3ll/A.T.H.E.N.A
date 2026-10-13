import asyncio
import importlib.util
import io
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from athena.core_client import SpokenBudget, spoken_update


spec = importlib.util.spec_from_file_location('tested_console',
    Path(__file__).resolve().parents[1] / 'packages/athena-console/athena_console.py')
console = importlib.util.module_from_spec(spec)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packages/athena-console'))
spec.loader.exec_module(console)
import athena_dashboard as dashboard


class ConsoleTests(unittest.TestCase):
    def test_dashboard_renders_live_transcript_tasks_and_conversation_without_terminal_injection(self):
        frame=dashboard.render({'core':'online','gateway':True,'voice':{'state':'speaking','heard':'create a folder'},
            'tasks':[{'id':'abc123','state':'running','objective':'Create folder'}],
            'conversation':[{'role':'user','text':'hello\033[2J','mode':'voice'}],
            'active_turns':[{'text':'On it.','state':'responding'}]},draft='typed input',width=80,height=30)
        self.assertIn('create a folder',frame)
        self.assertIn('running',frame)
        self.assertIn('typed input',frame)
        self.assertIn('SPEAKING',frame)
        self.assertIn('\033[96m',frame)
        self.assertNotIn('hello\033[2J',frame)
    def test_separate_ask_invocations_share_folder_session_and_accept_unquoted_text(self):
        with patch.object(console,'ask') as ask:
            self.assertEqual(console.main(['ask','create','a','folder']),0)
            self.assertEqual(console.main(['ask','name it test']),0)
        self.assertEqual(ask.call_args_list[0].args[1],ask.call_args_list[1].args[1])
        self.assertEqual(ask.call_args_list[0].args[0],'create a folder')

    def test_interactive_buttons_and_approval_execute_without_model_interpretation(self):
        with patch('builtins.input',side_effect=['hello','/approve abc123def456','4','0']), \
                patch.object(console,'ask') as ask, patch.object(console,'request',return_value={'accepted':True}) as request, \
                redirect_stdout(io.StringIO()) as output:
            console.main([])
        ask.assert_called_once()
        request.assert_called_once_with('/v1/tasks/abc123def456',{'action':'approve'})
        self.assertIn('[1] Tasks',output.getvalue())
        self.assertIn('/approve ID',output.getvalue())

    def test_shell_slash_approval_alias_is_supported(self):
        with patch.object(console,'request',return_value={'accepted':True}) as request, redirect_stdout(io.StringIO()):
            self.assertEqual(console.main(['/approve','abc123def456']),0)
        request.assert_called_once_with('/v1/tasks/abc123def456',{'action':'approve'})


class VoiceStyleTests(unittest.TestCase):
    def test_fragmented_words_count_once_and_long_reply_is_bounded(self):
        budget=SpokenBudget(limit=3)
        parts=[budget.feed(p) for p in ('He','llo, ','this ','works ','extra ','extra again')]
        result=''.join(parts)
        self.assertTrue(result.startswith('Hello, this works'))
        self.assertNotIn('extra',result)
        self.assertEqual(result.count('The details are in the console.'),1)

    def test_notifications_never_speak_task_ids_or_raw_reports(self):
        message=spoken_update({'type':'terminal','task_id':'abc123def456',
            'data':{'state':'verified','summary':'# Detailed report\n```python\nprint(secret)\n```'}})
        self.assertNotIn('abc123def456',message)
        self.assertNotIn('print',message)
        self.assertLess(len(message.split()),15)
