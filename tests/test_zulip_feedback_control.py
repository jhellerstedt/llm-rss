import json
import tempfile
import time
import unittest
from pathlib import Path

from zulip_feedback_control import (
    FeedbackControlSettings,
    apply_feedback_control_for_group,
    bot_feedback_posts_for_group,
    compute_feedback_control,
    consumption_posts_per_day,
    feedback_control_path,
    load_feedback_control_settings,
    queue_depth_for_group,
    up_ratio_from_recent_reacted,
)


class TestFeedbackControlConfig(unittest.TestCase):
    def test_default_settings(self) -> None:
        s = FeedbackControlSettings.from_cfg({})
        self.assertTrue(s.enabled)
        self.assertEqual(s.target_up_ratio, 0.80)
        self.assertEqual(s.consumption_window_days, 7)
        self.assertEqual(s.ratio_sample_size, 20)
        self.assertEqual(s.ratio_min_samples, 5)
        self.assertEqual(s.max_threshold_margin, 3)
        self.assertEqual(s.max_enqueue_per_run, 2)
        self.assertEqual(s.target_queue_depth, 2)
        self.assertEqual(s.margin_step, 1)
        self.assertEqual(s.ratio_deadband, 0.10)

    def test_disabled_via_cfg(self) -> None:
        s = FeedbackControlSettings.from_cfg({"feedback_control": {"enabled": False}})
        self.assertFalse(s.enabled)

    def test_state_path_default(self) -> None:
        p = Path("/tmp/x/config.toml")
        self.assertEqual(
            feedback_control_path(p, {}),
            Path("/tmp/x/config.feedback_control.json"),
        )

    def test_state_path_override(self) -> None:
        p = Path("/tmp/x/config.toml")
        out = feedback_control_path(p, {"feedback_control": {"file": "state/fc.json"}})
        self.assertEqual(out, Path("/tmp/x/state/fc.json").resolve())


class TestFeedbackControlMetrics(unittest.TestCase):
    def _bot_post(self, url: str, ts: int, reactions: list | None = None) -> dict:
        return {
            "content": f"Title\n\nLink: {url}",
            "timestamp": ts,
            "reactions": reactions or [],
            "sender_email": "bot@example.com",
        }

    def test_consumption_counts_posts_in_window(self) -> None:
        now = int(time.time())
        old = now - 10 * 86400
        posts = [
            self._bot_post("https://a.org/1", now),
            self._bot_post("https://a.org/2", old),
        ]
        rate = consumption_posts_per_day(posts, window_days=7, now_ts=now)
        self.assertEqual(rate, 1 / 7)

    def test_up_ratio_last_n_reacted(self) -> None:
        posts = [
            self._bot_post("https://a.org/1", 1, [{"emoji_name": "+1", "user_id": 1}]),
            self._bot_post("https://a.org/2", 2, [{"emoji_name": "-1", "user_id": 2}]),
            self._bot_post("https://a.org/3", 3, [{"emoji_name": "+1", "user_id": 3}]),
            self._bot_post("https://a.org/4", 4, []),
        ]
        ratio, n = up_ratio_from_recent_reacted(posts, sample_size=2)
        self.assertEqual(n, 2)
        self.assertEqual(ratio, 0.5)

    def test_bot_feedback_posts_filters_non_bot_and_no_link(self) -> None:
        msgs = [
            self._bot_post("https://a.org/1", 100),
            {"content": "human chat", "timestamp": 101, "sender_email": "human@x.com"},
            {"content": "no link", "timestamp": 102, "sender_email": "bot@example.com"},
        ]
        by_pair = {("r1", "s1"): msgs}
        zulip_sources = [{"realm": "R1", "stream": "s1"}]
        zulip_realms = {"r1": {"email": "bot@example.com"}}
        out = bot_feedback_posts_for_group(by_pair, zulip_sources, zulip_realms)
        self.assertEqual(len(out), 1)
        self.assertIn("a.org/1", out[0]["content"])


