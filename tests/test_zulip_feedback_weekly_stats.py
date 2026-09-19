import tempfile
import unittest
from pathlib import Path

from zulip_feedback_weekly_stats import (
    aggregate_votes_for_stream,
    category_by_group_from_cfg,
    collect_stats_by_bucket,
    feedback_weekly_stats_path,
    load_stats,
    markdown_stats_only,
    record_enqueued,
    record_posted,
    remap_counters_to_categories,
    reset_period_after_summary,
    resolve_bucket,
    save_stats,
    stats_nonzero,
)


class TestResolveBucket(unittest.TestCase):
    def test_category(self) -> None:
        self.assertEqual(resolve_bucket("g1", "cm physics"), ("c:cm", "cm", "category"))

    def test_group_fallback(self) -> None:
        self.assertEqual(resolve_bucket("solo", None), ("g:solo", "solo", "group"))


class TestStatsPath(unittest.TestCase):
    def test_default(self) -> None:
        p = Path("/tmp/x/config.toml")
        self.assertEqual(
            feedback_weekly_stats_path(p, {}),
            Path("/tmp/x/config.feedback_weekly_stats.json"),
        )


class TestRecordAndReset(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.toml"
        self.cfg_path.write_text("x=1\n", encoding="utf-8")
        self.zulip_cfg: dict = {}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enqueue_posted_and_votes_markdown(self) -> None:
        import time

        now = time.time()
        record_enqueued(
            self.cfg_path,
            self.zulip_cfg,
            realm="Tuesday",
            stream="science",
            bucket_id="c:cm",
            title="cm",
            kind="category",
            dryrun=False,
        )
        record_enqueued(
            self.cfg_path,
            self.zulip_cfg,
            realm="tuesday",
            stream="science",
            bucket_id="c:cm",
            title="cm",
            kind="category",
            dryrun=False,
        )
        record_posted(
            self.cfg_path,
            self.zulip_cfg,
            realm="tuesday",
            stream="science",
            bucket_id="c:cm",
            title="cm",
            kind="category",
            link="https://example.com/a",
            dryrun=False,
            ts=now - 100,
        )
        doc = load_stats(feedback_weekly_stats_path(self.cfg_path, self.zulip_cfg))
        self.assertEqual(len(doc["counters"]), 1)
        self.assertEqual(doc["counters"][0]["enqueued"], 2)
        self.assertEqual(doc["counters"][0]["posted"], 1)
        self.assertEqual(len(doc["posted_events"]), 1)

        msgs = [
            {
                "timestamp": now - 50,
                "content": "Title\n\nLink: https://example.com/a",
                "reactions": [{"emoji_name": "+1"}, {"emoji_name": "+1"}, {"emoji_name": "-1"}],
            },
            {
                "timestamp": now - 40,
                "content": "Other\n\nLink: https://example.com/a",
                "reactions": [],
            },
        ]
        votes = aggregate_votes_for_stream(
            msgs,
            doc["posted_events"],
            realm="tuesday",
            stream="science",
            period_start_unix=now - 1000,
        )
        self.assertEqual(votes["c:cm"], (2, 1))
        by = collect_stats_by_bucket(doc["counters"], votes)
        md = markdown_stats_only(by)
        self.assertIn("Category `cm`", md)
        self.assertIn("**Queued:** 2", md)
        self.assertIn("**Posted:** 1", md)
        self.assertIn("**Votes:** ↑2 / ↓1", md)
        self.assertTrue(stats_nonzero(doc["counters"], votes))

        reset_period_after_summary(self.cfg_path, self.zulip_cfg, dryrun=False, now=now + 10)
        doc2 = load_stats(feedback_weekly_stats_path(self.cfg_path, self.zulip_cfg))
        self.assertEqual(doc2["counters"], [])
        self.assertEqual(doc2["posted_events"], [])
        self.assertEqual(doc2["period_start_unix"], now + 10)

    def test_dryrun_noop(self) -> None:
        record_enqueued(
            self.cfg_path,
            self.zulip_cfg,
            realm="t",
            stream="s",
            bucket_id="c:x",
            title="x",
            kind="category",
            dryrun=True,
        )
        path = feedback_weekly_stats_path(self.cfg_path, self.zulip_cfg)
        self.assertFalse(path.exists())


class TestCategoryRemap(unittest.TestCase):
    def test_map_from_config_groups(self) -> None:
        cfg = {
            "groups": [
                {"name": "quantum_computing", "feed_category": "quantum hardware"},
                {"name": "aps_journals", "category": "nano"},
                {"name": "solo"},
            ]
        }
        self.assertEqual(
            category_by_group_from_cfg(cfg),
            {"quantum_computing": "quantum", "aps_journals": "nano"},
        )

    def test_group_bucket_posted_merges_into_category(self) -> None:
        counters = [
            {
                "realm": "tuesday",
                "stream": "science",
                "bucket_id": "c:quantum",
                "title": "quantum",
                "kind": "category",
                "enqueued": 9,
                "posted": 0,
            },
            {
                "realm": "tuesday",
                "stream": "science",
                "bucket_id": "g:quantum_computing",
                "title": "quantum_computing",
                "kind": "group",
                "enqueued": 0,
                "posted": 3,
            },
        ]
        merged = remap_counters_to_categories(
            counters, {"quantum_computing": "quantum"}
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["bucket_id"], "c:quantum")
        self.assertEqual(merged[0]["enqueued"], 9)
        self.assertEqual(merged[0]["posted"], 3)
        by = collect_stats_by_bucket(merged, {"c:quantum": (2, 1)})
        md = markdown_stats_only(by)
        self.assertIn("**Queued:** 9", md)
        self.assertIn("**Posted:** 3", md)
        self.assertIn("**Votes:** ↑2 / ↓1", md)


class TestVoteJoin(unittest.TestCase):
    def test_arxiv_body_matches_journal_event_via_alias(self) -> None:
        events = [
            {
                "link": "https://www.nature.com/articles/s41467-026-77618-6",
                "aliases": ["https://arxiv.org/abs/2506.21963"],
                "bucket_id": "c:nano",
                "realm": "tuesday",
                "stream": "science",
                "ts": 1000.0,
            }
        ]
        msgs = [
            {
                "timestamp": 1010.0,
                "content": "Paper\n\nLink: https://arxiv.org/abs/2506.21963",
                "reactions": [{"emoji_name": "+1"}],
            }
        ]
        votes = aggregate_votes_for_stream(
            msgs,
            events,
            realm="tuesday",
            stream="science",
            period_start_unix=0,
        )
        self.assertEqual(votes["c:nano"], (1, 0))

    def test_arxiv_body_matches_journal_event_by_nearby_timestamp(self) -> None:
        events = [
            {
                "link": "https://www.nature.com/articles/s41467-026-77618-6",
                "bucket_id": "c:nano",
                "realm": "tuesday",
                "stream": "science",
                "ts": 1000.0,
            }
        ]
        msgs = [
            {
                "timestamp": 1002.0,
                "content": "Paper\n\nLink: https://arxiv.org/abs/2506.21963",
                "reactions": [{"emoji_name": "-1"}],
            }
        ]
        votes = aggregate_votes_for_stream(
            msgs,
            events,
            realm="tuesday",
            stream="science",
            period_start_unix=0,
        )
        self.assertEqual(votes["c:nano"], (0, 1))


if __name__ == "__main__":
    unittest.main()
