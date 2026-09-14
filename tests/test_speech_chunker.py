import unittest

from athena.llm.speech_chunker import SpeechChunker


class SpeechChunkerTests(unittest.TestCase):
    def test_emits_complete_sentence_before_stream_finishes(self):
        chunker = SpeechChunker()
        self.assertEqual(
            chunker.feed("The lights are on. The temperature"),
            ["The lights are on."],
        )
        self.assertEqual(chunker.finish(), ["The temperature"])

    def test_emits_natural_long_comma_clause(self):
        chunker = SpeechChunker(comma_threshold=24)
        self.assertEqual(
            chunker.feed("The forecast is mostly sunny, with light wind"),
            ["The forecast is mostly sunny,"],
        )


if __name__ == "__main__":
    unittest.main()
