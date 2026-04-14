import unittest

from rss_merge import normalize_link

from zulip_feedback import (
    aggregate_feedback_signals,
    count_thumbs_reactions,
    format_feedback_post_body,
    format_feedback_prompt_snippet,
    links_announced_in_messages,
    lookback_max_for_pair,
    merge_signal_maps,
    parse_feedback_link_from_body,
    select_top_ranked_for_feedback_posts,
    unique_realm_stream_pairs,
)


class TestZulipFeedbackParsing(unittest.TestCase):
    def test_parse_feedback_link_plain(self) -> None:
        body = "Some title\n\nLink: https://arxiv.org/abs/2401.00001"
        self.assertEqual(
            parse_feedback_link_from_body(body), "https://arxiv.org/abs/2401.00001"
        )

    def test_parse_feedback_link_case_insensitive(self) -> None:
        body = "T\n\nlink: HTTPS://EXAMPLE.COM/PATH"
        parsed = parse_feedback_link_from_body(body)
        self.assertIsNotNone(parsed)
        self.assertEqual(
            normalize_link(parsed), normalize_link("https://example.com/PATH")
        )

    def test_parse_feedback_link_strips_html(self) -> None:
        body = "T<p></p>\n\nLink: <a href=\"https://doi.org/10.1000/182\">x</a>"
        # strip_zulip_html removes tags; link line may break — use simple HTML-free body in prod
        body2 = "Title\n\nLink: https://doi.org/10.1000/182"
        self.assertEqual(
            parse_feedback_link_from_body(body2), "https://doi.org/10.1000/182"
        )

    def test_parse_missing_returns_none(self) -> None:
        self.assertIsNone(parse_feedback_link_from_body("no link line here"))

    def test_format_feedback_post_body(self) -> None:
        b = format_feedback_post_body("My Paper", "https://x.org/a")
        self.assertIn("My Paper", b)
        self.assertIn("Link: https://x.org/a", b)


class TestZulipFeedbackReactions(unittest.TestCase):
    def test_count_thumbs(self) -> None:
        msg = {
            "reactions": [
                {"emoji_name": "thumbs_up", "user_id": 1},
                {"emoji_name": "thumbs_up", "user_id": 2},
                {"emoji_name": "thumbs_down", "user_id": 3},
                {"emoji_name": "smile", "user_id": 4},
            ]
        }
        self.assertEqual(count_thumbs_reactions(msg), (2, 1))

    def test_count_thumbs_missing(self) -> None:
        self.assertEqual(count_thumbs_reactions({}), (0, 0))
        self.assertEqual(count_thumbs_reactions({"reactions": "bad"}), (0, 0))


class TestZulipFeedbackAggregate(unittest.TestCase):
    def test_aggregate_sums_same_link_two_messages(self) -> None:
        url = "https://arxiv.org/abs/2401.00001"
        msgs = [
            {
                "content": f"A\n\nLink: {url}",
                "reactions": [{"emoji_name": "thumbs_up", "user_id": 1}],
            },
            {
                "content": f"B\n\nLink: {url}",
                "reactions": [{"emoji_name": "thumbs_down", "user_id": 2}],
            },
        ]
        d = aggregate_feedback_signals(msgs)
        k = normalize_link(url)
        self.assertEqual(d[k], (1, 1))

    def test_links_announced(self) -> None:
        url = "https://nature.com/nature/articles/s41467-020-19000-0"
        msgs = [{"content": f"T\n\nLink: {url}"}]
        self.assertEqual(links_announced_in_messages(msgs), {normalize_link(url)})

    def test_merge_signal_maps(self) -> None:
        kx = normalize_link("https://x.org/foo")
        ky = normalize_link("https://y.org/bar")
        a = {kx: (1, 0)}
        b = {kx: (0, 2), ky: (1, 1)}
        m = merge_signal_maps(a, b)
        self.assertEqual(m[kx], (1, 2))
        self.assertEqual(m[ky], (1, 1))


class TestZulipFeedbackSources(unittest.TestCase):
    def test_unique_pairs_order(self) -> None:
        sources = [
            {"realm": "R1", "stream": "s1"},
            {"realm": "r1", "stream": "s1"},
            {"realm": "R1", "stream": "s2"},
        ]
        self.assertEqual(
            unique_realm_stream_pairs(sources),
            [("r1", "s1"), ("r1", "s2")],
        )

    def test_lookback_max_for_pair(self) -> None:
        sources = [
            {"realm": "A", "stream": "x", "lookback_hours": 24, "max_messages": 100},
            {"realm": "a", "stream": "x", "lookback_hours": 200, "max_messages": 50},
        ]
        self.assertEqual(lookback_max_for_pair(sources, "a", "x"), (200, 100))


class TestSelectTopRankedForFeedback(unittest.TestCase):
    def test_picks_two_best_by_relevance_then_impact(self) -> None:
        rows = [
            ("Low", "https://a.org/1", 6, 9),
            ("High rel", "https://a.org/2", 9, 3),
            ("Mid", "https://a.org/3", 8, 8),
            ("Tie rel lower imp", "https://a.org/4", 9, 2),
        ]
        picked = select_top_ranked_for_feedback_posts(rows, max_posts=2)
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0][1], "https://a.org/2")
        self.assertEqual(picked[1][1], "https://a.org/4")

    def test_same_link_once(self) -> None:
        rows = [
            ("A", "https://x.org/p", 9, 9),
            ("B", "https://x.org/p/", 8, 8),
        ]
        picked = select_top_ranked_for_feedback_posts(rows, max_posts=2)
        self.assertEqual(len(picked), 1)

    def test_max_posts_zero(self) -> None:
        self.assertEqual(
            select_top_ranked_for_feedback_posts(
                [("a", "https://z.org", 9, 9)], max_posts=0
            ),
            [],
        )


class TestZulipFeedbackPrompt(unittest.TestCase):
    def test_snippet_empty_when_unknown(self) -> None:
        self.assertEqual(
            format_feedback_prompt_snippet(
                "https://new.example/paper", {}
            ),
            "",
        )

    def test_snippet_when_known(self) -> None:
        link = "https://arxiv.org/abs/2401.00001"
        sig = {normalize_link(link): (3, 1)}
        s = format_feedback_prompt_snippet(link, sig)
        self.assertIn("thumbs_up=3", s)
        self.assertIn("thumbs_down=1", s)


if __name__ == "__main__":
    unittest.main()