class TestFeedbackControlLogic(unittest.TestCase):
    def test_margin_increases_when_up_ratio_low(self) -> None:
        settings = FeedbackControlSettings(
            ratio_min_samples=3,
            ratio_deadband=0.05,
            margin_step=1,
            max_threshold_margin=3,
        )
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            queue_depth=0,
            prior_margin=0,
            settings=settings,
            up_ratio=0.60,
            ratio_sample_count=10,
            consumption_posts_per_day=1.0,
        )
        self.assertEqual(result.threshold_margin, 1)
        self.assertEqual(result.effective_relevance, 6)
        self.assertEqual(result.effective_impact, 4)

    def test_margin_decreases_when_up_ratio_high(self) -> None:
        settings = FeedbackControlSettings(ratio_min_samples=3, ratio_deadband=0.05)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            queue_depth=0,
            prior_margin=2,
            settings=settings,
            up_ratio=0.95,
            ratio_sample_count=10,
            consumption_posts_per_day=1.0,
        )
        self.assertEqual(result.threshold_margin, 1)

    def test_enqueue_matches_consumption_one_run_per_day(self) -> None:
        settings = FeedbackControlSettings(max_enqueue_per_run=2, target_queue_depth=2)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            queue_depth=0,
            prior_margin=0,
            settings=settings,
            up_ratio=0.80,
            ratio_sample_count=0,
            consumption_posts_per_day=1.0,
        )
        self.assertEqual(result.max_enqueue, 1)

    def test_enqueue_halved_when_queue_deep(self) -> None:
        settings = FeedbackControlSettings(max_enqueue_per_run=2, target_queue_depth=2)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            queue_depth=5,
            prior_margin=0,
            settings=settings,
            up_ratio=0.80,
            ratio_sample_count=0,
            consumption_posts_per_day=2.0,
        )
        self.assertEqual(result.max_enqueue, 1)

    def test_disabled_returns_baseline(self) -> None:
        settings = FeedbackControlSettings(enabled=False)
        result = compute_feedback_control(
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            queue_depth=99,
            prior_margin=3,
            settings=settings,
            up_ratio=0.0,
            ratio_sample_count=99,
            consumption_posts_per_day=99.0,
        )
        self.assertEqual(result.threshold_margin, 0)
        self.assertEqual(result.effective_relevance, 5)
        self.assertEqual(result.max_enqueue, 2)

    def test_queue_depth_sums_streams(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg_path = Path(tmp.name) / "cfg.toml"
        cfg_path.write_text("# stub\n", encoding="utf-8")
        qpath = cfg_path.with_name(f"{cfg_path.stem}.feedback_ranking_queue.json")
        doc = {
            "version": 1,
            "queues": [
                {
                    "realm": "r1",
                    "stream": "s1",
                    "pending": [{"title": "A", "link": "https://a.org/1"}],
                },
                {
                    "realm": "r1",
                    "stream": "s2",
                    "pending": [
                        {"title": "B", "link": "https://a.org/2"},
                        {"title": "C", "link": "https://a.org/3"},
                    ],
                },
            ],
        }
        qpath.write_text(json.dumps(doc), encoding="utf-8")
        zulip_sources = [
            {"realm": "R1", "stream": "s1"},
            {"realm": "R1", "stream": "s2"},
        ]
        depth = queue_depth_for_group(cfg_path, {}, zulip_sources)
        self.assertEqual(depth, 3)


class TestFeedbackControlState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "cfg.toml"
        self.cfg_path.write_text("# stub\n", encoding="utf-8")

    def test_persists_margin_after_apply(self) -> None:
        now = int(time.time())
        msgs = [
            {
                "content": "P\n\nLink: https://a.org/1",
                "timestamp": now,
                "reactions": [{"emoji_name": "-1", "user_id": 1}],
                "sender_email": "bot@example.com",
            }
        ]
        by_pair = {("r1", "s1"): msgs}
        cfg = {
            "feedback_control": {"ratio_min_samples": 1, "ratio_deadband": 0.0},
        }
        result = apply_feedback_control_for_group(
            self.cfg_path,
            cfg,
            group_name="g1",
            base_relevance=5,
            base_impact=3,
            period_hours=24,
            zulip_sources=[{"realm": "R1", "stream": "s1"}],
            messages_by_pair=by_pair,
            zulip_realms={"r1": {"email": "bot@example.com"}},
            zulip_cfg={},
        )
        self.assertEqual(result.threshold_margin, 1)
        state_path = feedback_control_path(self.cfg_path, cfg)
        self.assertTrue(state_path.exists())
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["groups"]["g1"]["threshold_margin"], 1)


if __name__ == "__main__":
    unittest.main()
