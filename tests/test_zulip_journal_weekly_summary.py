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

    def test_filter_excludes_other_category_buckets(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "cm_g",
                    "feed_category": "cm",
                    "urls": ["https://cm/rss"],
                    "research_areas": [],
                    "excluded_areas": [],
                },
                {
                    "name": "bio_g",
                    "feed_category": "bio",
                    "urls": ["https://bio/rss"],
                    "research_areas": [],
                    "excluded_areas": [],
                },
            ],
        }
        after = {
            "mode": "groups",
            "groups": [
                {
                    "name": "cm_g",
                    "feed_category": "cm",
                    "urls": ["https://cm/rss"],
                    "research_areas": [],
                    "excluded_areas": [],
                },
                {
                    "name": "bio_g",
                    "feed_category": "bio",
                    "urls": ["https://bio/rss", "https://bio/new"],
                    "research_areas": [],
                    "excluded_areas": [],
                },
            ],
        }
        md_cm = markdown_config_diff(before, after, allowed_bucket_ids=frozenset({"c:cm"}))
        self.assertEqual(md_cm, "")

        md_bio = markdown_config_diff(before, after, allowed_bucket_ids=frozenset({"c:bio"}))
        self.assertIn("Category `bio`", md_bio)
        self.assertIn("**1** RSS URL(s) added", md_bio)
        self.assertNotIn("`cm`", md_bio)

    def test_filter_keeps_group_bucket_when_no_category(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "solo",
                    "feed_category": None,
                    "urls": [],
                    "research_areas": [],
                    "excluded_areas": [],
                },
            ],
        }
        after = {
            "mode": "groups",
            "groups": [
                {
                    "name": "solo",
                    "feed_category": None,
                    "urls": ["https://x/rss"],
                    "research_areas": [],
                    "excluded_areas": [],
                },
            ],
        }
        md = markdown_config_diff(before, after, allowed_bucket_ids=frozenset({"g:solo"}))
        self.assertIn("Group `solo`", md)
        md_other = markdown_config_diff(before, after, allowed_bucket_ids=frozenset({"g:other"}))
        self.assertEqual(md_other, "")

    def test_stats_only_section(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
                    "feed_category": "cm",
                    "urls": ["https://a/rss"],
                    "research_areas": [],
                    "excluded_areas": [],
                }
            ],
        }
        after = before
        stats = {
            "c:cm": {
                "title": "cm",
                "kind": "category",
                "enqueued": 3,
                "posted": 1,
                "votes": (2, 1),
            }
        }
        md = markdown_config_diff(before, after, stats_by_bucket=stats)
        self.assertIn("Category `cm`", md)
        self.assertIn("**Queued:** 3", md)
        self.assertIn("**Posted:** 1", md)
        self.assertIn("**Votes:** ↑2 / ↓1", md)
        self.assertNotIn("Journal feeds", md)

    def test_config_and_stats_merged(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
                    "feed_category": "cm",
                    "urls": ["https://a/rss"],
                    "research_areas": [],
                    "excluded_areas": [],
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
                    "research_areas": [],
                    "excluded_areas": [],
                }
            ],
        }
        stats = {
            "c:cm": {
                "title": "cm",
                "kind": "category",
                "enqueued": 4,
                "posted": 2,
                "votes": (1, 0),
            }
        }
        md = markdown_config_diff(before, after, stats_by_bucket=stats)
        self.assertIn("Journal feeds:** 2", md)
        self.assertIn("**Queued:** 4", md)
        self.assertEqual(md.count("Category `cm`"), 1)


if __name__ == "__main__":
    unittest.main()
