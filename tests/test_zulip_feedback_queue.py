import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openalex_enrich import PaperEnrichment

from zulip_feedback_queue import (
    dispatch_feedback_ranking_queue_once,
    enqueue_feedback_ranking_for_group,
    feedback_ranking_queue_path,
    paper_enrichment_from_json,
    paper_enrichment_to_json,
)


class TestPaperEnrichmentJson(unittest.TestCase):
    def test_roundtrip(self) -> None:
        en = PaperEnrichment(
            top_author_name="A",
            first_affiliation="X",
            last_affiliation="Y",
            top_h_index=10,
            top_author_affiliation="Z",
        )
        d = paper_enrichment_to_json(en)
        self.assertIsNotNone(d)
        back = paper_enrichment_from_json(d)
        self.assertEqual(back, en)

    def test_roundtrip_with_author_count(self) -> None:
        en = PaperEnrichment(
            top_author_name="A",
            first_affiliation="X",
            last_affiliation="Y",
            top_h_index=1,
            top_author_affiliation="Z",
            author_count=1,
        )
        d = paper_enrichment_to_json(en)
        self.assertEqual(d.get("author_count"), 1)
        self.assertEqual(paper_enrichment_from_json(d), en)

    def test_roundtrip_null_top_h_index(self) -> None:
        en = PaperEnrichment(
            top_author_name="A",
            first_affiliation="X",
            last_affiliation="Y",
            top_h_index=None,
            top_author_affiliation="Z",
        )
        d = paper_enrichment_to_json(en)
        self.assertIsNone(d.get("top_h_index"))
        self.assertEqual(paper_enrichment_from_json(d), en)

    def test_none(self) -> None:
        self.assertIsNone(paper_enrichment_to_json(None))
        self.assertIsNone(paper_enrichment_from_json(None))


class TestFeedbackQueuePath(unittest.TestCase):
    def test_default_stem(self) -> None:
        p = Path("/tmp/x/config.toml")
        self.assertEqual(
            feedback_ranking_queue_path(p, {}),
            Path("/tmp/x/config.feedback_ranking_queue.json"),
        )

    def test_relative_override(self) -> None:
        p = Path("/tmp/x/config.toml")
        out = feedback_ranking_queue_path(
            p, {"feedback_ranking_queue_file": "q/feed.json"}
        )
        self.assertEqual(out, Path("/tmp/x/q/feed.json").resolve())


