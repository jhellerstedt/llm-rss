import unittest

from fastgpt_reply import Reply, parse_batch_replies_from_fastgpt_output


class TestBatchScoringParse(unittest.TestCase):
    def test_parse_object_keys(self) -> None:
        text = '{"A1": {"relevance": 8, "impact": 7, "reason": "ok"}, "A2": {"relevance": 3, "impact": 4}}'
        d = parse_batch_replies_from_fastgpt_output(text, ["A1", "A2"])
        self.assertEqual(d["A1"], Reply(relevance=8, impact=7, reason="ok"))
        self.assertEqual(d["A2"], Reply(relevance=3, impact=4, reason=None))

    def test_partial_missing_id(self) -> None:
        text = '{"A1": {"relevance": 1, "impact": 2}}'
        d = parse_batch_replies_from_fastgpt_output(text, ["A1", "A2"])
        self.assertIn("A1", d)
        self.assertNotIn("A2", d)

    def test_fenced_json(self) -> None:
        text = '```json\n{"A1": {"relevance": 5, "impact": 5}}\n```'
        d = parse_batch_replies_from_fastgpt_output(text, ["A1"])
        self.assertEqual(d["A1"].relevance, 5)


if __name__ == "__main__":
    unittest.main()
