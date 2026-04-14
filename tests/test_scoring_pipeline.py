"""Scoring pipeline: local shortlist + batched Kagi (fewer fastgpt calls than one-per-article)."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from adapter import ArticleInfo
from article_prefilter import shortlist_for_kagi_scoring
from kagi_batch_scoring import score_article_batch_with_kagi


def _art(i: int) -> ArticleInfo:
    return ArticleInfo(
        title=f"Paper {i}",
        link=f"https://example.com/paper/{i}",
        abstract="quantum computing error correction surface code",
        updated=datetime(2024, 1, 1, tzinfo=timezone.utc),
        authors="",
    )


class TestScoringPipeline(unittest.TestCase):
    def test_shortlist_smaller_than_input(self) -> None:
        arts = [_art(i) for i in range(50)]
        group = {
            "research_areas": ["quantum computing"],
            "excluded_areas": [],
        }
        sl = shortlist_for_kagi_scoring(arts, group, 20, None)
        self.assertEqual(len(sl), 20)

    def test_batch_scoring_single_kagi_call(self) -> None:
        kagi = MagicMock()
        kagi.fastgpt_query.return_value = (
            '{"A1": {"relevance": 9, "impact": 8}, "A2": {"relevance": 7, "impact": 6}}'
        )
        a1 = _art(1)
        a2 = _art(2)
        items = [
            ("A1", a1, ""),
            ("A2", a2, ""),
        ]
        group = {"research_areas": ["x"], "excluded_areas": []}
        out = score_article_batch_with_kagi(kagi, items, group, "")
        self.assertEqual(kagi.fastgpt_query.call_count, 1)
        self.assertEqual(out["A1"].relevance, 9)
        self.assertEqual(out["A2"].impact, 6)


if __name__ == "__main__":
    unittest.main()
