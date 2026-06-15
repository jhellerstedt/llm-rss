import unittest
from datetime import datetime, timezone

from adapter import ArticleInfo
from fastgpt_reply import Reply
from author_whitelist import (
    AuthorWhitelist,
    WhitelistedAuthor,
    force_included_whitelist_items,
)


def _art(link, authors):
    return ArticleInfo(
        title="t",
        link=link,
        abstract="a",
        updated=datetime(2026, 6, 15, tzinfo=timezone.utc),
        authors=authors,
    )


class TestForceInclude(unittest.TestCase):
    def setUp(self):
        self.wl = AuthorWhitelist()
        self.wl.add(
            WhitelistedAuthor(
                id="https://orcid.org/0000-0002-1825-0097",
                display_name="Josiah Carberry",
                name_aliases=["Josiah Carberry"],
            )
        )

    def test_low_scored_whitelisted_article_is_force_included(self):
        arts = [_art("https://www.nature.com/articles/x", "Josiah Carberry, Bob")]
        replies = [Reply(relevance=0, impact=0, reason="not shortlisted")]
        add = force_included_whitelist_items(arts, replies, [], self.wl)
        self.assertEqual(len(add), 1)
        self.assertEqual(str(add[0][0].link), str(arts[0].link))
        self.assertIn("whitelisted author", add[0][1].reason)

    def test_non_whitelisted_not_included(self):
        arts = [_art("https://www.nature.com/articles/y", "Alice, Bob")]
        replies = [Reply(relevance=0, impact=0)]
        self.assertEqual(force_included_whitelist_items(arts, replies, [], self.wl), [])

    def test_already_passing_not_duplicated(self):
        a = _art("https://www.nature.com/articles/x", "Josiah Carberry")
        replies = [Reply(relevance=8, impact=7)]
        self.assertEqual(
            force_included_whitelist_items([a], replies, [(a, replies[0])], self.wl), []
        )

    def test_none_whitelist(self):
        a = _art("https://www.nature.com/x", "Josiah Carberry")
        self.assertEqual(
            force_included_whitelist_items(
                [a], [Reply(relevance=0, impact=0)], [], None
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
