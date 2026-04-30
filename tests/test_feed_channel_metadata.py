import unittest

from openalex_enrich import PaperEnrichment

from main import _format_feed_description, _pick_feed_openalex_concept


class TestFeedChannelMetadata(unittest.TestCase):
    def test_description_without_concept(self) -> None:
        self.assertEqual(
            _format_feed_description("biomedical_signal_processing", None),
            "LLM-filtered feed (biomedical_signal_processing)",
        )

    def test_description_with_concept(self) -> None:
        self.assertEqual(
            _format_feed_description(
                "biomedical_signal_processing", "Biomedical signal processing"
            ),
            "LLM-filtered feed (biomedical_signal_processing) — OpenAlex concept: Biomedical signal processing",
        )

    def test_pick_concept_mode(self) -> None:
        a = PaperEnrichment(
            top_author_name="A",
            top_h_index=1,
            first_affiliation="X",
            last_affiliation="Y",
            top_concept="Physics",
        )
        b = PaperEnrichment(
            top_author_name="B",
            top_h_index=2,
            first_affiliation="X",
            last_affiliation="Y",
            top_concept="Physics",
        )
        c = PaperEnrichment(
            top_author_name="C",
            top_h_index=3,
            first_affiliation="X",
            last_affiliation="Y",
            top_concept="Engineering",
        )
        self.assertEqual(_pick_feed_openalex_concept([a, b, c]), "Physics")

    def test_pick_concept_ignores_unknown_and_none(self) -> None:
        a = PaperEnrichment(
            top_author_name="A",
            top_h_index=1,
            first_affiliation="X",
            last_affiliation="Y",
            top_concept="Unknown",
        )
        self.assertIsNone(_pick_feed_openalex_concept([None, a]))


if __name__ == "__main__":
    unittest.main()

