import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from adapter import ArticleInfo
from author_whitelist import (
    AuthorWhitelist,
    WhitelistedAuthor,
    normalize_author_name,
    split_author_names,
)


def _article(authors: str) -> ArticleInfo:
    return ArticleInfo(
        title="t",
        link="https://www.nature.com/articles/s41586-026-10636-y",
        abstract="a",
        updated=datetime(2026, 6, 15, tzinfo=timezone.utc),
        authors=authors,
    )


def _author(**kw) -> WhitelistedAuthor:
    base = dict(
        id="https://orcid.org/0000-0002-1825-0097",
        display_name="Josiah Carberry",
        name_aliases=["Josiah Carberry", "J. Carberry"],
        orcid="0000-0002-1825-0097",
        openalex_id="A5023888391",
    )
    base.update(kw)
    return WhitelistedAuthor(**base)


class TestHelpers(unittest.TestCase):
    def test_normalize_author_name(self):
        self.assertEqual(
            normalize_author_name("  Josiah   Carberry "), "josiah carberry"
        )

    def test_split_author_names(self):
        self.assertEqual(
            split_author_names("Alice Smith, Bob Jones and C. Doe"),
            ["Alice Smith", "Bob Jones", "C. Doe"],
        )
        self.assertEqual(split_author_names(""), [])


class TestStore(unittest.TestCase):
    def test_save_load_round_trip(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "wl.json"
            wl = AuthorWhitelist()
            wl.add(_author())
            wl.set_cursor("tuesday:science:author whitelist", 42)
            wl.save(p)
            loaded = AuthorWhitelist.load(p)
            self.assertEqual(len(loaded.authors), 1)
            self.assertEqual(
                loaded.get_cursor("tuesday:science:author whitelist"), 42
            )

    def test_load_missing_file_is_empty(self):
        with TemporaryDirectory() as d:
            self.assertEqual(AuthorWhitelist.load(Path(d) / "nope.json").authors, [])

    def test_load_malformed_is_empty(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "bad.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(AuthorWhitelist.load(p).authors, [])

    def test_add_is_idempotent_and_merges_aliases(self):
        wl = AuthorWhitelist()
        self.assertTrue(wl.add(_author()))
        self.assertFalse(wl.add(_author(name_aliases=["Josiah S. Carberry"])))
        self.assertEqual(len(wl.authors), 1)
        self.assertIn("Josiah S. Carberry", wl.authors[0].name_aliases)

    def test_remove_by_orcid_and_name(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNotNone(wl.remove("0000-0002-1825-0097"))
        self.assertEqual(wl.authors, [])
        wl.add(_author())
        self.assertIsNotNone(wl.remove("josiah carberry"))
        self.assertEqual(wl.authors, [])

    def test_cursor_only_advances(self):
        wl = AuthorWhitelist()
        wl.set_cursor("k", 10)
        wl.set_cursor("k", 5)
        self.assertEqual(wl.get_cursor("k"), 10)


class TestMatching(unittest.TestCase):
    def test_matches_alias_case_and_whitespace(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNotNone(wl.matches(_article("Alice Smith, josiah   carberry")))

    def test_no_match(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNone(wl.matches(_article("Alice Smith, Bob Jones")))

    def test_empty_whitelist_never_matches(self):
        self.assertIsNone(AuthorWhitelist().matches(_article("Josiah Carberry")))

    def test_matches_openalex_author_ids(self):
        wl = AuthorWhitelist()
        wl.add(_author())
        self.assertIsNotNone(
            wl.matches_openalex_author_ids(["A999", "A5023888391"])
        )
        self.assertIsNone(wl.matches_openalex_author_ids(["A999"]))


if __name__ == "__main__":
    unittest.main()
