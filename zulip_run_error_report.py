"""Post a deduplicated WARNING/ERROR summary to configured Zulip streams after each run."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from api_usage import get_api_usage_snapshot, record_zulip_api
from zulip_context import _client_for_realm

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = "llm-rss run"
ZULIP_MESSAGE_MAX_CHARS = 9500
_GROUP_PREFIX = re.compile(r"^\[[^\]]+\]\s*")


class _CollectorHandler(logging.Handler):
    def __init__(self, collector: RunLogCollector) -> None:
        super().__init__(level=logging.WARNING)
        self._collector = collector

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        self._collector._records.append(record)


class RunLogCollector:
    """Attach to the root logger for one main() run to capture warnings and errors."""

    def __init__(self) -> None:
        self._records: list[logging.LogRecord] = []
        self._handler = _CollectorHandler(self)
        self._attached = False

    def attach(self) -> None:
        if self._attached:
            return
        logging.getLogger().addHandler(self._handler)
        self._attached = True

    def detach(self) -> None:
        if not self._attached:
            return
        logging.getLogger().removeHandler(self._handler)
        self._attached = False

    @property
    def records(self) -> list[logging.LogRecord]:
        return list(self._records)


def normalize_log_message(message: str) -> str:
    """Collapse per-group prefixes like ``[high_impact_physics]`` for deduplication."""
    s = message.strip()
    while True:
        m = _GROUP_PREFIX.match(s)
        if not m:
            break
        s = s[m.end():].strip()
    return s


def _record_text(record: logging.LogRecord) -> str:
    msg = record.getMessage()
    if record.exc_info and record.exc_info[0] is not None:
        exc = record.exc_info[1]
        if exc is not None:
            return f"{msg} ({type(exc).__name__}: {exc})"
    return msg


def _group_records(
    records: list[logging.LogRecord],
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    errors: dict[str, int] = {}
    warnings: dict[str, int] = {}
    for rec in records:
        if rec.levelno < logging.WARNING:
            continue
        text = normalize_log_message(_record_text(rec))
        if not text:
            continue
        bucket = errors if rec.levelno >= logging.ERROR else warnings
        bucket[text] = bucket.get(text, 0) + 1
    error_lines = sorted(errors.items(), key=lambda x: (-x[1], x[0]))
    warning_lines = sorted(warnings.items(), key=lambda x: (-x[1], x[0]))
    return error_lines, warning_lines


def error_reporting_destinations(zulip_cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """Return stable-unique (realm, stream) pairs from ``[zulip.error_reporting]``."""
    er = zulip_cfg.get("error_reporting") or {}
    if not er.get("enabled", False):
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []

    def add(realm: object, stream: object) -> None:
        if not realm or not stream:
            return
        key = (str(realm).lower(), str(stream))
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    if er.get("realm") and er.get("stream"):
        add(er.get("realm"), er.get("stream"))
    for row in er.get("destinations") or []:
        if isinstance(row, dict):
            add(row.get("realm"), row.get("stream"))
    return out


def error_reporting_topic(zulip_cfg: dict[str, Any]) -> str:
    er = zulip_cfg.get("error_reporting") or {}
    topic = str(er.get("topic") or DEFAULT_TOPIC).strip()
    return topic or DEFAULT_TOPIC


def format_run_error_summary(
    *,
    config_name: str,
    records: list[logging.LogRecord],
    run_utc: datetime | None = None,
) -> str | None:
    """Build markdown body, or None when there are no warnings/errors."""
    error_lines, warning_lines = _group_records(records)
    if not error_lines and not warning_lines:
        return None

    when = run_utc or datetime.now(timezone.utc)
    stamp = when.strftime("%Y-%m-%d %H:%M UTC")
    parts = [f"**llm-rss run** — `{config_name}` — {stamp}", ""]

    if error_lines:
        total = sum(n for _, n in error_lines)
        unique = len(error_lines)
        label = "Error" if unique == 1 else "Errors"
        parts.append(f"**{label} ({unique} unique, {total} total)**")
        for text, count in error_lines:
            suffix = f" ×{count}" if count > 1 else ""
            parts.append(f"- {text}{suffix}")
        parts.append("")

    if warning_lines:
        total = sum(n for _, n in warning_lines)
        unique = len(warning_lines)
        label = "Warning" if unique == 1 else "Warnings"
        parts.append(f"**{label} ({unique} unique, {total} total)**")
        for text, count in warning_lines:
            suffix = f" ×{count}" if count > 1 else ""
            parts.append(f"- {text}{suffix}")
        parts.append("")

    usage = get_api_usage_snapshot()
    parts.append(
        "**API:** "
        f"OpenRouter_http={usage['openrouter']}, "
        f"Kagi_FastGPT_http={usage['kagi_fastgpt']}, "
        f"Kagi_summarize_http={usage['kagi_summarize']}, "
        f"RSS_feed_fetch={usage['rss_feed']}"
    )

    body = "\n".join(parts)
    if len(body) > ZULIP_MESSAGE_MAX_CHARS:
        body = body[: ZULIP_MESSAGE_MAX_CHARS - 40] + "\n\n_…(message truncated)_\n"
    return body


def maybe_post_run_error_summary(
    *,
    collector: RunLogCollector,
    config_path: Any,
    zulip_cfg: dict[str, Any],
    zulip_realms: dict[str, dict[str, str]],
    dryrun: bool,
    run_utc: datetime | None = None,
) -> None:
    """Post one summary per configured destination when the run logged issues."""
    destinations = error_reporting_destinations(zulip_cfg)
    if not destinations:
        return
    if not zulip_realms:
        logger.warning("Zulip error reporting enabled but no realm credentials loaded")
        return

    config_name = getattr(config_path, "name", str(config_path))
    body = format_run_error_summary(
        config_name=config_name,
        records=collector.records,
        run_utc=run_utc,
    )
    if body is None:
        return

    topic = error_reporting_topic(zulip_cfg)
    for realm, stream in destinations:
        if realm not in zulip_realms:
            logger.warning(
                "Zulip error reporting: unknown realm %r (known: %s)",
                realm,
                sorted(zulip_realms),
            )
            continue
        if dryrun:
            logger.info(
                "[dry run] would post run error summary realm=%s stream=%s topic=%r (%d chars)",
                realm,
                stream,
                topic,
                len(body),
            )
            continue
        try:
            client = _client_for_realm(zulip_realms, realm)
            result = client.send_message(
                {
                    "type": "stream",
                    "to": stream,
                    "topic": topic,
                    "content": body,
                }
            )
            record_zulip_api(1)
            if result.get("result") != "success":
                logger.warning(
                    "Zulip run error summary send failed realm=%s stream=%s: %s",
                    realm,
                    stream,
                    result,
                )
            else:
                logger.info(
                    "Posted run error summary realm=%s stream=%s topic=%r",
                    realm,
                    stream,
                    topic,
                )
        except Exception:
            logger.exception(
                "Zulip run error summary post failed realm=%s stream=%s",
                realm,
                stream,
            )
