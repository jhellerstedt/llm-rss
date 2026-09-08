import unittest
from datetime import datetime, timezone

from adapter import ArticleInfo
from article_prefilter import local_article_score, shortlist_for_kagi_scoring
from rss_merge import normalize_link


def _art(title: str, abstract: str, n: int = 1) -> ArticleInfo:
    return ArticleInfo(
        title=title,
        link=f"https://arxiv.org/abs/2401.{n:05d}",
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

    def test_method_include_force_scores_stm(self) -> None:
        group = {
            "research_areas": ["quantum error correction"],
            "excluded_areas": [],
            "method_include": ["STM", "scanning tunneling"],
        }
        stm = _art(
            "Zigzag edge states",
            "scanning tunneling microscopy (STM) of a kagome lattice",
            1,
        )
        other = _art(
            "Surface code",
            "We study quantum error correction and thresholds.",
            2,
        )
        sl = shortlist_for_kagi_scoring([other, stm], group, 1, None)
        self.assertEqual([a.title for a in sl], ["Zigzag edge states"])

    def test_method_hits_compete_only_with_each_other_when_over_cap(self) -> None:
        group = {
            "research_areas": ["quantum error correction"],
            "excluded_areas": [],
            "method_include": ["STM"],
        }
        weak_stm = _art("Weak STM", "STM image of a blank surface", 1)
        strong_stm = _art(
            "Strong STM",
            "STM STS scanning tunneling of quantum error correction devices",
            2,
        )
        qec = _art(
            "Surface code",
            "We study quantum error correction and thresholds.",
            3,
        )
        sl = shortlist_for_kagi_scoring(
            [qec, weak_stm, strong_stm], group, 1, None
        )
        self.assertEqual(len(sl), 1)
        self.assertEqual(sl[0].title, "Strong STM")

    def test_fill_remaining_cap_from_non_method(self) -> None:
        group = {
            "research_areas": ["quantum error correction"],
            "excluded_areas": [],
            "method_include": ["STM"],
        }
        stm = _art("STM paper", "STM of graphene", 1)
        qec = _art(
            "Surface code",
            "We study quantum error correction and thresholds.",
            2,
        )
        pasta = _art("Cooking pasta", "Boiling water and noodles.", 3)
        sl = shortlist_for_kagi_scoring([pasta, qec, stm], group, 2, None)
        titles = {a.title for a in sl}
        self.assertEqual(titles, {"STM paper", "Surface code"})


if __name__ == "__main__":
    unittest.main()
