import unittest

from journal_venue import tracked_venues_from_group_urls, venue_from_article_url
from zulip_context import domain_from_url, extract_urls_from_zulip_message_content
from zulip_journal_suggestions import (
    DEFAULT_DOMAIN_DENYLIST,
    ZULIP_SECTION_META_KEY,
    apex_domains_from_nested,
    domain_counts_from_zulip_messages,
    filter_academic_journal_domains_with_kagi,
    filter_nested_by_allowed_domains,
    format_missing_journals_message,
    format_missing_journals_message_nested,
    merge_journal_suggestion_maps,
    missing_domain_counts,
    missing_venues_by_section_from_messages,
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

    def test_tracked_venues_from_group_urls(self) -> None:
        tracked = tracked_venues_from_group_urls(
            ["https://www.nature.com/nphys.rss", "http://feeds.aps.org/rss/recent/prl.xml"]
        )
        self.assertIn("nature:nphys", tracked)
        self.assertIn("aps:prl", tracked)

    def test_venue_from_article_urls(self) -> None:
        n = venue_from_article_url(
            "https://www.nature.com/articles/s41563-024-00001-0",
        )
        self.assertIsNotNone(n)
        assert n is not None
        self.assertEqual(n.venue_key, "nature:nmat")
        self.assertIn("nmat.rss", n.suggested_rss or "")

        a = venue_from_article_url(
            "https://link.aps.org/doi/10.1103/PhysRevLett.130.010701",
        )
        self.assertIsNotNone(a)
        assert a is not None
        self.assertEqual(a.venue_key, "aps:prl")
        self.assertIn("prl.xml", a.suggested_rss or "")

        j = venue_from_article_url(
            "https://iopscience.iop.org/article/10.1088/1367-2630/26/1/015001",
        )
        self.assertIsNotNone(j)
        assert j is not None
        self.assertEqual(j.venue_key, "iop:1367-2630")
        self.assertIn("1367-2630", j.journal_page_url or "")

        ap = venue_from_article_url(
            "https://journals.aps.org/prx/abstract/10.1103/PhysRevX.14.011001",
        )
        self.assertIsNotNone(ap)
        assert ap is not None
        self.assertEqual(ap.venue_key, "aps:prx")

    def test_missing_venues_respects_section_and_tracked(self) -> None:
        msgs = [
            {
                ZULIP_SECTION_META_KEY: "r/s/t1",
                "content": '<a href="https://link.aps.org/doi/10.1103/PhysRevB.109.045678">b</a>',
            },
            {
                ZULIP_SECTION_META_KEY: "r/s/t2",
                "content": "https://link.aps.org/doi/10.1103/PhysRevB.109.045678",
            },
        ]
        tracked = {"aps:prl"}  # prb still missing
        by_sec = missing_venues_by_section_from_messages(
            msgs, tracked_venue_keys=tracked, denylist=DEFAULT_DOMAIN_DENYLIST
        )
        self.assertEqual(by_sec["r/s/t1"]["aps:prb"].count, 1)
        self.assertEqual(by_sec["r/s/t2"]["aps:prb"].count, 1)

    def test_merge_and_format_nested(self) -> None:
        msgs = [
            {
                ZULIP_SECTION_META_KEY: "realm/stream/a",
                "content": "https://www.nature.com/articles/s41467-024-00001-0",
            },
        ]
        nested = missing_venues_by_section_from_messages(
            msgs, tracked_venue_keys=set(), denylist=DEFAULT_DOMAIN_DENYLIST
        )
        dest: dict = {}
        merge_journal_suggestion_maps(dest, nested)
        merge_journal_suggestion_maps(dest, nested)
        self.assertEqual(dest["realm/stream/a"]["nature:ncomms"].count, 2)

        doms = apex_domains_from_nested(dest)
        self.assertIn("nature.com", doms)

        filtered = filter_nested_by_allowed_domains(dest, {"nature.com"})
        body = format_missing_journals_message_nested(filtered)
        self.assertIn("### realm/stream/a", body)
        self.assertIn("Nature Communications", body)
        self.assertIn("ncomms.rss", body)
        self.assertIn("[[groups]]", body)

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

    def test_kagi_filter_parsing_with_fences(self) -> None:
        class FakeKagi:
            def fastgpt_query(self, _prompt: str) -> str:
                return (
                    "```json\n"
                    '{"academic_domains":["science.org"],"reasons":{"science.org":"publisher"}}'
                    "\n```\n"
                )

        kept, _reasons = filter_academic_journal_domains_with_kagi(FakeKagi(), ["science.org"])
        self.assertEqual(kept, ["science.org"])

    def test_kagi_filter_empty_output(self) -> None:
        class FakeKagi:
            def fastgpt_query(self, _prompt: str) -> str:
                return "   "

        kept, reasons = filter_academic_journal_domains_with_kagi(FakeKagi(), ["science.org"])
        self.assertEqual(kept, [])
        self.assertEqual(reasons, {})


if __name__ == "__main__":
    unittest.main()

