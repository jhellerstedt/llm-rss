import unittest

from zulip_journal_weekly_summary import markdown_config_diff


class TestMarkdownConfigDiff(unittest.TestCase):
    def test_groups_urls_and_lists(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
                    "feed_category": "cm",
                    "urls": ["https://a/rss"],
                    "research_areas": ["old topic"],
                    "excluded_areas": ["x"],
                }
            ],
        }
        after = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
                    "feed_category": "cm",
                    "urls": ["https://a/rss", "https://b/rss"],
                    "research_areas": ["new topic"],
                    "excluded_areas": [],
                }
            ],
        }
        md = markdown_config_diff(before, after)
        self.assertIn("Category `cm`", md)
        self.assertIn("Journal feeds:** 2", md)
        self.assertIn("Δ +1", md)
        self.assertIn("Keywords", md)
        self.assertIn("Δ -1", md)
        self.assertIn("**1** RSS URL(s) added", md)
        self.assertNotIn("https://b/rss", md)
        self.assertNotIn("new topic", md)
        self.assertNotIn("old topic", md)

    def test_new_group(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {"name": "a", "feed_category": None, "urls": [], "research_areas": [], "excluded_areas": []}
            ],
        }
        after = {
            "mode": "groups",
            "groups": [
                {"name": "a", "feed_category": None, "urls": [], "research_areas": [], "excluded_areas": []},
                {"name": "b", "feed_category": None, "urls": ["u"], "research_areas": [], "excluded_areas": []},
            ],
        }
        md = markdown_config_diff(before, after)
        self.assertIn("Group `b`", md)
        self.assertIn("Journal feeds:** 1", md)
        self.assertIn("**1** RSS URL(s) added", md)

    def test_legacy_mode(self) -> None:
        before = {
            "mode": "legacy",
            "urls": ["a"],
            "research_areas": ["r"],
            "excluded_areas": [],
        }
        after = {
            "mode": "legacy",
            "urls": ["a", "b"],
            "research_areas": ["r"],
            "excluded_areas": ["e"],
        }
        md = markdown_config_diff(before, after)
        self.assertIn("Legacy config", md)
        self.assertIn("**1** RSS URL(s) added", md)
        self.assertIn("Keywords", md)
        self.assertIn("Δ +1", md)

    def test_same_category_merges_feed_counts(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
                    "feed_category": "cm",
                    "urls": ["https://shared/rss"],
                    "research_areas": ["a"],
                    "excluded_areas": [],
                },
                {
                    "name": "g2",
                    "feed_category": "cm",
                    "urls": ["https://other/rss"],
                    "research_areas": [],
                    "excluded_areas": ["b"],
                },
            ],
        }
        after = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
                    "feed_category": "cm",
                    "urls": ["https://shared/rss"],
                    "research_areas": ["a"],
                    "excluded_areas": [],
                },
                {
                    "name": "g2",
                    "feed_category": "cm",
                    "urls": ["https://other/rss", "https://new/rss"],
                    "research_areas": [],
                    "excluded_areas": ["b"],
                },
            ],
        }
        md = markdown_config_diff(before, after)
        self.assertIn("Category `cm`", md)
        self.assertIn("Journal feeds:** 3", md)
        self.assertIn("**1** RSS URL(s) added", md)


if __name__ == "__main__":
    unittest.main()
