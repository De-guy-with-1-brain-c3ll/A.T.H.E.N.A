import json
import unittest
from unittest.mock import MagicMock, patch
from athena.desktop import App

class DesktopInspectionTests(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app.api = MagicMock()
        app.history_offset = 40
        app.history_log = MagicMock()
        app.older_history = MagicMock()
        return app

    def test_history_pages_do_not_overwrite_chat(self):
        app = self.make_app()
        app.load_history(True)
        self.assertEqual(app.api.call_args.args[0], '/api/conversations?offset=40')
        app.api.call_args.kwargs['callback']({'conversations':[{'user':'older','assistant':'answer'}]})
        app.history_log.delete.assert_not_called()
        self.assertEqual(app.history_offset, 41)

    def test_raw_evidence_is_not_rewritten(self):
        app = self.make_app()
        app.evidence_picker = MagicMock()
        app.evidence_picker.current.return_value = 0
        app.evidence_log = MagicMock()
        app.evidence_rows = [{'data':{'text':'<script>source text</script>'},'request':{'query':'race'}}]
        app.replace = MagicMock()
        app.show_evidence()
        self.assertEqual(json.loads(app.replace.call_args.args[1]), app.evidence_rows[0])

    def test_clear_requires_confirmation_and_uses_shared_endpoint(self):
        app = self.make_app()
        with patch('athena.desktop.messagebox.askyesno', return_value=False):
            app.clear_context()
        app.api.assert_not_called()
        with patch('athena.desktop.messagebox.askyesno', return_value=True):
            app.clear_context()
        self.assertEqual(app.api.call_args.args[:2], ('/api/context/clear', {}))
