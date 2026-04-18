import unittest

from zulip_journal_weekly_summary import markdown_config_diff


class TestMarkdownConfigDiff(unittest.TestCase):
    def test_groups_urls_and_lists(self) -> None:
        before = {
            "mode": "groups",
            "groups": [
                {
                    "name": "g1",
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
                    "urls": ["https://a/rss", "https://b/rss"],
                    "research_areas": ["new topic"],
                    "excluded_areas": [],
                }
            ],
        }
        md = markdown_config_diff(before, after)
        self.assertIn("g1", md)
        self.assertIn("https://b/rss", md)
        self.assertIn("new topic", md)
        self.assertIn("old topic", md)
        self.assertIn("x", md)

    def test_new_group(self) -> None:
        before = {"mode": "groups", "groups": [{"name": "a", "urls": [], "research_areas": [], "excluded_areas": []}]}
        after = {
            "mode": "groups",
            "groups": [
                {"name": "a", "urls": [], "research_areas": [], "excluded_areas": []},
                {"name": "b", "urls": ["u"], "research_areas": [], "excluded_areas": []},
            ],
        }
        md = markdown_config_diff(before, after)
        self.assertIn("New group", md)
        self.assertIn("`b`", md)

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
        self.assertIn("b", md)
        self.assertIn("e", md)


if __name__ == "__main__":
    unittest.main()
