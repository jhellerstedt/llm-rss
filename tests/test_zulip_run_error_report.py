"""Tests for zulip_run_error_report."""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from zulip_run_error_report import (
    RunLogCollector,
    error_reporting_destinations,
    format_run_error_summary,
    maybe_post_run_error_summary,
    normalize_log_message,
)


def _record(level: int, msg: str, *, exc: BaseException | None = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
    )


class TestNormalizeLogMessage(unittest.TestCase):
    def test_strips_group_prefix(self) -> None:
        a = normalize_log_message("[high_impact_physics] OpenRouter batch scoring failed")
        b = normalize_log_message("[aps_journals] OpenRouter batch scoring failed")
        self.assertEqual(a, b)
        self.assertEqual(a, "OpenRouter batch scoring failed")


class TestFormatRunErrorSummary(unittest.TestCase):
    def test_none_when_clean(self) -> None:
        self.assertIsNone(format_run_error_summary(config_name="c.toml", records=[]))

    def test_dedupes_warnings(self) -> None:
        records = [
            _record(logging.WARNING, "[g1] OpenRouter batch scoring failed: 404"),
            _record(logging.WARNING, "[g2] OpenRouter batch scoring failed: 404"),
            _record(logging.ERROR, "OpenRouter journal-domain filter failed"),
        ]
        body = format_run_error_summary(
            config_name="config.toml",
            records=records,
            run_utc=datetime(2026, 6, 29, 6, 3, tzinfo=timezone.utc),
        )
        assert body is not None
        self.assertIn("**llm-rss run** — `config.toml`", body)
        self.assertIn("OpenRouter batch scoring failed: 404 ×2", body)
        self.assertIn("OpenRouter journal-domain filter failed", body)
        self.assertIn("OpenRouter_http=", body)

    def test_skips_info(self) -> None:
        records = [_record(logging.INFO, "OpenRouter enabled")]
        self.assertIsNone(format_run_error_summary(config_name="c.toml", records=records))


class TestErrorReportingDestinations(unittest.TestCase):
    def test_disabled(self) -> None:
        self.assertEqual(error_reporting_destinations({}), [])
        self.assertEqual(error_reporting_destinations({"error_reporting": {"enabled": False}}), [])

    def test_single_and_list(self) -> None:
        cfg = {
            "error_reporting": {
                "enabled": True,
                "realm": "r1",
                "stream": "llm errors",
                "destinations": [
                    {"realm": "r2", "stream": "llm errors"},
                    {"realm": "r1", "stream": "llm errors"},
                ],
            }
        }
        self.assertEqual(
            error_reporting_destinations(cfg),
            [("r1", "llm errors"), ("r2", "llm errors")],
        )


class TestMaybePostRunErrorSummary(unittest.TestCase):
    def test_skips_when_no_issues(self) -> None:
        collector = RunLogCollector()
        with patch("zulip_run_error_report._client_for_realm") as mock_client:
            maybe_post_run_error_summary(
                collector=collector,
                config_path=MagicMock(name="config.toml"),
                zulip_cfg={"error_reporting": {"enabled": True, "realm": "r", "stream": "s"}},
                zulip_realms={"r": {"email": "e", "api_key": "k", "site": "https://z"}},
                dryrun=False,
            )
            mock_client.assert_not_called()

    def test_posts_when_warnings(self) -> None:
        collector = RunLogCollector()
        collector._records.append(_record(logging.WARNING, "something broke"))
        mock_zulip = MagicMock()
        mock_zulip.send_message.return_value = {"result": "success"}
        with patch(
            "zulip_run_error_report._client_for_realm", return_value=mock_zulip
        ) as mock_client:
            maybe_post_run_error_summary(
                collector=collector,
                config_path=MagicMock(name="config.toml"),
                zulip_cfg={
                    "error_reporting": {
                        "enabled": True,
                        "realm": "myrealm",
                        "stream": "llm errors",
                        "topic": "llm-rss run",
                    }
                },
                zulip_realms={
                    "myrealm": {"email": "e", "api_key": "k", "site": "https://z"}
                },
                dryrun=False,
            )
            mock_client.assert_called_once()
            mock_zulip.send_message.assert_called_once()
            payload = mock_zulip.send_message.call_args[0][0]
            self.assertEqual(payload["to"], "llm errors")
            self.assertEqual(payload["topic"], "llm-rss run")
            self.assertIn("something broke", payload["content"])


if __name__ == "__main__":
    unittest.main()
