import unittest
from unittest.mock import patch

from adapter import ArticleInfo
from datetime import datetime, timezone

from openalex_enrich import (
    AuthorMetric,
    PaperEnrichment,
    apply_kagi_metadata_backfill,
    batch_enrich_articles,
    build_enrichment_for_work,
    extract_arxiv_id,
    extract_doi_from_link,
    format_enrichment_for_feed,
    merge_paper_enrichment,
    paper_enrichment_incomplete,
)


class TestOpenAlexHelpers(unittest.TestCase):
    def test_extract_arxiv_id(self) -> None:
        self.assertEqual(
            extract_arxiv_id("https://arxiv.org/abs/2401.12345v2"),
            "2401.12345",
        )
        self.assertEqual(
            extract_arxiv_id("http://arxiv.org/pdf/2312.00001"),
            "2312.00001",
        )

    def test_extract_doi_from_link(self) -> None:
        self.assertEqual(
            extract_doi_from_link(
                "https://doi.org/10.1038/s41586-020-2649-2?foo=1"
            ),
            "10.1038/s41586-020-2649-2",
        )


class TestBuildEnrichment(unittest.TestCase):
    def test_top_h_and_affiliations(self) -> None:
        work = {
            "authorships": [
                {
                    "author_position": "first",
                    "author": {"id": "https://openalex.org/A1"},
                    "institutions": [{"display_name": "MIT"}],
                },
                {
                    "author_position": "middle",
                    "author": {"id": "https://openalex.org/A2"},
                    "institutions": [],
                    "affiliations": [
                        {"raw_affiliation_string": "Somewhere Institute"}
                    ],
                },
                {
                    "author_position": "last",
                    "author": {"id": "https://openalex.org/A3"},
                    "institutions": [{"display_name": "Stanford University"}],
                },
            ]
        }
        metrics = {
            "https://openalex.org/A1": AuthorMetric("Alice", 10),
            "https://openalex.org/A2": AuthorMetric("Bob", 40),
            "https://openalex.org/A3": AuthorMetric("Carol", 12),
        }
        en = build_enrichment_for_work(work, metrics)
        assert en is not None
        self.assertEqual(en.top_author_name, "Bob")
        self.assertEqual(en.top_h_index, 40)
        self.assertEqual(en.first_affiliation, "MIT")
        self.assertEqual(en.last_affiliation, "Stanford University")

    def test_single_affiliation_line_when_same(self) -> None:
        en = PaperEnrichment(
            top_author_name="A",
            top_h_index=1,
            first_affiliation="MIT",
            last_affiliation="MIT",
        )
        self.assertIn("first & last author", en.format_block())
        self.assertNotIn("First author institution", en.format_block())


class TestBatchEnrichMocked(unittest.TestCase):
    @patch("openalex_enrich.fetch_work")
    @patch("openalex_enrich.fetch_author_metric")
    def test_batch_maps_link(
        self, mock_author: unittest.mock.MagicMock, mock_work: unittest.mock.MagicMock
    ) -> None:
        mock_work.return_value = {
            "authorships": [
                {
                    "author_position": "first",
                    "author": {"id": "https://openalex.org/AX"},
                    "institutions": [{"display_name": "Inst A"}],
                },
                {
                    "author_position": "last",
                    "author": {"id": "https://openalex.org/AX"},
                    "institutions": [{"display_name": "Inst A"}],
                },
            ]
        }
        mock_author.return_value = AuthorMetric("Solo", 7)
        art = ArticleInfo(
            title="Test paper",
            link="https://arxiv.org/abs/2401.00001",
            abstract="x",
            updated=datetime(2025, 1, 1, tzinfo=timezone.utc),
            authors="",
        )
        out = batch_enrich_articles([art], mailto="t@example.com")
        block = format_enrichment_for_feed(out[str(art.link)])
        self.assertIn("h-index 7", block)
        self.assertIn("Solo", block)


class TestMergeAndFallback(unittest.TestCase):
    def test_merge_prefers_openalex_when_known(self) -> None:
        oa = PaperEnrichment("Alice", 5, "MIT", "Unknown")
        kg = PaperEnrichment("Bob", 99, "Oxford", "Stanford")
        m = merge_paper_enrichment(oa, kg)
        assert m is not None
        self.assertEqual(m.top_author_name, "Alice")
        self.assertEqual(m.top_h_index, 5)
        self.assertEqual(m.first_affiliation, "MIT")
        self.assertEqual(m.last_affiliation, "Stanford")

    def test_incomplete_when_aff_unknown(self) -> None:
        en = PaperEnrichment("A", 1, "MIT", "Unknown")
        self.assertTrue(paper_enrichment_incomplete(en))

    def test_format_skips_all_unknown(self) -> None:
        z = PaperEnrichment("Unknown", 0, "Unknown", "Unknown")
        self.assertEqual(format_enrichment_for_feed(z), "")

    def test_apply_kagi_backfill_merges(self) -> None:
        art = ArticleInfo(
            title="T",
            link="https://example.com/p",
            abstract="abs",
            updated=datetime(2025, 1, 1, tzinfo=timezone.utc),
            authors="",
        )
        by_link: dict[str, PaperEnrichment | None] = {str(art.link): None}

        class FakeKagi:
            def fastgpt_query(self, query: str) -> str:
                return (
                    '{"top_author_name": "Zed", "top_author_h_index": 3, '
                    '"first_author_institution": "U1", "last_author_institution": "U2"}'
                )

        apply_kagi_metadata_backfill(by_link, [art], FakeKagi())  # type: ignore[arg-type]
        block = format_enrichment_for_feed(by_link[str(art.link)])
        self.assertIn("Zed", block)
        self.assertIn("U1", block)


if __name__ == "__main__":
    unittest.main()
