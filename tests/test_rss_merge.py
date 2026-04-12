import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from rss_merge import (
    FeedItem,
    load_persisted_feed_items,
    merge_feed_history,
    normalize_link,
)


class TestRssMerge(unittest.TestCase):
    def test_normalize_link_case_and_trailing_slash(self) -> None:
        a = normalize_link("HTTPS://Example.COM/foo/")
        b = normalize_link("https://example.com/foo")
        self.assertEqual(a, b)

    def test_merge_refreshes_description_keeps_pubdate_when_link_reappears(
        self,
    ) -> None:
        old_pub = datetime(2024, 1, 1, tzinfo=timezone.utc)
        new_pub = datetime(2025, 6, 1, tzinfo=timezone.utc)
        persisted = [
            FeedItem(
                title="Old title",
                link="https://example.com/paper",
                description="old desc",
                pubdate=old_pub,
                unique_id="https://example.com/paper",
            )
        ]
        new_items = [
            FeedItem(
                title="New title",
                link="https://example.com/paper",
                description="new desc with metadata",
                pubdate=new_pub,
                unique_id="https://example.com/paper",
            )
        ]
        merged = merge_feed_history(persisted, new_items, max_items=25)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].pubdate, old_pub)
        self.assertEqual(merged[0].unique_id, "https://example.com/paper")
        self.assertEqual(merged[0].title, "New title")
        self.assertEqual(merged[0].description, "new desc with metadata")

    def test_merge_adds_new_links_and_caps(self) -> None:
        persisted = [
            FeedItem(
                title=f"t{i}",
                link=f"https://example.com/{i}",
                description="d",
                pubdate=datetime(2024, 1, i, tzinfo=timezone.utc),
                unique_id=f"https://example.com/{i}",
            )
            for i in range(1, 26)
        ]
        new_items = [
            FeedItem(
                title="fresh",
                link="https://example.com/new",
                description="d",
                pubdate=datetime(2026, 1, 15, tzinfo=timezone.utc),
                unique_id="https://example.com/new",
            )
        ]
        merged = merge_feed_history(persisted, new_items, max_items=25)
        self.assertEqual(len(merged), 25)
        links = {normalize_link(i.link) for i in merged}
        self.assertIn(normalize_link("https://example.com/new"), links)
        self.assertNotIn(normalize_link("https://example.com/1"), links)

    def test_load_persisted_round_trip(self) -> None:
        xml = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     version="2.0"><channel><title>ch</title><link>https://example.org</link>
<description>d</description><language>en</language>
<item><title>One</title><link>https://arxiv.org/abs/2401.00001</link>
<description>Abstract here</description>
<pubDate>Wed, 15 Jan 2025 12:00:00 GMT</pubDate>
<guid>https://arxiv.org/abs/2401.00001</guid>
</item></channel></rss>"""
        with TemporaryDirectory() as td:
            p = Path(td) / "feed.xml"
            p.write_text(xml, encoding="utf-8")
            items = load_persisted_feed_items(p)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].link, "https://arxiv.org/abs/2401.00001")
        self.assertEqual(items[0].title, "One")
        self.assertEqual(items[0].pubdate.year, 2025)
        self.assertEqual(items[0].pubdate.month, 1)


if __name__ == "__main__":
    unittest.main()
