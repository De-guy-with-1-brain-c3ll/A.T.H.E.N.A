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

    def test_comma_does_not_create_an_edge_network_gap(self):
        chunker = SpeechChunker(comma_threshold=24)
        self.assertEqual(
            chunker.feed("The forecast is mostly sunny, with light wind"),
            [],
        )
        self.assertEqual(chunker.finish(), ["The forecast is mostly sunny, with light wind"])

    def test_numbered_race_results_do_not_split_after_position(self):
        chunker = SpeechChunker()
        self.assertEqual(chunker.feed('1. First driver\n2. Second driver'), [])
        self.assertEqual(chunker.finish(), ['1. First driver\n2. Second driver'])


if __name__ == "__main__":
    unittest.main()
