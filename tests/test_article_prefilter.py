import unittest
from datetime import datetime, timezone

from adapter import ArticleInfo
from article_prefilter import local_article_score, shortlist_for_kagi_scoring
from rss_merge import normalize_link


def _art(title: str, abstract: str) -> ArticleInfo:
    return ArticleInfo(
        title=title,
        link="https://arxiv.org/abs/2401.00001",
        abstract=abstract,
        updated=datetime(2024, 1, 1, tzinfo=timezone.utc),
        authors="",
    )


class TestArticlePrefilter(unittest.TestCase):
    def test_shortlist_prefers_keyword_overlap(self) -> None:
        group = {
            "research_areas": ["quantum error correction"],
            "excluded_areas": [],
        }
        a1 = _art("Surface code", "We study quantum error correction and thresholds.")
        a2 = _art("Cooking pasta", "Boiling water and noodles.")
        sl = shortlist_for_kagi_scoring([a1, a2], group, 1, None)
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0].title, "Surface code")

    def test_excluded_penalty(self) -> None:
        group = {
            "research_areas": ["quantum algorithms"],
            "excluded_areas": ["fusion energy"],
        }
        a1 = _art("Fusion energy roadmap", "fusion energy policy and reactors")
        a2 = _art("Quantum algorithms", "quantum algorithms for chemistry")
        s1 = local_article_score(a1, group, None)
        s2 = local_article_score(a2, group, None)
        self.assertGreater(s2, s1)

    def test_feedback_signal(self) -> None:
        group = {"research_areas": ["physics"], "excluded_areas": []}
        a = _art("X", "particle physics")
        sig = {normalize_link("https://arxiv.org/abs/2401.00001"): (3, 0)}
        s_plus = local_article_score(a, group, sig)
        s0 = local_article_score(a, group, {})
        self.assertGreater(s_plus, s0)


if __name__ == "__main__":
    unittest.main()
