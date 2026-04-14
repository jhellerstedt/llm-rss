import unittest

from zulip_context import domain_from_url, extract_urls_from_zulip_message_content
from zulip_journal_suggestions import (
    DEFAULT_DOMAIN_DENYLIST,
    domain_counts_from_zulip_messages,
    filter_academic_journal_domains_with_kagi,
    format_missing_journals_message,
    missing_domain_counts,
    tracked_domains_from_group_urls,
)


class TestZulipUrlExtraction(unittest.TestCase):
    def test_extract_href_and_plain_urls_stable_unique(self) -> None:
        raw = (
            '<p>See <a href="https://Nature.com/articles/123">paper</a> and '
            "also https://example.org/path). And again https://example.org/path</p>"
        )
        urls = extract_urls_from_zulip_message_content(raw)
        self.assertEqual(urls[0], "https://Nature.com/articles/123")
        self.assertEqual(urls[1], "https://example.org/path")
        self.assertEqual(len(urls), 2)

    def test_domain_from_url_normalizes(self) -> None:
        self.assertEqual(domain_from_url("https://www.Example.ORG/a"), "example.org")
        self.assertEqual(domain_from_url("https://user:pass@EXAMPLE.org:443/a"), "example.org")


class TestZulipJournalSuggestionsLogic(unittest.TestCase):
    def test_tracked_domains_from_group_urls(self) -> None:
        tracked = tracked_domains_from_group_urls(
            ["https://www.nature.com/nphys.rss", "http://feeds.aps.org/rss/recent/prl.xml"]
        )
        self.assertIn("nature.com", tracked)
        self.assertIn("feeds.aps.org", tracked)

    def test_domain_counts_and_missing(self) -> None:
        msgs = [
            {"content": '<a href="https://www.science.org/doi/10.1/abc">x</a>'},
            {"content": "https://doi.org/10.1000/182"},
            {"content": "https://arxiv.org/abs/2401.00001"},
            {"content": "https://science.org/doi/10.1/abc"},  # plain url; same domain again
        ]
        counts = domain_counts_from_zulip_messages(msgs, denylist=DEFAULT_DOMAIN_DENYLIST)
        # doi.org and arxiv.org should be filtered by denylist
        self.assertNotIn("doi.org", counts)
        self.assertNotIn("arxiv.org", counts)
        self.assertEqual(counts.get("science.org"), 2)

        tracked = {"nature.com"}
        missing = missing_domain_counts(tracked_domains=tracked, zulip_domain_counts=counts)
        self.assertEqual(missing, {"science.org": 2})

    def test_format_message_contains_domains(self) -> None:
        body = format_missing_journals_message({"science.org": 2, "cell.com": 1})
        self.assertIn("science.org", body)
        self.assertIn("(links: 2)", body)
        self.assertIn("urls = [...]", body)

    def test_kagi_filter_parsing(self) -> None:
        class FakeKagi:
            def fastgpt_query(self, _prompt: str) -> str:
                return '{"academic_domains":["science.org","www.nature.com"],"reasons":{"science.org":"publisher","nature.com":"journal"}}'

        kept, reasons = filter_academic_journal_domains_with_kagi(
            FakeKagi(),
            ["science.org", "arxiv.org", "www.nature.com"],
        )
        self.assertEqual(kept, ["science.org", "nature.com"])
        self.assertIn("science.org", reasons)


if __name__ == "__main__":
    unittest.main()

