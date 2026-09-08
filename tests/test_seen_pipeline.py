import unittest
from unittest.mock import MagicMock, patch

from adapter import GenericRSSAdapter
from fastgpt_reply import Reply
from main import llm_score_failed
from paper_identity import identity_keys


class TestRecentArticlesHoursNone(unittest.TestCase):
    @patch("adapter.record_rss_feed_fetch")
    @patch("adapter.feedparser.parse")
    def test_hours_none_keeps_old_entries(self, parse: MagicMock, _fetch: MagicMock) -> None:
        feed = MagicMock()
        feed.entries = [
            {
                "title": "Old",
                "link": "https://example.com/old",
                "summary": "s",
                "updated": "2020-01-01T00:00:00Z",
                "authors": [],
            }
        ]
        parse.return_value = feed
        adapter = GenericRSSAdapter("https://example.com/feed")
        arts = list(adapter.recent_articles(hours=None))
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0].title, "Old")
        old_arts = list(adapter.recent_articles(hours=24))
        self.assertEqual(len(old_arts), 0)


class TestLlmScoreFailed(unittest.TestCase):
    def test_not_shortlisted_is_not_failure(self) -> None:
        self.assertFalse(
            llm_score_failed(Reply(relevance=0, impact=0, reason="not shortlisted for Kagi scoring"))
        )

    def test_fallback_failed(self) -> None:
        self.assertTrue(
            llm_score_failed(
                Reply(relevance=0, impact=0, reason="batch parse miss and fallback failed")
            )
        )


class TestUnseenFilter(unittest.TestCase):
    def test_doi_in_seen_skips_html_url(self) -> None:
        seen = {"doi:10.1021/acs.nanolett.6c03195"}
        link = (
            "https://pubs.acs.org/nalefd/article/doi/10.1021/acs.nanolett.6c03195/"
            "5404181/Direct-Observation-of-the-Zigzag-Edge-States-of-a"
        )
        self.assertTrue(identity_keys(link) & seen)


if __name__ == "__main__":
    unittest.main()
