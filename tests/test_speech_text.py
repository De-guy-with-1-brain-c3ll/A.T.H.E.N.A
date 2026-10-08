import unittest
from athena.tts.text import speech_text

class SpeechTextTests(unittest.TestCase):
    def test_formatting_and_separators_are_not_spoken(self):
        self.assertEqual(speech_text('## Hello, sir.\n---\n**Ready**，now.'), 'Hello sir. Ready now.')
        self.assertEqual(speech_text('---'), '')
        self.assertEqual(speech_text('1. First\n- Second'), 'First. Second')

    def test_numbers_and_non_english_survive(self):
        self.assertEqual(speech_text('1,000 units, -5.2°C，好的。'), '1000 units -5.2°C 好的。')

    def test_link_labels_and_controls(self):
        self.assertEqual(speech_text('[Results](https://example.com)\x00\u200b 👍'), 'Results')
        self.assertEqual(speech_text('```python\nprint(1)\n```'), 'Code omitted.')
