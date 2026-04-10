import unittest
from unittest.mock import Mock, patch


class TestFastGPTHandling(unittest.TestCase):
    def test_parse_reply_allows_latex_backslashes(self) -> None:
        # This is a realistic failure mode: the model includes LaTeX like "\infty"
        # inside a JSON string, which is invalid JSON unless the backslash is escaped.
        from fastgpt_reply import parse_reply_from_fastgpt_output

        text = (
            """```json
{"relevance": 8, "impact": 7, "reason": "Uses H^\\infty and also \\infty in text"}
```"""
        )
        reply = parse_reply_from_fastgpt_output(text, article_title="t")
        self.assertEqual(reply.relevance, 8)
        self.assertEqual(reply.impact, 7)
        self.assertIn("infty", reply.reason or "")

    def test_parse_reply_allows_backslash_apostrophe_renyi(self) -> None:
        # Models often emit TeX-style \' inside JSON strings; JSON does not allow \'.
        from fastgpt_reply import parse_reply_from_fastgpt_output

        invalid_json = (
            '{"relevance": 2, "impact": 6, "reason": "R'
            + "\\'enyi"
            + ' entropy and key rates"}'
        )
        text = f"```json\n{invalid_json}\n```"
        reply = parse_reply_from_fastgpt_output(text, article_title="t")
        self.assertEqual(reply.relevance, 2)
        self.assertEqual(reply.impact, 6)
        self.assertIsNotNone(reply.reason)
        self.assertIn("enyi", reply.reason or "")

    def test_parse_reply_allows_caret_infty_in_reason(self) -> None:
        from fastgpt_reply import parse_reply_from_fastgpt_output

        invalid_json = (
            '{"relevance": 8, "impact": 7, "reason": "Coherent feedback $H^'
            + "\\infty"
            + '$ control"}'
        )
        text = f"```json\n{invalid_json}\n```"
        reply = parse_reply_from_fastgpt_output(text, article_title="t")
        self.assertEqual(reply.relevance, 8)
        self.assertEqual(reply.impact, 7)
        self.assertIn("infty", reply.reason or "")

    def test_fastgpt_retries_on_429_with_retry_after(self) -> None:
        from kagi_client import KagiClient

        r429 = Mock()
        r429.status_code = 429
        r429.headers = {"Retry-After": "0"}
        r429.raise_for_status.side_effect = Exception("HTTP 429")

        r200 = Mock()
        r200.status_code = 200
        r200.headers = {}
        r200.raise_for_status.return_value = None
        r200.json.return_value = {"data": {"output": '{"relevance": 1, "impact": 2, "reason": "ok"}'}}

        with patch("kagi_client.requests.post", side_effect=[r429, r200]) as post:
            with patch("time.sleep") as sleep:
                c = KagiClient(api_key="x", use_cache=True)
                out = c.fastgpt_query("q")
                self.assertIn('"relevance"', out)
                self.assertEqual(post.call_count, 2)
                sleep.assert_called()


if __name__ == "__main__":
    unittest.main()