class TestEnqueueDedupe(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "my.toml"
        self.cfg_path.write_text("# stub\n", encoding="utf-8")

    def _read_queue(self) -> dict:
        qpath = feedback_ranking_queue_path(self.cfg_path, {})
        with open(qpath, encoding="utf-8") as f:
            return json.load(f)

    def test_second_enqueue_same_link_no_duplicate(self) -> None:
        zulip_sources = [{"realm": "R1", "stream": "general"}]
        msgs_by_pair: dict = {("r1", "general"): []}
        titles = [("Paper", "https://arxiv.org/abs/2401.00001", None)]
        n1 = enqueue_feedback_ranking_for_group(
            self.cfg_path,
            {},
            zulip_sources,
            msgs_by_pair,
            titles,
            group_name="g1",
            dryrun=True,
        )
        self.assertEqual(n1, 1)
        n2 = enqueue_feedback_ranking_for_group(
            self.cfg_path,
            {},
            zulip_sources,
            msgs_by_pair,
            titles,
            group_name="g1",
            dryrun=True,
        )
        self.assertEqual(n2, 0)
        doc = self._read_queue()
        self.assertEqual(len(doc["queues"]), 1)
        self.assertEqual(len(doc["queues"][0]["pending"]), 1)

    def test_skips_if_already_in_topic(self) -> None:
        zulip_sources = [{"realm": "R1", "stream": "general"}]
        url = "https://arxiv.org/abs/2401.00002"
        msgs_by_pair = {
            ("r1", "general"): [{"content": f"T\n\nLink: {url}", "reactions": []}]
        }
        titles = [("Paper", url, None)]
        n = enqueue_feedback_ranking_for_group(
            self.cfg_path,
            {},
            zulip_sources,
            msgs_by_pair,
            titles,
            group_name="g1",
            dryrun=False,
        )
        self.assertEqual(n, 0)
        qpath = feedback_ranking_queue_path(self.cfg_path, {})
        self.assertFalse(qpath.exists())


class TestDispatchQueue(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "cfg.toml"
        self.cfg_path.write_text("# stub\n", encoding="utf-8")

    def test_dryrun_does_not_pop(self) -> None:
        qpath = feedback_ranking_queue_path(self.cfg_path, {})
        doc = {
            "version": 1,
            "queues": [
                {
                    "realm": "r1",
                    "stream": "general",
                    "pending": [
                        {
                            "title": "P",
                            "link": "https://arxiv.org/abs/2401.00003",
                            "enrichment": None,
                        }
                    ],
                }
            ],
        }
        qpath.write_text(json.dumps(doc), encoding="utf-8")
        cfg = {
            "groups": [
                {
                    "zulip_sources": [
                        {"realm": "R1", "stream": "general", "lookback_hours": 24}
                    ]
                }
            ]
        }
        fake_client = MagicMock()
        with patch(
            "zulip_feedback_queue.fetch_messages_narrow", return_value=[]
        ), patch(
            "zulip_feedback_queue._client_for_realm", return_value=fake_client
        ):
            dispatch_feedback_ranking_queue_once(
                self.cfg_path, cfg, {"r1": {}}, dryrun=True
            )
        fake_client.send_message.assert_not_called()
        after = json.loads(qpath.read_text(encoding="utf-8"))
        self.assertEqual(len(after["queues"][0]["pending"]), 1)

    def test_discards_stale_head_without_send(self) -> None:
        url = "https://arxiv.org/abs/2401.00004"
        qpath = feedback_ranking_queue_path(self.cfg_path, {})
        doc = {
            "version": 1,
            "queues": [
                {
                    "realm": "r1",
                    "stream": "general",
                    "pending": [
                        {"title": "P", "link": url, "enrichment": None},
                        {
                            "title": "Q",
                            "link": "https://arxiv.org/abs/2401.00005",
                            "enrichment": None,
                        },
                    ],
                }
            ],
        }
        qpath.write_text(json.dumps(doc), encoding="utf-8")
        cfg = {
            "groups": [
                {
                    "zulip_sources": [
                        {"realm": "R1", "stream": "general", "lookback_hours": 24}
                    ]
                }
            ]
        }
        fake_client = MagicMock()
        already = [{"content": f"P\n\nLink: {url}", "reactions": []}]
        with patch(
            "zulip_feedback_queue.fetch_messages_narrow", return_value=already
        ), patch(
            "zulip_feedback_queue._client_for_realm", return_value=fake_client
        ):
            dispatch_feedback_ranking_queue_once(
                self.cfg_path, cfg, {"r1": {}}, dryrun=False
            )
        fake_client.send_message.assert_not_called()
        after = json.loads(qpath.read_text(encoding="utf-8"))
        self.assertEqual(len(after["queues"][0]["pending"]), 1)
        self.assertIn("2401.00005", after["queues"][0]["pending"][0]["link"])

    def test_waits_for_reaction_on_previous_post(self) -> None:
        prev_url = "https://arxiv.org/abs/2401.00010"
        next_url = "https://arxiv.org/abs/2401.00011"
        qpath = feedback_ranking_queue_path(self.cfg_path, {})
        doc = {
            "version": 1,
            "queues": [
                {
                    "realm": "r1",
                    "stream": "general",
                    "pending": [
                        {"title": "Next", "link": next_url, "enrichment": None},
                    ],
                }
            ],
        }
        qpath.write_text(json.dumps(doc), encoding="utf-8")
        cfg = {
            "groups": [
                {
                    "zulip_sources": [
                        {"realm": "R1", "stream": "general", "lookback_hours": 24}
                    ]
                }
            ]
        }
        topic_msgs = [
            {"content": f"Prev\n\nLink: {prev_url}", "reactions": [], "timestamp": 1},
        ]
        fake_client = MagicMock()
        with patch(
            "zulip_feedback_queue.fetch_messages_narrow", return_value=topic_msgs
        ), patch(
            "zulip_feedback_queue._client_for_realm", return_value=fake_client
        ):
            dispatch_feedback_ranking_queue_once(
                self.cfg_path, cfg, {"r1": {}}, dryrun=False
            )
        fake_client.send_message.assert_not_called()
        after = json.loads(qpath.read_text(encoding="utf-8"))
        self.assertEqual(len(after["queues"][0]["pending"]), 1)

    def test_posts_when_previous_has_reaction(self) -> None:
        prev_url = "https://arxiv.org/abs/2401.00012"
        next_url = "https://arxiv.org/abs/2401.00013"
        qpath = feedback_ranking_queue_path(self.cfg_path, {})
        doc = {
            "version": 1,
            "queues": [
                {
                    "realm": "r1",
                    "stream": "general",
                    "pending": [
                        {"title": "Next", "link": next_url, "enrichment": None},
                    ],
                }
            ],
        }
        qpath.write_text(json.dumps(doc), encoding="utf-8")
        cfg = {
            "groups": [
                {
                    "zulip_sources": [
                        {"realm": "R1", "stream": "general", "lookback_hours": 24}
                    ]
                }
            ]
        }
        topic_msgs = [
            {
                "content": f"Prev\n\nLink: {prev_url}",
                "reactions": [{"emoji_name": "thumbs_down", "user_id": 2}],
                "timestamp": 1,
            },
        ]
        fake_client = MagicMock()
        fake_client.send_message.return_value = {"result": "success"}
        with patch(
            "zulip_feedback_queue.fetch_messages_narrow", return_value=topic_msgs
        ), patch(
            "zulip_feedback_queue._client_for_realm", return_value=fake_client
        ):
            dispatch_feedback_ranking_queue_once(
                self.cfg_path, cfg, {"r1": {}}, dryrun=False
            )
        fake_client.send_message.assert_called_once()
        after = json.loads(qpath.read_text(encoding="utf-8"))
        self.assertEqual(after["queues"], [])


if __name__ == "__main__":
    unittest.main()
