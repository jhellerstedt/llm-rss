import unittest

from main import _format_feed_description, _normalize_feed_category


class TestFeedChannelMetadata(unittest.TestCase):
    def test_description_without_category(self) -> None:
        self.assertEqual(
            _format_feed_description("biomedical_signal_processing", None),
            "LLM-filtered feed (biomedical_signal_processing)",
        )

    def test_description_with_category(self) -> None:
        self.assertEqual(
            _format_feed_description("biomedical_signal_processing", "biomedical"),
            "LLM-filtered feed (biomedical_signal_processing) — category: biomedical",
        )

    def test_normalize_category_first_word_only(self) -> None:
        self.assertEqual(_normalize_feed_category("  plasma  extra  "), "plasma")

    def test_normalize_category_empty(self) -> None:
        self.assertIsNone(_normalize_feed_category(""))
        self.assertIsNone(_normalize_feed_category(None))


if __name__ == "__main__":
    unittest.main()
